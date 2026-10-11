"""Traffic shaping primitives.

Two jobs, deliberately separated:

**Pacing (never drops).** Every packet gets a departure time computed from a virtual clock
(``depart = max(now, previous_departure) + size / rate``). Over any window the shaper
releases exactly ``rate`` bytes per second, in order, whatever the arrival pattern is. A
packet is *delayed*, never dropped, just because the limit is low: dropping an oversized
packet on an idle link would blackhole the connection, which is worse than being slow.

**AQM (drops the excess).** Dropping happens only when the caller says there is already a
queue waiting: if ``queued_bytes + size`` exceeds the delay budget
(``max(rate × max_delay, 2 MTU)``) the packet is dropped. TCP then backs off instead of
queueing forever, so the added latency stays bounded by ``max_delay``. This is the
bufferbloat control, and it can never trigger on an idle shaper because the caller passes
``queued_bytes == 0`` then.

Everything is injectable (``clock``) so the behaviour is testable with no network, no
driver and no admin rights.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

MB = 1024 * 1024

#: Two full-size packets: a burst budget smaller than one packet would make a low limit
#: drop nearly everything, because a single 1500-byte packet paced at 64 KB/s takes 23 ms.
MIN_BURST_BYTES = 3000.0

#: Sleeps shorter than this are counter-productive: measured on this machine, every
#: ``time.sleep`` call costs about 0.5 ms more than requested, regardless of duration
#: (0.05 ms -> 0.5 ms, 2 ms -> 2.5 ms, 10 ms -> 10.2 ms). A 26 MB/s limit sleeps ~60 us per
#: 1500-byte packet, so sleeping per packet capped the achieved rate at ~10 MB/s (measured).
#: Debts below this threshold are carried in the virtual clock and paid in one bigger sleep.
MIN_SLEEP_SECONDS = 0.010

#: Measured systematic overhead of a Windows sleep call; subtracted so the pacing lands on
#: the configured rate instead of ~5% below it.
SLEEP_OVERHEAD_SECONDS = 0.0005

#: Longest single sleep, so stopping stays responsive.
MAX_SLEEP_CHUNK = 0.025


class Clock(Protocol):  # pragma: no cover - typing helper
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class _RealClock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


class Shaper:
    """Rate limiter for one direction of one target (global or one application).

    Parameters
    ----------
    rate_bps:
        Allowed bytes per second (must be > 0).
    max_delay_ms:
        Delay budget. Sets the AQM threshold: ``rate × max_delay`` bytes may be queued
        before packets start being dropped.
    """

    __slots__ = ("_rate", "max_delay", "_burst", "_next_departure", "_lock", "_clock",
                 "_skip_sleep", "sent_bytes", "dropped_bytes", "sent_packets",
                 "dropped_packets", "delayed_packets", "max_observed_delay", "aqm_drops")

    def __init__(
        self,
        rate_bps: float,
        max_delay_ms: float = 50.0,
        clock: Clock | None = None,
        skip_sleep: bool = False,
    ) -> None:
        if rate_bps <= 0:
            raise ValueError("rate_bps must be > 0")
        self._clock = clock or _RealClock()
        self._rate = float(rate_bps)
        self.max_delay = max(float(max_delay_ms), 1.0) / 1000.0
        self._burst = self._compute_burst()
        self._next_departure = 0.0
        self._lock = threading.RLock()
        self._skip_sleep = skip_sleep
        self.sent_bytes = 0
        self.dropped_bytes = 0
        self.sent_packets = 0
        self.dropped_packets = 0
        self.delayed_packets = 0
        self.max_observed_delay = 0.0
        self.aqm_drops = 0

    # ---------------------------------------------------------------- properties
    @property
    def rate(self) -> float:
        return self._rate

    @property
    def burst_bytes(self) -> float:
        """Bytes that may be queued before the AQM starts dropping."""
        return self._burst

    @property
    def max_delay_ms(self) -> float:
        return self.max_delay * 1000.0

    def _compute_burst(self) -> float:
        return max(self._rate * self.max_delay, MIN_BURST_BYTES)

    def set_rate(self, rate_bps: float) -> None:
        """Change the rate live (called from the GUI thread)."""
        if rate_bps <= 0:
            raise ValueError("rate_bps must be > 0")
        with self._lock:
            self._rate = float(rate_bps)
            self._burst = self._compute_burst()

    def set_max_delay(self, max_delay_ms: float) -> None:
        with self._lock:
            self.max_delay = max(float(max_delay_ms), 1.0) / 1000.0
            self._burst = self._compute_burst()

    # ------------------------------------------------------------------ planning
    def plan(self, size: int, queued_bytes: float = 0.0) -> tuple[bool, float]:
        """Decide what to do with a packet of ``size`` bytes.

        Returns ``(allow, wait_seconds)``:

        * ``(True, w)``   - send it after sleeping ``w`` seconds (``w`` may be 0);
        * ``(False, 0)``  - drop it: the queue already holds more than the delay budget,
          so letting it through would add latency beyond ``max_delay``.
        """
        with self._lock:
            if queued_bytes > 0 and queued_bytes + size > self._burst:
                self.aqm_drops += 1
                return False, 0.0
            now = self._clock.monotonic()
            depart = self._next_departure
            if depart < now:
                depart = now
            self._next_departure = depart + (size / self._rate)
            return True, depart - now

    def wait(self, seconds: float) -> None:
        """Sleep until the packet may leave, in steps small enough to stay responsive.

        Two Windows-specific details, both measured rather than assumed:

        * waits below ``MIN_SLEEP_SECONDS`` are not slept at all (a sleep call costs ~0.5 ms
          no matter how short it is, so per-packet sleeps would cap the rate far below the
          configured one). The debt stays in the virtual departure clock and is paid later.
        * each sleep requests ``SLEEP_OVERHEAD_SECONDS`` less than needed, because the call
          itself overshoots by roughly that much.
        """
        if self._skip_sleep or seconds < MIN_SLEEP_SECONDS:
            return
        remaining = seconds
        while remaining > 0:
            chunk = min(MAX_SLEEP_CHUNK, remaining)
            self._clock.sleep(max(chunk - SLEEP_OVERHEAD_SECONDS, 0.0))
            remaining -= chunk

    # -------------------------------------------------------------------- stats
    def note_sent(self, size: int, delay: float = 0.0) -> None:
        self.sent_bytes += size
        self.sent_packets += 1
        if delay > 0:
            self.delayed_packets += 1
            if delay > self.max_observed_delay:
                self.max_observed_delay = delay

    def note_dropped(self, size: int) -> None:
        self.dropped_bytes += size
        self.dropped_packets += 1

    def snapshot(self) -> dict[str, float]:
        return {
            "rate_bps": self._rate,
            "burst_bytes": self._burst,
            "max_delay_ms": self.max_delay * 1000.0,
            "sent_bytes": self.sent_bytes,
            "dropped_bytes": self.dropped_bytes,
            "sent_packets": self.sent_packets,
            "dropped_packets": self.dropped_packets,
            "delayed_packets": self.delayed_packets,
            "max_observed_delay_ms": self.max_observed_delay * 1000.0,
            "aqm_drops": self.aqm_drops,
        }


class RateMeter:
    """Bytes/second estimator over a sliding window (used by the CLI and tests)."""

    def __init__(self, window: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._samples: list[tuple[float, int]] = []
        self._total = 0
        self._window_total = 0

    def add(self, n: int) -> None:
        now = self._clock()
        with self._lock:
            self._samples.append((now, n))
            self._total += n
            self._window_total += n
            cutoff = now - self.window
            while self._samples and self._samples[0][0] < cutoff:
                self._window_total -= self._samples.pop(0)[1]

    @property
    def total(self) -> int:
        return self._total

    def rate(self) -> float:
        """Bytes per second over the last ``window`` seconds."""
        now = self._clock()
        with self._lock:
            cutoff = now - self.window
            while self._samples and self._samples[0][0] < cutoff:
                self._window_total -= self._samples.pop(0)[1]
            return self._window_total / self.window

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._window_total = 0
