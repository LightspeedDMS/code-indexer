"""
Tests for Bug #543: Deadlock retry in job reconciliation.

Verifies that _reclaim_dead_node_jobs() and _reclaim_timed_out_jobs()
catch PostgreSQL deadlock errors (SQLSTATE 40P01) and retry with jitter
instead of failing immediately.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_reconciliation_service import (
    JobReconciliationService,
    _DEADLOCK_MAX_RETRIES,
)


def _make_deadlock_error():
    """Create a mock exception with sqlstate='40P01' (deadlock_detected)."""
    exc = Exception("deadlock detected")
    exc.sqlstate = "40P01"  # type: ignore[attr-defined]
    return exc


def _make_service():
    """Create a JobReconciliationService with mocked pool."""
    mock_pool = MagicMock()
    svc = JobReconciliationService.__new__(JobReconciliationService)
    svc._pool = mock_pool
    svc._sweep_interval = 5
    svc._max_execution_time = 1800
    svc._heartbeat_service = MagicMock()
    svc._node_id = "test-node"
    svc._stop_event = MagicMock()
    svc._stop_event.is_set.return_value = False
    return svc, mock_pool


def _setup_pool_for_deadlock_then_success(pool, deadlock_on_attempt=1):
    """Configure pool mock to raise deadlock on specified attempt, succeed after."""
    call_count = {"n": 0}
    real_conn = MagicMock()
    real_cursor = MagicMock()
    real_cursor.fetchall.return_value = []
    real_conn.cursor.return_value.__enter__ = MagicMock(return_value=real_cursor)
    real_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    def conn_enter():
        call_count["n"] += 1
        if call_count["n"] <= deadlock_on_attempt:
            raise _make_deadlock_error()
        return real_conn

    pool.connection.return_value.__enter__ = MagicMock(side_effect=conn_enter)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)


class TestDeadlockRetryDeadNodeReclaim:
    """Bug #543: _reclaim_dead_node_jobs retries on deadlock."""

    def test_succeeds_on_first_attempt(self):
        """Normal case: no deadlock, returns reclaimed count."""
        svc, pool = _make_service()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [("job1", "dead-node")]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = svc._reclaim_dead_node_jobs(["node1", "node2"])
        assert result == 1

    @patch("code_indexer.server.services.job_reconciliation_service.time.sleep")
    def test_retries_on_deadlock(self, mock_sleep):
        """Deadlock on first attempt, success on second."""
        svc, pool = _make_service()
        _setup_pool_for_deadlock_then_success(pool, deadlock_on_attempt=1)

        result = svc._reclaim_dead_node_jobs(["node1"])
        assert result == 0  # No rows reclaimed on retry
        assert mock_sleep.called

    def test_non_deadlock_exception_propagates(self):
        """Non-deadlock exceptions must not be retried."""
        svc, pool = _make_service()
        regular_error = RuntimeError("connection refused")
        pool.connection.return_value.__enter__ = MagicMock(side_effect=regular_error)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(RuntimeError, match="connection refused"):
            svc._reclaim_dead_node_jobs(["node1"])


class TestDeadlockRetryTimedOutReclaim:
    """Bug #543: _reclaim_timed_out_jobs retries on deadlock."""

    @patch("code_indexer.server.services.job_reconciliation_service.time.sleep")
    def test_retries_on_deadlock(self, mock_sleep):
        """Deadlock on first attempt, success on second."""
        svc, pool = _make_service()
        _setup_pool_for_deadlock_then_success(pool, deadlock_on_attempt=1)

        result = svc._reclaim_timed_out_jobs()
        assert result == 0  # No rows reclaimed on retry
        assert mock_sleep.called

    def test_non_deadlock_exception_propagates(self):
        """Non-deadlock exceptions must not be retried."""
        svc, pool = _make_service()

        regular_error = RuntimeError("connection refused")
        pool.connection.return_value.__enter__ = MagicMock(side_effect=regular_error)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(RuntimeError, match="connection refused"):
            svc._reclaim_timed_out_jobs()


class TestDeadlockConstants:
    """Verify deadlock retry constants."""

    def test_max_retries_is_three(self):
        assert _DEADLOCK_MAX_RETRIES == 3
