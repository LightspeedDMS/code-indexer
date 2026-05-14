"""Token bucket rate limiting for API authentication.

Implements per-username token buckets with time-based refills and thread safety.
In cluster mode, token state is stored in PostgreSQL for cross-node consistency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple, Callable

logger = logging.getLogger(__name__)

_TOKEN_COST = 1.0  # Token units consumed per request


class TokenBucket:
    """Simple token bucket supporting fractional refill based on elapsed time."""

    def __init__(
        self,
        capacity: int = 10,
        refill_rate: float = 1 / 6.0,  # tokens per second (1 token every 6s)
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_rate = float(refill_rate)
        self.last_refill = time_fn()
        self._time_fn = time_fn

    def _refill(self) -> None:
        now = self._time_fn()
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

    def consume(self) -> Tuple[bool, float]:
        """Attempt to consume a single token.

        Returns:
            (allowed, retry_after_seconds)
        """
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        # compute seconds until next token reaches 1.0
        needed = max(0.0, 1.0 - self.tokens)
        retry_after = (
            needed / self.refill_rate if self.refill_rate > 0 else float("inf")
        )
        return False, retry_after

    def refund(self) -> None:
        """Refund a token (e.g., for successful authentication)."""
        self._refill()
        self.tokens = min(self.capacity, self.tokens + 1.0)


class TokenBucketManager:
    """Manages per-username token buckets with thread-safe access and cleanup."""

    def __init__(
        self,
        capacity: int = 10,
        refill_rate: float = 1 / 6.0,
        cleanup_seconds: int = 3600,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.cleanup_seconds = cleanup_seconds
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._buckets: Dict[str, TokenBucket] = {}
        self._last_access: Dict[str, float] = {}
        self._pool: Optional[Any] = None

    def set_connection_pool(self, pool: Any) -> None:
        """Set PostgreSQL connection pool for cluster mode.

        When set, consume() and refund() use the token_bucket_state PG table
        instead of in-memory dicts, enabling cross-node rate limiting.
        """
        self._pool = pool
        logger.info(
            "TokenBucketManager: using PostgreSQL connection pool (cluster mode)"
        )

    def _pg_ensure_row(self, conn: Any, username: str, now: float) -> None:
        """Insert row with full capacity on first access. No-op if row exists."""
        conn.execute(
            "INSERT INTO token_bucket_state (username, tokens, last_refill, last_access) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (username) DO NOTHING",
            (username, float(self.capacity), now, now),
        )
        conn.commit()

    def _pg_consume(self, username: str) -> Tuple[bool, float]:
        """PG consume: ensure row, refill, decrement by _TOKEN_COST if available.

        Called under self._lock. Returns (allowed, retry_after_seconds).
        """
        assert self._pool is not None
        now = time.time()
        with self._pool.connection() as conn:
            self._pg_ensure_row(conn, username, now)
            row = conn.execute(
                "SELECT tokens, last_refill FROM token_bucket_state WHERE username = %s",
                (username,),
            ).fetchone()
            tokens, last_refill = row[0], row[1]
            elapsed = max(0.0, now - last_refill)
            tokens = min(float(self.capacity), tokens + elapsed * self.refill_rate)
            if tokens >= _TOKEN_COST:
                conn.execute(
                    "UPDATE token_bucket_state "
                    "SET tokens = %s, last_refill = %s, last_access = %s "
                    "WHERE username = %s",
                    (tokens - _TOKEN_COST, now, now, username),
                )
                conn.commit()
                return True, 0.0
            conn.execute(
                "UPDATE token_bucket_state "
                "SET tokens = %s, last_refill = %s, last_access = %s "
                "WHERE username = %s",
                (tokens, now, now, username),
            )
            conn.commit()
            needed = max(0.0, _TOKEN_COST - tokens)
            retry_after = (
                needed / self.refill_rate if self.refill_rate > 0 else float("inf")
            )
            return False, retry_after

    def _pg_refund(self, username: str) -> None:
        """PG refund: ensure row then increment tokens capped at capacity.

        Called under self._lock.
        """
        assert self._pool is not None
        now = time.time()
        with self._pool.connection() as conn:
            self._pg_ensure_row(conn, username, now)
            conn.execute(
                "UPDATE token_bucket_state "
                "SET tokens = MIN(%s, tokens + %s), last_access = %s "
                "WHERE username = %s",
                (float(self.capacity), _TOKEN_COST, now, username),
            )
            conn.commit()

    def _get_bucket(self, username: str) -> TokenBucket:
        b = self._buckets.get(username)
        if b is None:
            b = TokenBucket(
                capacity=self.capacity,
                refill_rate=self.refill_rate,
                time_fn=self._time_fn,
            )
            self._buckets[username] = b
        self._last_access[username] = self._time_fn()
        return b

    def _cleanup(self) -> None:
        now = self._time_fn()
        to_delete = [
            u for u, ts in self._last_access.items() if now - ts > self.cleanup_seconds
        ]
        for u in to_delete:
            self._buckets.pop(u, None)
            self._last_access.pop(u, None)

    def consume(self, username: str) -> Tuple[bool, float]:
        with self._lock:
            if self._pool is not None:
                return self._pg_consume(username)
            self._cleanup()
            bucket = self._get_bucket(username)
            allowed, retry = bucket.consume()
            self._last_access[username] = self._time_fn()
            return allowed, retry

    def refund(self, username: str) -> None:
        with self._lock:
            if self._pool is not None:
                self._pg_refund(username)
                return
            bucket = self._get_bucket(username)
            bucket.refund()
            self._last_access[username] = self._time_fn()

    def get_tokens(self, username: str) -> float:
        with self._lock:
            bucket = self._get_bucket(username)
            # Trigger refill to return up-to-date value
            allowed, _ = bucket.consume()
            if allowed:
                # Put it back since this is a read-only method
                bucket.refund()
            return bucket.tokens


# Default, module-level rate limiter instance for application use
rate_limiter = TokenBucketManager()
