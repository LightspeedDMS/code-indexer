"""
Unit tests for UsersPostgresBackend — MCP credentials, OIDC, email lookup.

Story #411: PostgreSQL Backend for Users and Sessions

Tests: add_mcp_credential, delete_mcp_credential,
       update_mcp_credential_last_used, list_all_mcp_credentials,
       get_system_mcp_credentials, get_mcp_credential_by_client_id,
       get_user_by_email, set_oidc_identity, remove_oidc_identity.

All tests use mocked connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
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
    from code_indexer.server.storage.postgres.users_backend import UsersPostgresBackend

    if pool is None:
        pool, _, _ = _make_pool_and_conn()
    return UsersPostgresBackend(pool)


# ---------------------------------------------------------------------------
# add_mcp_credential
# ---------------------------------------------------------------------------


class TestAddMcpCredential:
    def test_inserts_into_user_mcp_credentials(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.add_mcp_credential(
            username="alice",
            credential_id="cred-uuid-1",
            client_id="client-id-1",
            client_secret_hash="secret-hash",
            client_id_prefix="cx_",
            name="My Credential",
        )

        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO user_mcp_credentials" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "cred-uuid-1" in params
        assert "client-id-1" in params
        assert "secret-hash" in params

    def test_name_optional_passes_none(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.add_mcp_credential(
            username="alice",
            credential_id="cred-uuid-2",
            client_id="client-id-2",
            client_secret_hash="secret-hash",
            client_id_prefix="cx_",
        )

        sql, params = conn.execute.call_args[0]
        assert None in params


# ---------------------------------------------------------------------------
# delete_mcp_credential
# ---------------------------------------------------------------------------


class TestDeleteMcpCredential:
    def test_returns_true_when_deleted(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.delete_mcp_credential("alice", "cred-uuid-1") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.delete_mcp_credential("alice", "nonexistent") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.delete_mcp_credential("alice", "cred-uuid-1")

        sql, params = conn.execute.call_args[0]
        assert "DELETE FROM user_mcp_credentials" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "cred-uuid-1" in params
        assert "alice" not in sql
        assert "cred-uuid-1" not in sql


# ---------------------------------------------------------------------------
# update_mcp_credential_last_used
# ---------------------------------------------------------------------------


class TestUpdateMcpCredentialLastUsed:
    def test_returns_true_when_updated(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.update_mcp_credential_last_used("alice", "cred-uuid-1") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.update_mcp_credential_last_used("alice", "nonexistent") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.update_mcp_credential_last_used("alice", "cred-uuid-1")

        sql, params = conn.execute.call_args[0]
        assert "UPDATE user_mcp_credentials" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "cred-uuid-1" in params


# ---------------------------------------------------------------------------
# list_all_mcp_credentials
# ---------------------------------------------------------------------------


class TestListAllMcpCredentials:
    def test_returns_empty_list_when_none(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.list_all_mcp_credentials() == []

    def test_returns_list_of_credential_dicts(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = [
            (
                "alice",
                "cred-1",
                "client-id-1",
                "cx_",
                "Key 1",
                "2024-01-01T00:00:00+00:00",
                None,
            ),
        ]
        backend = _make_backend(pool)

        result = backend.list_all_mcp_credentials()

        assert len(result) == 1
        assert result[0]["username"] == "alice"
        assert result[0]["credential_id"] == "cred-1"
        assert result[0]["client_id"] == "client-id-1"

    def test_accepts_limit_and_offset_as_params(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        backend.list_all_mcp_credentials(limit=10, offset=20)

        sql, params = conn.execute.call_args[0]
        assert 10 in params
        assert 20 in params


# ---------------------------------------------------------------------------
# get_system_mcp_credentials
# ---------------------------------------------------------------------------


class TestGetSystemMcpCredentials:
    def test_returns_empty_list_when_none(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.get_system_mcp_credentials() == []

    def test_returned_dicts_have_is_system_true(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = [
            (
                "cred-1",
                "client-id-1",
                "cx_",
                "Sys Key",
                "2024-01-01T00:00:00+00:00",
                None,
            ),
        ]
        backend = _make_backend(pool)

        result = backend.get_system_mcp_credentials()

        assert len(result) == 1
        assert result[0]["is_system"] is True
        assert result[0]["owner"] == "admin (system)"


# ---------------------------------------------------------------------------
# get_mcp_credential_by_client_id
# ---------------------------------------------------------------------------


class TestGetMcpCredentialByClientId:
    def test_returns_none_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        assert backend.get_mcp_credential_by_client_id("nonexistent") is None

    def test_returns_tuple_of_username_and_credential_dict(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (
            "alice",
            "cred-1",
            "client-id-1",
            "secret-hash",
            "cx_",
            "My Key",
            "2024-01-01T00:00:00+00:00",
            None,
        )
        backend = _make_backend(pool)

        result = backend.get_mcp_credential_by_client_id("client-id-1")

        assert result is not None
        username, cred = result
        assert username == "alice"
        assert cred["credential_id"] == "cred-1"
        assert cred["client_id"] == "client-id-1"
        assert cred["client_secret_hash"] == "secret-hash"

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        backend.get_mcp_credential_by_client_id("some-client-id")

        sql, params = conn.execute.call_args[0]
        assert "%s" in sql
        assert "some-client-id" in params
        assert "some-client-id" not in sql


# ---------------------------------------------------------------------------
# get_user_by_email
# ---------------------------------------------------------------------------


class TestGetUserByEmail:
    def test_returns_none_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        assert backend.get_user_by_email("nobody@example.com") is None

    def test_uses_case_insensitive_search(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        backend.get_user_by_email("Alice@Example.Com")

        sql, params = conn.execute.call_args_list[0][0]
        assert "LOWER" in sql.upper() or "ILIKE" in sql.upper()

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        backend.get_user_by_email("alice@example.com")

        sql, params = conn.execute.call_args_list[0][0]
        assert "%s" in sql

    def test_returns_user_dict_with_expected_keys(self) -> None:
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

        result = backend.get_user_by_email("alice@example.com")

        assert result is not None
        assert result["username"] == "alice"
        assert "api_keys" in result
        assert "mcp_credentials" in result


# ---------------------------------------------------------------------------
# set_oidc_identity / remove_oidc_identity
# ---------------------------------------------------------------------------


class TestOidcIdentity:
    def test_set_oidc_identity_serializes_dict_to_json(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        identity = {"sub": "12345", "email": "alice@provider.com"}
        backend.set_oidc_identity("alice", identity)

        sql, params = conn.execute.call_args[0]
        assert "UPDATE users" in sql
        json_in_params = any(isinstance(p, str) and '"sub"' in p for p in params)
        assert json_in_params

    def test_set_oidc_identity_returns_true_on_success(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.set_oidc_identity("alice", {"sub": "123"}) is True

    def test_set_oidc_identity_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.set_oidc_identity("ghost", {"sub": "123"}) is False

    def test_remove_oidc_identity_sets_null(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.remove_oidc_identity("alice")

        sql, params = conn.execute.call_args[0]
        assert "oidc_identity" in sql
        assert "NULL" in sql or "= %s" in sql

    def test_remove_oidc_identity_returns_true_on_success(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.remove_oidc_identity("alice") is True

    def test_remove_oidc_identity_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.remove_oidc_identity("ghost") is False
