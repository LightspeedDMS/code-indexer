"""
Unit tests for UsersPostgresBackend — core CRUD methods.

Story #411: PostgreSQL Backend for Users and Sessions

Tests: Protocol conformance, create_user, get_user, list_users,
       update_user, delete_user.

All tests use mocked connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return (mock_pool, mock_conn, mock_cursor) wired together."""
    mock_cursor = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return mock_pool, mock_conn, mock_cursor


def _make_backend(pool=None):
    """Create a UsersPostgresBackend with an optional mock pool."""
    from code_indexer.server.storage.postgres.users_backend import UsersPostgresBackend

    if pool is None:
        pool, _, _ = _make_pool_and_conn()
    return UsersPostgresBackend(pool)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestUsersPostgresProtocolConformance:
    """Verify UsersPostgresBackend satisfies the UsersBackend Protocol."""

    def test_isinstance_check_passes(self) -> None:
        from code_indexer.server.storage.protocols import UsersBackend

        backend = _make_backend()
        assert isinstance(backend, UsersBackend)

    def test_all_protocol_methods_exist(self) -> None:
        required_methods = [
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
        ]
        backend = _make_backend()
        for method_name in required_methods:
            assert hasattr(backend, method_name), f"Missing method: {method_name}"
            assert callable(getattr(backend, method_name)), (
                f"Not callable: {method_name}"
            )


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    """Tests for create_user method."""

    def test_executes_insert_into_users(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.create_user(username="alice", password_hash="$2b$hash", role="user")

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO users" in sql
        assert "alice" in params
        assert "$2b$hash" in params
        assert "user" in params

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.create_user(
            username="bob'; DROP TABLE users; --",
            password_hash="hash",
            role="admin",
        )

        sql, params = conn.execute.call_args[0]
        assert "DROP TABLE" not in sql
        assert "%s" in sql

    def test_without_email_passes_none(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.create_user(username="carol", password_hash="hash", role="viewer")

        sql, params = conn.execute.call_args[0]
        assert None in params

    def test_accepts_custom_created_at(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        custom_ts = "2024-01-15T10:00:00+00:00"
        backend.create_user(
            username="dave", password_hash="hash", role="user", created_at=custom_ts
        )

        sql, params = conn.execute.call_args[0]
        assert custom_ts in params


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


class TestGetUser:
    """Tests for get_user method."""

    def test_returns_none_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.get_user("nonexistent") is None

    def test_returns_dict_with_expected_keys(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "alice",
            "$2b$hash",
            "user",
            "alice@example.com",
            "2024-01-01T00:00:00+00:00",
            None,
            None,
        )
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        result = backend.get_user("alice")

        assert result is not None
        assert result["username"] == "alice"
        assert result["password_hash"] == "$2b$hash"
        assert result["role"] == "user"
        assert result["email"] == "alice@example.com"
        assert result["oidc_identity"] is None
        assert "api_keys" in result
        assert "mcp_credentials" in result

    def test_parses_oidc_identity_json(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        oidc_data = {"sub": "12345", "email": "alice@provider.com"}
        cursor.fetchone.return_value = (
            "alice",
            "hash",
            "user",
            None,
            "2024-01-01T00:00:00+00:00",
            json.dumps(oidc_data),
            None,
        )
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        result = backend.get_user("alice")

        assert result is not None
        assert result["oidc_identity"] == oidc_data

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        backend.get_user("alice")

        sql, params = conn.execute.call_args_list[0][0]
        assert "%s" in sql
        assert "alice" in params
        assert "alice" not in sql


# ---------------------------------------------------------------------------
# list_users
# ---------------------------------------------------------------------------


class TestListUsers:
    """Tests for list_users method."""

    def test_returns_empty_list_when_no_users(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.list_users() == []

    def test_returns_all_user_dicts(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        rows = [
            ("alice", "hash1", "admin", None, "2024-01-01T00:00:00+00:00", None, None),
            (
                "bob",
                "hash2",
                "user",
                "bob@example.com",
                "2024-01-02T00:00:00+00:00",
                None,
                None,
            ),
        ]
        cursor.fetchall.side_effect = [rows, [], [], [], []]
        backend = _make_backend(pool)

        result = backend.list_users()

        assert len(result) == 2
        usernames = [u["username"] for u in result]
        assert "alice" in usernames
        assert "bob" in usernames


# ---------------------------------------------------------------------------
# update_user
# ---------------------------------------------------------------------------


class TestUpdateUser:
    """Tests for update_user method."""

    def test_returns_false_when_user_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.update_user("nonexistent", email="x@y.com") is False

    def test_returns_true_on_success(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "alice",
            "hash",
            "user",
            None,
            "2024-01-01T00:00:00+00:00",
            None,
            None,
        )
        cursor.fetchall.return_value = []
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.update_user("alice", email="new@example.com") is True

    def test_email_update_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "alice",
            "hash",
            "user",
            None,
            "2024-01-01T00:00:00+00:00",
            None,
            None,
        )
        cursor.fetchall.return_value = []
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.update_user("alice", email="new@example.com")

        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE users" in str(c)
        ]
        assert len(update_calls) >= 1
        sql, params = update_calls[0][0]
        assert "%s" in sql
        assert "new@example.com" in params


# ---------------------------------------------------------------------------
# delete_user
# ---------------------------------------------------------------------------


class TestDeleteUser:
    """Tests for delete_user method."""

    def test_returns_true_when_deleted(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.delete_user("alice") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.delete_user("ghost") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.delete_user("alice")

        sql, params = conn.execute.call_args[0]
        assert "DELETE FROM users" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "alice" not in sql


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    """Tests for close method."""

    def test_close_calls_pool_close(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.close()

        pool.close.assert_called_once()
