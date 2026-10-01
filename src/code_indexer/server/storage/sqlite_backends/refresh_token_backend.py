"""
SQLite backend for refresh token storage (Story #515).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager


class RefreshTokenSqliteBackend:
    """
    SQLite backend for refresh token storage (Story #515).

    Manages token_families and refresh_tokens tables for JWT refresh token
    rotation with family-based revocation and reuse detection.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create token_families and refresh_tokens tables if they do not already exist."""

        def _do_init(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_families (
                    family_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    is_revoked INTEGER DEFAULT 0,
                    revocation_reason TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_family_username ON token_families (username)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_family_revoked ON token_families (is_revoked)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_used INTEGER DEFAULT 0,
                    used_at TEXT,
                    parent_token_id TEXT,
                    FOREIGN KEY (family_id) REFERENCES token_families (family_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_family ON refresh_tokens (family_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_username ON refresh_tokens (username)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_hash ON refresh_tokens (token_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_expires ON refresh_tokens (expires_at)"
            )

        self._conn_manager.execute_atomic(_do_init)

    def create_token_family(
        self, family_id: str, username: str, created_at: str, last_used_at: str
    ) -> None:
        """Insert a new token family record."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO token_families (family_id, username, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
                """,
                (family_id, username, created_at, last_used_at),
            )

        self._conn_manager.execute_atomic(_op)

    def get_token_family(self, family_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a token family by its ID, or None if not found."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute("SELECT * FROM token_families WHERE family_id = ?", (family_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def revoke_token_family(self, family_id: str, reason: str) -> None:
        """Mark a token family as revoked with the given reason."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE token_families
                SET is_revoked = 1, revocation_reason = ?
                WHERE family_id = ?
                """,
                (reason, family_id),
            )

        self._conn_manager.execute_atomic(_op)

    def revoke_user_families(self, username: str, reason: str) -> int:
        """Revoke all token families for a user. Returns count of revoked families."""
        result: Dict[str, Any] = {}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE token_families
                SET is_revoked = 1, revocation_reason = ?
                WHERE username = ? AND is_revoked = 0
                """,
                (reason, username),
            )
            result["count"] = cursor.rowcount

        self._conn_manager.execute_atomic(_op)
        return result.get("count", 0)  # type: ignore[no-any-return]

    def update_family_last_used(self, family_id: str, last_used_at: str) -> None:
        """Update the last_used_at timestamp for a token family."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE token_families SET last_used_at = ? WHERE family_id = ?",
                (last_used_at, family_id),
            )

        self._conn_manager.execute_atomic(_op)

    def store_refresh_token(
        self,
        token_id: str,
        family_id: str,
        username: str,
        token_hash: str,
        created_at: str,
        expires_at: str,
        parent_token_id: Optional[str] = None,
    ) -> None:
        """Insert a new refresh token record."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO refresh_tokens
                    (token_id, family_id, username, token_hash, created_at,
                     expires_at, parent_token_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    family_id,
                    username,
                    token_hash,
                    created_at,
                    expires_at,
                    parent_token_id,
                ),
            )

        self._conn_manager.execute_atomic(_op)

    def get_refresh_token_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve a refresh token by its hash, or None if not found."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def mark_token_used(self, token_id: str, used_at: str) -> None:
        """Mark a refresh token as used with the given timestamp."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE refresh_tokens SET is_used = 1, used_at = ? WHERE token_id = ?",
                (used_at, token_id),
            )

        self._conn_manager.execute_atomic(_op)

    def count_active_tokens_in_family(self, family_id: str) -> int:
        """Return count of unused (active) tokens in a family."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM refresh_tokens WHERE family_id = ? AND is_used = 0",
            (family_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else 0

    def delete_expired_tokens(self, now_iso: str) -> int:
        """Delete all tokens expired before now_iso. Returns count deleted."""
        result: Dict[str, Any] = {}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "DELETE FROM refresh_tokens WHERE expires_at < ?", (now_iso,)
            )
            result["count"] = cursor.rowcount

        self._conn_manager.execute_atomic(_op)
        return result.get("count", 0)  # type: ignore[no-any-return]

    def delete_orphaned_families(self) -> int:
        """Delete token families that have no associated tokens. Returns count deleted."""
        result: Dict[str, Any] = {}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                DELETE FROM token_families
                WHERE family_id NOT IN (SELECT DISTINCT family_id FROM refresh_tokens)
                """
            )
            result["count"] = cursor.rowcount

        self._conn_manager.execute_atomic(_op)
        return result.get("count", 0)  # type: ignore[no-any-return]

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
