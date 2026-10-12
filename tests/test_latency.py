"""Latency measurement tests (real ICMP, no admin needed).

IcmpSendEcho works unprivileged, so these run anywhere. The loopback and the unreachable
address are deterministic; the internet target is only used for a smoke check.
"""

from __future__ import annotations

import time

import pytest

from limitador.latency import LatencyMonitor, LatencyStats, ping, resolve

# TEST-NET-1 (RFC 5737): guaranteed not to be routed, so a ping to it can only time out.
UNROUTABLE = "192.0.2.1"


def test_loopback_answers_without_admin():
    value = ping("127.0.0.1", timeout_ms=1000)
    assert value is not None, "el loopback debe responder a ICMP"
    assert value < 50.0


def test_unroutable_address_times_out():
    assert ping(UNROUTABLE, timeout_ms=300) is None


def test_invalid_hostname_returns_none():
    assert ping("no-existe.invalid", timeout_ms=200) is None
    assert resolve("no-existe.invalid") is None


def test_resolve_returns_ipv4_number():
    assert resolve("127.0.0.1") == 0x0100007F  # 127.0.0.1 en orden de red


def test_stats_math_with_injected_samples():
    monitor = LatencyMonitor(host="ejemplo", window=10)
    for value in (10.0, 20.0, 30.0, None):
        monitor._samples.append(value)
    stats = monitor.stats()
    assert stats.samples == 3
    assert stats.lost == 1
    assert stats.last_ms == 30.0
    assert stats.avg_ms == pytest.approx(20.0)
    assert stats.min_ms == 10.0
    assert stats.max_ms == 30.0
    assert stats.jitter_ms == pytest.approx(10.0)
    assert stats.loss_percent == pytest.approx(25.0)


def test_empty_stats_do_not_lie():
    stats = LatencyStats(host="nada")
    assert stats.samples == 0
    assert "sin respuesta" in stats.line()
    assert stats.stable is False


def test_stability_flag_tracks_spikes():
    steady = LatencyStats(samples=5, avg_ms=20.0, max_ms=25.0)
    spiky = LatencyStats(samples=5, avg_ms=20.0, max_ms=200.0)
    assert steady.stable is True
    assert spiky.stable is False


def test_monitor_thread_collects_samples():
    monitor = LatencyMonitor(host="127.0.0.1", interval=0.05, window=5)
    monitor.start()
    try:
        assert _wait_for(lambda: monitor.stats().samples >= 2, timeout=3.0)
    finally:
        monitor.stop()
    assert monitor.stats().avg_ms < 50.0


def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
