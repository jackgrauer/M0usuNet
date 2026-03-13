"""Token-bucket rate limiter for MQTT ingest."""

import time
import threading


class TokenBucket:
    """Thread-safe token bucket rate limiter.

    Args:
        rate: Tokens replenished per second.
        burst: Maximum tokens (bucket capacity).
    """

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        """Try to consume n tokens. Returns True if allowed."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


class RateLimiter:
    """Per-key rate limiter (one bucket per MQTT topic / source)."""

    def __init__(self, rate: float = 2.0, burst: int = 10):
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Check if a message from `key` should be allowed."""
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(self.rate, self.burst)
        return self._buckets[key].consume()
