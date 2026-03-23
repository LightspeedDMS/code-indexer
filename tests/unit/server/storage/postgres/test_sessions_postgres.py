"""
Unit tests for SessionsPostgresBackend.

Story #411: PostgreSQL Backend for Users and Sessions

Tests: Protocol conformance, invalidate_session, is_session_invalidated,
       clear_invalidated_sessions, set_password_change_timestamp,
       get_password_change_timestamp, cleanup_old_data, close.

All tests use mocked connection pool — no real PostgreSQL required.
"""

from __future__ import annotations

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
    from code_indexer.server.storage.postgres.sessions_backend import (
        SessionsPostgresBackend,
    )

    if pool is None:
        pool, _, _ = _make_pool_and_conn()
    return SessionsPostgresBackend(pool)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestSessionsPostgresProtocolConformance:
    """Verify SessionsPostgresBackend satisfies the SessionsBackend Protocol."""

    def test_isinstance_check_passes(self) -> None:
        from code_indexer.server.storage.protocols import SessionsBackend

        backend = _make_backend()
        assert isinstance(backend, SessionsBackend)

    def test_all_protocol_methods_exist(self) -> None:
        required_methods = [
            "invalidate_session",
            "is_session_invalidated",
            "clear_invalidated_sessions",
            "set_password_change_timestamp",
            "get_password_change_timestamp",
            "cleanup_old_data",
            "close",
        ]
        backend = _make_backend()
        for method_name in required_methods:
            assert hasattr(backend, method_name), f"Missing method: {method_name}"
            assert callable(
                getattr(backend, method_name)
            ), f"Not callable: {method_name}"


# ---------------------------------------------------------------------------
# invalidate_session
# ---------------------------------------------------------------------------


class TestInvalidateSession:
    """Tests for invalidate_session method."""

    def test_inserts_into_invalidated_sessions(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.invalidate_session("alice", "token-uuid-1")

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args[0]
        assert "invalidated_sessions" in sql
        assert "alice" in params
        assert "token-uuid-1" in params

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.invalidate_session("alice", "token-uuid-1")

        sql, params = conn.execute.call_args[0]
        assert "%s" in sql
        assert "alice" not in sql
        assert "token-uuid-1" not in sql

    def test_includes_created_at_timestamp(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.invalidate_session("alice", "token-uuid-1")

        sql, params = conn.execute.call_args[0]
        # At least 3 params: username, token_id, created_at
        assert len(params) >= 3


# ---------------------------------------------------------------------------
# is_session_invalidated
# ---------------------------------------------------------------------------


class TestIsSessionInvalidated:
    """Tests for is_session_invalidated method."""

    def test_returns_true_when_session_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = (1,)
        backend = _make_backend(pool)

        assert backend.is_session_invalidated("alice", "token-uuid-1") is True

    def test_returns_false_when_session_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        assert backend.is_session_invalidated("alice", "token-uuid-1") is False

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        backend.is_session_invalidated("alice", "token-uuid-1")

        sql, params = conn.execute.call_args[0]
        assert "invalidated_sessions" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "token-uuid-1" in params
        assert "alice" not in sql
        assert "token-uuid-1" not in sql


# ---------------------------------------------------------------------------
# clear_invalidated_sessions
# ---------------------------------------------------------------------------


class TestClearInvalidatedSessions:
    """Tests for clear_invalidated_sessions method."""

    def test_deletes_all_sessions_for_user(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.clear_invalidated_sessions("alice")

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args[0]
        assert "DELETE FROM invalidated_sessions" in sql
        assert "alice" in params

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.clear_invalidated_sessions("alice")

        sql, params = conn.execute.call_args[0]
        assert "%s" in sql
        assert "alice" not in sql


# ---------------------------------------------------------------------------
# set_password_change_timestamp
# ---------------------------------------------------------------------------


class TestSetPasswordChangeTimestamp:
    """Tests for set_password_change_timestamp method."""

    def test_upserts_password_change_timestamp(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        ts = "2024-06-01T12:00:00+00:00"
        backend.set_password_change_timestamp("alice", ts)

        conn.execute.assert_called_once()
        sql, params = conn.execute.call_args[0]
        assert "password_change_timestamps" in sql
        assert "alice" in params
        assert ts in params

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.set_password_change_timestamp("alice", "2024-06-01T12:00:00+00:00")

        sql, params = conn.execute.call_args[0]
        assert "%s" in sql
        assert "alice" not in sql


# ---------------------------------------------------------------------------
# get_password_change_timestamp
# ---------------------------------------------------------------------------


class TestGetPasswordChangeTimestamp:
    """Tests for get_password_change_timestamp method."""

    def test_returns_none_when_not_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        assert backend.get_password_change_timestamp("alice") is None

    def test_returns_timestamp_string_when_found(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        ts = "2024-06-01T12:00:00+00:00"
        cursor.fetchone.return_value = (ts,)
        backend = _make_backend(pool)

        result = backend.get_password_change_timestamp("alice")

        assert result == ts

    def test_uses_parameterized_query(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchone.return_value = None
        backend = _make_backend(pool)

        backend.get_password_change_timestamp("alice")

        sql, params = conn.execute.call_args[0]
        assert "password_change_timestamps" in sql
        assert "%s" in sql
        assert "alice" in params
        assert "alice" not in sql


# ---------------------------------------------------------------------------
# cleanup_old_data
# ---------------------------------------------------------------------------


class TestCleanupOldData:
    """Tests for cleanup_old_data method."""

    def test_returns_zero_when_nothing_to_cleanup(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        result = backend.cleanup_old_data(days_to_keep=30)

        assert result == 0

    def test_returns_count_of_cleaned_users(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = [("alice",), ("bob",)]
        backend = _make_backend(pool)

        result = backend.cleanup_old_data(days_to_keep=30)

        assert result == 2

    def test_deletes_password_change_timestamps_and_sessions(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = [("alice",)]
        backend = _make_backend(pool)

        backend.cleanup_old_data(days_to_keep=30)

        executed_sqls = [str(c) for c in conn.execute.call_args_list]
        all_sql = " ".join(executed_sqls)
        assert "password_change_timestamps" in all_sql
        assert "invalidated_sessions" in all_sql

    def test_uses_parameterized_cutoff_date(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        cursor.fetchall.return_value = []
        backend = _make_backend(pool)

        backend.cleanup_old_data(days_to_keep=7)

        first_call_sql, first_call_params = conn.execute.call_args_list[0][0]
        assert "%s" in first_call_sql
        assert len(first_call_params) >= 1


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestSessionsClose:
    """Tests for close method."""

    def test_close_calls_pool_close(self) -> None:
        pool, conn, cursor = _make_pool_and_conn()
        backend = _make_backend(pool)

        backend.close()

        pool.close.assert_called_once()
