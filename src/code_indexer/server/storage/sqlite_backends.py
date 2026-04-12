"""
SQLite backend implementations for all server managers.

Story #702: Migrate Central JSON Files to SQLite

Provides SQLite-backed storage implementations that replace JSON file storage,
eliminating race conditions from concurrent GlobalRegistry instances.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple

from .database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Sentinel value for distinguishing "not provided" from "explicitly None"
_UNSET: Any = object()


class GlobalReposSqliteBackend:
    """
    SQLite backend for global repository registry.

    Replaces global_registry.json with atomic SQLite operations,
    eliminating race conditions from concurrent instances.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None:
        """
        Register a new repository or update existing one.

        Args:
            alias_name: Unique alias for the repository (primary key).
            repo_name: Name of the repository.
            repo_url: Optional URL of the repository.
            index_path: Path to the repository index.
            enable_temporal: Whether temporal indexing is enabled.
            temporal_options: Optional temporal indexing options (stored as JSON).
            enable_scip: Whether SCIP code intelligence indexing is enabled.
        """
        now = datetime.now(timezone.utc).isoformat()
        temporal_json = json.dumps(temporal_options) if temporal_options else None

        def operation(conn):
            conn.execute(
                """INSERT INTO global_repos
                   (alias_name, repo_name, repo_url, index_path, created_at,
                    last_refresh, enable_temporal, temporal_options, enable_scip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(alias_name) DO UPDATE SET
                    repo_name = excluded.repo_name,
                    repo_url = excluded.repo_url,
                    index_path = excluded.index_path,
                    last_refresh = excluded.last_refresh,
                    enable_temporal = excluded.enable_temporal,
                    temporal_options = excluded.temporal_options,
                    enable_scip = excluded.enable_scip""",
                (
                    alias_name,
                    repo_name,
                    repo_url,
                    index_path,
                    now,
                    now,
                    enable_temporal,
                    temporal_json,
                    enable_scip,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Registered repo: {alias_name}")

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        """
        Get repository details by alias.

        Args:
            alias_name: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias_name, repo_name, repo_url, index_path, created_at,
                      last_refresh, enable_temporal, temporal_options, enable_scip,
                      next_refresh
               FROM global_repos WHERE alias_name = ?""",
            (alias_name,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "alias_name": row[0],
            "repo_name": row[1],
            "repo_url": row[2],
            "index_path": row[3],
            "created_at": row[4],
            "last_refresh": row[5],
            "enable_temporal": bool(row[6]),
            "temporal_options": json.loads(row[7]) if row[7] else None,
            "enable_scip": bool(row[8]),
            "next_refresh": row[9],
        }

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        """
        List all registered repositories.

        Returns:
            Dictionary mapping alias names to repository details.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias_name, repo_name, repo_url, index_path, created_at,
                      last_refresh, enable_temporal, temporal_options, enable_scip,
                      next_refresh
               FROM global_repos"""
        )

        result = {}
        for row in cursor.fetchall():
            alias = row[0]
            result[alias] = {
                "alias_name": alias,
                "repo_name": row[1],
                "repo_url": row[2],
                "index_path": row[3],
                "created_at": row[4],
                "last_refresh": row[5],
                "enable_temporal": bool(row[6]),
                "temporal_options": json.loads(row[7]) if row[7] else None,
                "enable_scip": bool(row[8]),
                "next_refresh": row[9],
            }

        return result

    def delete_repo(self, alias_name: str) -> bool:
        """
        Delete a repository by alias.

        Args:
            alias_name: Alias of the repository to delete.

        Returns:
            True if a record was deleted, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM global_repos WHERE alias_name = ?",
                (alias_name,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted repo: {alias_name}")
        return deleted

    def update_last_refresh(self, alias_name: str) -> bool:
        """
        Update the last_refresh timestamp for a repository.

        Args:
            alias_name: Alias of the repository to update.

        Returns:
            True if record was updated, False if not found.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET last_refresh = ? WHERE alias_name = ?",
                (now, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated last_refresh for repo: {alias_name}")
        return updated

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> bool:
        """
        Update the enable_temporal flag for a repository.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            enable_temporal: New value for enable_temporal flag

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET enable_temporal = ? WHERE alias_name = ?",
                (1 if enable_temporal else 0, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(
                f"Updated enable_temporal={enable_temporal} for repo: {alias_name}"
            )
        return updated

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool:
        """
        Update the enable_scip flag for a repository.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            enable_scip: New value for enable_scip flag

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET enable_scip = ? WHERE alias_name = ?",
                (1 if enable_scip else 0, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated enable_scip={enable_scip} for repo: {alias_name}")
        return updated

    def update_next_refresh(self, alias_name: str, next_refresh: Optional[str]) -> bool:
        """
        Update the next_refresh timestamp for a repository.

        Story #284: Back-propagating jitter scheduling.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            next_refresh: Unix timestamp as string, or None to clear

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET next_refresh = ? WHERE alias_name = ?",
                (next_refresh, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated next_refresh for repo: {alias_name}")
        return updated

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


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
        """Create a new user."""
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
    ) -> None:
        """Add an API key for a user."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO user_api_keys
                   (key_id, username, key_hash, key_prefix, name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key_id, username, key_hash, key_prefix, name, now),
            )
            return None

        self._conn_manager.execute_atomic(operation)

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


class SyncJobsSqliteBackend:
    """
    SQLite backend for sync job management.

    Replaces JSON file storage with atomic SQLite operations.
    Complex nested data (phases, analytics) stored as JSON blobs.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def create_job(
        self,
        job_id: str,
        username: str,
        user_alias: str,
        job_type: str,
        status: str,
        repository_url: Optional[str] = None,
    ) -> None:
        """Create a new sync job."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO sync_jobs
                   (job_id, username, user_alias, job_type, status, created_at, repository_url, progress)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    username,
                    user_alias,
                    job_type,
                    status,
                    now,
                    repository_url,
                    0,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Created sync job: {job_id}")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job details by job ID."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT job_id, username, user_alias, job_type, status, created_at,
                      started_at, completed_at, repository_url, progress, error_message,
                      phases, phase_weights, current_phase, progress_history,
                      recovery_checkpoint, analytics_data
               FROM sync_jobs WHERE job_id = ?""",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to job dictionary."""
        return {
            "job_id": row[0],
            "username": row[1],
            "user_alias": row[2],
            "job_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "started_at": row[6],
            "completed_at": row[7],
            "repository_url": row[8],
            "progress": row[9],
            "error_message": row[10],
            "phases": json.loads(row[11]) if row[11] else None,
            "phase_weights": json.loads(row[12]) if row[12] else None,
            "current_phase": row[13],
            "progress_history": json.loads(row[14]) if row[14] else None,
            "recovery_checkpoint": json.loads(row[15]) if row[15] else None,
            "analytics_data": json.loads(row[16]) if row[16] else None,
        }

    def update_job(self, job_id: str, **kwargs) -> None:
        """Update job fields. Accepts: status, progress, error_message, phases, etc."""
        json_fields = {
            "phases",
            "phase_weights",
            "progress_history",
            "recovery_checkpoint",
            "analytics_data",
        }
        updates, params = [], []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(json.dumps(value) if key in json_fields else value)
        if not updates:
            return
        params.append(job_id)

        def operation(conn):
            conn.execute(
                f"UPDATE sync_jobs SET {', '.join(updates)} WHERE job_id = ?", params
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def list_jobs(self) -> list:
        """List all sync jobs."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT job_id, username, user_alias, job_type, status, created_at,
                      started_at, completed_at, repository_url, progress, error_message,
                      phases, phase_weights, current_phase, progress_history,
                      recovery_checkpoint, analytics_data FROM sync_jobs"""
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID."""

        def operation(conn):
            cursor = conn.execute("DELETE FROM sync_jobs WHERE job_id = ?", (job_id,))
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted sync job: {job_id}")
        return deleted

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        """
        Clean up orphaned sync jobs on server startup.

        On server restart, any sync jobs with status 'running' or 'pending' are
        orphaned because the threads executing them no longer exist. This method
        marks them as 'failed' with an appropriate error message and timestamp
        for audit trail.

        Bug #436: Orphaned jobs persist as "running" after server restart.

        Returns:
            Number of orphaned jobs that were cleaned up.
        """
        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"

        def operation(conn):
            cursor = conn.execute(
                """UPDATE sync_jobs
                   SET status = 'failed',
                       error_message = ?,
                       completed_at = ?
                   WHERE status IN ('running', 'pending')""",
                (error_message, interrupted_at),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(
                f"SyncJobsSqliteBackend.cleanup_orphaned_jobs_on_startup: "
                f"marked {count} orphaned sync job(s) as failed"
            )
        return count

    def cleanup_old_completed(self, cutoff_iso: str) -> int:
        """Delete completed or failed sync jobs older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; jobs with completed_at before this
                        value and status IN ('completed', 'failed') are deleted.

        Returns:
            Number of rows deleted.
        """
        total_deleted = 0

        def operation(conn) -> int:
            cursor = conn.execute(
                """DELETE FROM sync_jobs
                   WHERE rowid IN (
                       SELECT rowid FROM sync_jobs
                       WHERE completed_at < ?
                         AND status IN ('completed', 'failed')
                       LIMIT 1000
                   )""",
                (cutoff_iso,),
            )
            return cursor.rowcount  # type: ignore[no-any-return]

        while True:
            batch: int = self._conn_manager.execute_atomic(operation)
            if batch == 0:
                break
            total_deleted += batch

        return total_deleted

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


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

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class SessionsSqliteBackend:
    """SQLite backend for session management (invalidated_sessions and password_change_timestamps)."""

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def invalidate_session(self, username: str, token_id: str) -> None:
        """Invalidate a specific session token."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                "INSERT OR REPLACE INTO invalidated_sessions (username, token_id, created_at) VALUES (?, ?, ?)",
                (username, token_id, now),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def is_session_invalidated(self, username: str, token_id: str) -> bool:
        """Check if a session token has been invalidated."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM invalidated_sessions WHERE username = ? AND token_id = ?",
            (username, token_id),
        )
        return cursor.fetchone() is not None

    def clear_invalidated_sessions(self, username: str) -> None:
        """Clear all invalidated sessions for a user."""

        def operation(conn):
            conn.execute(
                "DELETE FROM invalidated_sessions WHERE username = ?", (username,)
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None:
        """Set password change timestamp for a user."""

        def operation(conn):
            conn.execute(
                "INSERT OR REPLACE INTO password_change_timestamps (username, changed_at) VALUES (?, ?)",
                (username, changed_at),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def get_password_change_timestamp(self, username: str) -> Optional[str]:
        """Get password change timestamp for a user."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT changed_at FROM password_change_timestamps WHERE username = ?",
            (username,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def cleanup_old_data(self, days_to_keep: int = 30) -> int:
        """
        Clean up old session invalidation data.

        Story #702 SQLite migration: Added to support cleanup_old_data in
        PasswordChangeSessionManager SQLite mode.

        Args:
            days_to_keep: Number of days of data to keep

        Returns:
            Number of user records cleaned up
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
        cutoff_iso = cutoff_time.isoformat()

        def operation(conn):
            # Get usernames to clean up based on password change timestamp
            cursor = conn.execute(
                "SELECT username FROM password_change_timestamps WHERE changed_at < ?",
                (cutoff_iso,),
            )
            users_to_remove = [row[0] for row in cursor.fetchall()]

            if not users_to_remove:
                return 0

            # Delete password change timestamps
            for username in users_to_remove:
                conn.execute(
                    "DELETE FROM password_change_timestamps WHERE username = ?",
                    (username,),
                )
                # Also delete invalidated sessions for these users
                conn.execute(
                    "DELETE FROM invalidated_sessions WHERE username = ?",
                    (username,),
                )

            return len(users_to_remove)

        count: int = self._conn_manager.execute_atomic(operation)
        return count

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class DescriptionRefreshTrackingBackend:
    """
    SQLite backend for description refresh tracking (Story #190).

    Provides CRUD operations for the description_refresh_tracking table,
    which tracks when repositories need their descriptions regenerated.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def get_tracking_record(self, repo_alias: str) -> Optional[Dict[str, Any]]:
        """
        Get tracking record for a repository.

        Args:
            repo_alias: Alias of the repository

        Returns:
            Dictionary with tracking record, or None if not found
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at
               FROM description_refresh_tracking WHERE repo_alias = ?""",
            (repo_alias,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return {
            "repo_alias": row[0],
            "last_run": row[1],
            "next_run": row[2],
            "status": row[3],
            "error": row[4],
            "last_known_commit": row[5],
            "last_known_files_processed": row[6],
            "last_known_indexed_at": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

    def get_stale_repos(self, now_iso: str) -> List[Dict[str, Any]]:
        """
        Query repos where next_run <= now AND status != 'queued'.

        Args:
            now_iso: Current time in ISO 8601 format

        Returns:
            List of tracking records for stale repositories
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at
               FROM description_refresh_tracking
               WHERE next_run <= ? AND status != 'queued'""",
            (now_iso,),
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "repo_alias": row[0],
                    "last_run": row[1],
                    "next_run": row[2],
                    "status": row[3],
                    "error": row[4],
                    "last_known_commit": row[5],
                    "last_known_files_processed": row[6],
                    "last_known_indexed_at": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                }
            )

        return result

    def upsert_tracking(self, repo_alias: str, **fields) -> None:
        """
        Insert or update tracking record.

        Args:
            repo_alias: Alias of the repository (primary key)
            **fields: Fields to set (last_run, next_run, status, error,
                     last_known_commit, last_known_files_processed,
                     last_known_indexed_at, created_at, updated_at)
        """
        # Build list of fields to set
        valid_fields = {
            "last_run",
            "next_run",
            "status",
            "error",
            "last_known_commit",
            "last_known_files_processed",
            "last_known_indexed_at",
            "created_at",
            "updated_at",
        }
        set_fields = {k: v for k, v in fields.items() if k in valid_fields}

        if not set_fields:
            return

        # Build INSERT ON CONFLICT query
        all_columns = ["repo_alias"] + list(set_fields.keys())
        placeholders = ["?"] * len(all_columns)
        values = [repo_alias] + list(set_fields.values())

        update_clause = ", ".join(f"{k} = excluded.{k}" for k in set_fields.keys())

        def operation(conn):
            conn.execute(
                f"""INSERT INTO description_refresh_tracking
                   ({", ".join(all_columns)}) VALUES ({", ".join(placeholders)})
                   ON CONFLICT(repo_alias) DO UPDATE SET {update_clause}""",
                values,
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Upserted tracking record for repo: {repo_alias}")

    def delete_tracking(self, repo_alias: str) -> bool:
        """
        Remove tracking record for a repository.

        Args:
            repo_alias: Alias of the repository to delete

        Returns:
            True if a record was deleted, False if not found
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM description_refresh_tracking WHERE repo_alias = ?",
                (repo_alias,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted tracking record for repo: {repo_alias}")
        return deleted

    def get_all_tracking(self) -> List[Dict[str, Any]]:
        """
        List all tracking records.

        Returns:
            List of all tracking records (for diagnostics)
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at
               FROM description_refresh_tracking"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "repo_alias": row[0],
                    "last_run": row[1],
                    "next_run": row[2],
                    "status": row[3],
                    "error": row[4],
                    "last_known_commit": row[5],
                    "last_known_files_processed": row[6],
                    "last_known_indexed_at": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                }
            )

        return result

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class SSHKeysSqliteBackend:
    """SQLite backend for SSH key management. Uses junction table ssh_key_hosts."""

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def create_key(
        self,
        name: str,
        fingerprint: str,
        key_type: str,
        private_path: str,
        public_path: str,
        public_key: Optional[str] = None,
        email: Optional[str] = None,
        description: Optional[str] = None,
        is_imported: bool = False,
    ) -> None:
        """Create a new SSH key record."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO ssh_keys (name, fingerprint, key_type, private_path, public_path,
                   public_key, email, description, created_at, is_imported) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    fingerprint,
                    key_type,
                    private_path,
                    public_path,
                    public_key,
                    email,
                    description,
                    now,
                    is_imported,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Created SSH key: {name}")

    def get_key(self, name: str) -> Optional[Dict[str, Any]]:
        """Get SSH key details with hosts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT name, fingerprint, key_type, private_path, public_path, public_key,
               email, description, created_at, imported_at, is_imported FROM ssh_keys WHERE name = ?""",
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        hosts = self._get_hosts_for_key(conn, name)
        return {
            "name": row[0],
            "fingerprint": row[1],
            "key_type": row[2],
            "private_path": row[3],
            "public_path": row[4],
            "public_key": row[5],
            "email": row[6],
            "description": row[7],
            "created_at": row[8],
            "imported_at": row[9],
            "is_imported": bool(row[10]),
            "hosts": hosts,
        }

    def _get_hosts_for_key(self, conn: Any, key_name: str) -> list:
        """Get hosts for a key from junction table."""
        cursor = conn.execute(
            "SELECT hostname FROM ssh_key_hosts WHERE key_name = ?", (key_name,)
        )
        return [row[0] for row in cursor.fetchall()]

    def assign_host(self, key_name: str, hostname: str) -> None:
        """Assign a host to a key."""

        def operation(conn):
            conn.execute(
                "INSERT OR IGNORE INTO ssh_key_hosts (key_name, hostname) VALUES (?, ?)",
                (key_name, hostname),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def remove_host(self, key_name: str, hostname: str) -> None:
        """Remove a host from a key."""

        def operation(conn):
            conn.execute(
                "DELETE FROM ssh_key_hosts WHERE key_name = ? AND hostname = ?",
                (key_name, hostname),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def delete_key(self, name: str) -> bool:
        """Delete an SSH key (cascades to hosts)."""

        def operation(conn):
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute("DELETE FROM ssh_keys WHERE name = ?", (name,))
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted SSH key: {name}")
        return deleted

    def list_keys(self) -> list:
        """List all SSH keys with their hosts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT name, fingerprint, key_type, private_path, public_path, public_key,
               email, description, created_at, imported_at, is_imported FROM ssh_keys"""
        )
        results = []
        for row in cursor.fetchall():
            key_name = row[0]
            hosts = self._get_hosts_for_key(conn, key_name)
            results.append(
                {
                    "name": key_name,
                    "fingerprint": row[1],
                    "key_type": row[2],
                    "private_path": row[3],
                    "public_path": row[4],
                    "public_key": row[5],
                    "email": row[6],
                    "description": row[7],
                    "created_at": row[8],
                    "imported_at": row[9],
                    "is_imported": bool(row[10]),
                    "hosts": hosts,
                }
            )
        return results

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class GoldenRepoMetadataSqliteBackend:
    """
    SQLite backend for golden repository metadata (Story #711).

    Replaces golden-repos/metadata.json with atomic SQLite operations,
    eliminating race conditions from concurrent access.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def ensure_table_exists(self) -> None:
        """Ensure the golden_repos_metadata table exists (idempotent)."""

        def operation(conn):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                    alias TEXT PRIMARY KEY NOT NULL,
                    repo_url TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    clone_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enable_temporal INTEGER NOT NULL DEFAULT 0,
                    temporal_options TEXT,
                    category_id INTEGER,
                    category_auto_assigned INTEGER DEFAULT 0,
                    wiki_enabled INTEGER DEFAULT 0
                )
            """
            )
            # Migrate existing tables: add columns that may be missing
            cursor = conn.execute("PRAGMA table_info(golden_repos_metadata)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            migrations = [
                ("category_id", "INTEGER"),
                ("category_auto_assigned", "INTEGER DEFAULT 0"),
                ("wiki_enabled", "INTEGER DEFAULT 0"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE golden_repos_metadata ADD COLUMN {col_name} {col_def}"
                    )
                    logger.info(
                        "Migrated golden_repos_metadata: added column %s", col_name
                    )

        self._conn_manager.execute_atomic(operation)

    def add_repo(
        self,
        alias: str,
        repo_url: str,
        default_branch: str,
        clone_path: str,
        created_at: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
    ) -> None:
        """
        Add a new golden repository.

        Args:
            alias: Unique alias for the repository (primary key).
            repo_url: Git repository URL.
            default_branch: Default branch name.
            clone_path: Path to cloned repository.
            created_at: ISO 8601 timestamp when repository was created.
            enable_temporal: Whether temporal indexing is enabled.
            temporal_options: Optional temporal indexing options (stored as JSON).

        Raises:
            sqlite3.IntegrityError: If alias already exists.
        """
        temporal_json = json.dumps(temporal_options) if temporal_options else None

        def operation(conn):
            conn.execute(
                """INSERT INTO golden_repos_metadata
                   (alias, repo_url, default_branch, clone_path, created_at,
                    enable_temporal, temporal_options)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    alias,
                    repo_url,
                    default_branch,
                    clone_path,
                    created_at,
                    1 if enable_temporal else 0,
                    temporal_json,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Added golden repo: {alias}")

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        Get golden repository details by alias.

        Args:
            alias: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, category_id, category_auto_assigned,
                      COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata WHERE alias = ?""",
            (alias,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "alias": row[0],
            "repo_url": row[1],
            "default_branch": row[2],
            "clone_path": row[3],
            "created_at": row[4],
            "enable_temporal": bool(row[5]),
            "temporal_options": json.loads(row[6]) if row[6] else None,
            "category_id": row[7],
            "category_auto_assigned": bool(row[8]),
            "wiki_enabled": bool(row[9]),
        }

    def list_repos(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories.

        Returns:
            List of repository dictionaries.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "alias": row[0],
                    "repo_url": row[1],
                    "default_branch": row[2],
                    "clone_path": row[3],
                    "created_at": row[4],
                    "enable_temporal": bool(row[5]),
                    "temporal_options": json.loads(row[6]) if row[6] else None,
                    "wiki_enabled": bool(row[7]),
                }
            )

        return result

    def remove_repo(self, alias: str) -> bool:
        """
        Remove a golden repository by alias.

        Args:
            alias: Alias of the repository to remove.

        Returns:
            True if a record was deleted, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM golden_repos_metadata WHERE alias = ?",
                (alias,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Removed golden repo: {alias}")
        return deleted

    def repo_exists(self, alias: str) -> bool:
        """
        Check if a golden repository exists.

        Args:
            alias: Alias to check.

        Returns:
            True if alias exists, False otherwise.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM golden_repos_metadata WHERE alias = ?",
            (alias,),
        )
        return cursor.fetchone() is not None

    def update_enable_temporal(self, alias: str, enable: bool) -> bool:
        """
        Update the enable_temporal flag for a golden repository.

        Bug #131: This method is called after successful temporal index creation
        to update the enable_temporal flag in the database.

        Args:
            alias: Alias of the repository to update.
            enable: New value for enable_temporal flag.

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET enable_temporal = ? WHERE alias = ?",
                (1 if enable else 0, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated enable_temporal={enable} for golden repo: {alias}")
        return updated

    def update_temporal_options(self, alias: str, options: Optional[Dict]) -> bool:
        """
        Update the temporal_options JSON for a golden repository.

        Story #478: Persist temporal indexing configuration (max_commits,
        diff_context, since_date, all_branches) per repository so that
        admin-triggered rebuilds and scheduled refreshes apply stored options.

        Args:
            alias: Alias of the repository to update.
            options: Dict of temporal options, or None to clear.

        Returns:
            True if a record was updated, False if alias not found.
        """
        temporal_json = json.dumps(options) if options is not None else None

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET temporal_options = ? WHERE alias = ?",
                (temporal_json, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated temporal_options for golden repo: {alias}")
        return updated

    def update_repo_url(self, alias: str, repo_url: str) -> bool:
        """
        Update the repo_url for a golden repository.

        Bug #131: This method is used during legacy cidx-meta migration (Scenario 2)
        to update repo_url from None to "local://cidx-meta".

        Args:
            alias: Alias of the repository to update.
            repo_url: New repo_url value.

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE golden_repos_metadata SET repo_url = ? WHERE alias = ?",
                (repo_url, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.info(f"Updated repo_url={repo_url} for golden repo: {alias}")
        return updated

    def update_category(
        self, alias: str, category_id: Optional[int], auto_assigned: bool = True
    ) -> bool:
        """
        Update category assignment for a golden repository (Story #181).

        Args:
            alias: Alias of the repository to update.
            category_id: Category ID to assign, or None for Unassigned.
            auto_assigned: Whether this is an automatic assignment (True) or manual (False).

        Returns:
            True if a record was updated, False if alias not found.
        """

        def operation(conn):
            cursor = conn.execute(
                """UPDATE golden_repos_metadata
                   SET category_id = ?, category_auto_assigned = ?
                   WHERE alias = ?""",
                (category_id, 1 if auto_assigned else 0, alias),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(
                f"Updated category_id={category_id} (auto={auto_assigned}) for repo: {alias}"
            )
        return updated

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None:
        """Update wiki_enabled flag for a golden repo (Story #280)."""

        def operation(conn):
            conn.execute(
                "UPDATE golden_repos_metadata SET wiki_enabled = ? WHERE alias = ?",
                (1 if enabled else 0, alias),
            )

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated wiki_enabled={enabled} for golden repo: {alias}")

    def update_default_branch(self, alias: str, branch: str) -> None:
        """
        Update the default_branch for a golden repository (Story #303).

        Args:
            alias: Repository alias (primary key).
            branch: New default branch name.

        Notes:
            If alias does not exist, this is a no-op (no error raised).
        """

        def operation(conn):
            conn.execute(
                "UPDATE golden_repos_metadata SET default_branch = ? WHERE alias = ?",
                (branch, alias),
            )

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated default_branch={branch!r} for golden repo: {alias}")

    def invalidate_description_refresh_tracking(self, alias: str) -> None:
        """
        Invalidate description refresh tracking for a repo after branch change (Story #303).

        Sets last_known_commit to NULL so the next refresh cycle re-analyzes.
        No-op if the alias has no tracking record.
        """

        def operation(conn):
            conn.execute(
                "UPDATE description_refresh_tracking SET last_known_commit = NULL WHERE repo_alias = ?",
                (alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def invalidate_dependency_map_tracking(self, alias: str) -> None:
        """
        Remove alias entry from dependency_map_tracking.commit_hashes JSON (Story #303).

        The commit_hashes column stores a JSON object mapping aliases to commit hashes.
        This removes the entry for the specified alias so the next analysis re-processes it.
        No-op if no tracking record exists or alias not in commit_hashes.
        """
        import json as _json

        def operation(conn):
            row = conn.execute(
                "SELECT commit_hashes FROM dependency_map_tracking WHERE id = 1"
            ).fetchone()
            if not row or not row[0]:
                return
            try:
                hashes = _json.loads(row[0])
            except (ValueError, TypeError):
                return
            if alias not in hashes:
                return
            del hashes[alias]
            conn.execute(
                "UPDATE dependency_map_tracking SET commit_hashes = ? WHERE id = 1",
                (_json.dumps(hashes),),
            )

        self._conn_manager.execute_atomic(operation)

    def list_repos_with_categories(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories with category information (Story #181).

        Returns:
            List of repository dictionaries including category_id and category_auto_assigned.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, category_id, category_auto_assigned,
                      COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "alias": row[0],
                    "repo_url": row[1],
                    "default_branch": row[2],
                    "clone_path": row[3],
                    "created_at": row[4],
                    "enable_temporal": bool(row[5]),
                    "temporal_options": json.loads(row[6]) if row[6] else None,
                    "category_id": row[7],
                    "category_auto_assigned": bool(row[8]),
                    "wiki_enabled": bool(row[9]),
                }
            )

        return result

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class DependencyMapTrackingBackend:
    """
    SQLite backend for dependency map tracking (Story #192).

    Uses a singleton row (id=1) to track dependency map analysis state.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def get_tracking(self) -> Dict[str, Any]:
        """
        Get the singleton tracking record.

        Initializes the singleton row if it doesn't exist.
        Also ensures run_history table exists for AC9 compatibility.
        Also ensures refinement columns exist for Story #359 compatibility.

        Returns:
            Dictionary with tracking data (id, last_run, next_run, status,
            commit_hashes, error_message, refinement_cursor, refinement_next_run)
        """
        self._ensure_run_history_table()
        self._ensure_refinement_columns()
        conn = self._conn_manager.get_connection()

        # Try to fetch existing singleton row
        cursor = conn.execute(
            """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                      refinement_cursor, refinement_next_run
               FROM dependency_map_tracking WHERE id = 1"""
        )
        row = cursor.fetchone()

        if row is None:
            # Initialize singleton row
            def operation(conn):
                conn.execute(
                    """INSERT INTO dependency_map_tracking (id, status)
                       VALUES (1, 'pending')"""
                )
                return None

            self._conn_manager.execute_atomic(operation)

            # Fetch newly created row
            cursor = conn.execute(
                """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                          refinement_cursor, refinement_next_run
                   FROM dependency_map_tracking WHERE id = 1"""
            )
            row = cursor.fetchone()

        return {
            "id": row[0],
            "last_run": row[1],
            "next_run": row[2],
            "status": row[3],
            "commit_hashes": row[4],
            "error_message": row[5],
            "refinement_cursor": row[6],
            "refinement_next_run": row[7],
        }

    def update_tracking(
        self,
        last_run: Optional[str] = _UNSET,
        next_run: Optional[str] = _UNSET,
        status: Optional[str] = _UNSET,
        commit_hashes: Optional[str] = _UNSET,
        error_message: Optional[str] = _UNSET,
        refinement_cursor: Optional[int] = _UNSET,
        refinement_next_run: Optional[str] = _UNSET,
    ) -> None:
        """
        Update the singleton tracking record.

        Only updates fields that are explicitly provided (partial updates supported).

        Args:
            last_run: ISO timestamp of last analysis run
            next_run: ISO timestamp of next scheduled run
            status: Analysis status (pending/running/completed/failed)
            commit_hashes: JSON string mapping repo alias to commit hash
            error_message: Error message if analysis failed (None clears the error)
            refinement_cursor: Index of next domain to refine (Story #359)
            refinement_next_run: ISO timestamp of next refinement cycle (Story #359)
        """
        # Build UPDATE statement for provided fields only
        updates: list[str] = []
        params: list[Any] = []

        if last_run is not _UNSET:
            updates.append("last_run = ?")
            params.append(last_run)

        if next_run is not _UNSET:
            updates.append("next_run = ?")
            params.append(next_run)

        if status is not _UNSET:
            updates.append("status = ?")
            params.append(status)

        if commit_hashes is not _UNSET:
            updates.append("commit_hashes = ?")
            params.append(commit_hashes)

        if error_message is not _UNSET:
            updates.append("error_message = ?")
            params.append(error_message)

        if refinement_cursor is not _UNSET:
            updates.append("refinement_cursor = ?")
            params.append(refinement_cursor)

        if refinement_next_run is not _UNSET:
            updates.append("refinement_next_run = ?")
            params.append(refinement_next_run)

        if not updates:
            return  # No fields to update

        def operation(conn):
            conn.execute(
                f"UPDATE dependency_map_tracking SET {', '.join(updates)} WHERE id = 1",
                params,
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug("Updated dependency map tracking record")

    def cleanup_stale_status_on_startup(self) -> bool:
        """Reset stale running/pending status to failed on server startup.

        Called once during server startup. If the singleton row has status
        'running' or 'pending', the previous server process was killed
        mid-analysis. Reset to 'failed' so new jobs can be triggered.

        Returns:
            True if a stale status was cleaned up, False otherwise.
        """

        def operation(conn):
            cursor = conn.execute(
                "SELECT status FROM dependency_map_tracking WHERE id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                return False
            status = row[0]
            if status in ("running", "pending"):
                conn.execute(
                    "UPDATE dependency_map_tracking SET status = 'failed', "
                    "error_message = 'orphaned - server restarted' "
                    "WHERE id = 1",
                )
                return True
            return False

        cleaned = self._conn_manager.execute_atomic(operation)
        if cleaned:
            logger.info(
                "DependencyMapTrackingBackend: reset stale status to 'failed' on startup"
            )
        return bool(cleaned)

    def _ensure_run_history_table(self) -> None:
        """Ensure dependency_map_run_history table exists (idempotent).

        Also ensures the parent dependency_map_tracking table exists
        so this backend works in test databases created without initialize_database().
        """

        def operation(conn):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dependency_map_tracking (
                    id INTEGER PRIMARY KEY,
                    last_run TEXT,
                    next_run TEXT,
                    status TEXT DEFAULT 'pending',
                    commit_hashes TEXT,
                    error_message TEXT
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dependency_map_run_history (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    domain_count INTEGER,
                    total_chars INTEGER,
                    edge_count INTEGER,
                    zero_char_domains INTEGER,
                    repos_analyzed INTEGER,
                    repos_skipped INTEGER,
                    pass1_duration_s REAL,
                    pass2_duration_s REAL
                )
            """
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def _ensure_refinement_columns(self) -> None:
        """Add refinement tracking columns if they don't exist (Story #359).

        Idempotent: safe to call on both new and existing databases.
        Uses ALTER TABLE for backward-compatible schema migration.
        Probes each column independently to handle half-migration scenarios.
        """

        def _do_migrate(conn: sqlite3.Connection) -> None:
            for col, col_type in [
                ("refinement_cursor", "INTEGER DEFAULT 0"),
                ("refinement_next_run", "TEXT"),
            ]:
                try:
                    conn.execute(f"SELECT {col} FROM dependency_map_tracking LIMIT 1")
                except sqlite3.OperationalError:
                    conn.execute(
                        f"ALTER TABLE dependency_map_tracking ADD COLUMN {col} {col_type}"
                    )

        self._conn_manager.execute_atomic(_do_migrate)

    def record_run_metrics(self, metrics: Dict[str, Any]) -> None:
        """
        Store run metrics to dependency_map_run_history (AC9, Story #216).

        Args:
            metrics: Dict with keys: timestamp, domain_count, total_chars, edge_count,
                     zero_char_domains, repos_analyzed, repos_skipped,
                     pass1_duration_s, pass2_duration_s
        """
        self._ensure_run_history_table()

        def operation(conn):
            conn.execute(
                """INSERT INTO dependency_map_run_history
                   (timestamp, domain_count, total_chars, edge_count, zero_char_domains,
                    repos_analyzed, repos_skipped, pass1_duration_s, pass2_duration_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metrics.get("timestamp"),
                    metrics.get("domain_count"),
                    metrics.get("total_chars"),
                    metrics.get("edge_count"),
                    metrics.get("zero_char_domains"),
                    metrics.get("repos_analyzed"),
                    metrics.get("repos_skipped"),
                    metrics.get("pass1_duration_s"),
                    metrics.get("pass2_duration_s"),
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug("Recorded dependency map run metrics")

    def get_run_history(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Retrieve recent run metrics ordered most-recent-first (AC9, Story #216).

        Args:
            limit: Maximum number of records to return (default 5)

        Returns:
            List of metric dicts ordered by run_id descending (most recent first)
        """
        self._ensure_run_history_table()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT run_id, timestamp, domain_count, total_chars, edge_count,
                      zero_char_domains, repos_analyzed, repos_skipped,
                      pass1_duration_s, pass2_duration_s
               FROM dependency_map_run_history
               ORDER BY run_id DESC
               LIMIT ?""",
            (limit,),
        )
        rows = cursor.fetchall()
        return [
            {
                "run_id": row[0],
                "timestamp": row[1],
                "domain_count": row[2],
                "total_chars": row[3],
                "edge_count": row[4],
                "zero_char_domains": row[5],
                "repos_analyzed": row[6],
                "repos_skipped": row[7],
                "pass1_duration_s": row[8],
                "pass2_duration_s": row[9],
            }
            for row in rows
        ]

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        """Delete dependency_map_run_history records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of deleted records.
        """
        self._ensure_run_history_table()
        deleted = 0

        def operation(conn):
            nonlocal deleted
            cursor = conn.execute(
                "DELETE FROM dependency_map_run_history WHERE timestamp < ?",
                (cutoff_iso,),
            )
            deleted = cursor.rowcount

        self._conn_manager.execute_atomic(operation)
        return deleted

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class BackgroundJobsSqliteBackend:
    """
    SQLite backend for background job management.

    Bug fix: BackgroundJobManager SQLite migration - Jobs not showing in Dashboard.
    Replaces JSON file storage with atomic SQLite operations.
    Complex nested data (result, claude_actions, extended_error, etc.) stored as JSON blobs.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def save_job(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        current_phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Save a new background job."""

        def operation(conn):
            conn.execute(
                """INSERT OR IGNORE INTO background_jobs
                   (job_id, operation_type, status, created_at, started_at, completed_at,
                    result, error, progress, username, is_admin, cancelled, repo_alias,
                    resolution_attempts, claude_actions, failure_reason, extended_error,
                    language_resolution_status, current_phase, phase_detail,
                    progress_info, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    operation_type,
                    status,
                    created_at,
                    started_at,
                    completed_at,
                    json.dumps(result) if result else None,
                    error,
                    progress,
                    username,
                    1 if is_admin else 0,
                    1 if cancelled else 0,
                    repo_alias,
                    resolution_attempts,
                    json.dumps(claude_actions) if claude_actions else None,
                    failure_reason,
                    json.dumps(extended_error) if extended_error else None,
                    (
                        json.dumps(language_resolution_status)
                        if language_resolution_status
                        else None
                    ),
                    current_phase,
                    phase_detail,
                    progress_info,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Saved background job: {job_id}")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job details by job ID."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT job_id, operation_type, status, created_at, started_at, completed_at,
                      result, error, progress, username, is_admin, cancelled, repo_alias,
                      resolution_attempts, claude_actions, failure_reason, extended_error,
                      language_resolution_status, current_phase, phase_detail,
                      progress_info, metadata
               FROM background_jobs WHERE job_id = ?""",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to job dictionary."""
        return {
            "job_id": row[0],
            "operation_type": row[1],
            "status": row[2],
            "created_at": row[3],
            "started_at": row[4],
            "completed_at": row[5],
            "result": json.loads(row[6]) if row[6] else None,
            "error": row[7],
            "progress": row[8],
            "username": row[9],
            "is_admin": bool(row[10]),
            "cancelled": bool(row[11]),
            "repo_alias": row[12],
            "resolution_attempts": row[13],
            "claude_actions": json.loads(row[14]) if row[14] else None,
            "failure_reason": row[15],
            "extended_error": json.loads(row[16]) if row[16] else None,
            "language_resolution_status": json.loads(row[17]) if row[17] else None,
            "current_phase": row[18] if len(row) > 18 else None,
            "phase_detail": row[19] if len(row) > 19 else None,
            "progress_info": row[20] if len(row) > 20 else None,
            "metadata": json.loads(row[21]) if len(row) > 21 and row[21] else None,
        }

    def update_job(self, job_id: str, **kwargs) -> None:
        """Update job fields. Accepts any field from the background_jobs table."""
        json_fields = {
            "result",
            "claude_actions",
            "extended_error",
            "language_resolution_status",
        }
        bool_fields = {"is_admin", "cancelled"}
        updates: List[str] = []
        params: List[Any] = []

        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            if value is None:
                params.append(None)
            elif key in json_fields:
                params.append(json.dumps(value))
            elif key in bool_fields:
                params.append(1 if value else 0)
            else:
                params.append(value)

        if not updates:
            return

        params.append(job_id)

        def operation(conn):
            conn.execute(
                f"UPDATE background_jobs SET {', '.join(updates)} WHERE job_id = ?",
                params,
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def list_jobs(
        self,
        username: Optional[str] = None,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List background jobs with optional filtering and pagination."""
        conn = self._conn_manager.get_connection()

        query = """SELECT job_id, operation_type, status, created_at, started_at, completed_at,
                          result, error, progress, username, is_admin, cancelled, repo_alias,
                          resolution_attempts, claude_actions, failure_reason, extended_error,
                          language_resolution_status, current_phase, phase_detail,
                          progress_info, metadata
                   FROM background_jobs"""

        conditions = []
        params: List[Any] = []

        if username:
            conditions.append("username = ?")
            params.append(username)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = ?")
            params.append(operation_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = conn.execute(query, params)
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[set] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> tuple:
        """Return (list_of_job_dicts, total_count) with dynamic SQL WHERE filters.

        Story #271: Filtered jobs query with pagination support.

        Args:
            status: Filter by exact status value (e.g. 'completed', 'failed')
            operation_type: Filter by exact operation_type value
            search_text: Case-insensitive LIKE match against repo_alias, username,
                         operation_type, and error columns
            exclude_ids: Set of job_ids to exclude (used to skip in-memory active jobs)
            limit: Maximum number of rows to return (None = no limit)
            offset: Number of rows to skip for pagination (default 0)

        Returns:
            Tuple of (jobs: List[Dict], total_count: int) where total_count reflects
            the full matching set ignoring limit/offset.
        """
        conn = self._conn_manager.get_connection()

        base_select = """SELECT job_id, operation_type, status, created_at, started_at,
                                completed_at, result, error, progress, username, is_admin,
                                cancelled, repo_alias, resolution_attempts, claude_actions,
                                failure_reason, extended_error, language_resolution_status,
                                current_phase, phase_detail
                         FROM background_jobs"""

        conditions: List[str] = []
        params: List[Any] = []

        if status:
            conditions.append("status = ?")
            params.append(status)

        if operation_type:
            conditions.append("operation_type = ?")
            params.append(operation_type)

        if search_text:
            # Case-insensitive LIKE across key text columns
            like_pattern = f"%{search_text}%"
            conditions.append(
                "(LOWER(repo_alias) LIKE LOWER(?)"
                " OR LOWER(username) LIKE LOWER(?)"
                " OR LOWER(operation_type) LIKE LOWER(?)"
                " OR LOWER(COALESCE(error, '')) LIKE LOWER(?))"
            )
            params.extend([like_pattern, like_pattern, like_pattern, like_pattern])

        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            conditions.append(f"job_id NOT IN ({placeholders})")
            params.extend(list(exclude_ids))

        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        # Count query (no LIMIT/OFFSET) for accurate total
        count_query = f"SELECT COUNT(*) FROM background_jobs{where_clause}"
        count_cursor = conn.execute(count_query, params)
        total_count: int = count_cursor.fetchone()[0]

        # Data query with ORDER BY and optional pagination
        data_query = base_select + where_clause + " ORDER BY created_at DESC"
        data_params = list(params)

        if limit is not None:
            data_query += " LIMIT ? OFFSET ?"
            data_params.extend([limit, offset])

        cursor = conn.execute(data_query, data_params)
        jobs = [self._row_to_dict(row) for row in cursor.fetchall()]

        return jobs, total_count

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID."""

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM background_jobs WHERE job_id = ?", (job_id,)
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(f"Deleted background job: {job_id}")
        return deleted

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Clean up old completed/failed/cancelled jobs."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff_time.isoformat()

        def operation(conn):
            cursor = conn.execute(
                """DELETE FROM background_jobs
                   WHERE status IN ('completed', 'failed', 'cancelled')
                   AND completed_at IS NOT NULL
                   AND completed_at < ?""",
                (cutoff_iso,),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(f"Cleaned up {count} old background jobs")
        return count

    def count_jobs_by_status(self) -> Dict[str, int]:
        """Get count of jobs grouped by status."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT status, COUNT(*) FROM background_jobs GROUP BY status"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]:
        """Get job statistics filtered by time period."""
        now = datetime.now(timezone.utc)

        if time_filter == "24h":
            cutoff = now - timedelta(hours=24)
        elif time_filter == "7d":
            cutoff = now - timedelta(days=7)
        elif time_filter == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = now - timedelta(hours=24)

        cutoff_iso = cutoff.isoformat()

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT status, COUNT(*) FROM background_jobs
               WHERE completed_at IS NOT NULL AND completed_at >= ?
               GROUP BY status""",
            (cutoff_iso,),
        )

        stats = {"completed": 0, "failed": 0}
        for row in cursor.fetchall():
            if row[0] in stats:
                stats[row[0]] = row[1]

        return stats

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        """
        Clean up orphaned jobs on server startup.

        On server restart, any jobs with status 'running' or 'pending' are orphaned
        because the processes that were executing them no longer exist.

        This method marks them as 'failed' with an appropriate error message
        and timestamp for audit trail.

        Story #723: Clean Up Orphaned Jobs on Server Startup

        Returns:
            Number of orphaned jobs that were cleaned up.
        """
        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"

        def operation(conn):
            cursor = conn.execute(
                """UPDATE background_jobs
                   SET status = 'failed',
                       error = ?,
                       completed_at = ?
                   WHERE status IN ('running', 'pending')""",
                (error_message, interrupted_at),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(f"Cleaned up {count} orphaned jobs on server startup")
        return count

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


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
        """Close database connections."""
        self._conn_manager.close_all()


class NodeMetricsSqliteBackend:
    """
    SQLite backend for cluster node metrics storage (Story #492).

    Stores periodic snapshots of per-node system metrics (CPU, memory, disk,
    network) so the dashboard can display cluster health without polling psutil
    directly in the HTTP request path.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Write a single metrics snapshot for a node.

        Args:
            snapshot: Dict with keys: node_id, node_ip, timestamp, cpu_usage,
                memory_percent, memory_used_bytes, process_rss_mb, index_memory_mb,
                swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                net_rx_kb_s, net_tx_kb_s, volumes_json, server_version.
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """INSERT INTO node_metrics
                   (node_id, node_ip, timestamp, cpu_usage, memory_percent,
                    memory_used_bytes, process_rss_mb, index_memory_mb,
                    swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                    net_rx_kb_s, net_tx_kb_s, volumes_json, server_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["node_id"],
                    snapshot["node_ip"],
                    snapshot["timestamp"],
                    snapshot["cpu_usage"],
                    snapshot["memory_percent"],
                    snapshot["memory_used_bytes"],
                    snapshot["process_rss_mb"],
                    snapshot["index_memory_mb"],
                    snapshot["swap_used_mb"],
                    snapshot["swap_total_mb"],
                    snapshot["disk_read_kb_s"],
                    snapshot["disk_write_kb_s"],
                    snapshot["net_rx_kb_s"],
                    snapshot["net_tx_kb_s"],
                    snapshot["volumes_json"],
                    snapshot["server_version"],
                ),
            )

        self._conn_manager.execute_atomic(operation)
        logger.debug(
            "Wrote node_metrics snapshot for node: %s", snapshot.get("node_id")
        )

    def get_latest_per_node(self) -> List[Dict[str, Any]]:
        """Return the latest snapshot for each distinct node_id.

        Returns:
            List of snapshot dicts, one per distinct node_id, ordered by
            node_id. Each dict has all snapshot fields.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT nm.node_id, nm.node_ip, nm.timestamp, nm.cpu_usage,
                      nm.memory_percent, nm.memory_used_bytes, nm.process_rss_mb,
                      nm.index_memory_mb, nm.swap_used_mb, nm.swap_total_mb,
                      nm.disk_read_kb_s, nm.disk_write_kb_s, nm.net_rx_kb_s,
                      nm.net_tx_kb_s, nm.volumes_json, nm.server_version
               FROM node_metrics nm
               INNER JOIN (
                   SELECT node_id, MAX(timestamp) AS max_ts
                   FROM node_metrics
                   GROUP BY node_id
               ) latest ON nm.node_id = latest.node_id AND nm.timestamp = latest.max_ts
               ORDER BY nm.node_id"""
        )
        rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_all_snapshots(self, since: "datetime") -> List[Dict[str, Any]]:
        """Return all snapshots since the given datetime.

        Args:
            since: Datetime cutoff; only snapshots with timestamp >= since are returned.

        Returns:
            List of snapshot dicts ordered by timestamp ascending.
        """
        since_str = since.isoformat()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT node_id, node_ip, timestamp, cpu_usage, memory_percent,
                      memory_used_bytes, process_rss_mb, index_memory_mb,
                      swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                      net_rx_kb_s, net_tx_kb_s, volumes_json, server_version
               FROM node_metrics
               WHERE timestamp >= ?
               ORDER BY timestamp ASC""",
            (since_str,),
        )
        rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def cleanup_older_than(self, cutoff: "datetime") -> int:
        """Delete all snapshots with timestamp older than cutoff.

        Args:
            cutoff: Datetime threshold; records with timestamp < cutoff are deleted.

        Returns:
            Number of rows deleted.
        """
        cutoff_str = cutoff.isoformat()

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                "DELETE FROM node_metrics WHERE timestamp < ?",
                (cutoff_str,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug("Cleaned up %d old node_metrics records", deleted)
        return deleted

    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a snapshot dict."""
        return {
            "node_id": row[0],
            "node_ip": row[1],
            "timestamp": row[2],
            "cpu_usage": row[3],
            "memory_percent": row[4],
            "memory_used_bytes": row[5],
            "process_rss_mb": row[6],
            "index_memory_mb": row[7],
            "swap_used_mb": row[8],
            "swap_total_mb": row[9],
            "disk_read_kb_s": row[10],
            "disk_write_kb_s": row[11],
            "net_rx_kb_s": row[12],
            "net_tx_kb_s": row[13],
            "volumes_json": row[14],
            "server_version": row[15],
        }

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


class LogsSqliteBackend:
    """
    SQLite backend for operational log storage (Story #500).

    Stores log records written by SQLiteLogHandler (and cluster nodes) so
    the admin UI and REST API can query them with filtering and pagination.

    Uses a dedicated logs.db file (separate from the main cidx_server.db)
    to isolate high-volume log writes from other server state.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the logs table if it does not exist.

        Args:
            db_path: Path to SQLite database file (e.g. ~/.cidx-server/logs.db).
        """
        import os

        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the logs table and indexes if they do not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT,
                    message TEXT,
                    correlation_id TEXT,
                    user_id TEXT,
                    request_path TEXT,
                    extra_data TEXT,
                    node_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_correlation_id ON logs(correlation_id)"
            )
            # Migrate existing databases: add node_id column if missing
            # (must run BEFORE creating the index on node_id)
            cursor = conn.execute("PRAGMA table_info(logs)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                conn.execute("ALTER TABLE logs ADD COLUMN node_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_node_id ON logs(node_id)")

        self._conn_manager.execute_atomic(operation)

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: Optional[str] = None,
        message: Optional[str] = None,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single log record.

        Args:
            timestamp: ISO 8601 timestamp string.
            level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            source: Logger name / source identifier.
            message: Formatted log message text.
            correlation_id: Optional request correlation ID.
            user_id: Optional user identifier.
            request_path: Optional HTTP request path.
            extra_data: Optional JSON-serialised extra fields.
            node_id: Optional cluster node identifier (NULL in standalone).
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO logs
                    (timestamp, level, source, message, correlation_id,
                     user_id, request_path, extra_data, node_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    level,
                    source,
                    message,
                    correlation_id,
                    user_id,
                    request_path,
                    extra_data,
                    node_id,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _build_query_conditions(
        self,
        level: Optional[str],
        source: Optional[str],
        correlation_id: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
        node_id: Optional[str],
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and params list for log queries."""
        conditions: List[str] = []
        params: List[Any] = []
        if level is not None:
            conditions.append("level = ?")
            params.append(level)
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)
        if date_from is not None:
            conditions.append("timestamp >= ?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("timestamp <= ?")
            params.append(date_to)
        if node_id is not None:
            conditions.append("node_id = ?")
            params.append(node_id)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where_clause, params

    def _row_to_log_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a log record dict."""
        return {
            "id": row[0],
            "timestamp": row[1],
            "level": row[2],
            "source": row[3],
            "message": row[4],
            "correlation_id": row[5],
            "user_id": row[6],
            "request_path": row[7],
            "extra_data": row[8],
            "node_id": row[9],
            "created_at": row[10],
        }

    def query_logs(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        correlation_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        node_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query log records with optional filtering and pagination.

        Returns:
            Tuple of (list_of_log_dicts, total_count) where total_count reflects
            the full match count before pagination is applied.
        """
        where_clause, params = self._build_query_conditions(
            level, source, correlation_id, date_from, date_to, node_id
        )
        conn = self._conn_manager.get_connection()
        total_count: int = conn.execute(
            f"SELECT COUNT(*) FROM logs {where_clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, timestamp, level, source, message, correlation_id,
                   user_id, request_path, extra_data, node_id, created_at
            FROM logs {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        return [self._row_to_log_dict(row) for row in rows], total_count

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        """Delete log records older than days_to_keep days.

        Args:
            days_to_keep: Records with timestamp older than this many days are deleted.

        Returns:
            Number of rows deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                "DELETE FROM logs WHERE timestamp < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug("Cleaned up %d old log records", deleted)
        return deleted

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass


# Period-to-tier mapping for bucketed query methods (Story #673).
# Maps period_seconds → granularity tier stored in api_metrics_buckets.
PERIOD_TO_TIER: Dict[int, str] = {
    900: "min1",  # 15 minutes  → 1-minute buckets
    3600: "min5",  # 1 hour      → 5-minute buckets
    86400: "hour1",  # 24 hours    → 1-hour buckets
    604800: "day1",  # 7 days      → 1-day buckets
    1296000: "day1",  # 15 days     → 1-day buckets
}


class ApiMetricsSqliteBackend:
    """
    SQLite backend for API metrics storage (Story #502).

    Stores rolling-window API call timestamps so the dashboard can report
    semantic_searches, other_index_searches, regex_searches, and other_api_calls
    within any time window.

    Uses a dedicated api_metrics.db file (separate from the main cidx_server.db)
    to isolate high-volume metric writes from other server state.

    Includes a node_id column for cluster support — each node tags its own
    metrics so per-node filtering is possible.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the api_metrics table if it does not exist.

        Args:
            db_path: Path to SQLite database file
                     (e.g. ~/.cidx-server/data/api_metrics.db).
        """
        import os

        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # Enable WAL mode outside any transaction (PRAGMA cannot run inside BEGIN).
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the api_metrics table and indexes if they do not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_metrics_type_timestamp
                ON api_metrics(metric_type, timestamp)
                """
            )
            # Migrate existing databases: add node_id column if missing
            # (must run BEFORE creating the index on node_id)
            cursor = conn.execute("PRAGMA table_info(api_metrics)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                conn.execute("ALTER TABLE api_metrics ADD COLUMN node_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_metrics_node_id ON api_metrics(node_id)"
            )

        self._conn_manager.execute_atomic(operation)
        self._ensure_buckets_schema()

    # Valid values for bucket fields — used in upsert_bucket validation
    _VALID_GRANULARITIES = frozenset({"min1", "min5", "hour1", "day1"})
    _VALID_METRIC_TYPES = frozenset({"semantic", "other_index", "regex", "other_api"})

    # Retention window per granularity tier (Story #672) — immutable
    _RETENTION_WINDOWS = MappingProxyType(
        {
            "min1": timedelta(minutes=15),
            "min5": timedelta(hours=1),
            "hour1": timedelta(hours=24),
            "day1": timedelta(days=15),
        }
    )

    def _ensure_buckets_schema(self) -> None:
        """Create the api_metrics_buckets table if it does not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_metrics_buckets (
                    username      TEXT NOT NULL,
                    granularity   TEXT NOT NULL,
                    bucket_start  TEXT NOT NULL,
                    metric_type   TEXT NOT NULL,
                    count         INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (username, granularity, bucket_start, metric_type)
                )
                """
            )

        self._conn_manager.execute_atomic(operation)

    def upsert_bucket(
        self,
        username: str,
        granularity: str,
        bucket_start: str,
        metric_type: str,
    ) -> None:
        """Upsert a bucket row — increment count by 1, creating the row if needed.

        Args:
            username: Non-empty username for attribution.
            granularity: One of 'min1', 'min5', 'hour1', 'day1'.
            bucket_start: ISO 8601 timestamp of the bucket boundary.
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').

        Raises:
            ValueError: If any argument fails validation.
        """
        if not isinstance(username, str) or not username.strip():
            raise ValueError(f"username must be a non-empty string, got {username!r}")
        if granularity not in self._VALID_GRANULARITIES:
            raise ValueError(
                f"Invalid granularity {granularity!r}. "
                f"Must be one of: {sorted(self._VALID_GRANULARITIES)}"
            )
        if metric_type not in self._VALID_METRIC_TYPES:
            raise ValueError(
                f"Invalid metric_type {metric_type!r}. "
                f"Must be one of: {sorted(self._VALID_METRIC_TYPES)}"
            )
        try:
            datetime.fromisoformat(bucket_start)
        except (ValueError, TypeError):
            raise ValueError(
                f"bucket_start must be a valid ISO 8601 datetime string, got {bucket_start!r}"
            )

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO api_metrics_buckets (username, granularity, bucket_start, metric_type, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(username, granularity, bucket_start, metric_type)
                DO UPDATE SET count = count + 1
                """,
                (username, granularity, bucket_start, metric_type),
            )

        self._conn_manager.execute_atomic(operation)

    def cleanup_expired_buckets(self) -> None:
        """Delete expired bucket rows per granularity retention policy.

        Retention windows are defined by _RETENTION_WINDOWS:
            min1  — 15 minutes
            min5  — 1 hour
            hour1 — 24 hours
            day1  — 15 days
        """
        now = datetime.now(timezone.utc)

        def operation(conn: Any) -> None:
            for granularity, window in self._RETENTION_WINDOWS.items():
                cutoff = (now - window).isoformat()
                conn.execute(
                    "DELETE FROM api_metrics_buckets WHERE granularity = ? AND bucket_start < ?",
                    (granularity, cutoff),
                )

        self._conn_manager.execute_atomic(operation)

    def insert_metric(
        self,
        metric_type: str,
        timestamp: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single metric record.

        Args:
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
            timestamp: ISO 8601 timestamp. Uses current UTC time when None.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        now = (
            timestamp
            if timestamp is not None
            else datetime.now(timezone.utc).isoformat()
        )

        def operation(conn: Any) -> None:
            conn.execute(
                "INSERT INTO api_metrics (metric_type, timestamp, node_id) VALUES (?, ?, ?)",
                (metric_type, now, node_id),
            )

        self._conn_manager.execute_atomic(operation)

    def get_metrics(
        self,
        window_seconds: int = 3600,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric counts within the rolling window.

        Args:
            window_seconds: Time window in seconds (default 3600 = 1 hour).
            node_id: When provided, filter to metrics from this node only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()

        conn = self._conn_manager.get_connection()
        if node_id is not None:
            rows = conn.execute(
                """
                SELECT metric_type, COUNT(*) as count
                FROM api_metrics
                WHERE timestamp >= ? AND node_id = ?
                GROUP BY metric_type
                """,
                (cutoff, node_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT metric_type, COUNT(*) as count
                FROM api_metrics
                WHERE timestamp >= ?
                GROUP BY metric_type
                """,
                (cutoff,),
            ).fetchall()

        counts = {row[0]: row[1] for row in rows}
        return {
            "semantic_searches": counts.get("semantic", 0),
            "other_index_searches": counts.get("other_index", 0),
            "regex_searches": counts.get("regex", 0),
            "other_api_calls": counts.get("other_api", 0),
        }

    # Seconds in 24 hours — identifies the period that uses 2-hour grouping
    _PERIOD_24H_SECONDS: int = 86400

    # Hours per timeseries group for the 24h period (12 buckets total)
    _TIMESERIES_GROUP_HOURS: int = 2

    def _resolve_tier_and_cutoff(self, period_seconds: int) -> Tuple[str, str]:
        """Resolve granularity tier and ISO cutoff for a given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            (tier, cutoff_iso) where tier is the granularity string and
            cutoff_iso is the ISO 8601 lower bound for bucket_start queries.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        if period_seconds not in PERIOD_TO_TIER:
            raise ValueError(
                f"period_seconds {period_seconds!r} not in PERIOD_TO_TIER. "
                f"Valid values: {sorted(PERIOD_TO_TIER)}"
            )
        tier = PERIOD_TO_TIER[period_seconds]
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=period_seconds)
        ).isoformat()
        return tier, cutoff

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER, then
        sums counts from all bucket rows within the rolling window.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.

        Returns:
            Dict with keys: semantic, other_index, regex, other_api — each
            mapped to the integer sum of counts in the period.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()
        if username is not None:
            rows = conn.execute(
                """
                SELECT metric_type, SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ? AND username = ?
                GROUP BY metric_type
                """,
                (tier, cutoff, username),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT metric_type, SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ?
                GROUP BY metric_type
                """,
                (tier, cutoff),
            ).fetchall()

        counts = {row[0]: int(row[1]) for row in rows}
        return {
            "semantic": counts.get("semantic", 0),
            "other_index": counts.get("other_index", 0),
            "regex": counts.get("regex", 0),
            "other_api": counts.get("other_api", 0),
        }

    def get_metrics_by_user(
        self,
        period_seconds: int,
    ) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER, then
        groups by username and metric_type within the rolling window.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
            Example: {"alice": {"semantic": 5, "regex": 2}, "bob": {"semantic": 3}}

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()
        rows = conn.execute(
            """
            SELECT username, metric_type, SUM(count) AS total
            FROM api_metrics_buckets
            WHERE granularity = ? AND bucket_start >= ?
            GROUP BY username, metric_type
            ORDER BY username ASC, metric_type ASC
            """,
            (tier, cutoff),
        ).fetchall()

        result: Dict[str, Dict[str, int]] = {}
        for row_username, metric_type, total in rows:
            if row_username not in result:
                result[row_username] = {}
            result[row_username][metric_type] = int(total)
        return result

    def get_metrics_timeseries(
        self,
        period_seconds: int,
    ) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Maps period_seconds to a granularity tier via PERIOD_TO_TIER.
        For the 24h period (hour1 tier), buckets are grouped into 2-hour windows
        producing at most 12 data points. All other periods use raw bucket granularity.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) tuples ordered by
            bucket_start ASC. bucket_start is an ISO 8601 string.

        Raises:
            ValueError: If period_seconds is not in PERIOD_TO_TIER.
        """
        tier, cutoff = self._resolve_tier_and_cutoff(period_seconds)

        conn = self._conn_manager.get_connection()

        if period_seconds == self._PERIOD_24H_SECONDS:
            # Group hour1 buckets into _TIMESERIES_GROUP_HOURS-hour windows → max 12 buckets.
            # CAST integer division ensures correct floor: e.g. hour 3 → (3/2)*2 = 2.
            rows = conn.execute(
                """
                SELECT
                    strftime('%Y-%m-%dT', bucket_start) ||
                    printf('%02d',
                        CAST(CAST(strftime('%H', bucket_start) AS INTEGER) / ? AS INTEGER) * ?
                    ) || ':00:00' AS grouped_bucket,
                    metric_type,
                    SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ?
                GROUP BY grouped_bucket, metric_type
                ORDER BY grouped_bucket ASC, metric_type ASC
                """,
                (
                    self._TIMESERIES_GROUP_HOURS,
                    self._TIMESERIES_GROUP_HOURS,
                    tier,
                    cutoff,
                ),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT bucket_start, metric_type, SUM(count) AS total
                FROM api_metrics_buckets
                WHERE granularity = ? AND bucket_start >= ?
                GROUP BY bucket_start, metric_type
                ORDER BY bucket_start ASC, metric_type ASC
                """,
                (tier, cutoff),
            ).fetchall()

        return [(row[0], row[1], int(row[2])) for row in rows]

    def cleanup_old(self, max_age_seconds: int = 86400) -> int:
        """Delete metric records older than max_age_seconds.

        Args:
            max_age_seconds: Records older than this many seconds are deleted
                             (default 86400 = 24 hours).

        Returns:
            Number of rows deleted.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                "DELETE FROM api_metrics WHERE timestamp < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(
                "ApiMetricsSqliteBackend: cleaned up %d old metric records", deleted
            )
        return deleted

    def reset(self) -> None:
        """Delete all metric records (used for testing / manual resets)."""

        def operation(conn: Any) -> None:
            conn.execute("DELETE FROM api_metrics")

        self._conn_manager.execute_atomic(operation)

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass


class PayloadCacheSqliteBackend:
    """
    SQLite backend for payload cache storage (Story #504).

    Stores large content with TTL-based eviction, keyed by a unique cache handle.
    Uses a dedicated payload_cache.db file to isolate cache writes from other server state.

    Includes a node_id column for cluster support.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the payload_cache table if it does not exist.

        Args:
            db_path: Path to SQLite database file
                     (e.g. ~/.cidx-server/data/payload_cache.db).
        """
        import os

        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # Enable WAL mode outside any transaction (PRAGMA cannot run inside BEGIN).
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the payload_cache table if it does not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payload_cache (
                    cache_handle TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    node_id TEXT
                )
                """
            )

        self._conn_manager.execute_atomic(operation)

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry, replacing any existing entry with the same handle.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO payload_cache
                    (cache_handle, content, preview, created_at, ttl_seconds, node_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cache_handle, content, preview, now, ttl_seconds, node_id),
            )

        self._conn_manager.execute_atomic(operation)

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
            Dict with keys: content, preview, created_at, node_id — or None
            if the entry does not exist or has exceeded its TTL.
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            """
            SELECT cache_handle, content, preview, created_at, ttl_seconds, node_id
            FROM payload_cache
            WHERE cache_handle = ?
            """,
            (cache_handle,),
        ).fetchone()

        if row is None:
            return None

        created_at_str: str = row[3]
        ttl_secs: int = row[4]

        # Check TTL expiry
        try:
            created_at = datetime.fromisoformat(created_at_str)
            now = datetime.now(timezone.utc)
            # Ensure created_at is timezone-aware for comparison
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            elapsed = (now - created_at).total_seconds()
            if elapsed > ttl_secs:
                return None
        except (ValueError, TypeError):
            return None

        return {
            "content": row[1],
            "preview": row[2],
            "created_at": created_at_str,
            "node_id": row[5],
        }

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.

        Returns:
            Number of rows deleted.
        """
        # Use Unix epoch seconds for reliable comparison (SQLite strftime
        # cannot parse ISO timestamps with '+00:00' timezone offsets).
        now_epoch = int(datetime.now(timezone.utc).timestamp())

        def operation(conn: Any) -> int:
            # Strip timezone suffix from created_at so strftime can parse it,
            # then compare epoch seconds.
            cursor = conn.execute(
                """
                DELETE FROM payload_cache
                WHERE (? - strftime('%s', REPLACE(REPLACE(created_at, '+00:00', ''), 'Z', '')))
                      >= ttl_seconds
                """,
                (now_epoch,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(
                "PayloadCacheSqliteBackend: cleaned up %d expired cache entries",
                deleted,
            )
        return deleted

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""


class OAuthSqliteBackend:
    """
    SQLite backend for OAuth 2.1 storage.

    Satisfies the OAuthBackend Protocol (protocols.py).
    Replicates OAuthManager data operations as a standalone backend
    using DatabaseConnectionManager for thread-safe atomic operations.
    """

    ACCESS_TOKEN_LIFETIME_HOURS = 8
    REFRESH_TOKEN_LIFETIME_DAYS = 30
    HARD_EXPIRATION_DAYS = 30
    EXTENSION_THRESHOLD_HOURS = 4

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        import secrets as _secrets
        import hashlib as _hashlib
        import base64 as _base64

        self._secrets = _secrets
        self._hashlib = _hashlib
        self._base64 = _base64
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create all OAuth tables if they do not already exist."""

        def _do_init(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    access_token TEXT UNIQUE NOT NULL,
                    refresh_token TEXT UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    hard_expires_at TEXT NOT NULL,
                    FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokens_access ON oauth_tokens (access_token)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oidc_identity_links (
                    username TEXT NOT NULL PRIMARY KEY,
                    subject TEXT NOT NULL UNIQUE,
                    email TEXT,
                    linked_at TEXT NOT NULL,
                    last_login TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oidc_subject ON oidc_identity_links (subject)"
            )
            # Seed synthetic client_credentials row to satisfy FK constraint.
            conn.execute(
                """
                INSERT OR IGNORE INTO oauth_clients
                    (client_id, client_name, redirect_uris, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "client_credentials",
                    "System: Client Credentials Grant",
                    "[]",
                    "2000-01-01T00:00:00+00:00",
                    "{}",
                ),
            )

        self._conn_manager.execute_atomic(_do_init)

    def register_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: Optional[List[str]] = None,
        response_types: Optional[List[str]] = None,
        token_endpoint_auth_method: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a new OAuth client and return its registration data."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not client_name or client_name.strip() == "":
            raise OAuthError("client_name cannot be empty")
        client_id = self._secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
            "scope": scope,
        }

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    client_id,
                    client_name,
                    json.dumps(redirect_uris),
                    created_at,
                    json.dumps(metadata),
                ),
            )

        self._conn_manager.execute_atomic(_do_insert)
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "client_secret_expires_at": 0,
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "grant_types": grant_types or ["authorization_code", "refresh_token"],
            "response_types": response_types or ["code"],
        }

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a registered client by its client_id."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,))
        row = cursor.fetchone()
        if row:
            return {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "redirect_uris": json.loads(row["redirect_uris"]),
                "created_at": row["created_at"],
            }
        return None

    def generate_authorization_code(
        self,
        client_id: str,
        user_id: str,
        code_challenge: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        """Generate a one-time PKCE authorization code."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not code_challenge or code_challenge.strip() == "":
            raise OAuthError("code_challenge required")

        client = self.get_client(client_id)
        if not client:
            raise OAuthError(f"Invalid client_id: {client_id}")
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError(f"Invalid redirect_uri: {redirect_uri}")

        code = self._secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO oauth_codes (code, client_id, user_id, code_challenge, redirect_uri, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    code,
                    client_id,
                    user_id,
                    code_challenge,
                    redirect_uri,
                    expires_at.isoformat(),
                ),
            )

        self._conn_manager.execute_atomic(_do_insert)
        return code

    def exchange_code_for_token(
        self, code: str, code_verifier: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a PKCE authorization code for access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import (
            OAuthError,
            PKCEVerificationError,
        )

        token_id = self._secrets.token_urlsafe(32)
        access_token = self._secrets.token_urlsafe(48)
        refresh_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        result: Dict[str, Any] = {}

        def _do_exchange(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
            cursor.execute(
                "SELECT * FROM oauth_codes WHERE code = ? AND client_id = ?",
                (code, client_id),
            )
            code_row = cursor.fetchone()
            if not code_row:
                raise OAuthError("Invalid authorization code")
            if code_row["used"]:
                raise OAuthError("Authorization code already used")
            expires_at_dt = datetime.fromisoformat(code_row["expires_at"])
            if datetime.now(timezone.utc) > expires_at_dt:
                raise OAuthError("Authorization code expired")

            # PKCE verification
            code_challenge = code_row["code_challenge"]
            computed_challenge = (
                self._base64.urlsafe_b64encode(
                    self._hashlib.sha256(code_verifier.encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            if computed_challenge != code_challenge:
                raise PKCEVerificationError("PKCE verification failed")

            conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))

            token_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)

            conn.execute(
                """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                   expires_at, created_at, last_activity, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    code_row["client_id"],
                    code_row["user_id"],
                    access_token,
                    refresh_token,
                    token_expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    hard_expires_at.isoformat(),
                ),
            )
            result["access_token"] = access_token
            result["refresh_token"] = refresh_token

        self._conn_manager.execute_atomic(_do_exchange)

        return {
            "access_token": result["access_token"],
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
            "refresh_token": result["refresh_token"],
        }

    def validate_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Validate an access token and return its associated data."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return None
        return {
            "token_id": row["token_id"],
            "client_id": row["client_id"],
            "user_id": row["user_id"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def extend_token_on_activity(self, access_token: str) -> bool:
        """Extend an access token's expiry if it is within the extension threshold."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
        )
        row = cursor.fetchone()
        if not row:
            return False
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(row["expires_at"])
        hard_expires_at = datetime.fromisoformat(row["hard_expires_at"])
        remaining = (expires_at - now).total_seconds() / 3600
        if remaining >= self.EXTENSION_THRESHOLD_HOURS:
            return False
        new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
        if new_expires_at > hard_expires_at:
            new_expires_at = hard_expires_at

        def _do_extend(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE oauth_tokens SET expires_at = ?, last_activity = ? WHERE access_token = ?",
                (new_expires_at.isoformat(), now.isoformat(), access_token),
            )

        self._conn_manager.execute_atomic(_do_extend)
        return True

    def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a refresh token for new access and refresh tokens."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        new_access_token = self._secrets.token_urlsafe(48)
        new_refresh_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)

        def _do_refresh(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
            cursor.execute(
                "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
            )
            row = cursor.fetchone()
            if not row:
                raise OAuthError("Invalid refresh token")

            conn.execute(
                """UPDATE oauth_tokens
                   SET access_token = ?, refresh_token = ?, expires_at = ?, last_activity = ?
                   WHERE refresh_token = ?""",
                (
                    new_access_token,
                    new_refresh_token,
                    new_expires_at.isoformat(),
                    now.isoformat(),
                    refresh_token,
                ),
            )

        self._conn_manager.execute_atomic(_do_refresh)

        return {
            "access_token": new_access_token,
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
            "refresh_token": new_refresh_token,
        }

    def revoke_token(
        self, token: str, token_type_hint: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """Revoke an access or refresh token."""
        result: Dict[str, Optional[str]] = {"username": None, "token_type": None}

        def _do_revoke(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.row_factory = sqlite3.Row  # type: ignore[assignment]

            if token_type_hint == "access_token":
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE access_token = ?", (token,)
                )
            elif token_type_hint == "refresh_token":
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (token,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM oauth_tokens WHERE access_token = ? OR refresh_token = ?",
                    (token, token),
                )

            row = cursor.fetchone()
            if not row:
                return

            token_id = row["token_id"]
            user_id = row["user_id"]
            access_token_val = row["access_token"]

            cursor.execute("DELETE FROM oauth_tokens WHERE token_id = ?", (token_id,))

            determined_type = (
                "access_token" if access_token_val == token else "refresh_token"
            )
            result["username"] = user_id
            result["token_type"] = determined_type

        self._conn_manager.execute_atomic(_do_revoke)

        return result

    def handle_client_credentials_grant(
        self,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        mcp_credential_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Handle OAuth 2.1 client_credentials grant type."""
        from code_indexer.server.auth.oauth.oauth_manager import OAuthError

        if not client_id or not client_secret:
            raise OAuthError("client_id and client_secret required")

        if not mcp_credential_manager:
            raise OAuthError("MCPCredentialManager not available")

        user_id = mcp_credential_manager.verify_credential(client_id, client_secret)
        if not user_id:
            raise OAuthError("Invalid client credentials")

        token_id = self._secrets.token_urlsafe(32)
        access_token = self._secrets.token_urlsafe(48)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.ACCESS_TOKEN_LIFETIME_HOURS)
        hard_expires_at = now + timedelta(days=self.HARD_EXPIRATION_DAYS)

        def _do_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO oauth_tokens (token_id, client_id, user_id, access_token, refresh_token,
                   expires_at, created_at, last_activity, hard_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    "client_credentials",
                    user_id,
                    access_token,
                    None,
                    expires_at.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    hard_expires_at.isoformat(),
                ),
            )

        self._conn_manager.execute_atomic(_do_insert)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(self.ACCESS_TOKEN_LIFETIME_HOURS * 3600),
        }

    def link_oidc_identity(
        self, username: str, subject: str, email: Optional[str] = None
    ) -> None:
        """Link an OIDC subject to a local username."""
        now = datetime.now(timezone.utc).isoformat()

        def _do_link(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO oidc_identity_links (username, subject, email, linked_at, last_login)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, subject, email, now, now),
            )

        self._conn_manager.execute_atomic(_do_link)

    def get_oidc_identity(self, subject: str) -> Optional[Dict[str, Any]]:
        """Retrieve an OIDC identity link by subject."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT * FROM oidc_identity_links WHERE subject = ?", (subject,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "username": row["username"],
                "subject": row["subject"],
                "email": row["email"],
            }
        return None

    def delete_oidc_identity(self, subject: str) -> None:
        """Delete a stale OIDC identity link by subject."""

        def _do_delete(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM oidc_identity_links WHERE subject = ?", (subject,)
            )

        self._conn_manager.execute_atomic(_do_delete)

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass


class SCIPAuditSqliteBackend:
    """
    SQLite backend for SCIP dependency installation audit records (Story #516).

    Implements the SCIPAuditBackend Protocol.
    Adds node_id column to the original SCIPAuditRepository schema for
    cluster node identification.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (scip_audit.db).
        """
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the scip_dependency_installations table and indexes if they don't exist."""

        def _do_init(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scip_dependency_installations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    job_id VARCHAR(36) NOT NULL,
                    repo_alias VARCHAR(255) NOT NULL,
                    project_path VARCHAR(255),
                    project_language VARCHAR(50),
                    project_build_system VARCHAR(50),
                    package VARCHAR(255) NOT NULL,
                    command TEXT NOT NULL,
                    reasoning TEXT,
                    username VARCHAR(255),
                    node_id VARCHAR(255)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON scip_dependency_installations (timestamp)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_repo_alias
                ON scip_dependency_installations (repo_alias)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_job_id
                ON scip_dependency_installations (job_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_project_language
                ON scip_dependency_installations (project_language)
                """
            )

        self._conn_manager.execute_atomic(_do_init)

    def create_audit_record(
        self,
        job_id: str,
        repo_alias: str,
        package: str,
        command: str,
        project_path: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        reasoning: Optional[str] = None,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> int:
        """Create an audit record for a dependency installation.

        Args:
            job_id: Background job ID that triggered installation.
            repo_alias: Repository alias being processed.
            package: Package name that was installed.
            command: Full installation command executed.
            project_path: Project path within repository (optional).
            project_language: Programming language (optional).
            project_build_system: Build system used (optional).
            reasoning: Claude's reasoning for installation (optional).
            username: User who triggered the job (optional).
            node_id: Cluster node identifier (optional, Story #516 AC1).

        Returns:
            Record ID of created audit record.
        """
        result: Dict[str, Any] = {}

        def _do_insert(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                INSERT INTO scip_dependency_installations
                (job_id, repo_alias, project_path, project_language,
                 project_build_system, package, command, reasoning, username, node_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    repo_alias,
                    project_path,
                    project_language,
                    project_build_system,
                    package,
                    command,
                    reasoning,
                    username,
                    node_id,
                ),
            )
            result["record_id"] = cursor.lastrowid

        self._conn_manager.execute_atomic(_do_insert)
        record_id = result.get("record_id")
        if record_id is None:
            raise RuntimeError("Failed to get record ID after INSERT")
        return record_id  # type: ignore[no-any-return]

    def query_audit_records(
        self,
        job_id: Optional[str] = None,
        repo_alias: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query audit records with filtering and pagination.

        Args:
            job_id: Filter by job ID (optional).
            repo_alias: Filter by repository alias (optional).
            project_language: Filter by project language (optional).
            project_build_system: Filter by build system (optional).
            since: Filter records after this ISO timestamp (optional).
            until: Filter records before this ISO timestamp (optional).
            limit: Maximum records to return (default 100).
            offset: Number of records to skip (default 0).

        Returns:
            Tuple of (records list, total count).
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]

        where_sql, params = self._build_where_clause(
            job_id=job_id,
            repo_alias=repo_alias,
            project_language=project_language,
            project_build_system=project_build_system,
            since=since,
            until=until,
        )

        count_sql = f"""
            SELECT COUNT(*) as total
            FROM scip_dependency_installations
            {where_sql}
        """
        cursor.execute(count_sql, params)
        total = cursor.fetchone()["total"]

        query_sql = f"""
            SELECT
                id, timestamp, job_id, repo_alias, project_path,
                project_language, project_build_system, package,
                command, reasoning, username, node_id
            FROM scip_dependency_installations
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query_sql, params + [limit, offset])
        records = [dict(row) for row in cursor.fetchall()]
        return records, total

    def _build_where_clause(
        self,
        job_id: Optional[str],
        repo_alias: Optional[str],
        project_language: Optional[str],
        project_build_system: Optional[str],
        since: Optional[str],
        until: Optional[str],
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and parameters for query filtering."""
        where_clauses = []
        params: List[Any] = []

        if job_id:
            where_clauses.append("job_id = ?")
            params.append(job_id)
        if repo_alias:
            where_clauses.append("repo_alias = ?")
            params.append(repo_alias)
        if project_language:
            where_clauses.append("project_language = ?")
            params.append(project_language)
        if project_build_system:
            where_clauses.append("project_build_system = ?")
            params.append(project_build_system)
        if since:
            where_clauses.append("timestamp >= ?")
            params.append(since)
        if until:
            where_clauses.append("timestamp <= ?")
            params.append(until)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        return where_sql, params

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass


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


class ResearchSessionsSqliteBackend:
    """
    SQLite backend for research sessions and messages storage (Story #522).

    Satisfies the ResearchSessionsBackend Protocol.
    Uses the main cidx_server.db (research tables already live there).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create research tables if they do not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claude_session_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES research_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_research_messages_session_id
                ON research_messages(session_id)
                """
            )

        self._conn_manager.execute_atomic(_op)

    def create_session(
        self,
        session_id: str,
        name: str,
        folder_path: str,
        claude_session_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """Insert a new research session record."""
        from datetime import datetime, timezone

        now = created_at or datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO research_sessions (id, name, folder_path, claude_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, name, folder_path, claude_session_id, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID, or None if not found."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
            "FROM research_sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions ordered by updated_at DESC."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, name, folder_path, claude_session_id, created_at, updated_at "
            "FROM research_sessions ORDER BY updated_at DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (CASCADE removes messages). Returns True if found."""
        result: Dict[str, Any] = {"found": False}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "DELETE FROM research_sessions WHERE id = ?", (session_id,)
            )
            result["found"] = cursor.rowcount > 0

        self._conn_manager.execute_atomic(_op)
        return bool(result["found"])

    def update_session_title(self, session_id: str, name: str) -> bool:
        """Update session name. Returns True if session was found and updated."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        result: Dict[str, Any] = {"found": False}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE research_sessions SET name = ?, updated_at = ? WHERE id = ?",
                (name, now, session_id),
            )
            result["found"] = cursor.rowcount > 0

        self._conn_manager.execute_atomic(_op)
        return bool(result["found"])

    def update_session_claude_id(self, session_id: str, claude_session_id: str) -> None:
        """Store the Claude CLI session ID for a research session."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE research_sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
                (claude_session_id, now, session_id),
            )

        self._conn_manager.execute_atomic(_op)

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a message and return the full message dict."""
        from datetime import datetime, timezone

        now = timestamp or datetime.now(timezone.utc).isoformat()
        result: Dict[str, Any] = {"message_id": None}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "INSERT INTO research_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            result["message_id"] = cursor.lastrowid

        self._conn_manager.execute_atomic(_op)

        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, session_id, role, content, created_at FROM research_messages WHERE id = ?",
            (result["message_id"],),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"add_message: inserted row id={result['message_id']} not found after INSERT"
            )
        return dict(row)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all messages for a session in insertion order."""
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            "SELECT id, session_id, role, content, created_at "
            "FROM research_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()


class DiagnosticsSqliteBackend:
    """
    SQLite backend for diagnostics results storage (Story #525).

    Satisfies the DiagnosticsBackend Protocol.
    Uses the main cidx_server.db (diagnostic_results table).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create diagnostic_results table if it does not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_results (
                    category TEXT PRIMARY KEY,
                    results_json TEXT NOT NULL,
                    run_at TEXT NOT NULL
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        """Persist (upsert) diagnostic results for a category."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO diagnostic_results (category, results_json, run_at) VALUES (?, ?, ?)",
                (category, results_json, run_at),
            )

        self._conn_manager.execute_atomic(_op)

    def load_all_results(self) -> List[Tuple[str, str, str]]:
        """Return all rows as list of (category, results_json, run_at) tuples."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT category, results_json, run_at FROM diagnostic_results"
        )
        return [(row[0], row[1], row[2]) for row in cursor.fetchall()]

    def load_category_results(self, category: str) -> Optional[Tuple[str, str]]:
        """Return (results_json, run_at) for a category, or None if absent."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT results_json, run_at FROM diagnostic_results WHERE category = ?",
            (category,),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()


class SelfMonitoringSqliteBackend:
    """
    SQLite backend for self-monitoring storage (Story #524).

    Satisfies the SelfMonitoringBackend Protocol.
    Uses the main cidx_server.db (self_monitoring_scans and
    self_monitoring_issues tables).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create self_monitoring tables if they do not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS self_monitoring_scans (
                    scan_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    log_id_start INTEGER NOT NULL,
                    log_id_end INTEGER,
                    completed_at TEXT,
                    issues_created INTEGER,
                    error_message TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS self_monitoring_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    github_issue_number INTEGER,
                    github_issue_url TEXT,
                    classification TEXT NOT NULL,
                    title TEXT NOT NULL,
                    error_codes TEXT,
                    fingerprint TEXT NOT NULL,
                    source_log_ids TEXT,
                    source_files TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        """Insert initial scan record with RUNNING status."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, started_at, "RUNNING", log_id_start, log_id_start, 0),
            )

        self._conn_manager.execute_atomic(_op)

    def get_last_scan_log_id(self) -> int:
        """Return log_id_end from most recent SUCCESS scan, or 0."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT log_id_end FROM self_monitoring_scans "
            "WHERE status = 'SUCCESS' AND log_id_end IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else 0  # type: ignore[no-any-return]

    def update_scan_record(
        self,
        scan_id: str,
        status: str,
        completed_at: str,
        log_id_end: Optional[int] = None,
        issues_created: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update scan record with completion status and metrics."""
        update_fields = ["status = ?", "completed_at = ?"]
        update_values: List[Any] = [status, completed_at]

        if log_id_end is not None:
            update_fields.append("log_id_end = ?")
            update_values.append(log_id_end)
        if issues_created is not None:
            update_fields.append("issues_created = ?")
            update_values.append(issues_created)
        if error_message is not None:
            update_fields.append("error_message = ?")
            update_values.append(error_message)

        update_values.append(scan_id)
        query = f"UPDATE self_monitoring_scans SET {', '.join(update_fields)} WHERE scan_id = ?"

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(query, update_values)

        self._conn_manager.execute_atomic(_op)

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        """Mark scans started before cutoff_iso with no completed_at as FAILURE.

        Returns count of scans updated.
        """
        result: Dict[str, Any] = {"count": 0}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE self_monitoring_scans SET status = 'FAILURE', error_message = 'Orphaned scan' "
                "WHERE started_at < ? AND completed_at IS NULL",
                (cutoff_iso,),
            )
            result["count"] = cursor.rowcount

        self._conn_manager.execute_atomic(_op)
        return result["count"]  # type: ignore[no-any-return]

    def get_last_started_at(self) -> Optional[str]:
        """Return started_at from most recent scan (any status), or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT started_at FROM self_monitoring_scans ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> List[Tuple[str, str, str, str, str]]:
        """Return fingerprint rows (fingerprint, classification, error_codes, title, created_at)."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT fingerprint, classification, error_codes, title, created_at "
            "FROM self_monitoring_issues "
            "WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days') "
            "ORDER BY created_at DESC",
            (retention_days,),
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in cursor.fetchall()]

    def store_issue_metadata(
        self,
        scan_id: str,
        github_issue_number: Optional[int],
        github_issue_url: Optional[str],
        classification: str,
        title: str,
        error_codes: str,
        fingerprint: str,
        source_log_ids: str,
        source_files: str,
        created_at: str,
    ) -> None:
        """Persist issue metadata in self_monitoring_issues."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    github_issue_number,
                    github_issue_url,
                    classification,
                    error_codes,
                    fingerprint,
                    source_log_ids,
                    source_files,
                    title,
                    created_at,
                ),
            )

        self._conn_manager.execute_atomic(_op)

    def list_scans(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return scan history records, most recent first."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """
            SELECT scan_id, started_at, completed_at, status,
                   log_id_start, log_id_end, issues_created, error_message
            FROM self_monitoring_scans
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [
            "scan_id",
            "started_at",
            "completed_at",
            "status",
            "log_id_start",
            "log_id_end",
            "issues_created",
            "error_message",
        ]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def list_issues(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return issue records, most recent first."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """
            SELECT id, scan_id, github_issue_number, github_issue_url,
                   classification, title, fingerprint,
                   source_log_ids, source_files, created_at
            FROM self_monitoring_issues
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [
            "id",
            "scan_id",
            "github_issue_number",
            "github_issue_url",
            "classification",
            "title",
            "fingerprint",
            "source_log_ids",
            "source_files",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_running_scan_count(self) -> int:
        """Return count of scans where completed_at IS NULL."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM self_monitoring_scans WHERE completed_at IS NULL"
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()


class WikiCacheSqliteBackend:
    """
    SQLite backend for wiki cache storage (Story #523).

    Satisfies the WikiCacheBackend Protocol.
    Uses the main cidx_server.db (wiki_cache, wiki_sidebar_cache,
    wiki_article_views tables).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create wiki tables if they do not already exist, with migration."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_cache (
                    repo_alias TEXT NOT NULL,
                    article_path TEXT NOT NULL,
                    rendered_html TEXT NOT NULL,
                    title TEXT NOT NULL,
                    file_mtime REAL NOT NULL,
                    file_size INTEGER NOT NULL,
                    rendered_at TEXT NOT NULL,
                    metadata_json TEXT,
                    PRIMARY KEY (repo_alias, article_path)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
                    repo_alias TEXT PRIMARY KEY,
                    sidebar_json TEXT NOT NULL,
                    max_mtime REAL NOT NULL,
                    built_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_article_views (
                    repo_alias TEXT NOT NULL,
                    article_path TEXT NOT NULL,
                    real_views INTEGER DEFAULT 0,
                    first_viewed_at TIMESTAMP,
                    last_viewed_at TIMESTAMP,
                    PRIMARY KEY (repo_alias, article_path)
                )
                """
            )
            # Migration: add metadata_json column if it does not exist
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(wiki_cache)").fetchall()
            }
            if "metadata_json" not in existing_cols:
                conn.execute("ALTER TABLE wiki_cache ADD COLUMN metadata_json TEXT")

        self._conn_manager.execute_atomic(_op)

    def get_article(
        self, repo_alias: str, article_path: str
    ) -> Optional[Dict[str, Any]]:
        """Return dict with rendered_html, title, file_mtime, file_size, metadata_json or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT rendered_html, title, file_mtime, file_size, metadata_json "
            "FROM wiki_cache WHERE repo_alias = ? AND article_path = ?",
            (repo_alias, article_path),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "rendered_html": row[0],
            "title": row[1],
            "file_mtime": row[2],
            "file_size": row[3],
            "metadata_json": row[4],
        }

    def put_article(
        self,
        repo_alias: str,
        article_path: str,
        html: str,
        title: str,
        file_mtime: float,
        file_size: int,
        rendered_at: str,
        metadata_json: Optional[str],
    ) -> None:
        """Store (upsert) rendered article row."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_cache "
                "(repo_alias, article_path, rendered_html, title, file_mtime, file_size, rendered_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_alias,
                    article_path,
                    html,
                    title,
                    file_mtime,
                    file_size,
                    rendered_at,
                    metadata_json,
                ),
            )

        self._conn_manager.execute_atomic(_op)

    def get_sidebar(self, repo_alias: str) -> Optional[str]:
        """Return sidebar_json string for repo_alias, or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT sidebar_json FROM wiki_sidebar_cache WHERE repo_alias = ?",
            (repo_alias,),
        )
        row = cursor.fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def put_sidebar(
        self,
        repo_alias: str,
        sidebar_json: str,
        max_mtime: float,
        built_at: str,
    ) -> None:
        """Store (upsert) sidebar row."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO wiki_sidebar_cache "
                "(repo_alias, sidebar_json, max_mtime, built_at) VALUES (?, ?, ?, ?)",
                (repo_alias, sidebar_json, max_mtime, built_at),
            )

        self._conn_manager.execute_atomic(_op)

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache rows for repo_alias."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM wiki_cache WHERE repo_alias = ?", (repo_alias,))
            conn.execute(
                "DELETE FROM wiki_sidebar_cache WHERE repo_alias = ?", (repo_alias,)
            )

        self._conn_manager.execute_atomic(_op)

    def increment_view(self, repo_alias: str, article_path: str, now: str) -> None:
        """Upsert wiki_article_views, incrementing real_views."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(repo_alias, article_path) DO UPDATE SET
                    real_views = real_views + 1,
                    last_viewed_at = excluded.last_viewed_at
                """,
                (repo_alias, article_path, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return real_views count for article, or 0."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT real_views FROM wiki_article_views WHERE repo_alias = ? AND article_path = ?",
            (repo_alias, article_path),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_all_view_counts(self, repo_alias: str) -> List[Dict[str, Any]]:
        """Return all view records for repo as list of dicts."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT article_path, real_views, first_viewed_at, last_viewed_at "
            "FROM wiki_article_views WHERE repo_alias = ? ORDER BY real_views DESC",
            (repo_alias,),
        )
        return [
            {
                "article_path": row[0],
                "real_views": row[1],
                "first_viewed_at": row[2],
                "last_viewed_at": row[3],
            }
            for row in cursor.fetchall()
        ]

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views rows for repo_alias."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM wiki_article_views WHERE repo_alias = ?", (repo_alias,)
            )

        self._conn_manager.execute_atomic(_op)

    def insert_initial_views(
        self, repo_alias: str, article_path: str, views: int, now: str
    ) -> None:
        """Insert initial view count (INSERT OR IGNORE)."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR IGNORE INTO wiki_article_views
                    (repo_alias, article_path, real_views, first_viewed_at, last_viewed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_alias, article_path, views, now, now),
            )

        self._conn_manager.execute_atomic(_op)

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()


class MaintenanceSqliteBackend:
    """
    SQLite backend for maintenance mode state storage (Story #529).

    Satisfies the MaintenanceBackend Protocol.
    Uses the main cidx_server.db with a single-row maintenance_state table.

    In standalone (SQLite) mode the MaintenanceService reads this table so
    maintenance state survives server restarts. In cluster (PostgreSQL) mode
    the MaintenancePostgresBackend provides cross-node coordination.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create maintenance_state table if it does not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    started_at TEXT,
                    started_by TEXT
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def enter_maintenance(self, started_by: str, reason: str, started_at: str) -> None:
        """Persist maintenance mode as active (upsert single row).

        Args:
            started_by: Username or identifier of who activated maintenance mode.
            reason: Human-readable reason for entering maintenance mode.
            started_at: ISO 8601 timestamp when maintenance mode was activated.
        """

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO maintenance_state
                    (id, enabled, reason, started_at, started_by)
                VALUES (1, 1, ?, ?, ?)
                """,
                (reason, started_at, started_by),
            )

        self._conn_manager.execute_atomic(_op)

    def exit_maintenance(self) -> None:
        """Mark maintenance mode as inactive."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE maintenance_state SET enabled = 0 WHERE id = 1")

        self._conn_manager.execute_atomic(_op)

    def get_status(self) -> Dict[str, Any]:
        """Return current maintenance state dict.

        Returns:
            Dict with keys: enabled (bool), reason, started_at, started_by.
            enabled is False when no row exists or row has enabled=0.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT enabled, reason, started_at, started_by FROM maintenance_state WHERE id = 1"
        )
        row = cursor.fetchone()
        if row is None:
            return {
                "enabled": False,
                "reason": None,
                "started_at": None,
                "started_by": None,
            }
        return {
            "enabled": bool(row[0]),
            "reason": row[1],
            "started_at": row[2],
            "started_by": row[3],
        }

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
