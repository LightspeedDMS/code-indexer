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
# sweep() — stuck index-blocking reclaim (Bug #1141)
# ---------------------------------------------------------------------------


class TestSweepStuckIndexBlocking:
    """
    Bug #1141: Jobs in any index-blocking status (pending OR running) that
    are older than max_execution_time but never caught by the dead-node
    reclaim path must be moved to 'failed' so idx_active_job_per_repo
    unblocks and a fresh job can be submitted.

    Gap 1: A 'running' job with started_at IS NULL — never started, so no
    duration-based check applies to it; only the ``started_at IS NULL``
    guard in _reclaim_stuck_index_blocking_jobs catches it.

    Gap 2: A 'pending' job older than max_execution_time — the dead-node
    reclaim path filters ``status = 'running'``, so pending jobs are never
    touched.

    Post-Bug #1310: JobReconciliationService has exactly two reclaim paths
    — _reclaim_dead_node_jobs (heartbeat/liveness-based) and
    _reclaim_stuck_index_blocking_jobs (the started_at IS NULL guard
    verified by this test class). The former _reclaim_timed_out_jobs
    method was removed by Bug #1310.
    """

    def test_sweep_calls_stuck_index_blocking_reclaim(self):
        """
        sweep() must invoke the stuck-index-blocking reclaim path in addition
        to the two existing paths (three execute calls total when active_nodes
        is non-empty).
        """
        # Two fetchall entries: dead-node, stuck-index-blocking (Path 2 removed
        # by Bug #1310 — the only two remaining reclaim paths).
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        assert len(update_calls) == 2, (
            "Expected 2 UPDATE calls (dead-node, stuck-index-blocking); "
            f"got {len(update_calls)}"
        )

    def test_stuck_running_job_with_null_started_at_is_failed(self):
        """
        A 'running' job with started_at IS NULL that is old (via claimed_at
        or created_at fallback) must be set to 'failed' (not 'pending') so
        idx_active_job_per_repo unblocks. Bug #1141 preserved by Bug #1310.
        """
        # First fetchall (dead-node): nothing
        # Second fetchall (stuck-index-blocking): one stuck job returned
        pool, _, cur = _make_pool(
            fetchall=[[], [("job-stuck-null-start", "running", None)]]
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
        # Bug #1310: must require started_at IS NULL so a job with a valid
        # started_at (genuinely running) can never match this query.
        assert "started_at IS NULL" in sql

    def test_stuck_pending_job_older_than_timeout_is_failed(self):
        """
        A 'pending' job older than max_execution_time must be set to 'failed'.
        The existing reclaim paths ignore pending — this new path must not.
        """
        pool, _, cur = _make_pool(
            fetchall=[[], [("job-stuck-pending", "pending", None)]]
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
        pool, _, cur = _make_pool(fetchall=[[], [("job-stuck", "running", None)]])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_calls = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        # The second UPDATE (stuck-index-blocking) must set status='failed'
        stuck_calls = [
            c for c in update_calls if "failed" in c.args[0] and "COALESCE" in c.args[0]
        ]
        assert stuck_calls, "No stuck-index-blocking UPDATE found"
        sql = stuck_calls[0].args[0]
        # 'failed' must appear as the target status value
        assert "failed" in sql

    def test_stuck_reclaim_passes_max_execution_time_as_param(self):
        """max_execution_time must be a SQL parameter in the stuck-index-blocking query."""
        pool, _, cur = _make_pool(fetchall=[[], []])
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
        alongside dead-node reclaims.
        """
        pool, _, cur = _make_pool(
            fetchall=[
                [("j1", "dead-node")],  # dead-node reclaim
                [("j3", "running", None)],  # stuck-index-blocking
            ]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        count = svc.sweep()

        assert count == 2

    def test_stuck_reclaim_uses_interval_multiplication(self):
        """
        The COALESCE-based age check must use %s * INTERVAL '1 second'
        (not string interpolation) for safe parameterization.
        """
        pool, _, cur = _make_pool(fetchall=[[], []])
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

    def test_stuck_reclaim_excludes_path_1_reclaimed_ids(self):
        """
        Clobber-safety: job_ids reclaimed by the dead-node path in the SAME
        sweep must be EXCLUDED from the stuck-index-blocking 'failed' UPDATE
        (so a job path 1 just re-queued to 'pending' is not flipped to
        'failed'). The stuck UPDATE must carry a ``job_id <> ALL(%s)`` clause
        whose param list contains those reclaimed ids. (Path 2 no longer
        exists — Bug #1310 removed it.)
        """
        pool, _, cur = _make_pool(
            fetchall=[
                [("j1", "dead-node")],  # path 1 reclaims j1
                [],  # path 2 (stuck): nothing matched after exclusion
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
        # The exclusion clause must be present when path 1 reclaimed ids.
        assert "job_id <> ALL" in sql, (
            f"stuck UPDATE missing clobber-safety exclusion clause: {sql}"
        )
        # The reclaimed id (j1 from dead-node) must be excluded.
        excluded = [p for p in params if isinstance(p, (list, set, tuple))]
        assert excluded, f"no exclusion-list param found in {params}"
        excluded_ids = set(excluded[0])
        assert {"j1"} <= excluded_ids, f"expected j1 excluded; got {excluded_ids}"

    def test_sweep_reclaim_status_filters(self):
        """Per-path status filters (2-path design after Bug #1310).

        The dead-node reclaim-to-pending path targets only
        ``status = 'running'``. The stuck index-blocking path covers
        ``status IN ('pending', 'running')`` (guarded by
        ``started_at IS NULL``) because pending/never-started jobs can also
        be stuck active and block idx_active_job_per_repo.
        """
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb)

        svc.sweep()

        update_sqls = [
            " ".join(c.args[0].split())  # normalize whitespace
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        assert len(update_sqls) == 2
        running_only = [s for s in update_sqls if "status = 'running'" in s]
        stuck = [s for s in update_sqls if "status IN ('pending', 'running')" in s]
        assert len(running_only) == 1, f"expected 1 running-only UPDATE: {update_sqls}"
        assert len(stuck) == 1, (
            f"expected 1 stuck pending/running UPDATE: {update_sqls}"
        )


# ---------------------------------------------------------------------------
# sweep() — Bug #1310: live running jobs must NEVER be wall-clock reaped
# ---------------------------------------------------------------------------


class TestSweepBug1310LiveRunningJobNotReclaimed:
    """
    Bug #1310: a RUNNING job with a VALID started_at older than
    max_execution_time, whose executing_node IS present in active_nodes
    (i.e. the node is alive and, presumably, the job is still progressing),
    must NEVER be reclaimed or failed by sweep(). Bug #1218 forbids any
    wall-clock timeout on indexing / golden-repo / SCIP jobs — the only
    legitimate reclaim mechanism for a running job is the dead-node
    (heartbeat) path.
    """

    def test_running_job_valid_started_at_alive_node_is_not_reclaimed(self):
        """
        sweep() must report zero reclaims for a live, long-running job.

        With active_nodes=["node-1"] (alive) and no dead-node rows returned,
        a running job with an old, valid started_at must not be reset to
        pending nor failed by any reclaim path this sweep.
        """
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert count == 0
        update_sqls = [
            " ".join(c.args[0].split())
            for c in cur.execute.call_args_list
            if "UPDATE background_jobs" in c.args[0]
        ]
        # Any query capable of setting status='failed' must require a NULL
        # started_at — a job with a valid started_at can never match it.
        for sql in update_sqls:
            if "'failed'" in sql:
                assert "started_at IS NULL" in sql, (
                    "a 'failed' UPDATE without a started_at IS NULL guard "
                    f"would wall-clock-reap a live running job: {sql}"
                )

    def test_no_blanket_wall_clock_reclaim_of_running_jobs_remains(self):
        """
        No UPDATE query may reset/fail status='running' jobs purely on
        ``started_at <= NOW() - max_execution_time`` without also requiring
        node death (executing_node != ALL(active_nodes)). This is the
        deleted Path 2 (_reclaim_timed_out_jobs) behavior — it must not
        exist anywhere in the sweep.
        """
        pool, _, cur = _make_pool(fetchall=[[], []])
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        svc.sweep()

        all_sqls = [" ".join(c.args[0].split()) for c in cur.execute.call_args_list]
        blanket_timeout_queries = [
            sql
            for sql in all_sqls
            if "started_at <=" in sql and "executing_node != ALL" not in sql
        ]
        assert not blanket_timeout_queries, (
            "found a blanket wall-clock reclaim query with no dead-node "
            f"liveness check (Bug #1310 regression): {blanket_timeout_queries}"
        )
