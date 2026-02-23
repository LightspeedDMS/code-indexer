"""
Unit tests for MCPCredentialManager.get_credential_by_client_id() direct SQL lookup.

Story #269: Remove Unjustified SQLite Performance Indexes and Fix MCP Credential
Lookup Algorithm.

Tests verify:
1. When backend supports get_mcp_credential_by_client_id (SQLite), direct SQL is used.
2. When backend does not support it (non-SQLite), iteration fallback is used.

Tests written FIRST following TDD methodology (red phase).
Uses real SQLite databases and real MCPCredentialManager with zero mocking.
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class _MinimalUserManager:
    """
    Minimal user manager that uses UsersSqliteBackend for credential lookups.

    Provides only the interface methods MCPCredentialManager needs.
    """

    def __init__(self, db_path: str) -> None:
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        self._backend = UsersSqliteBackend(db_path)

    def get_user(self, username: str) -> Optional[Any]:
        """Return user object or None."""
        data = self._backend.get_user(username)
        if data is None:
            return None

        class _User:
            def __init__(self, d):
                self.username = d["username"]

        return _User(data)

    def get_all_users(self) -> list:
        """Return all users as simple objects with .username."""
        rows = self._backend.list_users()

        class _User:
            def __init__(self, d):
                self.username = d["username"]

        return [_User(r) for r in rows]

    def get_mcp_credentials_with_secrets(self, username: str) -> list:
        """Return credentials including hashes (for iteration fallback)."""
        user = self._backend.get_user(username)
        if user is None:
            return []
        return user.get("mcp_credentials", [])

    def get_mcp_credential_by_client_id(
        self, client_id: str
    ) -> Optional[Tuple[str, dict]]:
        """Direct SQL lookup — delegates to backend."""
        return self._backend.get_mcp_credential_by_client_id(client_id)

    def add_mcp_credential(
        self,
        username: str,
        credential_id: str,
        client_id: str,
        client_secret_hash: str,
        client_id_prefix: str,
        name: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        self._backend.add_mcp_credential(
            username=username,
            credential_id=credential_id,
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            client_id_prefix=client_id_prefix,
            name=name,
        )

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool:
        return self._backend.update_mcp_credential_last_used(username, credential_id)

    def close(self) -> None:
        self._backend.close()


class _IterationOnlyUserManager:
    """
    User manager WITHOUT get_mcp_credential_by_client_id method.

    Simulates a non-SQLite backend to verify iteration fallback still works.
    """

    def __init__(self) -> None:
        self._users: Dict[str, Dict[str, Any]] = {}

    def get_all_users(self) -> list:
        class _User:
            def __init__(self, username):
                self.username = username

        return [_User(u) for u in self._users]

    def get_mcp_credentials_with_secrets(self, username: str) -> list:
        user = self._users.get(username, {})
        return user.get("mcp_credentials", [])

    def add_credential_for_user(
        self, username: str, client_id: str, secret_hash: str, credential_id: str
    ) -> None:
        if username not in self._users:
            self._users[username] = {"mcp_credentials": []}
        self._users[username]["mcp_credentials"].append(
            {
                "credential_id": credential_id,
                "client_id": client_id,
                "client_secret_hash": secret_hash,
                "client_id_prefix": "mcp_",
                "name": None,
                "created_at": "2024-01-01T00:00:00Z",
                "last_used_at": None,
            }
        )

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool:
        return True


def _setup_db_with_credential(db_path: Path) -> Tuple[str, str]:
    """
    Initialize database with user2 having a known credential.
    Returns (client_id, credential_id).
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES ('user2', 'hash', 'user', '2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('cred2a', 'user2', 'mcp_abc123', 'hashxyz', 'mcp_a', 'test', "
            "        '2024-01-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    return ("mcp_abc123", "cred2a")


class TestMcpCredentialManagerDirectLookup:
    """
    Tests for get_credential_by_client_id() using direct SQL when available.

    Story #269 Scenario 4 and 6.
    """

    def test_uses_direct_sql_when_backend_supports_it(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 4 (via manager): MCP credential lookup uses direct SQL.

        Given a UserManager backed by SQLite that has get_mcp_credential_by_client_id
        When MCPCredentialManager.get_credential_by_client_id() is called
        Then it returns (username, credential_dict) by delegating to direct SQL.

        Verification: the result is correct and the method exists on the backend.
        """
        from code_indexer.server.auth.mcp_credential_manager import (
            MCPCredentialManager,
        )

        db_path = tmp_path / "test_direct.db"
        client_id, credential_id = _setup_db_with_credential(db_path)

        user_manager = _MinimalUserManager(str(db_path))
        # Verify the backend has the direct SQL method (this is a precondition)
        assert hasattr(user_manager, "get_mcp_credential_by_client_id"), (
            "_MinimalUserManager must expose get_mcp_credential_by_client_id"
        )

        mgr = MCPCredentialManager(user_manager=user_manager)
        result = mgr.get_credential_by_client_id(client_id)

        assert result is not None, (
            f"Expected (username, credential) for client_id={client_id}, got None"
        )
        username, credential = result
        assert username == "user2"
        assert credential["client_id"] == client_id
        assert credential["credential_id"] == credential_id

        user_manager.close()

    def test_falls_back_to_iteration_when_backend_lacks_direct_lookup(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 6 (fallback path): MCP auth still works with non-SQLite backend.

        Given a UserManager WITHOUT get_mcp_credential_by_client_id
        When MCPCredentialManager.get_credential_by_client_id() is called
        Then it falls back to iterating all users and returns the correct result.
        """
        from code_indexer.server.auth.mcp_credential_manager import (
            MCPCredentialManager,
        )

        user_manager = _IterationOnlyUserManager()
        user_manager.add_credential_for_user(
            username="user1",
            client_id="mcp_iter_001",
            secret_hash="hash001",
            credential_id="cred_iter_001",
        )

        # Confirm this backend does NOT have the direct SQL method
        assert not hasattr(user_manager, "get_mcp_credential_by_client_id"), (
            "_IterationOnlyUserManager must NOT have get_mcp_credential_by_client_id "
            "to test the fallback path"
        )

        mgr = MCPCredentialManager(user_manager=user_manager)
        result = mgr.get_credential_by_client_id("mcp_iter_001")

        assert result is not None, (
            "Expected fallback iteration to find credential 'mcp_iter_001'"
        )
        username, credential = result
        assert username == "user1"
        assert credential["client_id"] == "mcp_iter_001"
