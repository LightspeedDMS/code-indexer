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
        # Sequence of return values for successive calls. Extra calls beyond the
        # provided entries return [] (a benign empty result) rather than raising
        # StopIteration, so adding a new reclaim query (Bug #1141 third path)
        # does not break tests that script fewer entries.
        _seq = list(fetchall)

        def _next_fetchall(*_args, **_kwargs):
            return _seq.pop(0) if _seq else []

        cur.fetchall.side_effect = _next_fetchall
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

    def test_sweep_reclaim_status_filters(self):
        """Per-path status filters (Bug #1141 adds a third path).

        The two reclaim-to-pending paths (dead-node, timeout) target only
        ``status = 'running'``.  The new stuck index-blocking path covers
        ``status IN ('pending', 'running')`` because pending jobs can also be
        stuck active and block idx_active_job_per_repo.
        """
        pool, _, cur = _make_pool(fetchall=[[], [], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_sqls = [
            " ".join(c.args[0].split())  # normalize whitespace
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        assert len(update_sqls) == 3
        running_only = [s for s in update_sqls if "status = 'running'" in s]
        stuck = [s for s in update_sqls if "status IN ('pending', 'running')" in s]
        assert len(running_only) == 2, f"expected 2 running-only UPDATEs: {update_sqls}"
        assert len(stuck) == 1, (
            f"expected 1 stuck pending/running UPDATE: {update_sqls}"
        )


# ---------------------------------------------------------------------------
# sweep() — stuck index-blocking reclaim (Bug #1141)
# ---------------------------------------------------------------------------


class TestSweepStuckIndexBlocking:
    """
    Bug #1141: Jobs in any index-blocking status (pending OR running) that
    are older than max_execution_time but never caught by the existing two
    reclaim paths must be moved to 'failed' so idx_active_job_per_repo
    unblocks and a fresh job can be submitted.

    Gap 1: A 'running' job with started_at IS NULL — _reclaim_timed_out_jobs
    uses ``started_at <= NOW() - timeout`` which never matches NULL.

    Gap 2: A 'pending' job older than max_execution_time — both existing
    reclaim paths filter ``status = 'running'``, so pending jobs are never
    touched.
    """

    def test_sweep_calls_stuck_index_blocking_reclaim(self):
        """
        sweep() must invoke the stuck-index-blocking reclaim path in addition
        to the two existing paths (three execute calls total when active_nodes
        is non-empty).
        """
        # Three fetchall entries: dead-node, timeout, stuck-index-blocking
        pool, _, cur = _make_pool(fetchall=[[], [], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        assert len(update_calls) == 3, (
            "Expected 3 UPDATE calls (dead-node, timeout, stuck-index-blocking); "
            f"got {len(update_calls)}"
        )

    def test_stuck_running_job_with_null_started_at_is_failed(self):
        """
        A 'running' job with started_at IS NULL that is old (via claimed_at
        or created_at fallback) must be set to 'failed' (not 'pending') so
        idx_active_job_per_repo unblocks.
        """
        # First fetchall (dead-node): nothing
        # Second fetchall (timeout): nothing — started_at IS NULL so it skips
        # Third fetchall (stuck-index-blocking): one stuck job returned
        pool, _, cur = _make_pool(
            fetchall=[[], [], [("job-stuck-null-start", "running", None)]]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert count == 1
        # Locate the stuck-index-blocking UPDATE (the one that sets status='failed'
        # and covers pending/running with COALESCE age fallback)
        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert len(stuck_calls) == 1, (
            "Expected exactly one UPDATE that sets status='failed' using COALESCE; "
            f"got {len(stuck_calls)}"
        )
        sql = stuck_calls[0].args[0]
        # Must cover BOTH pending and running (not just running)
        assert "'pending'" in sql or "pending" in sql
        assert "'running'" in sql or "running" in sql
        # Must use COALESCE to handle NULL started_at
        assert "COALESCE" in sql

    def test_stuck_pending_job_older_than_timeout_is_failed(self):
        """
        A 'pending' job older than max_execution_time must be set to 'failed'.
        The existing reclaim paths ignore pending — this new path must not.
        """
        pool, _, cur = _make_pool(
            fetchall=[[], [], [("job-stuck-pending", "pending", None)]]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert count == 1
        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert len(stuck_calls) == 1
        # The SQL must cover the 'pending' status
        sql = stuck_calls[0].args[0]
        assert "'pending'" in sql or "pending" in sql

    def test_stuck_index_blocking_sets_status_to_failed_not_pending(self):
        """
        Terminal action must be 'failed', NOT 'pending'.  Setting to pending
        would keep the job ACTIVE (still in the partial unique index) and
        block future submissions forever.
        """
        pool, _, cur = _make_pool(fetchall=[[], [], [("job-stuck", "running", None)]])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        # The third UPDATE (stuck-index-blocking) must set status='failed'
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert stuck_calls, "No stuck-index-blocking UPDATE found"
        sql = stuck_calls[0].args[0]
        # 'failed' must appear as the target status value
        assert "failed" in sql

    def test_stuck_reclaim_passes_max_execution_time_as_param(self):
        """max_execution_time must be a SQL parameter in the stuck-index-blocking query."""
        pool, _, cur = _make_pool(fetchall=[[], [], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=7200)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert stuck_calls, "No stuck-index-blocking UPDATE found"
        params = stuck_calls[0].args[1]
        assert 7200 in params, f"max_execution_time=7200 not found in params {params}"

    def test_stuck_reclaim_count_included_in_sweep_total(self):
        """
        sweep() return value must include stuck-index-blocking reclaims
        alongside dead-node and timeout counts.
        """
        pool, _, cur = _make_pool(
            fetchall=[
                [("j1", "dead-node")],  # dead-node reclaim
                [("j2", "node-1", "2026-01-01")],  # timeout reclaim
                [("j3", "running", None)],  # stuck-index-blocking
            ]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        count = svc.sweep()

        assert count == 3

    def test_stuck_reclaim_uses_interval_multiplication(self):
        """
        The COALESCE-based age check must use %s * INTERVAL '1 second'
        (not string interpolation) for safe parameterization.
        """
        pool, _, cur = _make_pool(fetchall=[[], [], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert stuck_calls
        sql = stuck_calls[0].args[0]
        assert "INTERVAL '1 second'" in sql

    def test_stuck_reclaim_excludes_paths_1_and_2_reclaimed_ids(self):
        """
        Clobber-safety: job_ids reclaimed by the dead-node and timeout paths in
        the SAME sweep must be EXCLUDED from the stuck-index-blocking 'failed'
        UPDATE (so a job path 2 just re-queued to 'pending' is not flipped to
        'failed').  The stuck UPDATE must carry an ``job_id <> ALL(%s)`` clause
        whose param list contains those reclaimed ids.
        """
        pool, _, cur = _make_pool(
            fetchall=[
                [("j1", "dead-node")],  # path 1 reclaims j1
                [("j2", "node-1", "2026-01-01")],  # path 2 reclaims j2
                [],  # path 3: nothing matched after exclusion
            ]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert stuck_calls, "No stuck-index-blocking UPDATE found"
        sql = stuck_calls[0].args[0]
        params = stuck_calls[0].args[1]
        # The exclusion clause must be present when paths 1&2 reclaimed ids.
        assert "job_id <> ALL" in sql, (
            f"stuck UPDATE missing clobber-safety exclusion clause: {sql}"
        )
        # The reclaimed ids (j1 from dead-node, j2 from timeout) must be excluded.
        excluded = [p for p in params if isinstance(p, (list, set, tuple))]
        assert excluded, f"no exclusion-list param found in {params}"
        excluded_ids = set(excluded[0])
        assert {"j1", "j2"} <= excluded_ids, (
            f"expected j1,j2 excluded; got {excluded_ids}"
        )
