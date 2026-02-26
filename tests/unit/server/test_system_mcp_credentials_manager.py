"""
Unit tests for Story #275: UserManager.get_system_mcp_credentials().

Tests are written FIRST following TDD methodology (red phase).
Tests both SQLite backend path and JSON file fallback path.
Zero mocking - uses real SQLite databases and real JSON files.
"""

import json
import sqlite3
from pathlib import Path


def _setup_db_with_admin_creds(db_path: Path) -> None:
    """Initialize a SQLite database with admin user having 2 MCP credentials."""
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
            "VALUES ('sys1', 'admin', 'cli_sys1', 'hash1', "
            "'mcp1', 'cidx-local-auto', '2024-01-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('sys2', 'admin', 'cli_sys2', 'hash2', "
            "'mcp2', 'cidx-server-auto', '2024-02-01T00:00:00Z')",
        )
        conn.execute(
            "INSERT INTO user_mcp_credentials "
            "(credential_id, username, client_id, client_secret_hash, "
            " client_id_prefix, name, created_at) "
            "VALUES ('usr1', 'alice', 'cli_usr1', 'hash3', "
            "'mcp3', 'Alice Key', '2024-01-15T00:00:00Z')",
        )
        conn.commit()
    finally:
        conn.close()


class TestUserManagerGetSystemMcpCredentials:
    """
    Tests for UserManager.get_system_mcp_credentials().

    Story #275 AC2: UserManager must expose system credentials with proper metadata.
    Tests both SQLite and JSON fallback paths.
    """

    def test_sqlite_path_returns_admin_credentials(self, tmp_path: Path) -> None:
        """SQLite path: returns 2 admin-owned credentials with correct metadata."""
        from code_indexer.server.auth.user_manager import UserManager

        db_path = tmp_path / "test_um_sqlite.db"
        _setup_db_with_admin_creds(db_path)

        manager = UserManager(
            users_file_path=str(tmp_path / "users.json"),
            use_sqlite=True,
            db_path=str(db_path),
        )
        result = manager.get_system_mcp_credentials()

        assert len(result) == 2
        for cred in result:
            assert cred.get("is_system") is True
            assert cred.get("owner") == "admin (system)"

    def test_sqlite_path_excludes_non_admin_credentials(self, tmp_path: Path) -> None:
        """SQLite path: alice's credential must not appear in result."""
        from code_indexer.server.auth.user_manager import UserManager

        db_path = tmp_path / "test_um_excl.db"
        _setup_db_with_admin_creds(db_path)

        manager = UserManager(
            users_file_path=str(tmp_path / "users.json"),
            use_sqlite=True,
            db_path=str(db_path),
        )
        result = manager.get_system_mcp_credentials()

        cred_ids = {c["credential_id"] for c in result}
        assert "usr1" not in cred_ids, "Non-admin credential must not appear in system creds"

    def test_sqlite_path_returns_empty_when_no_admin_creds(
        self, tmp_path: Path
    ) -> None:
        """SQLite path: returns empty list when admin has no MCP credentials."""
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.auth.user_manager import UserManager

        db_path = tmp_path / "test_um_empty.db"
        DatabaseSchema(str(db_path)).initialize_database()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('admin', 'hash', 'admin', '2024-01-01T00:00:00Z')",
            )
            conn.commit()
        finally:
            conn.close()

        manager = UserManager(
            users_file_path=str(tmp_path / "users.json"),
            use_sqlite=True,
            db_path=str(db_path),
        )
        assert manager.get_system_mcp_credentials() == []

    def test_json_fallback_returns_admin_credentials_with_system_flag(
        self, tmp_path: Path
    ) -> None:
        """JSON path: wraps get_mcp_credentials('admin') result with is_system=True."""
        from code_indexer.server.auth.user_manager import UserManager

        users_file = tmp_path / "users.json"
        users_data = {
            "admin": {
                "username": "admin",
                "password_hash": "hash",
                "role": "admin",
                "created_at": "2024-01-01T00:00:00Z",
                "mcp_credentials": [
                    {
                        "credential_id": "json_cred1",
                        "client_id": "client_json1",
                        "client_secret_hash": "hash_json1",
                        "client_id_prefix": "mcp_j1",
                        "name": "cidx-local-auto",
                        "created_at": "2024-01-01T00:00:00Z",
                        "last_used_at": None,
                    }
                ],
            }
        }
        users_file.write_text(json.dumps(users_data))

        manager = UserManager(users_file_path=str(users_file), use_sqlite=False)
        result = manager.get_system_mcp_credentials()

        assert len(result) == 1
        assert result[0]["credential_id"] == "json_cred1"
        assert result[0].get("is_system") is True
        assert result[0].get("owner") == "admin (system)"

    def test_json_fallback_returns_empty_when_no_admin_creds(
        self, tmp_path: Path
    ) -> None:
        """JSON path: returns empty list when admin has no MCP credentials."""
        from code_indexer.server.auth.user_manager import UserManager

        users_file = tmp_path / "users_empty.json"
        users_data = {
            "admin": {
                "username": "admin",
                "password_hash": "hash",
                "role": "admin",
                "created_at": "2024-01-01T00:00:00Z",
                "mcp_credentials": [],
            }
        }
        users_file.write_text(json.dumps(users_data))

        manager = UserManager(users_file_path=str(users_file), use_sqlite=False)
        assert manager.get_system_mcp_credentials() == []
