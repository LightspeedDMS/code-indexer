"""
PostgreSQL backend for refresh token storage (Story #515).

Drop-in replacement for RefreshTokenSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the RefreshTokenBackend Protocol (protocols.py).

Tables are created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for new deployments.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class RefreshTokenPostgresBackend:
    """
    PostgreSQL backend for refresh token storage.

    Satisfies the RefreshTokenBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure the schema exists.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create token_families and refresh_tokens tables and indexes if they do not already exist."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_families (
                    family_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    is_revoked BOOLEAN DEFAULT FALSE,
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
                    is_used BOOLEAN DEFAULT FALSE,
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
            conn.commit()

    def create_token_family(
        self, family_id: str, username: str, created_at: str, last_used_at: str
    ) -> None:
        """Insert a new token family record."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO token_families (family_id, username, created_at, last_used_at)
                VALUES (%s, %s, %s, %s)
                """,
                (family_id, username, created_at, last_used_at),
            )
            conn.commit()

    def get_token_family(self, family_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a token family by its ID, or None if not found."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT family_id, username, created_at, last_used_at,
                       is_revoked, revocation_reason
                FROM token_families WHERE family_id = %s
                """,
                (family_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "family_id": row[0],
            "username": row[1],
            "created_at": row[2],
            "last_used_at": row[3],
            "is_revoked": row[4],
            "revocation_reason": row[5],
        }

    def revoke_token_family(self, family_id: str, reason: str) -> None:
        """Mark a token family as revoked with the given reason."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE token_families
                SET is_revoked = TRUE, revocation_reason = %s
                WHERE family_id = %s
                """,
                (reason, family_id),
            )
            conn.commit()

    def revoke_user_families(self, username: str, reason: str) -> int:
        """Revoke all token families for a user. Returns count of revoked families."""
        with self._pool.connection() as conn:
            result = conn.execute(
                """
                UPDATE token_families
                SET is_revoked = TRUE, revocation_reason = %s
                WHERE username = %s AND is_revoked = FALSE
                """,
                (reason, username),
            )
            count = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return count

    def update_family_last_used(self, family_id: str, last_used_at: str) -> None:
        """Update the last_used_at timestamp for a token family."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE token_families SET last_used_at = %s WHERE family_id = %s",
                (last_used_at, family_id),
            )
            conn.commit()

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
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO refresh_tokens
                    (token_id, family_id, username, token_hash, created_at,
                     expires_at, parent_token_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
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
            conn.commit()

    def get_refresh_token_by_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve a refresh token by its hash, or None if not found."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT token_id, family_id, username, token_hash, created_at,
                       expires_at, is_used, used_at, parent_token_id
                FROM refresh_tokens WHERE token_hash = %s
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        return {
            "token_id": row[0],
            "family_id": row[1],
            "username": row[2],
            "token_hash": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "is_used": row[6],
            "used_at": row[7],
            "parent_token_id": row[8],
        }

    def mark_token_used(self, token_id: str, used_at: str) -> None:
        """Mark a refresh token as used with the given timestamp."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET is_used = TRUE, used_at = %s WHERE token_id = %s",
                (used_at, token_id),
            )
            conn.commit()

    def count_active_tokens_in_family(self, family_id: str) -> int:
        """Return count of unused (active) tokens in a family."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM refresh_tokens WHERE family_id = %s AND is_used = FALSE",
                (family_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def delete_expired_tokens(self, now_iso: str) -> int:
        """Delete all tokens expired before now_iso. Returns count deleted."""
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM refresh_tokens WHERE expires_at < %s",
                (now_iso,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return deleted

    def delete_orphaned_families(self) -> int:
        """Delete token families that have no associated tokens. Returns count deleted."""
        with self._pool.connection() as conn:
            result = conn.execute(
                """
                DELETE FROM token_families
                WHERE family_id NOT IN (SELECT DISTINCT family_id FROM refresh_tokens)
                """
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return deleted

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
