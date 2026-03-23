"""
PostgreSQL backend for user management.

Story #411: PostgreSQL Backend for Users and Sessions

Drop-in replacement for UsersSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the UsersBackend Protocol (protocols.py).

Tables used:
    users                   — primary user records
    user_api_keys           — per-user API keys (one-to-many)
    user_mcp_credentials    — per-user MCP credentials (one-to-many)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .pg_utils import sanitize_row
from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class UsersPostgresBackend:
    """
    PostgreSQL backend for user management.

    Satisfies the UsersBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        email: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Create a new user record."""
        now = created_at if created_at else datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, email, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, password_hash, role, email, now),
            )
            conn.commit()

        logger.info("Created user: %s", username)

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user with all related data (api_keys, mcp_credentials)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT username, password_hash, role, email, created_at, oidc_identity
                FROM users
                WHERE username = %s
                """,
                (username,),
            ).fetchone()

            if row is None:
                return None

            api_keys = self._get_api_keys(conn, username)
            mcp_credentials = self._get_mcp_credentials(conn, username)

        return sanitize_row(
            {
                "username": row[0],
                "password_hash": row[1],
                "role": row[2],
                "email": row[3],
                "created_at": row[4],
                "oidc_identity": self._parse_json(row[5]),
                "api_keys": api_keys,
                "mcp_credentials": mcp_credentials,
            }
        )

    def list_users(self) -> list:
        """List all users with their related data."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT username, password_hash, role, email, created_at, oidc_identity
                FROM users
                """
            ).fetchall()

            result = []
            for row in rows:
                uname = row[0]
                result.append(
                    {
                        "username": uname,
                        "password_hash": row[1],
                        "role": row[2],
                        "email": row[3],
                        "created_at": row[4],
                        "oidc_identity": self._parse_json(row[5]),
                        "api_keys": self._get_api_keys(conn, uname),
                        "mcp_credentials": self._get_mcp_credentials(conn, uname),
                    }
                )

        return result

    def update_user(
        self,
        username: str,
        new_username: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bool:
        """
        Update user's username or email.

        Returns:
            True if successful, False if user not found.
        """
        if self.get_user(username) is None:
            return False

        with self._pool.connection() as conn:
            if new_username and new_username != username:
                conn.execute(
                    "UPDATE users SET username = %s, email = COALESCE(%s, email) WHERE username = %s",
                    (new_username, email, username),
                )
                conn.execute(
                    "UPDATE user_api_keys SET username = %s WHERE username = %s",
                    (new_username, username),
                )
                conn.execute(
                    "UPDATE user_mcp_credentials SET username = %s WHERE username = %s",
                    (new_username, username),
                )
            elif email is not None:
                conn.execute(
                    "UPDATE users SET email = %s WHERE username = %s",
                    (email, username),
                )
            conn.commit()

        logger.info("Updated user: %s", username)
        return True

    def delete_user(self, username: str) -> bool:
        """Delete user and all related records (cascade via FK or explicit)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM users WHERE username = %s",
                (username,),
            )
            deleted = bool(cur.rowcount > 0)
            conn.commit()

        if deleted:
            logger.info("Deleted user: %s", username)
        return deleted

    def update_user_role(self, username: str, role: str) -> bool:
        """Update user's role."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE users SET role = %s WHERE username = %s",
                (role, username),
            )
            updated = bool(cur.rowcount > 0)
            conn.commit()

        if updated:
            logger.info("Updated role for user: %s", username)
        return updated

    def update_password_hash(self, username: str, password_hash: str) -> bool:
        """Update user's password hash."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
                (password_hash, username),
            )
            updated = bool(cur.rowcount > 0)
            conn.commit()

        if updated:
            logger.info("Updated password for user: %s", username)
        return updated

    # ------------------------------------------------------------------
    # API key management
    # ------------------------------------------------------------------

    def add_api_key(
        self,
        username: str,
        key_id: str,
        key_hash: str,
        key_prefix: str,
        name: Optional[str] = None,
    ) -> None:
        """Add an API key for a user."""
        now = datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO user_api_keys
                    (key_id, username, key_hash, key_prefix, name, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (key_id, username, key_hash, key_prefix, name, now),
            )
            conn.commit()

    def delete_api_key(self, username: str, key_id: str) -> bool:
        """Delete an API key for a user."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_api_keys WHERE username = %s AND key_id = %s",
                (username, key_id),
            )
            deleted = bool(cur.rowcount > 0)
            conn.commit()

        return deleted

    # ------------------------------------------------------------------
    # MCP credential management
    # ------------------------------------------------------------------

    def add_mcp_credential(
        self,
        username: str,
        credential_id: str,
        client_id: str,
        client_secret_hash: str,
        client_id_prefix: str,
        name: Optional[str] = None,
    ) -> None:
        """Add MCP credential for a user."""
        now = datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO user_mcp_credentials
                    (credential_id, username, client_id, client_secret_hash,
                     client_id_prefix, name, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    credential_id,
                    username,
                    client_id,
                    client_secret_hash,
                    client_id_prefix,
                    name,
                    now,
                ),
            )
            conn.commit()

    def delete_mcp_credential(self, username: str, credential_id: str) -> bool:
        """Delete an MCP credential for a user."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_mcp_credentials WHERE username = %s AND credential_id = %s",
                (username, credential_id),
            )
            deleted = bool(cur.rowcount > 0)
            conn.commit()

        return deleted

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool:
        """Update last_used_at timestamp for an MCP credential."""
        now = datetime.now(timezone.utc).isoformat()

        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE user_mcp_credentials
                SET last_used_at = %s
                WHERE username = %s AND credential_id = %s
                """,
                (now, username, credential_id),
            )
            updated = bool(cur.rowcount > 0)
            conn.commit()

        return updated

    def list_all_mcp_credentials(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List MCP credentials across all users with pagination."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT username, credential_id, client_id, client_id_prefix,
                       name, created_at, last_used_at
                FROM user_mcp_credentials
                ORDER BY username, credential_id
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            ).fetchall()

        return [
            {
                "username": r[0],
                "credential_id": r[1],
                "client_id": r[2],
                "client_id_prefix": r[3],
                "name": r[4],
                "created_at": r[5],
                "last_used_at": r[6],
            }
            for r in rows
        ]

    def get_system_mcp_credentials(self) -> List[Dict[str, Any]]:
        """Return MCP credentials owned by the 'admin' user (system-managed)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT credential_id, client_id, client_id_prefix,
                       name, created_at, last_used_at
                FROM user_mcp_credentials
                WHERE username = 'admin'
                ORDER BY created_at DESC
                """
            ).fetchall()

        return [
            {
                "credential_id": r[0],
                "client_id": r[1],
                "client_id_prefix": r[2],
                "name": r[3],
                "created_at": r[4],
                "last_used_at": r[5],
                "owner": "admin (system)",
                "is_system": True,
            }
            for r in rows
        ]

    def get_mcp_credential_by_client_id(
        self, client_id: str
    ) -> Optional[Tuple[str, dict]]:
        """
        Find MCP credential by client_id (O(1) indexed lookup).

        Returns:
            Tuple of (username, credential_dict) if found, None otherwise.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT username, credential_id, client_id, client_secret_hash,
                       client_id_prefix, name, created_at, last_used_at
                FROM user_mcp_credentials
                WHERE client_id = %s
                """,
                (client_id,),
            ).fetchone()

        if row is None:
            return None

        username = row[0]
        credential = {
            "credential_id": row[1],
            "client_id": row[2],
            "client_secret_hash": row[3],
            "client_id_prefix": row[4],
            "name": row[5],
            "created_at": row[6],
            "last_used_at": row[7],
        }
        return (username, credential)

    # ------------------------------------------------------------------
    # Email and OIDC
    # ------------------------------------------------------------------

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email address (case-insensitive)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT username, password_hash, role, email, created_at, oidc_identity
                FROM users
                WHERE LOWER(email) = LOWER(%s)
                """,
                (email.strip(),),
            ).fetchone()

            if row is None:
                return None

            username = row[0]
            api_keys = self._get_api_keys(conn, username)
            mcp_credentials = self._get_mcp_credentials(conn, username)

        return sanitize_row(
            {
                "username": username,
                "password_hash": row[1],
                "role": row[2],
                "email": row[3],
                "created_at": row[4],
                "oidc_identity": self._parse_json(row[5]),
                "api_keys": api_keys,
                "mcp_credentials": mcp_credentials,
            }
        )

    def set_oidc_identity(self, username: str, identity: Dict[str, Any]) -> bool:
        """Set OIDC identity for a user."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE users SET oidc_identity = %s WHERE username = %s",
                (json.dumps(identity), username),
            )
            updated = bool(cur.rowcount > 0)
            conn.commit()

        return updated

    def remove_oidc_identity(self, username: str) -> bool:
        """Remove OIDC identity from a user (set to NULL)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE users SET oidc_identity = NULL WHERE username = %s",
                (username,),
            )
            updated = bool(cur.rowcount > 0)
            conn.commit()

        return updated

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(value: Any) -> Any:
        """Parse a JSON string or return dict/None as-is (psycopg JSONB returns dict)."""
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return value

    def _get_api_keys(self, conn: Any, username: str) -> list:
        """Fetch api_keys for a user using an existing connection."""
        rows = conn.execute(
            """
            SELECT key_id, key_hash, key_prefix, name, created_at
            FROM user_api_keys
            WHERE username = %s
            """,
            (username,),
        ).fetchall()
        return [
            {
                "key_id": r[0],
                "key_hash": r[1],
                "key_prefix": r[2],
                "name": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def _get_mcp_credentials(self, conn: Any, username: str) -> list:
        """Fetch mcp_credentials for a user using an existing connection."""
        rows = conn.execute(
            """
            SELECT credential_id, client_id, client_secret_hash, client_id_prefix,
                   name, created_at, last_used_at
            FROM user_mcp_credentials
            WHERE username = %s
            """,
            (username,),
        ).fetchall()
        return [
            {
                "credential_id": r[0],
                "client_id": r[1],
                "client_secret_hash": r[2],
                "client_id_prefix": r[3],
                "name": r[4],
                "created_at": r[5],
                "last_used_at": r[6],
            }
            for r in rows
        ]
