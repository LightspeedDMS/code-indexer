"""
SQLite backend for user git credentials storage.

Story #386: Git Credential Management with Identity Discovery.
Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


def _row_to_credential_dict(row: Any) -> Dict[str, Any]:
    """Map an 11-column user_git_credentials row to its dict shape (shared
    by list_credentials and get_credential_for_host to avoid duplicating
    this field mapping)."""
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


class GitCredentialsSqliteBackend:
    """SQLite backend for user git credentials storage.

    Story #386: Git Credential Management with Identity Discovery.
    Stores encrypted PATs per user per forge host with discovered identity fields.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

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

        def operation(conn):
            conn.execute(
                """INSERT INTO user_git_credentials
                       (credential_id, username, forge_type, forge_host, encrypted_token,
                        git_user_name, git_user_email, forge_username, name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(username, forge_type, forge_host) DO UPDATE SET
                       credential_id = excluded.credential_id,
                       encrypted_token = excluded.encrypted_token,
                       git_user_name = excluded.git_user_name,
                       git_user_email = excluded.git_user_email,
                       forge_username = excluded.forge_username,
                       name = excluded.name""",
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
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Upserted git credential for user={username} host={forge_host}")

    def list_credentials(self, username: str) -> List[Dict[str, Any]]:
        """Return all credentials belonging to the given username."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                      git_user_name, git_user_email, forge_username, name, created_at,
                      last_used_at
               FROM user_git_credentials
               WHERE username = ?
               ORDER BY created_at DESC""",
            (username,),
        )
        return [_row_to_credential_dict(row) for row in cursor.fetchall()]

    def delete_credential(self, username: str, credential_id: str) -> bool:
        """Delete a credential by id AND username (ownership enforced). Returns True if deleted."""

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM user_git_credentials WHERE credential_id = ? AND username = ?",
                (credential_id, username),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(f"Deleted git credential {credential_id} for user={username}")
        return deleted

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

        def operation(conn):
            cursor = conn.execute(
                "UPDATE user_git_credentials SET encrypted_token = ? WHERE credential_id = ?",
                (new_encrypted_token, credential_id),
            )
            return cursor.rowcount

        rows_updated: int = self._conn_manager.execute_atomic(operation)
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

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]:
        """Return credential dict for (username, forge_host) or None if absent."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                      git_user_name, git_user_email, forge_username, name, created_at,
                      last_used_at
               FROM user_git_credentials
               WHERE username = ? AND forge_host = ?
               LIMIT 1""",
            (username, forge_host),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_credential_dict(row)

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
