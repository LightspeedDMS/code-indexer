"""
Rate limiters for OAuth endpoints to prevent abuse.

Following CLAUDE.md principles: NO MOCKS - Real rate limiting implementation.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class OAuthTokenRateLimiter:
    """
    Rate limiter for /oauth/token endpoint.

    Security requirements:
    - Maximum 10 failed attempts per client
    - 5-minute lockout period after exceeding limit
    - Thread-safe implementation
    """

    def __init__(self):
        self._attempts: Dict[str, Dict] = {}
        self._lock = Lock()
        self._max_attempts = 10
        self._lockout_duration_minutes = 5
        self._pool: Optional[Any] = None
        self._limiter_type = "oauth_token"

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL for cluster mode (Bug #574)."""
        self._pool = pool
        logger.info("%s: using PostgreSQL (cluster mode)", self.__class__.__name__)

    def check_rate_limit(self, client_id: str) -> Optional[str]:
        """
        Check if client is rate limited.

        Args:
            client_id: Client ID to check

        Returns:
            None if not rate limited, error message if rate limited
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_check_locked(client_id)

            now = datetime.now(timezone.utc)
            self._cleanup_expired_entries(now)

            if client_id not in self._attempts:
                return None

            client_data = self._attempts[client_id]

            if client_data.get("locked_until") and now < client_data["locked_until"]:
                remaining_time = client_data["locked_until"] - now
                remaining_minutes = int(remaining_time.total_seconds() / 60) + 1
                return f"Too many failed attempts. Try again in {remaining_minutes} minutes."

            return None

    def record_failed_attempt(self, client_id: str) -> bool:
        """
        Record a failed token request attempt.

        Args:
            client_id: Client ID that failed

        Returns:
            True if client should be locked out, False otherwise
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_record_failure(client_id)

            now = datetime.now(timezone.utc)

            if client_id not in self._attempts:
                self._attempts[client_id] = {
                    "count": 0,
                    "first_attempt": now,
                    "locked_until": None,
                }

            client_data = self._attempts[client_id]

            if client_data.get("locked_until") and now >= client_data["locked_until"]:
                client_data["count"] = 0
                client_data["locked_until"] = None
                client_data["first_attempt"] = now

            client_data["count"] += 1

            if client_data["count"] >= self._max_attempts:
                lockout_until = now + timedelta(minutes=self._lockout_duration_minutes)
                client_data["locked_until"] = lockout_until
                return True

            return False

    def record_successful_attempt(self, client_id: str) -> None:
        """
        Record a successful token request (clears rate limiting).

        Args:
            client_id: Client ID that succeeded
        """
        with self._lock:
            if self._pool is not None:
                self._pg_record_success(client_id)
                return
            if client_id in self._attempts:
                del self._attempts[client_id]

    def _cleanup_expired_entries(self, now: datetime) -> None:
        """Clean up expired rate limiting entries."""
        expired_clients = []

        for client_id, client_data in self._attempts.items():
            locked_until = client_data.get("locked_until")
            if locked_until and now > locked_until + timedelta(hours=1):
                expired_clients.append(client_id)

        for client_id in expired_clients:
            del self._attempts[client_id]

    # ------------------------------------------------------------------
    # PostgreSQL helpers for cluster mode (Bug #574). Called under _lock.
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


class OAuthRegisterRateLimiter:
    """
    Rate limiter for /oauth/register endpoint.

    Security requirements:
    - Maximum 5 failed attempts per IP
    - 15-minute lockout period after exceeding limit
    - Thread-safe implementation
    """

    def __init__(self):
        self._attempts: Dict[str, Dict] = {}
        self._lock = Lock()
        self._max_attempts = 5
        self._lockout_duration_minutes = 15
        self._pool: Optional[Any] = None
        self._limiter_type = "oauth_register"

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL for cluster mode (Bug #574)."""
        self._pool = pool
        logger.info("%s: using PostgreSQL (cluster mode)", self.__class__.__name__)

    def check_rate_limit(self, ip_address: str) -> Optional[str]:
        """
        Check if IP is rate limited.

        Args:
            ip_address: IP address to check

        Returns:
            None if not rate limited, error message if rate limited
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_check_locked(ip_address)

            now = datetime.now(timezone.utc)
            self._cleanup_expired_entries(now)

            if ip_address not in self._attempts:
                return None

            ip_data = self._attempts[ip_address]

            if ip_data.get("locked_until") and now < ip_data["locked_until"]:
                remaining_time = ip_data["locked_until"] - now
                remaining_minutes = int(remaining_time.total_seconds() / 60) + 1
                return f"Too many failed attempts. Try again in {remaining_minutes} minutes."

            return None

    def record_failed_attempt(self, ip_address: str) -> bool:
        """
        Record a failed registration attempt.

        Args:
            ip_address: IP address that failed

        Returns:
            True if IP should be locked out, False otherwise
        """
        with self._lock:
            if self._pool is not None:
                return self._pg_record_failure(ip_address)

            now = datetime.now(timezone.utc)

            if ip_address not in self._attempts:
                self._attempts[ip_address] = {
                    "count": 0,
                    "first_attempt": now,
                    "locked_until": None,
                }

            ip_data = self._attempts[ip_address]

            if ip_data.get("locked_until") and now >= ip_data["locked_until"]:
                ip_data["count"] = 0
                ip_data["locked_until"] = None
                ip_data["first_attempt"] = now

            ip_data["count"] += 1

            if ip_data["count"] >= self._max_attempts:
                lockout_until = now + timedelta(minutes=self._lockout_duration_minutes)
                ip_data["locked_until"] = lockout_until
                return True

            return False

    def record_successful_attempt(self, ip_address: str) -> None:
        """
        Record a successful registration (clears rate limiting).

        Args:
            ip_address: IP address that succeeded
        """
        with self._lock:
            if self._pool is not None:
                self._pg_record_success(ip_address)
                return
            if ip_address in self._attempts:
                del self._attempts[ip_address]

    def _cleanup_expired_entries(self, now: datetime) -> None:
        """Clean up expired rate limiting entries."""
        expired_ips = []

        for ip_address, ip_data in self._attempts.items():
            locked_until = ip_data.get("locked_until")
            if locked_until and now > locked_until + timedelta(hours=1):
                expired_ips.append(ip_address)

        for ip_address in expired_ips:
            del self._attempts[ip_address]

    # ------------------------------------------------------------------
    # PostgreSQL helpers for cluster mode (Bug #574). Called under _lock.
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


# Global rate limiter instances
oauth_token_rate_limiter = OAuthTokenRateLimiter()
oauth_register_rate_limiter = OAuthRegisterRateLimiter()
