"""RateLimiter unit tests."""

from __future__ import annotations

import time

from mcgram.rate_limiter import RateLimiter, TokenBucket


def test_token_bucket_initial_capacity() -> None:
    b = TokenBucket(rate_per_min=60)
    assert b.capacity == 60
    for _ in range(60):
        assert b.acquire()
    assert not b.acquire()  # exhausted


def test_token_bucket_refills_over_time(monkeypatch) -> None:
    b = TokenBucket(rate_per_min=60)
    for _ in range(60):
        b.acquire()
    assert not b.acquire()
    # Fast-forward monotonic clock by 1s → 1 token refilled (60/60 per sec)
    now = b.last_refill
    monkeypatch.setattr(time, "monotonic", lambda: now + 1.0)
    assert b.acquire()
    assert not b.acquire()


def test_rate_limiter_independent_buckets() -> None:
    rl = RateLimiter(rate_per_min=2)
    assert rl.try_acquire("send_message")
    assert rl.try_acquire("send_message")
    assert not rl.try_acquire("send_message")
    # Other tool gets its own bucket
    assert rl.try_acquire("send_file")
    assert rl.try_acquire("send_file")
    assert not rl.try_acquire("send_file")


def test_rate_limiter_creates_bucket_lazily() -> None:
    rl = RateLimiter(rate_per_min=5)
    # arbitrary tool name works
    assert rl.try_acquire("anything")
