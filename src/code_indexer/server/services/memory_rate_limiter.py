"""
Per-user token-bucket rate limiter for memory write operations.

Story #877 Phase 1b — in-memory, process-local, thread-safe.
Multi-node cluster enforcement is explicitly out of scope.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class RateLimitConfig:
    """Token-bucket parameters."""

    capacity: int
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")


class MemoryRateLimiter:
    """
    In-memory per-user token-bucket rate limiter for memory write operations.

    Thread-safe. Tokens are consumed on each successful .consume() call.
    If fewer than `tokens` tokens are available, .consume() returns False.
    Clock is injected for deterministic testing.
    """

    def __init__(
        self,
        config: RateLimitConfig,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if config is None:
            raise ValueError("config must not be None")
        self._config = config
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.monotonic
        )
        # Per-user state: user_id -> (tokens, last_refill_time)
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not user_id:
            raise ValueError("user_id must be a non-empty string")

    def _refill(self, tokens: float, last_time: float, now: float) -> float:
        """Return token count after refilling based on elapsed time, capped at capacity."""
        elapsed = max(0.0, now - last_time)
        refilled = tokens + elapsed * self._config.refill_per_second
        return min(refilled, float(self._config.capacity))

    def _get_bucket(self, user_id: str, now: float) -> Tuple[float, float]:
        """Return (current_tokens, now) for user_id, creating bucket if absent."""
        if user_id not in self._buckets:
            return float(self._config.capacity), now
        stored_tokens, last_time = self._buckets[user_id]
        return self._refill(stored_tokens, last_time, now), now

    def consume(self, user_id: str, tokens: int = 1) -> bool:
        """
        Attempt to consume `tokens` tokens for user_id.
        Returns True if allowed, False if throttled.
        Refills the bucket based on elapsed time before attempting consumption.
        Raises ValueError if user_id is None/empty or tokens <= 0.
        """
        self._validate_user_id(user_id)
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        with self._lock:
            now = self._clock()
            current, _ = self._get_bucket(user_id, now)
            if current < tokens:
                # Persist the refilled amount but do not subtract
                self._buckets[user_id] = (current, now)
                return False
            self._buckets[user_id] = (current - tokens, now)
            return True

    def peek(self, user_id: str) -> float:
        """Return current token count for user_id (does not consume).
        Raises ValueError if user_id is None/empty.
        """
        self._validate_user_id(user_id)
        with self._lock:
            now = self._clock()
            current, _ = self._get_bucket(user_id, now)
            # Materialise the bucket so future peeks/consumes start from now
            self._buckets[user_id] = (current, now)
            return current

    def reset(self, user_id: str) -> None:
        """Reset user_id's bucket to full capacity (admin/test helper).
        Raises ValueError if user_id is None/empty.
        """
        self._validate_user_id(user_id)
        with self._lock:
            now = self._clock()
            self._buckets[user_id] = (float(self._config.capacity), now)
