"""
Tests for AuditLogPostgresBackend (Story #415).

Verifies Protocol compliance, SQL parameterization, and method behavior
without requiring a real PostgreSQL connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.postgres.audit_log_backend import (
    AuditLogPostgresBackend,
)
from code_indexer.server.storage.protocols import AuditLogBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pool():
    """Mock psycopg v3 ConnectionPool."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool, conn, cursor


@pytest.fixture
def backend(mock_pool):
    pool, _, _ = mock_pool
    return AuditLogPostgresBackend(pool)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    """Verify AuditLogPostgresBackend satisfies AuditLogBackend Protocol."""

    def test_isinstance_check(self, backend):
        assert isinstance(backend, AuditLogBackend)

    def test_has_log_method(self, backend):
        assert callable(getattr(backend, "log", None))

    def test_has_log_raw_method(self, backend):
        assert callable(getattr(backend, "log_raw", None))

    def test_has_the_shared_read_methods_and_no_legacy_query(self, backend):
        # Rows are read only through services/audit_log_query.query_audit_log.
        for name in ("query_page", "count_capped", "aggregate", "find_terminal_rows"):
            assert callable(getattr(backend, name, None)), name
        assert not hasattr(backend, "query")

    def test_has_get_pr_logs_method(self, backend):
        assert callable(getattr(backend, "get_pr_logs", None))

    def test_has_get_cleanup_logs_method(self, backend):
        assert callable(getattr(backend, "get_cleanup_logs", None))


# ---------------------------------------------------------------------------
# log()
# ---------------------------------------------------------------------------


def _single_row_params(cursor):
    """The one row written by the backend's single write function."""
    cursor.executemany.assert_called_once()
    rows = cursor.executemany.call_args[0][1]
    assert len(rows) == 1
    return rows[0]


class TestLog:
    """Tests for log() method (routed through insert_events)."""

    def test_log_inserts_row(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log("admin", "user_created", "user", "alice", "some details")
        _single_row_params(cursor)
        sql = cursor.executemany.call_args[0][0]
        assert "INSERT INTO audit_logs" in sql
        assert "%s" in sql

    def test_log_runs_in_one_transaction(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log("admin", "user_created", "user", "alice")
        conn.transaction.assert_called_once()

    def test_log_passes_all_params(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log("admin", "user_created", "user", "alice", "details")
        params = _single_row_params(cursor)
        assert params[1] == "admin"
        assert params[2] == "user_created"
        assert params[3] == "user"
        assert params[4] == "alice"
        assert params[5] == "details"

    def test_log_details_defaults_to_none(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log("admin", "login", "auth", "admin")
        params = _single_row_params(cursor)
        assert params[5] is None


# ---------------------------------------------------------------------------
# log_raw()
# ---------------------------------------------------------------------------


class TestLogRaw:
    """Tests for log_raw() method."""

    def test_log_raw_uses_explicit_timestamp(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log_raw("2026-01-01T00:00:00", "admin", "migrated", "system", "all")
        params = _single_row_params(cursor)
        assert params[0] == "2026-01-01T00:00:00"

    def test_log_raw_runs_in_one_transaction(self, mock_pool):
        pool, conn, cursor = mock_pool
        backend = AuditLogPostgresBackend(pool)
        backend.log_raw("2026-01-01T00:00:00", "admin", "migrated", "system", "all")
        conn.transaction.assert_called_once()


# ---------------------------------------------------------------------------
# get_pr_logs()
# ---------------------------------------------------------------------------


class TestGetPrLogs:
    """Tests for get_pr_logs() method."""

    @patch("code_indexer.server.storage.postgres.audit_log_backend._dict_row_factory")
    def test_get_pr_logs_filters_by_pr_action_types(self, mock_factory, mock_pool):
        pool, conn, cursor = mock_pool
        mock_factory.return_value = None
        cursor.fetchall.return_value = []
        backend = AuditLogPostgresBackend(pool)
        backend.get_pr_logs()
        sql = cursor.execute.call_args[0][0]
        assert "action_type IN" in sql

    @patch("code_indexer.server.storage.postgres.audit_log_backend._dict_row_factory")
    def test_get_pr_logs_filters_by_repo_alias(self, mock_factory, mock_pool):
        pool, conn, cursor = mock_pool
        mock_factory.return_value = None
        cursor.fetchall.return_value = []
        backend = AuditLogPostgresBackend(pool)
        backend.get_pr_logs(repo_alias="my-repo")
        sql = cursor.execute.call_args[0][0]
        assert "target_id = %s" in sql


# ---------------------------------------------------------------------------
# get_cleanup_logs()
# ---------------------------------------------------------------------------


class TestGetCleanupLogs:
    """Tests for get_cleanup_logs() method."""

    @patch("code_indexer.server.storage.postgres.audit_log_backend._dict_row_factory")
    def test_get_cleanup_logs_filters_by_cleanup_type(self, mock_factory, mock_pool):
        pool, conn, cursor = mock_pool
        mock_factory.return_value = None
        cursor.fetchall.return_value = []
        backend = AuditLogPostgresBackend(pool)
        backend.get_cleanup_logs()
        sql = cursor.execute.call_args[0][0]
        assert "action_type = %s" in sql

    @patch("code_indexer.server.storage.postgres.audit_log_backend._dict_row_factory")
    def test_get_cleanup_logs_filters_by_repo_path(self, mock_factory, mock_pool):
        pool, conn, cursor = mock_pool
        mock_factory.return_value = None
        cursor.fetchall.return_value = []
        backend = AuditLogPostgresBackend(pool)
        backend.get_cleanup_logs(repo_path="/path/to/repo")
        sql = cursor.execute.call_args[0][0]
        assert "target_id = %s" in sql
