"""End-to-end capture/inject tests with a fake WinDivert handle.

The most dangerous failure mode of a userspace packet filter is a capture thread dying
while the driver still holds packets: the machine loses its internet connection because
nothing is reinjected. These tests reproduce that scenario, and the parser surprises that
caused it, without touching the real driver.

The fake handle mimics just enough of pydivert's API (``open``/``recv``/``send``/``close``)
and, like the real thing, hands packets over as ``memoryview``.
"""

from __future__ import annotations

import ipaddress
import queue
import struct
import threading
import time

import pytest

from limitador import engine as engine_mod
from limitador.engine import Engine
from limitador.rules import AppRule, Config, Settings

MB = 1024 * 1024
DEMO_EXE = r"C:\test\demo.exe"


# --------------------------------------------------------------------- fake driver
class FakePacket:
    """Mimics pydivert's Packet: a ``raw`` memoryview is the only thing we rely on."""

    def __init__(self, raw: bytes) -> None:
        self.raw = memoryview(bytes(raw))
        self.socket = None


class FakeHandle:
    def __init__(self, filter_string: str = "true", layer: int = 0) -> None:
        self.filter = filter_string
        self.layer = layer
        self.inbox: queue.Queue = queue.Queue()
        self.sent: list[FakePacket] = []
        self.closed = False
        self.received = 0
        self.checksum_recalcs = 0

    # pydivert API -------------------------------------------------------------
    def open(self):
        if self.layer == 3:  # Layer.SOCKET in the fake, to emulate WinError 87
            raise OSError(87, "El parámetro no es correcto.")
        return self

    def close(self) -> None:
        self.closed = True
        self.inbox.put(None)

    def recv(self, *args, **kwargs) -> FakePacket:
        item = self.inbox.get()
        if item is None:
            raise OSError("handle closed")
        self.received += 1
        return item

    def send(self, packet, recalculate_checksum: bool = True) -> int:
        if self.closed:
            raise OSError("handle closed")
        if recalculate_checksum:
            self.checksum_recalcs += 1
        self.sent.append(packet)
        return len(packet.raw)

    # test helpers -------------------------------------------------------------
    def feed(self, packets: list[FakePacket]) -> None:
        for packet in packets:
            self.inbox.put(packet)


class NoSocketHandle(FakeHandle):
    """Fails at the socket layer exactly like the real driver did on this machine."""


def tcp_packet(
    src: str, dst: str, sport: int = 51000, dport: int = 443, payload: int = 100
) -> FakePacket:
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        40 + payload,
        0,
        0,
        64,
        6,
        0,
        ipaddress.IPv4Address(src).packed,
        ipaddress.IPv4Address(dst).packed,
    )
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, 0x18, 65535, 0, 0)
    return FakePacket(ip + tcp + b"\xa5" * payload)


@pytest.fixture
def fake_driver(monkeypatch):
    """Pretend we are elevated and the driver is ready."""
    monkeypatch.setattr(engine_mod.driver, "is_admin", lambda: True)
    monkeypatch.setattr(engine_mod.driver, "prepare", lambda *a, **k: (True, "fake"))
    return monkeypatch


def build_engine(config: Config, factory=FakeHandle) -> Engine:
    return Engine(config, handle_factory=factory)


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -------------------------------------------------------------------------- tests
def test_all_captured_packets_are_reinjected(fake_driver):
    engine = build_engine(Config(settings=Settings(global_down_bps=0)))
    engine.start()
    try:
        out_handle = engine._handles[0]
        out_handle.feed([tcp_packet("192.168.1.10", "8.8.8.8") for _ in range(20)])
        assert wait_until(lambda: len(out_handle.sent) == 20), (
            "no se reinyectaron todos los paquetes"
        )
        assert engine.is_running
        assert engine.capture_released is False
    finally:
        engine.stop()


