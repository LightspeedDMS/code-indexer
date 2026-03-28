"""
Concurrency protection for password change operations.

Story #538: Uses PostgreSQL advisory locks for cluster-wide protection
when a connection pool is available, falls back to file-based locks
for SQLite standalone mode.
"""

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

import psycopg

logger = logging.getLogger(__name__)


class PasswordChangeConcurrencyProtection:
    """
    Concurrency protection for password change operations.

    Security requirements:
    - Prevent concurrent password changes for the same user
    - Return 409 Conflict for concurrent attempts
    - Cluster-wide protection via PostgreSQL advisory locks (Story #538)
    - Fallback to file locks for SQLite standalone mode
    """

    def __init__(self, lock_dir: Optional[str] = None):
        if lock_dir:
            self.lock_dir = Path(lock_dir)
        else:
            server_dir = Path.home() / ".cidx-server" / "locks"
            self.lock_dir = server_dir

        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_timeout_seconds = 30
        # Story #538: Connection pool for PostgreSQL advisory locks.
        self._pool: Optional[Any] = None

    def set_connection_pool(self, pool: Any) -> None:
        """Set the PostgreSQL connection pool for advisory lock mode."""
        self._pool = pool
        logger.info(
            "PasswordChangeConcurrencyProtection: using PostgreSQL advisory locks"
        )

    @contextmanager
    def acquire_password_change_lock(
        self, username: str
    ) -> Generator[bool, None, None]:
        """
        Acquire exclusive lock for password change operation.

        Uses PostgreSQL advisory locks in cluster mode, file locks in
        standalone mode.

        Args:
            username: Username to lock for password change

        Yields:
            True if lock acquired successfully

        Raises:
            ConcurrencyConflictError: If lock cannot be acquired
        """
        if self._pool is not None:
            yield from self._acquire_advisory_lock(username)
        else:
            yield from self._acquire_file_lock(username)

    def _acquire_advisory_lock(self, username: str) -> Generator[bool, None, None]:
        """Acquire PostgreSQL advisory lock for cluster-wide protection."""
        assert self._pool is not None
        lock_key = f"password_change_{username}"
        cm = self._pool.connection()
        conn = cm.__enter__()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,))
                acquired = cur.fetchone()[0]

            if not acquired:
                raise ConcurrencyConflictError(
                    f"Password change already in progress for user '{username}'. "
                    "Please try again in a few moments."
                )

            yield True

        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        (lock_key,),
                    )
            except psycopg.Error as unlock_err:
                logger.warning(
                    "Failed to release advisory lock for %s: %s",
                    username,
                    unlock_err,
                )
            cm.__exit__(None, None, None)

    def _acquire_file_lock(self, username: str) -> Generator[bool, None, None]:
        """Acquire file-based lock for standalone SQLite mode."""
        import fcntl

        lock_file_path = self.lock_dir / f"password_change_{username}.lock"
        lock_file = None

        try:
            lock_file = open(lock_file_path, "w")

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file.write(f"pid={os.getpid()}\n")
                lock_file.write(f"timestamp={time.time()}\n")
                lock_file.flush()
                yield True

            except (IOError, OSError):
                raise ConcurrencyConflictError(
                    f"Password change already in progress for user '{username}'. "
                    "Please try again in a few moments."
                )

        finally:
            if lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    lock_file.close()
                except (IOError, OSError):
                    pass
            try:
                if lock_file_path.exists():
                    lock_file_path.unlink()
            except (IOError, OSError):
                pass

    def cleanup_stale_locks(self, max_age_seconds: int = 300) -> int:
        """Clean up stale file-based lock files."""
        current_time = time.time()
        cleaned_count = 0

        try:
            for lock_file_path in self.lock_dir.glob("password_change_*.lock"):
                try:
                    file_mtime = lock_file_path.stat().st_mtime
                    if current_time - file_mtime > max_age_seconds:
                        lock_file_path.unlink()
                        cleaned_count += 1
                except (IOError, OSError):
                    continue
        except (IOError, OSError):
            return 0

        return cleaned_count

    def is_user_locked(self, username: str) -> bool:
        """Check if a user currently has a password change lock."""
        if self._pool is not None:
            return self._is_user_locked_advisory(username)
        return self._is_user_locked_file(username)

    def _is_user_locked_advisory(self, username: str) -> bool:
        """Check advisory lock status in PostgreSQL."""
        assert self._pool is not None
        lock_key = f"password_change_{username}"
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,)
                    )
                    acquired = cur.fetchone()[0]
                    if acquired:
                        cur.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (lock_key,),
                        )
                        return False
                    return True
        except psycopg.Error as e:
            logger.warning(
                "Failed to check advisory lock status for %s: %s", username, e
            )
            return False

    def _is_user_locked_file(self, username: str) -> bool:
        """Check file lock status."""
        import fcntl

        lock_file_path = self.lock_dir / f"password_change_{username}.lock"
        if not lock_file_path.exists():
            return False

        try:
            with open(lock_file_path, "r") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return False
        except (IOError, OSError):
            return True


class ConcurrencyConflictError(Exception):
    """Exception raised when a concurrency conflict occurs during password change."""

    pass


# Global concurrency protection instance
password_change_concurrency_protection = PasswordChangeConcurrencyProtection()
