"""
SQLite backend for CI token storage. Replaces ci_tokens.json.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager
from ..stored_secret_rows import SqliteStoredSecretRowsMixin

logger = logging.getLogger(__name__)


class CITokensSqliteBackend(SqliteStoredSecretRowsMixin):
    """SQLite backend for CI token storage. Replaces ci_tokens.json."""

    _SECRET_TABLE = "ci_tokens"
    _SECRET_PK = "platform"
    _SECRET_LABELS = ("base_url",)

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def save_token(
        self, platform: str, encrypted_token: str, base_url: Optional[str] = None
    ) -> None:
        """Save or update a CI token."""

        def operation(conn):
            conn.execute(
                "INSERT OR REPLACE INTO ci_tokens (platform, encrypted_token, base_url) VALUES (?, ?, ?)",
                (platform, encrypted_token, base_url),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Saved CI token for platform: {platform}")

    def get_token(self, platform: str) -> Optional[Dict[str, Any]]:
        """Get token for a platform."""
        self._ensure_column()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT platform, encrypted_token, base_url, needs_reentry_at"
            " FROM ci_tokens WHERE platform = ?",
            (platform,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "platform": row[0],
            "encrypted_token": row[1],
            "base_url": row[2],
            "needs_reentry_at": row[3],
        }

    def delete_token(self, platform: str) -> bool:
        """Delete token for a platform."""

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM ci_tokens WHERE platform = ?", (platform,)
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted CI token for platform: {platform}")
        return deleted

    def list_tokens(self) -> Dict[str, Dict[str, Any]]:
        """List all tokens keyed by platform."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT platform, encrypted_token, base_url FROM ci_tokens"
        )
        result = {}
        for row in cursor.fetchall():
            result[row[0]] = {
                "platform": row[0],
                "encrypted_token": row[1],
                "base_url": row[2],
            }
        return result

    def close(self) -> None:
        self._conn_manager.close_thread_connection()