def test_packets_are_reinjected_without_recalculating_checksums(fake_driver):
    """Regression guard for the throughput killer: pydivert recomputes checksums in pure
    Python on Windows (~60-150 us per packet), which capped the pipeline at ~9 MB/s."""
    engine = build_engine(Config(settings=Settings(global_down_bps=1 * MB)))
    engine.start()
    try:
        out_handle = engine._handles[0]
        out_handle.feed([tcp_packet("192.168.1.10", "8.8.8.8") for _ in range(10)])
        assert wait_until(lambda: len(out_handle.sent) == 10)
        assert out_handle.checksum_recalcs == 0
    finally:
        engine.stop()


def test_parser_failure_still_forwards_every_packet(fake_driver, monkeypatch):
    """Regression test for the outage: a raising classifier must not trap traffic."""

    def boom(*args, **kwargs):
        raise ValueError("parser broken")

    monkeypatch.setattr(engine_mod, "inspect_safe", boom)
    engine = build_engine(Config(settings=Settings(global_down_bps=1 * MB)))
    engine.start()
    try:
        out_handle = engine._handles[0]
        out_handle.feed([tcp_packet("192.168.1.10", "8.8.8.8") for _ in range(10)])
        assert wait_until(lambda: len(out_handle.sent) == 10), (
            "un fallo del analizador dejó paquetes atrapados (corte de internet)"
        )
        assert engine.is_running
    finally:
        engine.stop()


