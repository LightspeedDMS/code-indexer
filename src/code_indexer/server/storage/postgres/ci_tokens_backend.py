"""
PostgreSQL backend for CI token storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for CITokensSqliteBackend satisfying the CITokensBackend protocol.
Uses psycopg v3 sync mode with a connection pool.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


class CITokensPostgresBackend:
    """
    PostgreSQL backend for CI token storage.

    Satisfies the CITokensBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def save_token(
        self, platform: str, encrypted_token: str, base_url: Optional[str] = None
    ) -> None:
        """Save or update a CI token."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO ci_tokens (platform, encrypted_token, base_url)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (platform) DO UPDATE SET
                       encrypted_token = EXCLUDED.encrypted_token,
                       base_url = EXCLUDED.base_url""",
                (platform, encrypted_token, base_url),
            )
        logger.info(f"Saved CI token for platform: {platform}")

    def get_token(self, platform: str) -> Optional[Dict[str, Any]]:
        """Get token for a platform."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT platform, encrypted_token, base_url FROM ci_tokens WHERE platform = %s",
                (platform,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {"platform": row[0], "encrypted_token": row[1], "base_url": row[2]}

    def delete_token(self, platform: str) -> bool:
        """Delete token for a platform. Returns True if a row was deleted."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM ci_tokens WHERE platform = %s",
                (platform,),
            )
            deleted: bool = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted CI token for platform: {platform}")
        return deleted

    def list_tokens(self) -> Dict[str, Dict[str, Any]]:
        """List all tokens keyed by platform."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT platform, encrypted_token, base_url FROM ci_tokens"
            )
            rows = cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row[0]] = {
                "platform": row[0],
                "encrypted_token": row[1],
                "base_url": row[2],
            }
        return result

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
