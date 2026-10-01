"""
SQLite backend for user management with normalized tables.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class UsersSqliteBackend:
    """
    SQLite backend for user management with normalized tables.

    Replaces users.json with atomic SQLite operations. User data is normalized
    across 4 tables: users, user_api_keys, user_mcp_credentials, user_oidc_identities.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        email: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Create a new user.

        Raises:
            sqlite3.IntegrityError: When a user with the same username already exists.
                Callers must pre-check existence (e.g. get_user()) before calling this
                method.  Race safety is provided by the outer bootstrap FileLock in
                service_init.py, not by silent OR IGNORE tolerance.
        """
        now = created_at if created_at else datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO users
                   (username, password_hash, role, email, created_at, password_changed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, password_hash, role, email, now, now),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Created user: {username}")

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user with all related data (api_keys, mcp_credentials)."""
        conn = self._conn_manager.get_connection()

        cursor = conn.execute(
            """SELECT username, password_hash, role, email, created_at,
                      oidc_identity, password_changed_at
               FROM users WHERE username = ?""",
            (username,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return {
            "username": row[0],
            "password_hash": row[1],
            "role": row[2],
            "email": row[3],
            "created_at": row[4],
            "oidc_identity": json.loads(row[5]) if row[5] else None,
            "password_changed_at": row[6],
            "api_keys": self._get_api_keys(conn, username),
            "mcp_credentials": self._get_mcp_credentials(conn, username),
        }

    def _get_api_keys(self, conn, username: str) -> list:
        """Get api_keys for a user."""
        cursor = conn.execute(
            """SELECT key_id, key_hash, key_prefix, name, created_at
               FROM user_api_keys WHERE username = ?""",
            (username,),
        )
        return [
            {
                "key_id": r[0],
                "key_hash": r[1],
                "key_prefix": r[2],
                "name": r[3],
                "created_at": r[4],
            }
            for r in cursor.fetchall()
        ]

    def _get_mcp_credentials(self, conn, username: str) -> list:
        """Get mcp_credentials for a user."""
        cursor = conn.execute(
            """SELECT credential_id, client_id, client_secret_hash, client_id_prefix,
                      name, created_at, last_used_at
               FROM user_mcp_credentials WHERE username = ?""",
            (username,),
        )
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
            for r in cursor.fetchall()
        ]

    def add_api_key(
        self,
        username: str,
        key_id: str,
        key_hash: str,
        key_prefix: str,
        name: Optional[str] = None,
        key_sha256: Optional[str] = None,
    ) -> None:
        """Add an API key for a user.

        key_sha256: SHA-256 hex of the raw key for O(1) bearer-auth lookup
            (Bug #1144). None for legacy callers that don't supply it.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO user_api_keys
                   (key_id, username, key_hash, key_prefix, name, created_at, key_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key_id, username, key_hash, key_prefix, name, now, key_sha256),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def get_api_key_by_sha256(self, sha256_hex: str) -> Optional[Dict[str, Any]]:
        """Look up an API key record by its SHA-256 hex digest (Bug #1144).

        Direct indexed SELECT — never scans all rows. Returns None when not found
        (including legacy rows with NULL key_sha256).

        Returns a dict with at minimum: key_id, username, key_hash.
        NEVER cached — must be a live DB read so revocation takes effect immediately.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT key_id, username, key_hash, key_prefix, name, created_at
               FROM user_api_keys
               WHERE key_sha256 = ?""",
            (sha256_hex,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "key_id": row[0],
            "username": row[1],
            "key_hash": row[2],
            "key_prefix": row[3],
            "name": row[4],
            "created_at": row[5],
        }

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

        def operation(conn):
            conn.execute(
                """INSERT INTO user_mcp_credentials
                   (credential_id, username, client_id, client_secret_hash,
                    client_id_prefix, name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
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
            return None

        self._conn_manager.execute_atomic(operation)

    def list_users(self) -> list:
        """List all users with their related data."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT username, password_hash, role, email, created_at,
                      oidc_identity, password_changed_at
               FROM users"""
        )
        results = []
        for row in cursor.fetchall():
            username = row[0]
            results.append(
                {
                    "username": username,
                    "password_hash": row[1],
                    "role": row[2],
                    "email": row[3],
                    "created_at": row[4],
                    "oidc_identity": json.loads(row[5]) if row[5] else None,
                    "password_changed_at": row[6],
                    "api_keys": self._get_api_keys(conn, username),
                    "mcp_credentials": self._get_mcp_credentials(conn, username),
                }
            )
        return results

    def update_user(
        self,
        username: str,
        new_username: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bool:
        """
        Update user's username or email.

        Args:
            username: Current username
            new_username: New username (if changing)
            email: New email (if changing)

        Returns:
            True if successful, False if user not found
        """
        # First check if user exists
        if self.get_user(username) is None:
            return False

        def operation(conn):
            if new_username and new_username != username:
                # Update username (primary key change)
                conn.execute(
                    "UPDATE users SET username = ?, email = COALESCE(?, email) WHERE username = ?",
                    (new_username, email, username),
                )
                # Update foreign keys in related tables
                conn.execute(
                    "UPDATE user_api_keys SET username = ? WHERE username = ?",
                    (new_username, username),
                )
                conn.execute(
                    "UPDATE user_mcp_credentials SET username = ? WHERE username = ?",
                    (new_username, username),
                )
            elif email is not None:
                # Only update email
                conn.execute(
                    "UPDATE users SET email = ? WHERE username = ?",
                    (email, username),
                )
            return True

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated user: {username}")
        return True

    def delete_user(self, username: str) -> bool:
        """Delete user and all related records (cascade)."""

        def operation(conn):
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted user: {username}")
        return deleted

    def update_user_role(self, username: str, role: str) -> bool:
        """Update user's role."""

        def operation(conn):
            cursor = conn.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (role, username),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated role for user: {username}")
        return updated

    def update_password_hash(self, username: str, password_hash: str) -> bool:
        """Update user's password hash and password_changed_at timestamp."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? "
                "WHERE username = ?",
                (password_hash, now, username),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated password for user: {username}")
        return updated

    def set_password_changed_at(self, username: str, timestamp: Optional[str]) -> bool:
        """Set password_changed_at for a user (Story #565)."""

        def operation(conn):
            cursor = conn.execute(
                "UPDATE users SET password_changed_at = ? WHERE username = ?",
                (timestamp, username),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def delete_api_key(self, username: str, key_id: str) -> bool:
        """Delete an API key for a user."""

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM user_api_keys WHERE username = ? AND key_id = ?",
                (username, key_id),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get user by email address (case-insensitive).

        Story #702 SSO fix: This method was missing from SQLite backend,
        causing AttributeError when SSO login tried to look up users by email.

        Args:
            email: Email address to search for (case-insensitive, whitespace trimmed)

        Returns:
            User data dictionary with api_keys and mcp_credentials, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT username, password_hash, role, email, created_at,
                      oidc_identity, password_changed_at
               FROM users WHERE LOWER(email) = LOWER(?)""",
            (email.strip(),),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        username = row[0]
        return {
            "username": username,
            "password_hash": row[1],
            "role": row[2],
            "email": row[3],
            "created_at": row[4],
            "oidc_identity": json.loads(row[5]) if row[5] else None,
            "password_changed_at": row[6],
            "api_keys": self._get_api_keys(conn, username),
            "mcp_credentials": self._get_mcp_credentials(conn, username),
        }

    def set_oidc_identity(self, username: str, identity: Dict[str, Any]) -> bool:
        """
        Set OIDC identity for a user.

        Story #702 SSO fix: This method was missing from SQLite backend,
        causing AttributeError when SSO login tried to store OIDC identity.

        Args:
            username: Username of the user
            identity: OIDC identity data (subject, email, linked_at, last_login)

        Returns:
            True if user was updated, False if user not found.
        """

        def operation(conn):
            cursor = conn.execute(
                """UPDATE users SET oidc_identity = ? WHERE username = ?""",
                (json.dumps(identity), username),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def delete_mcp_credential(self, username: str, credential_id: str) -> bool:
        """
        Delete an MCP credential for a user.

        Story #702 SQLite migration: This method was missing, causing
        AttributeError when deleting MCP credentials in SQLite mode.

        Args:
            username: Username of the credential owner
            credential_id: ID of the credential to delete

        Returns:
            True if credential was deleted, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM user_mcp_credentials WHERE username = ? AND credential_id = ?",
                (username, credential_id),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool:
        """
        Update last_used_at timestamp for an MCP credential.

        Story #702 SQLite migration: This method was missing, causing
        AttributeError when updating MCP credential timestamps in SQLite mode.

        Args:
            username: Username of the credential owner
            credential_id: ID of the credential to update

        Returns:
            True if credential was updated, False if not found.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                """UPDATE user_mcp_credentials SET last_used_at = ?
                   WHERE username = ? AND credential_id = ?""",
                (now, username, credential_id),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def list_all_mcp_credentials(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List MCP credentials across all users with pagination.

        Story #702 SQLite migration: This method was missing, causing
        AttributeError when listing all MCP credentials in SQLite mode.

        Args:
            limit: Maximum number of credentials to return
            offset: Number of credentials to skip

        Returns:
            List of credential metadata with username information.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT username, credential_id, client_id, client_id_prefix,
                      name, created_at, last_used_at
               FROM user_mcp_credentials
               ORDER BY username, credential_id
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
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
            for r in cursor.fetchall()
        ]

    def get_system_mcp_credentials(self) -> List[Dict[str, Any]]:
        """
        Return MCP credentials owned by the 'admin' user (system-managed credentials).

        Story #275: Display system-managed MCP credentials to admin users.
        System credentials are those belonging to the built-in 'admin' user, which
        are created automatically by the CIDX server (e.g. cidx-local-auto, cidx-server-auto).

        Returns:
            List of credential dicts with is_system=True and owner='admin (system)',
            ordered by created_at DESC (newest first).
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT credential_id, client_id, client_id_prefix,
                      name, created_at, last_used_at
               FROM user_mcp_credentials
               WHERE username = 'admin'
               ORDER BY created_at DESC""",
        )
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
            for r in cursor.fetchall()
        ]

    def remove_oidc_identity(self, username: str) -> bool:
        """
        Remove OIDC identity from a user (unlink SSO).

        Story #702 SQLite migration: This method was missing, causing
        AttributeError when unlinking SSO accounts in SQLite mode.

        Args:
            username: Username to remove OIDC identity from

        Returns:
            True if user was updated, False if user not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE users SET oidc_identity = NULL WHERE username = ?",
                (username,),
            )
            return cursor.rowcount > 0

        result: bool = self._conn_manager.execute_atomic(operation)
        return result

    def get_mcp_credential_by_client_id(
        self, client_id: str
    ) -> Optional[Tuple[str, dict]]:
        """
        Find MCP credential by client_id using direct SQL (Story #269).

        O(1) lookup via idx_user_mcp_credentials_client_id index instead of
        the O(users x credentials) Python iteration previously used in
        MCPCredentialManager.get_credential_by_client_id().

        Args:
            client_id: The client_id to search for.

        Returns:
            Tuple of (username, credential_dict) if found, None otherwise.
            credential_dict contains: credential_id, client_id,
            client_secret_hash, client_id_prefix, name, created_at,
            last_used_at.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT username, credential_id, client_id, client_secret_hash,
                      client_id_prefix, name, created_at, last_used_at
               FROM user_mcp_credentials
               WHERE client_id = ?""",
            (client_id,),
        )
        row = cursor.fetchone()
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

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
