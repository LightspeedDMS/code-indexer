"""
SQLite backend for session management (invalidated_sessions and password_change_timestamps).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..database_manager import DatabaseConnectionManager


class SessionsSqliteBackend:
    """SQLite backend for session management (invalidated_sessions and password_change_timestamps)."""

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def invalidate_session(self, username: str, token_id: str) -> None:
        """Invalidate a specific session token."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                "INSERT OR REPLACE INTO invalidated_sessions (username, token_id, created_at) VALUES (?, ?, ?)",
                (username, token_id, now),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def is_session_invalidated(self, username: str, token_id: str) -> bool:
        """Check if a session token has been invalidated."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM invalidated_sessions WHERE username = ? AND token_id = ?",
            (username, token_id),
        )
        return cursor.fetchone() is not None

    def clear_invalidated_sessions(self, username: str) -> None:
        """Clear all invalidated sessions for a user."""

        def operation(conn):
            conn.execute(
                "DELETE FROM invalidated_sessions WHERE username = ?", (username,)
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None:
        """Set password change timestamp for a user."""

        def operation(conn):
            conn.execute(
                "INSERT OR REPLACE INTO password_change_timestamps (username, changed_at) VALUES (?, ?)",
                (username, changed_at),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def get_password_change_timestamp(self, username: str) -> Optional[str]:
        """Get password change timestamp for a user."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT changed_at FROM password_change_timestamps WHERE username = ?",
            (username,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def cleanup_old_data(self, days_to_keep: int = 30) -> int:
        """
        Clean up old session invalidation data.

        Story #702 SQLite migration: Added to support cleanup_old_data in
        PasswordChangeSessionManager SQLite mode.

        Args:
            days_to_keep: Number of days of data to keep

        Returns:
            Number of user records cleaned up
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        cutoff_iso = cutoff_time.isoformat()

        def operation(conn):
            # Get usernames to clean up based on password change timestamp
            cursor = conn.execute(
                "SELECT username FROM password_change_timestamps WHERE changed_at < ?",
                (cutoff_iso,),
            )
            users_to_remove = [row[0] for row in cursor.fetchall()]

            if not users_to_remove:
                return 0

            # Delete password change timestamps
            for username in users_to_remove:
                conn.execute(
                    "DELETE FROM password_change_timestamps WHERE username = ?",
                    (username,),
                )
                # Also delete invalidated sessions for these users
                conn.execute(
                    "DELETE FROM invalidated_sessions WHERE username = ?",
                    (username,),
                )

            return len(users_to_remove)

        count: int = self._conn_manager.execute_atomic(operation)
        return count

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
