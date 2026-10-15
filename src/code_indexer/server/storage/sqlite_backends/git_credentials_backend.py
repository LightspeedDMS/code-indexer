"""
SQLite backend for user git credentials storage.

Story #386: Git Credential Management with Identity Discovery.
Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager
from ..stored_secret_rows import SqliteStoredSecretRowsMixin

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
        "needs_reentry_at": row[11],
    }


class GitCredentialsSqliteBackend(SqliteStoredSecretRowsMixin):
    """SQLite backend for user git credentials storage.

    Story #386: Git Credential Management with Identity Discovery.
    Stores encrypted PATs per user per forge host with discovered identity fields.
    """

    _SECRET_TABLE = "user_git_credentials"
    _SECRET_PK = "credential_id"
    _SECRET_LABELS = ("username", "forge_type", "forge_host", "name")

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
        self._ensure_column()

        def operation(conn):
            conn.execute(
                """INSERT INTO user_git_credentials
                       (credential_id, username, forge_type, forge_host, encrypted_token,
                        git_user_name, git_user_email, forge_username, name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(username, forge_type, forge_host) DO UPDATE SET
                       credential_id = excluded.credential_id,
                       encrypted_token = excluded.encrypted_token,
                       needs_reentry_at = NULL,
                       needs_reentry_node = NULL,
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
        self._ensure_column()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                      git_user_name, git_user_email, forge_username, name, created_at,
                      last_used_at, needs_reentry_at
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

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]:
        """Return credential dict for (username, forge_host) or None if absent."""
        self._ensure_column()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT credential_id, username, forge_type, forge_host, encrypted_token,
                      git_user_name, git_user_email, forge_username, name, created_at,
                      last_used_at, needs_reentry_at
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
