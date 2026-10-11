"""Shaper tests: exact pacing, bounded queue delay (AQM) and live rate changes.

Everything runs on a fake clock, so the tests are deterministic and need no admin
rights, no network and no driver.
"""
from __future__ import annotations

import pytest

from limitador.shaper import (
    MIN_BURST_BYTES,
    MIN_SLEEP_SECONDS,
    SLEEP_OVERHEAD_SECONDS,
    RateMeter,
    Shaper,
)

KB = 1024
MB = 1024 * 1024


class FakeClock:
    """A clock that advances like the real one: including Windows' sleep overshoot."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += seconds + SLEEP_OVERHEAD_SECONDS


def pace_with_sleeps(shaper: Shaper, clock: FakeClock, packets: int, size: int) -> float:
    """Feed packets the way the injector does: sleep for the wait it is given."""
    start = clock.t
    for _ in range(packets):
        allow, wait = shaper.plan(size)
        assert allow, "un shaper sin cola detrás nunca debe descartar"
        shaper.wait(wait)
        shaper.note_sent(size, wait)
    return clock.t - start


def test_first_packet_of_a_fresh_shaper_leaves_immediately():
    clock = FakeClock()
    shaper = Shaper(1 * MB, max_delay_ms=50, clock=clock)
    allow, wait = shaper.plan(1500)
    assert allow and wait == 0.0


def test_paces_to_the_configured_rate():
    clock = FakeClock()
    rate = 1 * MB
    shaper = Shaper(rate, max_delay_ms=50, clock=clock)
    size = 1500
    packets = 400
    elapsed = pace_with_sleeps(shaper, clock, packets, size)
    expected = (packets - 1) * size / rate      # first packet is immediate
    assert elapsed == pytest.approx(expected, rel=0.02)


def test_sustained_rate_equals_the_limit():
    clock = FakeClock()
    rate = 512 * KB
    shaper = Shaper(rate, max_delay_ms=50, clock=clock)
    size, packets = 1400, 5000
    elapsed = pace_with_sleeps(shaper, clock, packets, size)
    achieved = (packets * size) / elapsed
    assert achieved == pytest.approx(rate, rel=0.02)


def test_rate_is_never_exceeded_over_a_window():
    clock = FakeClock()
    rate = 256 * KB
    shaper = Shaper(rate, max_delay_ms=50, clock=clock)
    size, packets = 1500, 1000
    elapsed = pace_with_sleeps(shaper, clock, packets, size)
    # The very first packet of an idle shaper leaves immediately (a one-packet burst),
    # which is at most size bytes of credit; over 1000 packets that is 0.1%.
    assert (packets * size) / elapsed <= rate * 1.01


def test_idle_shaper_never_drops_even_a_packet_bigger_than_the_burst():
    """A packet larger than the whole budget must still be paced, not dropped:
    dropping it would blackhole the connection at very low limits."""
    clock = FakeClock()
    rate = 8 * KB
    shaper = Shaper(rate, max_delay_ms=10, clock=clock)         # budget = min 3000 B
    allow, wait = shaper.plan(65_000, queued_bytes=0)
    assert allow is True and wait == 0.0                        # first packet goes now
    # The oversized packet is paid for by the next one instead of being discarded.
    allow, wait = shaper.plan(65_000, queued_bytes=0)
    assert allow is True
    assert wait == pytest.approx(65_000 / rate)


def test_aqm_drops_when_the_queue_exceeds_the_delay_budget():
    clock = FakeClock()
    shaper = Shaper(100 * KB, max_delay_ms=50, clock=clock)     # budget = 5 KB
    allow, wait = shaper.plan(1500, queued_bytes=shaper.burst_bytes)
    assert allow is False and wait == 0.0
    assert shaper.snapshot()["aqm_drops"] == 1


def test_queue_within_the_budget_is_accepted_and_delayed():
    clock = FakeClock()
    shaper = Shaper(100 * KB, max_delay_ms=50, clock=clock)
    allow, wait = shaper.plan(1500, queued_bytes=shaper.burst_bytes - 1500)
    assert allow and wait >= 0.0


def test_the_budget_is_never_smaller_than_two_packets():
    shaper = Shaper(1 * KB, max_delay_ms=5)                      # 1KB/s * 5ms = 5 bytes
    assert shaper.burst_bytes >= MIN_BURST_BYTES


def test_delayed_packet_lands_before_the_next_one_can_pass():
    clock = FakeClock()
    shaper = Shaper(10 * KB, max_delay_ms=100, clock=clock)
    allow, first = shaper.plan(1000)
    assert allow and first == 0.0
    allow, second = shaper.plan(1000)
    assert allow and second == pytest.approx(1000 / (10 * KB))
    shaper.wait(second)
    allow, third = shaper.plan(1000)
    assert allow and third == pytest.approx(1000 / (10 * KB))


def test_sub_millisecond_waits_are_batched_not_slept():
    """Windows' timer granularity (~0.5 ms) makes tiny sleeps overshoot, which used to cap
    the achievable rate at a fraction of the target. Small debts must be carried in the
    virtual clock instead of being slept."""
    clock = FakeClock()
    shaper = Shaper(26 * MB, max_delay_ms=50, clock=clock)
    started = clock.t
    waited_below_threshold = False
    last_wait = 0.0
    for _ in range(500):
        allow, wait = shaper.plan(1500)
        assert allow
        if wait >= MIN_SLEEP_SECONDS:
            last_wait = wait
            break
        shaper.wait(wait)
        if clock.t != started:
            waited_below_threshold = True
    assert not waited_below_threshold, "no debe dormirse por debajo del umbral"
    assert clock.t == started
    # The debt accumulates until it is worth a real sleep, then it is paid at once.
    assert last_wait >= MIN_SLEEP_SECONDS
    assert last_wait < MIN_SLEEP_SECONDS + 1500 * 2 / (26 * MB)


def test_a_long_wait_is_slept_in_chunks():
    clock = FakeClock()
    shaper = Shaper(100 * KB, max_delay_ms=50, clock=clock)
    shaper.plan(1500)                      # first packet goes out now
    allow, wait = shaper.plan(1500)
    assert allow and wait >= MIN_SLEEP_SECONDS
    started = clock.t
    shaper.wait(wait)
    # The requested time is short by the sleep overhead, so real time lands on target.
    assert clock.t - started == pytest.approx(wait, rel=1e-6)


def test_zero_rate_is_rejected():
    with pytest.raises(ValueError):
        Shaper(0)


def test_set_rate_applies_immediately():
    clock = FakeClock()
    shaper = Shaper(1 * MB, max_delay_ms=50, clock=clock)
    shaper.set_rate(100 * KB)
    allow, wait = shaper.plan(4000)
    assert allow and wait == 0.0                 # fresh shaper: first packet is immediate
    allow, wait = shaper.plan(4000)
    assert allow and wait == pytest.approx(4000 / (100 * KB))
    assert shaper.burst_bytes == pytest.approx(100 * KB * 0.05)


def test_stats_track_sent_delayed_and_dropped():
    clock = FakeClock()
    shaper = Shaper(100 * KB, max_delay_ms=50, clock=clock)
    allow, wait = shaper.plan(500)
    assert allow
    shaper.note_sent(500, wait)
    assert shaper.plan(500, queued_bytes=shaper.burst_bytes)[0] is False
    shaper.note_dropped(500)
    snapshot = shaper.snapshot()
    assert snapshot["sent_bytes"] == 500
    assert snapshot["sent_packets"] == 1
    assert snapshot["dropped_bytes"] == 500
    assert snapshot["dropped_packets"] == 1


class MeterClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_rate_meter_uses_a_sliding_window():
    clock = MeterClock()
    meter = RateMeter(window=1.0, clock=clock)
    for _ in range(1000):
        meter.add(1000)          # 1,000,000 bytes within the same instant
    assert meter.rate() == pytest.approx(1_000_000, rel=1e-6)
    assert meter.total == 1_000_000
    clock.t += 1.5
    assert meter.rate() == 0.0
