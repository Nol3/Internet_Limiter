"""WinDivert-based packet interceptor with low-latency throttle."""
from __future__ import annotations
import threading
import time
from typing import Callable

import pydivert

from limiter import TokenBucket


class BandwidthLimiter:
    """Global IP traffic throttle (PC-wide, not per-app).

    rate_down_bps: download limit (bytes/s), 0 = unlimited
    rate_up_bps:   upload limit (bytes/s),   0 = unlimited
    """

    def __init__(self, rate_down_bps: float, rate_up_bps: float,
                 on_stats: Callable[[int, int], None] | None = None):
        self.down_bucket = TokenBucket(max(rate_down_bps, 1))
        self.up_bucket = TokenBucket(max(rate_up_bps, 1))
        self.down_enabled = rate_down_bps > 0
        self.up_enabled = rate_up_bps > 0
        self.on_stats = on_stats
        self._running = False
        self._in_thread: threading.Thread | None = None
        self._out_thread: threading.Thread | None = None
        self._in_handle: pydivert.WinDivert | None = None
        self._out_handle: pydivert.WinDivert | None = None
        self._bytes_down = 0
        self._bytes_up = 0
        self._dropped_down = 0
        self._dropped_up = 0
        self._stats_lock = threading.Lock()
        self._thread_errors: dict[str, Exception] = {}

    def set_limits(self, rate_down_bps: float, rate_up_bps: float) -> None:
        self.down_enabled = rate_down_bps > 0
        self.up_enabled = rate_up_bps > 0
        if self.down_enabled:
            self.down_bucket.set_rate(rate_down_bps)
        if self.up_enabled:
            self.up_bucket.set_rate(rate_up_bps)

    def _loop(self, direction: str) -> None:
        is_in = direction == "in"
        filter_str = (
            ("inbound" if is_in else "outbound")
            + " and ip"
            + " and not loopback"
            + " and not (ip.SrcAddr >= 10.0.0.0 and ip.SrcAddr <= 10.255.255.255)"
            + " and not (ip.DstAddr >= 10.0.0.0 and ip.DstAddr <= 10.255.255.255)"
            + " and not (ip.SrcAddr >= 192.168.0.0 and ip.SrcAddr <= 192.168.255.255)"
            + " and not (ip.DstAddr >= 192.168.0.0 and ip.DstAddr <= 192.168.255.255)"
            + " and not (ip.SrcAddr >= 172.16.0.0 and ip.SrcAddr <= 172.31.255.255)"
            + " and not (ip.DstAddr >= 172.16.0.0 and ip.DstAddr <= 172.31.255.255)"
        )
        bucket = self.down_bucket if is_in else self.up_bucket
        enabled_attr = "down_enabled" if is_in else "up_enabled"

        # Open WinDivert handle — errors here are captured and surfaced to caller
        try:
            handle = pydivert.WinDivert(filter_str)
            if is_in:
                self._in_handle = handle
            else:
                self._out_handle = handle
            handle.open()
        except Exception as e:
            self._thread_errors[direction] = e
            return

        # Main packet loop
        try:
            while self._running:
                try:
                    packet = handle.recv()
                except Exception:
                    if not self._running:
                        break
                    continue
                size = len(packet.raw)
                enabled = getattr(self, enabled_attr)
                drop = False
                if enabled:
                    allow, delay = bucket.try_consume(size)
                    if not allow:
                        drop = True
                    elif delay > 0:
                        time.sleep(delay)
                if drop:
                    with self._stats_lock:
                        if is_in:
                            self._dropped_down += size
                        else:
                            self._dropped_up += size
                    continue
                try:
                    handle.send(packet)
                except Exception:
                    continue
                with self._stats_lock:
                    if is_in:
                        self._bytes_down += size
                    else:
                        self._bytes_up += size
        finally:
            try:
                handle.close()
            except Exception:
                pass
            # Clear own handle reference; never touch the other direction's handle
            if is_in:
                self._in_handle = None
            else:
                self._out_handle = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread_errors.clear()
        self._in_thread = threading.Thread(target=self._loop, args=("in",), daemon=True)
        self._out_thread = threading.Thread(target=self._loop, args=("out",), daemon=True)
        self._in_thread.start()
        self._out_thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for h in (self._in_handle, self._out_handle):
            if h is not None:
                try:
                    h.close()
                except Exception:
                    pass

    def get_stats(self) -> tuple[int, int]:
        with self._stats_lock:
            return self._bytes_down, self._bytes_up

    def reset_stats(self) -> None:
        with self._stats_lock:
            self._bytes_down = 0
            self._bytes_up = 0
            self._dropped_down = 0
            self._dropped_up = 0
