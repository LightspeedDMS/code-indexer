"""
Tests for Bug #542: Ownership guard on update_job.

Verifies that update_job() adds AND executing_node = %s when the
executing_node parameter is provided, preventing cross-node overwrites.
"""

from unittest.mock import MagicMock

from code_indexer.server.storage.postgres.background_jobs_backend import (
    BackgroundJobsPostgresBackend,
)


def _make_backend():
    """Create backend with mocked pool."""
    mock_pool = MagicMock()
    backend = BackgroundJobsPostgresBackend.__new__(BackgroundJobsPostgresBackend)
    backend._pool = mock_pool
    backend._node_id = "test-node"

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return backend, mock_cursor


class TestUpdateJobOwnershipGuard:
    """Bug #542: update_job ownership guard prevents cross-node overwrites."""

    def test_without_executing_node_uses_simple_where(self):
        """Default: no ownership guard, backward compatible."""
        backend, cursor = _make_backend()
        backend.update_job("job-123", status="completed")
        sql = cursor.execute.call_args[0][0]
        assert "WHERE job_id = %s" in sql
        assert "executing_node" not in sql

    def test_with_executing_node_adds_guard(self):
        """Bug #542: executing_node adds AND executing_node = %s."""
        backend, cursor = _make_backend()
        backend.update_job("job-123", executing_node="node-A", status="completed")
        sql = cursor.execute.call_args[0][0]
        assert "WHERE job_id = %s" in sql
        assert "AND executing_node = %s" in sql

    def test_executing_node_value_in_params(self):
        """The executing_node value must be in the query params."""
        backend, cursor = _make_backend()
        backend.update_job("job-123", executing_node="node-A", status="running")
        params = cursor.execute.call_args[0][1]
        assert "node-A" in params
        assert "job-123" in params

    def test_no_kwargs_is_noop(self):
        """Calling with no kwargs returns without executing."""
        backend, cursor = _make_backend()
        backend.update_job("job-123", executing_node="node-A")
        cursor.execute.assert_not_called()
