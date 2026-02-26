"""
Unit tests for Story #275: SQLite backend - get_system_mcp_credentials().

Tests are written FIRST following TDD methodology (red phase).
Uses real in-memory SQLite databases with zero mocking.
"""

import sqlite3
from pathlib import Path


def _setup_db_with_admin_and_user_credentials(db_path: Path) -> None:
    """
    Helper: Initialize database with admin (2 creds) and alice (1 cred).

    admin: sys_cred1 (cidx-local-auto, 2024-01-01), sys_cred2 (cidx-server-auto, 2024-02-01)
    alice: user_cred1 (Alice Personal, 2024-01-15)
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(db_path)).initialize_database()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES ('admin', 'admin_hash', 'admin', '2024-01-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES ('alice', 'alice_hash', 'user', '2024-01-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('sys_cred1', 'admin', 'client_sys1', 'hash_sys1', "
            "'mcp_sys1', 'cidx-local-auto', '2024-01-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('sys_cred2', 'admin', 'client_sys2', 'hash_sys2', "
            "'mcp_sys2', 'cidx-server-auto', '2024-02-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('user_cred1', 'alice', 'client_usr1', 'hash_usr1', "
            "'mcp_u1', 'Alice Personal', '2024-01-15T00:00:00Z')",
        )
        conn.commit()
    finally:
        conn.close()


class TestUsersSqliteBackendGetSystemMcpCredentials:
    """
    Tests for UsersSqliteBackend.get_system_mcp_credentials().

    Story #275 AC1/AC2: Backend must return credentials owned by 'admin' user
    with is_system=True and owner='admin (system)'.
    """

    def test_returns_only_admin_owned_credentials(self, tmp_path: Path) -> None:
        """admin has 2 creds, alice has 1 - only admin's 2 are returned."""
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_system_creds.db"
        _setup_db_with_admin_and_user_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_system_mcp_credentials()

        assert len(result) == 2, f"Expected 2 admin credentials, got {len(result)}"
        credential_ids = {r["credential_id"] for r in result}
        assert credential_ids == {"sys_cred1", "sys_cred2"}
        assert "user_cred1" not in credential_ids

        backend.close()

    def test_returned_credentials_have_is_system_true(self, tmp_path: Path) -> None:
        """All returned credentials must have is_system=True."""
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_is_system.db"
        _setup_db_with_admin_and_user_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_system_mcp_credentials()

        assert len(result) > 0
        for cred in result:
            assert cred.get("is_system") is True, (
                f"Expected is_system=True on {cred.get('credential_id')}, "
                f"got {cred.get('is_system')}"
            )

        backend.close()

    def test_returned_credentials_have_owner_admin_system(self, tmp_path: Path) -> None:
        """All returned credentials must have owner='admin (system)'."""
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_owner.db"
        _setup_db_with_admin_and_user_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_system_mcp_credentials()

        assert len(result) > 0
        for cred in result:
            assert cred.get("owner") == "admin (system)", (
                f"Expected owner='admin (system)', got '{cred.get('owner')}'"
            )

        backend.close()

    def test_returns_expected_fields(self, tmp_path: Path) -> None:
        """Each record must have all required fields."""
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_fields.db"
        _setup_db_with_admin_and_user_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_system_mcp_credentials()

        assert len(result) > 0
        required_fields = {
            "credential_id", "client_id", "client_id_prefix",
            "name", "created_at", "last_used_at", "owner", "is_system",
        }
        missing = required_fields - set(result[0].keys())
        assert not missing, f"Missing fields: {missing}"

        backend.close()

    def test_returns_empty_list_when_no_admin_credentials(self, tmp_path: Path) -> None:
        """Returns empty list when only non-admin credentials exist."""
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_empty.db"
        DatabaseSchema(str(db_path)).initialize_database()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('alice', 'alice_hash', 'user', '2024-01-01T00:00:00Z')",
            )
            conn.execute(
                "INSERT INTO user_mcp_credentials "
                "(credential_id, username, client_id, client_secret_hash, "
                " client_id_prefix, name, created_at) "
                "VALUES ('u1', 'alice', 'cli1', 'h1', 'pre1', 'Alice', "
                "'2024-01-01T00:00:00Z')",
            )
            conn.commit()
        finally:
            conn.close()

        backend = UsersSqliteBackend(str(db_path))
        assert backend.get_system_mcp_credentials() == []
        backend.close()

    def test_ordered_by_created_at_descending(self, tmp_path: Path) -> None:
        """Newest credential appears first (ORDER BY created_at DESC)."""
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = tmp_path / "test_order.db"
        _setup_db_with_admin_and_user_credentials(db_path)

        backend = UsersSqliteBackend(str(db_path))
        result = backend.get_system_mcp_credentials()

        assert len(result) == 2
        # sys_cred2 created 2024-02-01 > sys_cred1 created 2024-01-01
        assert result[0]["credential_id"] == "sys_cred2", (
            f"Expected sys_cred2 first (newest), got {result[0]['credential_id']}"
        )

        backend.close()
