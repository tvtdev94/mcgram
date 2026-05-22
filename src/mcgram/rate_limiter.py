"""Per-tool token-bucket rate limiter (async-safe; lock-free reads)."""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_min: int, burst: int | None = None) -> None:
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = float(burst if burst is not None else rate_per_min)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_refill)
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class RateLimiter:
    """Per-tool token bucket. Each tool name has its own bucket sized to `rate_per_min`."""

    def __init__(self, rate_per_min: int) -> None:
        self._rate = rate_per_min
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tool: str) -> bool:
        bucket = self._buckets.get(tool)
        if bucket is None:
            with self._lock:
                bucket = self._buckets.get(tool)
                if bucket is None:
                    bucket = TokenBucket(self._rate)
                    self._buckets[tool] = bucket
        return bucket.acquire()
