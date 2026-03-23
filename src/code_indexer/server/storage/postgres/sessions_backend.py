"""
PostgreSQL backend for session management.

Story #411: PostgreSQL Backend for Users and Sessions

Drop-in replacement for SessionsSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the SessionsBackend Protocol (protocols.py).

Tables used:
    invalidated_sessions        — blacklisted JWT token IDs
    password_change_timestamps  — tracks when passwords last changed (for session invalidation)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class SessionsPostgresBackend:
    """
    PostgreSQL backend for session management.

    Satisfies the SessionsBackend Protocol (protocols.py).
    All mutations commit immediately after the DML statement.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # Session invalidation
    # ------------------------------------------------------------------

    def invalidate_session(self, username: str, token_id: str) -> None:
        """Blacklist a specific JWT token ID for a user."""
        now = datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO invalidated_sessions (username, token_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (username, token_id) DO NOTHING
                """,
                (username, token_id, now),
            )
            conn.commit()

    def is_session_invalidated(self, username: str, token_id: str) -> bool:
        """Return True if the token has been blacklisted."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM invalidated_sessions
                WHERE username = %s AND token_id = %s
                """,
                (username, token_id),
            ).fetchone()

        return row is not None

    def clear_invalidated_sessions(self, username: str) -> None:
        """Remove all invalidated session records for a user."""
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM invalidated_sessions WHERE username = %s",
                (username,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Password change timestamps
    # ------------------------------------------------------------------

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None:
        """Record when a user's password was last changed."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO password_change_timestamps (username, changed_at)
                VALUES (%s, %s)
                ON CONFLICT (username) DO UPDATE SET changed_at = EXCLUDED.changed_at
                """,
                (username, changed_at),
            )
            conn.commit()

    def get_password_change_timestamp(self, username: str) -> Optional[str]:
        """Return the ISO timestamp of the last password change, or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT changed_at FROM password_change_timestamps WHERE username = %s",
                (username,),
            ).fetchone()

        return row[0] if row else None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_old_data(self, days_to_keep: int = 30) -> int:
        """
        Remove stale password-change and session-invalidation records.

        Args:
            days_to_keep: Records older than this many days are deleted.

        Returns:
            Number of users whose records were cleaned up.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()

        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT username FROM password_change_timestamps WHERE changed_at < %s",
                (cutoff,),
            ).fetchall()

            usernames = [r[0] for r in rows]
            if not usernames:
                return 0

            for username in usernames:
                conn.execute(
                    "DELETE FROM password_change_timestamps WHERE username = %s",
                    (username,),
                )
                conn.execute(
                    "DELETE FROM invalidated_sessions WHERE username = %s",
                    (username,),
                )

            conn.commit()

        return len(usernames)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()
