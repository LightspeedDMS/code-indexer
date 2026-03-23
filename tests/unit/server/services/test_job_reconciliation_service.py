"""
Unit tests for JobReconciliationService.

Story #422: Job Reconciliation Service

Mock hierarchy (no real PostgreSQL required):
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchall()
    heartbeat_service.get_active_nodes()
"""

from __future__ import annotations

from unittest.mock import MagicMock


from code_indexer.server.services.job_reconciliation_service import (
    JobReconciliationService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchall=None):
    """
    Build a mocked ConnectionPool.

    ``fetchall`` may be a list of return values consumed in order
    (one per cursor.fetchall() call) or a single list used for every call.
    """
    cur = MagicMock()
    if isinstance(fetchall, list) and fetchall and isinstance(fetchall[0], list):
        # Sequence of return values for successive calls
        cur.fetchall.side_effect = fetchall
    else:
        cur.fetchall.return_value = fetchall if fetchall is not None else []

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


def _make_heartbeat(active_nodes=None):
    """Build a mocked NodeHeartbeatService."""
    hb = MagicMock()
    hb.get_active_nodes.return_value = active_nodes if active_nodes is not None else []
    return hb


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_spawns_daemon_thread(self):
        """start() must spawn a daemon thread named JobReconciliation."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, sweep_interval=100)

        svc.start()
        try:
            assert svc._thread is not None
            assert svc._thread.is_alive()
            assert svc._thread.daemon is True
            assert "JobReconciliation" in svc._thread.name
        finally:
            svc.stop()

    def test_start_idempotent_when_already_running(self):
        """Calling start() twice must not start a second thread."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, sweep_interval=100)

        svc.start()
        first_thread = svc._thread
        svc.start()
        second_thread = svc._thread

        try:
            assert first_thread is second_thread
        finally:
            svc.stop()

    def test_stop_clears_thread_reference(self):
        """After stop(), _thread must be None."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, sweep_interval=100)

        svc.start()
        svc.stop()

        assert svc._thread is None

    def test_stop_signals_stop_event(self):
        """stop() must set the _stop_event so the loop exits."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, sweep_interval=100)

        svc.start()
        svc.stop()

        assert svc._stop_event.is_set()

    def test_thread_not_started_on_init(self):
        """Background thread must not start until start() is called."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat()
        svc = JobReconciliationService(pool, hb)
        assert svc._thread is None


# ---------------------------------------------------------------------------
# sweep() — dead-node reclaim
# ---------------------------------------------------------------------------


class TestSweepDeadNode:
    def test_sweep_queries_active_nodes(self):
        """sweep() must call heartbeat_service.get_active_nodes()."""
        pool, _, _ = _make_pool()
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        hb.get_active_nodes.assert_called_once()

    def test_sweep_resets_dead_node_jobs_to_pending(self):
        """
        When a job's executing_node is not in active_nodes, the UPDATE
        must set status='pending' and clear executing_node / started_at.
        """
        # First fetchall: dead-node reclaim returns one reclaimed job
        # Second fetchall: timeout reclaim returns nothing
        pool, _, cur = _make_pool(fetchall=[[("job-dead", "node-gone")], []])
        hb = _make_heartbeat(active_nodes=["node-alive"])
        svc = JobReconciliationService(pool, hb)

        count = svc.sweep()

        assert count == 1
        all_calls = cur.execute.call_args_list
        dead_node_calls = [c for c in all_calls if "executing_node != ALL" in c.args[0]]
        assert len(dead_node_calls) == 1
        sql = dead_node_calls[0].args[0]
        params = dead_node_calls[0].args[1]
        assert "status" in sql
        assert "pending" in sql
        assert "executing_node = NULL" in sql
        assert "started_at" in sql
        assert params[0] == ["node-alive"]

    def test_sweep_skips_dead_node_reclaim_when_active_nodes_empty(self):
        """
        When get_active_nodes() returns [], dead-node reclaim must be
        skipped entirely to avoid false positives during heartbeat outages.
        """
        pool, _, cur = _make_pool(fetchall=[[]])
        hb = _make_heartbeat(active_nodes=[])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        all_sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert not any("executing_node != ALL" in sql for sql in all_sqls)

    def test_sweep_returns_zero_when_no_abandoned_jobs(self):
        """sweep() must return 0 when no jobs are reclaimed."""
        pool, _, _ = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        count = svc.sweep()

        assert count == 0

    def test_sweep_passes_active_nodes_list_as_param(self):
        """The active_nodes list must be passed as the SQL parameter for != ALL."""
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1", "node-2"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        all_calls = cur.execute.call_args_list
        dead_node_calls = [c for c in all_calls if "executing_node != ALL" in c.args[0]]
        assert len(dead_node_calls) == 1
        params = dead_node_calls[0].args[1]
        assert params[0] == ["node-1", "node-2"]


# ---------------------------------------------------------------------------
# sweep() — execution-timeout reclaim
# ---------------------------------------------------------------------------


class TestSweepTimeout:
    def test_sweep_resets_timed_out_jobs_to_pending(self):
        """
        Jobs running longer than max_execution_time must be reset to pending.
        """
        # First fetchall: dead-node reclaim returns nothing
        # Second fetchall: timeout reclaim returns one job
        pool, _, cur = _make_pool(
            fetchall=[[], [("job-hung", "node-1", "2026-01-01T00:00:00")]]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert count == 1
        all_calls = cur.execute.call_args_list
        timeout_calls = [c for c in all_calls if "started_at <=" in c.args[0]]
        assert len(timeout_calls) == 1
        sql = timeout_calls[0].args[0]
        params = timeout_calls[0].args[1]
        assert "status" in sql
        assert "pending" in sql
        assert "executing_node = NULL" in sql
        assert params[0] == 1800

    def test_sweep_timeout_sql_uses_interval_multiplication(self):
        """
        Timeout SQL must use %s * INTERVAL '1 second', not INTERVAL '%s seconds'.
        """
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=600)

        svc.sweep()

        all_calls = cur.execute.call_args_list
        timeout_calls = [c for c in all_calls if "started_at <=" in c.args[0]]
        assert len(timeout_calls) == 1
        sql = timeout_calls[0].args[0]
        assert "INTERVAL '1 second'" in sql
        assert "%s" in sql

    def test_sweep_timeout_passes_max_execution_time_as_param(self):
        """max_execution_time must be passed as a SQL parameter."""
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=3600)

        svc.sweep()

        all_calls = cur.execute.call_args_list
        timeout_calls = [c for c in all_calls if "started_at <=" in c.args[0]]
        params = timeout_calls[0].args[1]
        assert params[0] == 3600

    def test_sweep_counts_both_dead_node_and_timeout_reclaims(self):
        """sweep() return value must be the sum of both reclaim types."""
        pool, _, cur = _make_pool(
            fetchall=[
                [("job-1", "dead-node"), ("job-2", "dead-node")],  # dead-node
                [("job-3", "node-1", "2026-01-01")],  # timeout
            ]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        count = svc.sweep()

        assert count == 3

    def test_sweep_only_resets_running_jobs(self):
        """Both UPDATE statements must filter WHERE status = 'running'."""
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        for c in cur.execute.call_args_list:
            sql = " ".join(c.args[0].split())  # normalize whitespace
            if "UPDATE background_jobs" in sql:
                assert "status = 'running'" in sql
