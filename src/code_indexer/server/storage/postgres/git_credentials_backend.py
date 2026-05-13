"""
PostgreSQL backend for user git credentials storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for GitCredentialsSqliteBackend satisfying the
GitCredentialsBackend protocol.
Uses psycopg v3 sync mode with a connection pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .pg_utils import sanitize_row

logger = logging.getLogger(__name__)


class GitCredentialsPostgresBackend:
    """
    PostgreSQL backend for user git credentials storage.

    Satisfies the GitCredentialsBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def upsert_credential(
        self,
        credential_id: str,
        username: str,
        forge_type: str,
        forge_host: str,
        encrypted_token: str,
        git_user_name: Optional[str] = None,
        git_user_email: Optional[str] = None,
        forge_username: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        """Insert or update a credential by (username, forge_type, forge_host) uniqueness."""
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO user_git_credentials
                       (credential_id, username, forge_type, forge_host, encrypted_token,
                        git_user_name, git_user_email, forge_username, name, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (username, forge_type, forge_host) DO UPDATE SET
                       credential_id = EXCLUDED.credential_id,
                       encrypted_token = EXCLUDED.encrypted_token,
                       git_user_name = EXCLUDED.git_user_name,
                       git_user_email = EXCLUDED.git_user_email,
                       forge_username = EXCLUDED.forge_username,
                       name = EXCLUDED.name""",
                (
                    credential_id,
                    username,
                    forge_type,
                    forge_host,
                    encrypted_token,
                    git_user_name,
                    git_user_email,
                    forge_username,
                    name,
                    now,
                ),
            )
        logger.debug(f"Upserted git credential for user={username} host={forge_host}")

    def list_credentials(self, username: str) -> List[Dict[str, Any]]:
        """Return all credentials belonging to the given username."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                          git_user_name, git_user_email, forge_username, name, created_at,
                          last_used_at
                   FROM user_git_credentials
                   WHERE username = %s
                   ORDER BY created_at DESC""",
                (username,),
            )
            rows = cursor.fetchall()
        return [
            sanitize_row(
                {
                    "credential_id": row[0],
                    "username": row[1],
                    "forge_type": row[2],
                    "forge_host": row[3],
                    "encrypted_token": row[4],
                    "git_user_name": row[5],
                    "git_user_email": row[6],
                    "forge_username": row[7],
                    "name": row[8],
                    "created_at": row[9],
                    "last_used_at": row[10],
                }
            )
            for row in rows
        ]

    def delete_credential(self, username: str, credential_id: str) -> bool:
        """Delete a credential by id AND username (ownership enforced). Returns True if deleted."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM user_git_credentials WHERE credential_id = %s AND username = %s",
                (credential_id, username),
            )
            deleted: bool = cursor.rowcount > 0
        if deleted:
            logger.debug(f"Deleted git credential {credential_id} for user={username}")
        return deleted

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]:
        """Return credential dict for (username, forge_host) or None if absent."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                          git_user_name, git_user_email, forge_username, name, created_at,
                          last_used_at
                   FROM user_git_credentials
                   WHERE username = %s AND forge_host = %s
                   LIMIT 1""",
                (username, forge_host),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return sanitize_row(
            {
                "credential_id": row[0],
                "username": row[1],
                "forge_type": row[2],
                "forge_host": row[3],
                "encrypted_token": row[4],
                "git_user_name": row[5],
                "git_user_email": row[6],
                "forge_username": row[7],
                "name": row[8],
                "created_at": row[9],
                "last_used_at": row[10],
            }
        )

    def update_encrypted_token(
        self, credential_id: str, new_encrypted_token: str
    ) -> None:
        """Update the encrypted_token for a credential in-place (lazy re-encryption).

        Used by GitCredentialManager when a fallback key decryption succeeds so the
        token is re-encrypted with the canonical key for all future reads (Story #999).

        When no matching row is found, logs a WARNING and returns without raising
        (the credential may have been deleted concurrently; caller can continue safely).

        Args:
            credential_id: Primary key of the credential row. Must be non-empty.
            new_encrypted_token: New base64-encoded ciphertext. Must be non-empty.

        Raises:
            ValueError: If credential_id or new_encrypted_token are None or empty.
        """
        if not credential_id:
            raise ValueError("credential_id must be a non-empty string")
        if not new_encrypted_token:
            raise ValueError("new_encrypted_token must be a non-empty string")

        with self._pool.connection() as conn:
            cursor = conn.execute(
                "UPDATE user_git_credentials SET encrypted_token = %s WHERE credential_id = %s",
                (new_encrypted_token, credential_id),
            )
            rows_updated: int = cursor.rowcount
            conn.commit()

        if rows_updated == 1:
            logger.debug(
                "Re-encrypted git credential %s with canonical key", credential_id
            )
        elif rows_updated == 0:
            logger.warning(
                "update_encrypted_token: no user_git_credentials row found for "
                "credential_id %r — re-encryption skipped",
                credential_id,
            )
        else:
            logger.warning(
                "update_encrypted_token: unexpected rowcount %d for credential_id %r",
                rows_updated,
                credential_id,
            )

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
