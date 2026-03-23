"""
Unit tests for UsersPostgresBackend — auth/key management methods.

Story #411: PostgreSQL Backend for Users and Sessions

Tests: update_user_role, update_password_hash, add_api_key, delete_api_key.

All tests use mocked connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared helpers (duplicated from core to keep files independent)
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
    from code_indexer.server.storage.postgres.users_backend import UsersPostgresBackend

    if pool is None:
        pool, _, _ = _make_pool_and_conn()
    return UsersPostgresBackend(pool)


# ---------------------------------------------------------------------------
# update_user_role
# ---------------------------------------------------------------------------


class TestUpdateUserRole:
    """Tests for update_user_role method."""

    def test_returns_true_when_updated(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.update_user_role("alice", "admin") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.update_user_role("ghost", "admin") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.update_user_role("alice", "admin")

        sql, params = conn.execute.call_args[0]
        assert "UPDATE users" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "admin" in params
        assert "alice" not in sql
        assert "admin" not in sql


# ---------------------------------------------------------------------------
# update_password_hash
# ---------------------------------------------------------------------------


class TestUpdatePasswordHash:
    """Tests for update_password_hash method."""

    def test_returns_true_when_updated(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.update_password_hash("alice", "$2b$newhash") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.update_password_hash("ghost", "$2b$hash") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.update_password_hash("alice", "$2b$newhash")

        sql, params = conn.execute.call_args[0]
        assert "UPDATE users" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "$2b$newhash" in params


# ---------------------------------------------------------------------------
# add_api_key
# ---------------------------------------------------------------------------


class TestAddApiKey:
    """Tests for add_api_key method."""

    def test_inserts_into_user_api_keys(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.add_api_key(
            username="alice",
            key_id="key-uuid-1",
            key_hash="hashed-key",
            key_prefix="ck_",
            name="My Key",
        )

        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO user_api_keys" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "key-uuid-1" in params
        assert "hashed-key" in params
        assert "ck_" in params

    def test_name_optional_passes_none(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.add_api_key(
            username="alice",
            key_id="key-uuid-2",
            key_hash="hashed-key",
            key_prefix="ck_",
        )

        sql, params = conn.execute.call_args[0]
        assert None in params

    def test_includes_created_at_timestamp(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.add_api_key(
            username="alice",
            key_id="key-uuid-3",
            key_hash="hashed-key",
            key_prefix="ck_",
        )

        sql, params = conn.execute.call_args[0]
        # created_at should be in params as a non-None string
        non_none_strings = [p for p in params if isinstance(p, str)]
        assert any(
            "T" in s and ":" in s for s in non_none_strings
        ), "Expected ISO timestamp in params"


# ---------------------------------------------------------------------------
# delete_api_key
# ---------------------------------------------------------------------------


class TestDeleteApiKey:
    """Tests for delete_api_key method."""

    def test_returns_true_when_deleted(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        assert backend.delete_api_key("alice", "key-uuid-1") is True

    def test_returns_false_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 0
        backend = _make_backend(pool)

        assert backend.delete_api_key("alice", "nonexistent-key") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.rowcount = 1
        backend = _make_backend(pool)

        backend.delete_api_key("alice", "key-uuid-1")

        sql, params = conn.execute.call_args[0]
        assert "DELETE FROM user_api_keys" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "key-uuid-1" in params
        assert "alice" not in sql
        assert "key-uuid-1" not in sql
