"""Token bucket for bandwidth throttling (low-latency variant)."""
import time
import threading


class TokenBucket:
    """Rate limiter with small burst to minimize bufferbloat.

    rate_bps:     bytes per second allowed
    burst_ms:     max burst window in ms (smaller = less bufferbloat, more CPU)
    max_delay_ms: if a packet would need to wait > this, drop it (AQM)
    """

    def __init__(self, rate_bps: float, burst_ms: int = 20, max_delay_ms: int = 30):
        self.rate = max(rate_bps, 1.0)
        self.burst_ms = burst_ms
        self.max_delay = max_delay_ms / 1000.0
        self.capacity = self.rate * burst_ms / 1000.0
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, rate_bps: float) -> None:
        with self.lock:
            self.rate = max(rate_bps, 1.0)
            self.capacity = self.rate * self.burst_ms / 1000.0
            if self.tokens > self.capacity:
                self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def try_consume(self, n: int) -> tuple[bool, float]:
        """Returns (allow, delay_seconds).

        allow=False → drop packet (exceeds max_delay budget).
        allow=True with delay > 0 → sleep then send.
        """
        with self.lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True, 0.0
            deficit = n - self.tokens
            wait = deficit / self.rate
            if wait > self.max_delay:
                return False, 0.0
            self.tokens -= n
            return True, wait
