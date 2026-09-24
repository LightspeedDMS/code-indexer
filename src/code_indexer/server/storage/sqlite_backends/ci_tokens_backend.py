"""
SQLite backend for CI token storage. Replaces ci_tokens.json.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class CITokensSqliteBackend:
    """SQLite backend for CI token storage. Replaces ci_tokens.json."""

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
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT platform, encrypted_token, base_url FROM ci_tokens WHERE platform = ?",
            (platform,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {"platform": row[0], "encrypted_token": row[1], "base_url": row[2]}

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

    def update_encrypted_token(self, platform: str, new_encrypted_token: str) -> None:
        """Update the encrypted_token for a platform in-place (lazy re-encryption).

        Used by CITokenManager when a fallback key decryption succeeds so the token
        is re-encrypted with the canonical key for future reads (Story #999).

        When no matching row is found for platform, logs a WARNING and returns without
        raising (caller can continue safely; the token was likely already deleted).

        Args:
            platform: Platform key (e.g. "github", "gitlab"). Must be non-empty.
            new_encrypted_token: New base64-encoded ciphertext. Must be non-empty.

        Raises:
            ValueError: If platform or new_encrypted_token are None or empty.
        """
        if not platform:
            raise ValueError("platform must be a non-empty string")
        if not new_encrypted_token:
            raise ValueError("new_encrypted_token must be a non-empty string")

        def operation(conn):
            cursor = conn.execute(
                "UPDATE ci_tokens SET encrypted_token = ? WHERE platform = ?",
                (new_encrypted_token, platform),
            )
            return cursor.rowcount

        rows_updated: int = self._conn_manager.execute_atomic(operation)
        if rows_updated == 1:
            logger.debug(
                "Re-encrypted CI token for platform %s with canonical key", platform
            )
        elif rows_updated == 0:
            logger.warning(
                "update_encrypted_token: no ci_tokens row found for platform %r — "
                "re-encryption skipped",
                platform,
            )
        else:
            logger.warning(
                "update_encrypted_token: unexpected rowcount %d for platform %r",
                rows_updated,
                platform,
            )

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
