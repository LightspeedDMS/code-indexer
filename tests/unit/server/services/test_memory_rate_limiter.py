"""
TDD tests for MemoryRateLimiter — Story #877 Phase 1b.

Per-user token-bucket rate limiter for memory write operations.
All tests use an injected frozen clock for determinism.
"""

import threading
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)


# ---------------------------------------------------------------------------
# RateLimitConfig validation
# ---------------------------------------------------------------------------


def test_none_config_raises() -> None:
    """MemoryRateLimiter rejects None config at construction time."""
    # type: ignore required — intentionally passing None to verify runtime
    # entry-point validation rejects invalid config before any attribute access.
    with pytest.raises(ValueError, match="config"):
        MemoryRateLimiter(None)  # type: ignore[arg-type]  # intentional invalid-type for runtime guard test


def test_invalid_user_id_raises() -> None:
    """consume/peek/reset reject None or empty user_id."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    for method in (limiter.consume, limiter.peek, limiter.reset):
        # type: ignore required — intentionally passing None to confirm the public
        # API rejects invalid user_id values at runtime, not just at the type-checker level.
        with pytest.raises(ValueError, match="user_id"):
            method(None)  # type: ignore[arg-type]  # intentional invalid-type for runtime guard test
        with pytest.raises(ValueError, match="user_id"):
            method("")


def test_invalid_config_raises() -> None:
    """RateLimitConfig rejects non-positive capacity or refill_per_second."""
    with pytest.raises(ValueError, match="capacity"):
        RateLimitConfig(capacity=0, refill_per_second=1.0)

    with pytest.raises(ValueError, match="refill_per_second"):
        RateLimitConfig(capacity=1, refill_per_second=0.0)

    with pytest.raises(ValueError, match="refill_per_second"):
        RateLimitConfig(capacity=1, refill_per_second=-1.0)

    with pytest.raises(ValueError, match="capacity"):
        RateLimitConfig(capacity=-5, refill_per_second=1.0)


# ---------------------------------------------------------------------------
# Basic consume / allow / throttle
# ---------------------------------------------------------------------------


def test_first_consume_allowed() -> None:
    """Fresh limiter with capacity=5: first consume returns True."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    assert limiter.consume("alice") is True


def test_burst_up_to_capacity() -> None:
    """capacity=5: five successive consumes all return True."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    results = [limiter.consume("alice") for _ in range(5)]
    assert all(results)


def test_exceeds_capacity_throttled() -> None:
    """capacity=5: five succeed, sixth returns False (no refill, frozen clock)."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    for _ in range(5):
        limiter.consume("alice")
    assert limiter.consume("alice") is False


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------


def test_per_user_isolation() -> None:
    """Draining user A's bucket does not affect user B."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    for _ in range(5):
        limiter.consume("alice")
    assert limiter.consume("alice") is False
    # Bob's bucket is still full
    assert limiter.consume("bob") is True


# ---------------------------------------------------------------------------
# Refill behaviour
# ---------------------------------------------------------------------------


def test_refill_restores_tokens() -> None:
    """Drain bucket; advance clock 3 s → 3 more consumes succeed."""
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    # drain
    for _ in range(5):
        limiter.consume("alice")
    assert limiter.consume("alice") is False
    # advance 3 seconds
    now[0] = 3.0
    results = [limiter.consume("alice") for _ in range(3)]
    assert all(results)
    # 4th is throttled (only 3 tokens refilled)
    assert limiter.consume("alice") is False


def test_refill_cap_at_capacity() -> None:
    """After very long idle the bucket does not exceed capacity."""
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    # Touch the bucket so it is initialised
    limiter.peek("alice")
    # Advance 1000 seconds — should still be capped at 5
    now[0] = 1000.0
    assert limiter.peek("alice") == 5.0


def test_fractional_refill_accumulates() -> None:
    """refill=0.5/s: 1 s → 0.5 tokens (cannot consume 1). 2 more s → 1.5 total (can consume 1, 0.5 left)."""
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=0.5), clock=clock
    )
    # Drain to 0
    for _ in range(5):
        limiter.consume("alice")
    assert limiter.peek("alice") == 0.0

    # 1 second → 0.5 tokens; cannot consume 1
    now[0] = 1.0
    assert limiter.consume("alice") is False
    assert limiter.peek("alice") == pytest.approx(0.5, abs=1e-9)

    # 2 more seconds → 1.5 tokens; can consume 1, leaving 0.5
    now[0] = 3.0
    assert limiter.consume("alice") is True
    assert limiter.peek("alice") == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# Multi-token consume
# ---------------------------------------------------------------------------


def test_consume_invalid_tokens() -> None:
    """consume() raises ValueError for tokens <= 0."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    with pytest.raises(ValueError, match="tokens"):
        limiter.consume("alice", tokens=0)
    with pytest.raises(ValueError, match="tokens"):
        limiter.consume("alice", tokens=-1)


def test_multi_token_consume() -> None:
    """consume(tokens=3) with 5 available → True; peek → 2."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    assert limiter.consume("alice", tokens=3) is True
    assert limiter.peek("alice") == pytest.approx(2.0, abs=1e-9)


def test_multi_token_consume_insufficient() -> None:
    """consume(tokens=3) with 2 available → False; peek unchanged at 2."""
    clock = MagicMock(return_value=0.0)
    limiter = MemoryRateLimiter(
        RateLimitConfig(capacity=5, refill_per_second=1.0), clock=clock
    )
    # consume 3 to leave 2
    limiter.consume("alice", tokens=3)
    assert limiter.peek("alice") == pytest.approx(2.0, abs=1e-9)
    # now try to consume 3 more (only 2 left)
    assert limiter.consume("alice", tokens=3) is False
    assert limiter.peek("alice") == pytest.approx(2.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_restores_full_capacity() -> None:
    """Drain bucket, reset → peek == capacity."""
    clock = MagicMock(return_value=0.0)
    config = RateLimitConfig(capacity=5, refill_per_second=1.0)
    limiter = MemoryRateLimiter(config, clock=clock)
    for _ in range(5):
        limiter.consume("alice")
    assert limiter.peek("alice") == 0.0
    limiter.reset("alice")
    assert limiter.peek("alice") == float(config.capacity)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_thread_safe_consume() -> None:
    """10 threads × 100 consumes with frozen clock and capacity=1000: all succeed, no race crash."""
    # Frozen clock — no refill ever fires. Capacity=1000 > 1000 total consumes,
    # so every thread succeeds deterministically without relying on real time.
    clock = MagicMock(return_value=0.0)
    config = RateLimitConfig(capacity=1000, refill_per_second=1.0)
    limiter = MemoryRateLimiter(config, clock=clock)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(100):
            outcome = limiter.consume("shared_user")
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1000
    assert all(results), (
        "All consumes should succeed: capacity=1000 >= 1000 total consumes"
    )


def test_thread_safe_throttling() -> None:
    """100 threads racing for capacity=10 bucket with frozen clock: exactly 10 succeed."""
    frozen_time = 0.0
    clock = lambda: frozen_time  # noqa: E731
    config = RateLimitConfig(capacity=10, refill_per_second=0.001)
    limiter = MemoryRateLimiter(config, clock=clock)

    success_count = 0
    counter_lock = threading.Lock()
    barrier = threading.Barrier(100)

    def worker() -> None:
        nonlocal success_count
        barrier.wait()  # synchronise all threads to start together
        result = limiter.consume("shared_user")
        if result:
            with counter_lock:
                success_count += 1

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert success_count == 10, f"Expected exactly 10 successes, got {success_count}"
