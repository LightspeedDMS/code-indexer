"""
Unit tests for storage Protocol interfaces - core storage backends (Story #410).

Verifies GlobalReposBackend, UsersBackend, and SessionsBackend Protocols.

Tests use runtime_checkable isinstance() checks.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Create and initialise a test SQLite database, return path string."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "test.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


# ---------------------------------------------------------------------------
# GlobalReposBackend
# ---------------------------------------------------------------------------


class TestGlobalReposBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """GlobalReposSqliteBackend must satisfy GlobalReposBackend protocol."""
        from code_indexer.server.storage.protocols import GlobalReposBackend
        from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend

        db_path = _make_db(tmp_path)
        backend = GlobalReposSqliteBackend(db_path)

        assert isinstance(backend, GlobalReposBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy GlobalReposBackend."""
        from code_indexer.server.storage.protocols import GlobalReposBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), GlobalReposBackend)

    def test_protocol_has_required_methods(self) -> None:
        """GlobalReposBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import GlobalReposBackend

        required = {
            "register_repo",
            "get_repo",
            "list_repos",
            "delete_repo",
            "update_last_refresh",
            "update_enable_temporal",
            "update_enable_scip",
            "update_next_refresh",
            "close",
        }
        protocol_attrs = set(
            m for m in dir(GlobalReposBackend) if not m.startswith("_")
        )
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# UsersBackend
# ---------------------------------------------------------------------------


class TestUsersBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """UsersSqliteBackend must satisfy UsersBackend protocol."""
        from code_indexer.server.storage.protocols import UsersBackend
        from code_indexer.server.storage.sqlite_backends import UsersSqliteBackend

        db_path = _make_db(tmp_path)
        backend = UsersSqliteBackend(db_path)

        assert isinstance(backend, UsersBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy UsersBackend."""
        from code_indexer.server.storage.protocols import UsersBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), UsersBackend)

    def test_protocol_has_required_methods(self) -> None:
        """UsersBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import UsersBackend

        required = {
            "create_user",
            "get_user",
            "list_users",
            "update_user",
            "delete_user",
            "update_user_role",
            "update_password_hash",
            "add_api_key",
            "delete_api_key",
            "add_mcp_credential",
            "delete_mcp_credential",
            "get_user_by_email",
            "set_oidc_identity",
            "remove_oidc_identity",
            "update_mcp_credential_last_used",
            "list_all_mcp_credentials",
            "get_system_mcp_credentials",
            "get_mcp_credential_by_client_id",
            "close",
        }
        protocol_attrs = set(m for m in dir(UsersBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# SessionsBackend
# ---------------------------------------------------------------------------


class TestSessionsBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """SessionsSqliteBackend must satisfy SessionsBackend protocol."""
        from code_indexer.server.storage.protocols import SessionsBackend
        from code_indexer.server.storage.sqlite_backends import SessionsSqliteBackend

        db_path = _make_db(tmp_path)
        backend = SessionsSqliteBackend(db_path)

        assert isinstance(backend, SessionsBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy SessionsBackend."""
        from code_indexer.server.storage.protocols import SessionsBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), SessionsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """SessionsBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import SessionsBackend

        required = {
            "invalidate_session",
            "is_session_invalidated",
            "clear_invalidated_sessions",
            "set_password_change_timestamp",
            "get_password_change_timestamp",
            "cleanup_old_data",
            "close",
        }
        protocol_attrs = set(m for m in dir(SessionsBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)
