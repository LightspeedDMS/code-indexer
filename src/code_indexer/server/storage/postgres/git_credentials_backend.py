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
        return {
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

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
