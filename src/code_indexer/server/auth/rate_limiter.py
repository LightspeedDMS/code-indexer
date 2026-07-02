"""
Rate limiter for password change attempts to prevent brute force attacks.

Implements secure rate limiting with 15-minute lockout after 5 failed attempts.
Following CLAUDE.md principles: NO MOCKS - Real rate limiting implementation.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PasswordChangeRateLimiter:
    """
    Rate limiter for password change attempts to prevent brute force attacks.

    Security requirements:
    - Maximum 5 failed attempts per user
    - 15-minute lockout period after exceeding limit
    - Thread-safe implementation
    - Automatic cleanup of expired entries
    """

    def __init__(self) -> None:
        """Initialize rate limiter."""
        self._attempts: Dict[str, Dict] = {}
        self._lock = Lock()
        self._max_attempts = 5
        self._lockout_duration_minutes = 15
        self._pool: Optional[Any] = None
        self._limiter_type = "password_change"

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL for cluster mode (Bug #573)."""
        self._pool = pool
        logger.info("%s: using PostgreSQL (cluster mode)", self.__class__.__name__)

    def check_rate_limit(self, username: str) -> Optional[str]:
        """
        Check if user is rate limited.

        Args:
            username: Username to check

        Returns:
            None if not rate limited, error message if rate limited
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_check_locked(username)

            now = datetime.now(timezone.utc)

            # Clean up expired entries first
            self._cleanup_expired_entries(now)

            if username not in self._attempts:
                return None

            user_data = self._attempts[username]

            # Check if user is currently locked out
            if user_data.get("locked_until") and now < user_data["locked_until"]:
                remaining_time = user_data["locked_until"] - now
                remaining_minutes = int(remaining_time.total_seconds() / 60) + 1
                return f"Too many failed attempts. Try again in {remaining_minutes} minutes."

            # User is not rate limited
            return None

    def record_failed_attempt(self, username: str) -> bool:
        """
        Record a failed password change attempt.

        Args:
            username: Username that failed authentication

        Returns:
            True if user should be locked out, False otherwise
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_record_failure(username)

            now = datetime.now(timezone.utc)

            if username not in self._attempts:
                self._attempts[username] = {
                    "count": 0,
                    "first_attempt": now,
                    "locked_until": None,
                }

            user_data = self._attempts[username]

            # Reset counter if lockout has expired
            if user_data.get("locked_until") and now >= user_data["locked_until"]:
                user_data["count"] = 0
                user_data["locked_until"] = None
                user_data["first_attempt"] = now

            # Increment attempt counter
            user_data["count"] += 1

            # Check if user should be locked out (at exactly 5 attempts for PasswordChangeRateLimiter)
            if user_data["count"] >= self._max_attempts:
                lockout_until = now + timedelta(minutes=self._lockout_duration_minutes)
                user_data["locked_until"] = lockout_until
                return True

            return False

    def record_successful_attempt(self, username: str) -> None:
        """
        Record a successful password change attempt (clears rate limiting).

        Args:
            username: Username that successfully changed password
        """
        with self._lock:
            if self._pool is not None:
                self._pg_record_success(username)
                return
            if username in self._attempts:
                del self._attempts[username]

    def _cleanup_expired_entries(self, now: datetime) -> None:
        """
        Clean up expired rate limiting entries.

        Args:
            now: Current timestamp for comparison
        """
        expired_users = []

        for username, user_data in self._attempts.items():
            # Remove entries that are fully expired (lockout ended > 1 hour ago)
            locked_until = user_data.get("locked_until")
            if locked_until and now > locked_until + timedelta(hours=1):
                expired_users.append(username)

        for username in expired_users:
            del self._attempts[username]

    # ------------------------------------------------------------------
    # PostgreSQL helpers for cluster mode (Bug #573). Called under _lock.
    # ------------------------------------------------------------------

    def _pg_check_locked(self, identifier: str) -> Optional[str]:
        """Check lockout in PostgreSQL. Called under self._lock."""
        assert self._pool is not None
        now = time.time()
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT locked_until FROM rate_limit_lockouts "
                "WHERE limiter_type = %s AND identifier = %s "
                "AND locked_until > %s",
                (self._limiter_type, identifier, now),
            ).fetchone()
        if row is None:
            return None
        remaining = row[0] - now
        remaining_minutes = int(remaining / 60) + 1
        return f"Too many failed attempts. Try again in {remaining_minutes} minutes."

    def _pg_record_failure(self, identifier: str) -> bool:
        """Record failure in PostgreSQL, return True if locked out. Called under self._lock."""
        assert self._pool is not None
        now = time.time()
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO rate_limit_failures "
                "(limiter_type, identifier, failed_at) VALUES (%s, %s, %s)",
                (self._limiter_type, identifier, now),
            )
            # Window is 2x lockout duration to catch distributed retry attacks
            # that span across a lockout boundary
            cutoff = now - (self._lockout_duration_minutes * 60 * 2)
            row = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_failures "
                "WHERE limiter_type = %s AND identifier = %s "
                "AND failed_at > %s",
                (self._limiter_type, identifier, cutoff),
            ).fetchone()
            count = row[0] if row else 0
            if count >= self._max_attempts:
                locked_until = now + (self._lockout_duration_minutes * 60)
                conn.execute(
                    "INSERT INTO rate_limit_lockouts "
                    "(limiter_type, identifier, locked_until) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (limiter_type, identifier) "
                    "DO UPDATE SET locked_until = EXCLUDED.locked_until",
                    (self._limiter_type, identifier, locked_until),
                )
                conn.commit()
                return True
            conn.commit()
            return False

    def _pg_record_success(self, identifier: str) -> None:
        """Clear failures and lockouts in PostgreSQL. Called under self._lock."""
        assert self._pool is not None
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM rate_limit_failures "
                "WHERE limiter_type = %s AND identifier = %s",
                (self._limiter_type, identifier),
            )
            conn.execute(
                "DELETE FROM rate_limit_lockouts "
                "WHERE limiter_type = %s AND identifier = %s",
                (self._limiter_type, identifier),
            )
            conn.commit()

    def get_attempt_count(self, username: str) -> int:
        """
        Get current attempt count for user.

        Args:
            username: Username to check

        Returns:
            Current attempt count
        """
        with self._lock:
            if username not in self._attempts:
                return 0
            count: int = self._attempts[username]["count"]
            return count

    def is_locked_out(self, username: str) -> bool:
        """
        Check if user is currently locked out.

        Args:
            username: Username to check

        Returns:
            True if user is locked out, False otherwise
        """
        with self._lock:
            if username not in self._attempts:
                return False

            user_data = self._attempts[username]
            locked_until = user_data.get("locked_until")

            if not locked_until:
                return False

            now = datetime.now(timezone.utc)
            is_locked: bool = now < locked_until
            return is_locked


class RefreshTokenRateLimiter(PasswordChangeRateLimiter):
    """
    Rate limiter for refresh token attempts to prevent brute force attacks.

    Security requirements:
    - Maximum 10 failed refresh attempts per user
    - 5-minute lockout period after exceeding limit
    - Inherits thread-safety and cleanup from base class
    """

    def __init__(self):
        """Initialize refresh token rate limiter with different limits."""
        super().__init__()
        self._max_attempts = 10
        self._lockout_duration_minutes = 5
        self._limiter_type = "refresh_token"


# Global rate limiter instances
password_change_rate_limiter = PasswordChangeRateLimiter()
refresh_token_rate_limiter = RefreshTokenRateLimiter()