def test_decision_failure_still_forwards_every_packet(fake_driver, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("decision broken")

    monkeypatch.setattr(engine_mod, "should_pass_through", boom)
    engine = build_engine(Config(settings=Settings(global_down_bps=1 * MB)))
    engine.start()
    try:
        out_handle = engine._handles[0]
        out_handle.feed([tcp_packet("192.168.1.10", "8.8.8.8") for _ in range(10)])
        assert wait_until(lambda: len(out_handle.sent) == 10)
    finally:
        engine.stop()


def test_watchdog_releases_the_capture_when_a_worker_dies(fake_driver):
    engine = build_engine(Config(settings=Settings(global_down_bps=1 * MB)))
    engine.start()
    try:
        # A thread that was never started is not alive: same as a thread that crashed.
        engine._capture_threads["out"] = threading.Thread(target=lambda: None)
        assert wait_until(lambda: engine.capture_released, timeout=3.0), (
            "el vigilante no liberó la captura tras morir un hilo"
        )
        assert engine.is_running is False
        assert engine._handles == []
    finally:
        engine.stop()


def test_socket_layer_failure_does_not_prevent_limiting(fake_driver):
    engine = build_engine(Config(settings=Settings(global_down_bps=1 * MB)), NoSocketHandle)
    engine.start()
    try:
        assert engine.is_running
        assert engine.snapshot().socket_events_ok is False
        assert len(engine._handles) == 2  # only the two network handles
    finally:
        engine.stop()


def feed_gradually(handle, packets, gap: float) -> threading.Thread:
    """Offer packets at a controlled rate, the way a real sender does."""

    def run() -> None:
        for packet in packets:
            handle.inbox.put(packet)
            time.sleep(gap)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_below_the_limit_nothing_is_dropped_and_no_latency_is_added(fake_driver):
    rate = 2 * MB
    size = 1480
    offered = rate // 2
    engine = build_engine(
        Config(settings=Settings(global_down_bps=rate, global_up_bps=rate, max_delay_ms=50))
    )
    engine.start()
    try:
        out_handle = engine._handles[0]
        packets = [tcp_packet("192.168.1.10", "8.8.8.8", payload=size - 40) for _ in range(200)]
        started = time.monotonic()
        feed_gradually(out_handle, packets, size / offered)
        assert wait_until(lambda: len(out_handle.sent) == len(packets), timeout=10.0), (
            "no se reinyectaron todos los paquetes ofrecidos por debajo del límite"
        )
        elapsed = time.monotonic() - started
        snapshot = engine.snapshot()
        assert snapshot.dropped_packets == 0
        expected = len(packets) * size / offered
        assert elapsed == pytest.approx(expected, rel=0.5), (
            f"por debajo del límite no debe añadirse retardo ({elapsed:.3f}s vs {expected:.3f}s)"
        )
        # No packet should have been held back meaningfully.
        assert snapshot.shapers["global:out"]["max_observed_delay_ms"] < 5.0
    finally:
        engine.stop()


def test_offered_load_above_the_limit_is_paced_to_the_limit(fake_driver):
    rate = 1 * MB
    size = 1480
    engine = build_engine(
        Config(settings=Settings(global_down_bps=rate, global_up_bps=rate, max_delay_ms=50))
    )
    engine.start()
    try:
        out_handle = engine._handles[0]
        packets = [tcp_packet("192.168.1.10", "8.8.8.8", payload=size - 40) for _ in range(300)]
        started = time.monotonic()
        feed_gradually(out_handle, packets, size / (3 * rate))  # offered load = 3x limit
        assert wait_until(
            lambda: len(out_handle.sent) + engine.snapshot().dropped_packets >= len(packets),
            timeout=20.0,
        )
        elapsed = time.monotonic() - started
        snapshot = engine.snapshot()
        sent = len(out_handle.sent)
        expected = sent * size / rate
        assert elapsed >= expected * 0.7, f"se envió más rápido que el límite ({elapsed:.3f}s)"
        assert snapshot.dropped_packets > 0, "un exceso sostenido debe descartar el sobrante"
        assert sent > 0
        assert snapshot.shapers["global:out"]["max_observed_delay_ms"] <= 60.0
    finally:
        engine.stop()


def test_stop_flushes_queued_packets_instead_of_dropping_them(fake_driver):
    # A budget large enough to hold the whole burst, so nothing is dropped by the AQM
    # and everything that is still queued when we stop has to be flushed.
    engine = build_engine(
        Config(
            settings=Settings(global_down_bps=64 * 1024, global_up_bps=64 * 1024, max_delay_ms=2000)
        )
    )
    engine.start()
    out_handle = engine._handles[0]
    packets = [tcp_packet("192.168.1.10", "8.8.8.8", payload=1440) for _ in range(60)]
    out_handle.feed(packets)
    assert wait_until(lambda: len(out_handle.sent) > 0, timeout=3.0)
    partial = len(out_handle.sent)
    assert partial < len(packets), "el limitador no estaba reteniendo nada que vaciar"
    engine.stop()
    assert len(out_handle.sent) == len(packets), (
        "al detenerse deben reinyectarse los paquetes que estaban en cola"
    )
    assert engine.is_running is False
    assert engine._handles == []


def test_per_application_rule_uses_its_own_shaper(fake_driver, monkeypatch):
    config = Config(settings=Settings(global_down_bps=0, global_up_bps=0))
    config.set_rule(AppRule(exe=DEMO_EXE, down_bps=256 * 1024, up_bps=256 * 1024))
    engine = build_engine(config)
    monkeypatch.setattr(engine.procmap, "pid_for", lambda *_a, **_k: 4321)
    monkeypatch.setattr(engine.procmap, "path_for", lambda *_a, **_k: DEMO_EXE)
    engine.start()
    try:
        out_handle = engine._handles[0]
        out_handle.feed([tcp_packet("192.168.1.10", "8.8.8.8", payload=200) for _ in range(5)])
        assert wait_until(lambda: len(out_handle.sent) == 5, timeout=5.0)
        snapshot = engine.snapshot()
        # El nombre del planificador lleva la ruta normalizada y la dirección.
        expected_key = f"app:{Config.normalise_exe(DEMO_EXE)}:out"
        assert expected_key in snapshot.shapers
        assert snapshot.per_pid.get(4321, (0, 0))[0] > 0
    finally:
        engine.stop()
