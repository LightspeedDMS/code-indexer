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

from datetime import datetime, timedelta, timezone
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


class _FakeJobsTable:
    """
    Faithful in-memory evaluator for the two UPDATE queries issued by
    JobReconciliationService.sweep(). Unlike ``_make_pool`` above (which
    just replays a scripted ``fetchall`` return value regardless of what
    the SQL actually says), this interprets the REAL WHERE-clause
    semantics -- status filters, ``started_at IS NULL``, the
    ``COALESCE(claimed_at, created_at)`` age fallback, executing_node
    liveness, and job_id exclusion -- against a table of row dicts and
    mutates them exactly as a real PostgreSQL UPDATE ... RETURNING would.

    This exists because the Bug #1312 code review found that SQL-shape-only
    tests (asserting substrings in the query text against a MagicMock
    cursor) gave false confidence: they never actually evaluated whether a
    given row would or would not be selected, so a wrong predicate could
    pass every test while still wall-clock-failing a legitimately-queued
    pending job in production.

    Rows are plain dicts with keys: job_id, status, executing_node,
    started_at, claimed_at, created_at (datetimes or None).
    """

    def __init__(self, rows, now):
        self.rows = rows
        self.now = now

    def dead_node_reclaim(self, active_nodes, grace_seconds):
        updated = []
        for r in self.rows:
            if r["status"] != "running":
                continue
            if r["executing_node"] is None:
                continue
            if r["executing_node"] in active_nodes:
                continue
            claimed_at = r.get("claimed_at")
            if claimed_at is not None and claimed_at >= self.now - timedelta(
                seconds=grace_seconds
            ):
                continue
            old_node = r["executing_node"]
            r["status"] = "pending"
            r["executing_node"] = None
            r["started_at"] = None
            r["claimed_at"] = None
            updated.append((r["job_id"], old_node))
        return updated

    def stuck_index_blocking_reclaim(
        self, sql, max_execution_time, exclude_ids, active_nodes
    ):
        """
        Parses the ACTUAL generated SQL text to determine which statuses,
        guards, and gates are really in effect -- rather than applying an
        independently-assumed "correct" predicate -- so this evaluator
        reacts to a real production regression (e.g. 'pending' creeping
        back into the status filter) exactly as real PostgreSQL would.
        """
        if "status IN ('pending', 'running')" in sql:
            allowed_statuses = {"pending", "running"}
        elif "status = 'running'" in sql:
            allowed_statuses = {"running"}
        else:
            raise AssertionError(f"unrecognized status filter in stuck query: {sql}")

        requires_started_at_null = "started_at IS NULL" in sql
        has_live_sibling_gate = (
            "NOT EXISTS" in sql
            and "sibling.repo_alias = background_jobs.repo_alias" in sql
            and "'running' = sibling.status" in sql
            and "sibling.executing_node = ANY" in sql
        )

        updated = []
        for r in self.rows:
            if r["job_id"] in exclude_ids:
                continue
            if r["status"] not in allowed_statuses:
                continue
            if requires_started_at_null and r["started_at"] is not None:
                continue
            age_basis = r.get("claimed_at") or r["created_at"]
            if age_basis > self.now - timedelta(seconds=max_execution_time):
                continue
            if has_live_sibling_gate and r["status"] == "pending":
                live_sibling = any(
                    other["status"] == "running"
                    and other.get("executing_node") in (active_nodes or [])
                    and other.get("repo_alias") == r.get("repo_alias")
                    and other["job_id"] != r["job_id"]
                    for other in self.rows
                )
                if live_sibling:
                    continue
            old_status = r["status"]
            r["status"] = "failed"
            updated.append((r["job_id"], old_status, r["executing_node"]))
        return updated


def _make_behavioral_pool(rows, now):
    """
    Build a pool/conn/cursor whose execute() genuinely evaluates the real
    WHERE-clause predicates (via _FakeJobsTable) against `rows`, dispatching
    on the distinguishing SQL substring for each of the two known query
    shapes issued by JobReconciliationService. fetchall() returns the real
    matched rows, and the underlying dicts in `rows` are mutated (status
    flips) exactly as a real PostgreSQL UPDATE ... RETURNING would -- this
    is what makes the test behavioral rather than a string-shape check.
    The stuck-index-blocking dispatch parses the ACTUAL SQL text (status
    filter, started_at guard, sibling gate) rather than assuming it.
    """
    table = _FakeJobsTable(rows, now)
    cur = MagicMock()

    def _execute(sql, params=()):
        normalized = " ".join(sql.split())
        if "executing_node != ALL" in normalized:
            active_nodes, grace_seconds = params
            cur.fetchall.return_value = table.dead_node_reclaim(
                active_nodes, grace_seconds
            )
        elif "COALESCE" in normalized and "'failed'" in normalized:
            remaining = list(params[1:])
            max_execution_time = params[0]
            exclude_ids = set()
            if "job_id <> ALL" in normalized:
                exclude_ids = set(remaining.pop(0))
            sibling_active_nodes = None
            if "executing_node = ANY" in normalized:
                sibling_active_nodes = remaining.pop(0)
            cur.fetchall.return_value = table.stuck_index_blocking_reclaim(
                normalized, max_execution_time, exclude_ids, sibling_active_nodes
            )
        else:
            cur.fetchall.return_value = []

    cur.execute.side_effect = _execute

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur, table


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
    Bug #1141, narrowed by Bug #1312: a 'running' job with started_at IS
    NULL (claimed but never actually recorded starting — a defensive
    anomaly catch, since the normal claim paths always set started_at
    atomically with status='running') that is older than
    max_execution_time must be moved to 'failed' so
    idx_active_job_per_repo unblocks and a fresh job can be submitted.

    Bug #1312 correction: 'pending' jobs are DELIBERATELY EXCLUDED from
    this path (see TestSweepBug1312PendingNeverReaped /
    TestSweepBug1312BehavioralPoolExhaustion below for the proof and the
    rationale — a pending job's started_at is unconditionally NULL, so
    naively including it here wall-clock-fails jobs merely waiting for
    worker-pool capacity, not just genuinely-abandoned ones).

    Post-Bug #1310/#1312: JobReconciliationService has exactly two reclaim
    paths — _reclaim_dead_node_jobs (heartbeat/liveness-based, status=
    'running' only) and _reclaim_stuck_index_blocking_jobs (status=
    'running' + started_at IS NULL only, verified by this test class).
    The former _reclaim_timed_out_jobs method was removed by Bug #1310.
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
        # Bug #1312: must cover 'running' ONLY -- 'pending' must never appear
        # as a status literal in this query (that was the #1312 false-failure).
        assert "status = 'running'" in sql
        assert "'pending'" not in sql
        # Must use COALESCE to handle NULL started_at
        assert "COALESCE" in sql
        # Bug #1310: must require started_at IS NULL so a job with a valid
        # started_at (genuinely running) can never match this query.
        assert "started_at IS NULL" in sql

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
        """Per-path status filters (2-path design after Bug #1310/#1312).

        Both paths target ``status = 'running'`` only -- Bug #1312 removed
        ``'pending'`` from the stuck index-blocking path's status filter
        entirely (a pending job is NEVER wall-clock-reaped by either path).
        The two queries are distinguished by their other clauses: the
        dead-node path has ``executing_node != ALL``; the stuck-index-
        blocking path has ``started_at IS NULL`` + ``COALESCE``.
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
        dead_node = [s for s in update_sqls if "executing_node != ALL" in s]
        stuck = [
            s for s in update_sqls if "started_at IS NULL" in s and "COALESCE" in s
        ]
        assert len(dead_node) == 1, f"expected 1 dead-node UPDATE: {update_sqls}"
        assert len(stuck) == 1, f"expected 1 stuck running-only UPDATE: {update_sqls}"
        assert "status = 'running'" in dead_node[0]
        assert "status = 'running'" in stuck[0]
        # Bug #1312: the stuck-index-blocking query specifically must NEVER
        # reference 'pending' as a status literal (Path 1's SET clause
        # legitimately writes status='pending' as its target -- that's fine
        # and out of scope for this check).
        assert "'pending'" not in stuck[0], (
            f"Bug #1312 regression: 'pending' status literal found in the "
            f"stuck-index-blocking query: {stuck[0]}"
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


# ---------------------------------------------------------------------------
# sweep() — Bug #1312: legitimately-queued pending jobs must NOT be reaped
# ---------------------------------------------------------------------------


class TestSweepBug1312PendingNeverReaped:
    """
    Bug #1312 (SQL-shape, reworked after code-review rejection): a prior
    attempt gated the 'pending' sub-case on the absence of a live sibling
    'running' job for the SAME repo_alias. That predicate was wrong on two
    counts, established by reading the actual codebase (not assumed):

    1. ``idx_active_job_per_repo`` is a UNIQUE index on
       ``(operation_type, repo_alias)`` among active rows, so a pending
       job's own key can NEVER be blocked by another active row sharing
       that exact key -- the schema forbids it. The real reason a pending
       job waits is generic BOUNDED WORKER-POOL exhaustion
       (``BackgroundJobManager`` default ``max_concurrent_background_jobs
       = 5`` -- ``utils/config_manager.py``, ``repositories/
       background_jobs.py``) by jobs for OTHER, unrelated repos -- which a
       same-repo_alias sibling check cannot see at all (see
       TestSweepBug1312BehavioralPoolExhaustion below for the proof).
    2. ``repo_alias IS NULL`` jobs (discovery jobs) were never protected,
       since ``sibling.repo_alias = background_jobs.repo_alias`` is UNKNOWN
       (neither true nor false) for NULL in SQL.

    The corrected fix removes the 'pending' sub-case from this path
    ENTIRELY: ``_reclaim_stuck_index_blocking_jobs`` now targets
    ``status = 'running'`` only (never ``IN ('pending', 'running')``),
    unconditionally, regardless of ``active_nodes``. A pending row is
    NEVER wall-clock-failed by JobReconciliationService, no matter how
    old -- eliminating the false-failure uniformly, including the
    pool-exhaustion-behind-other-repos case and the repo_alias IS NULL
    case (both trivially correct now since the predicate no longer
    references repo_alias at all). See the module docstring for why a
    genuinely-abandoned pending row (Bug #1141's real scenario) is still
    cleared via the sibling DistributedJobWorkerService (Bug #582).
    """

    def test_pending_status_literal_never_appears_in_stuck_query(self):
        """The stuck-index-blocking UPDATE must never reference 'pending'
        as a status value -- it targets status = 'running' only."""
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
        assert stuck_calls, "No stuck-index-blocking UPDATE found"
        sql = " ".join(stuck_calls[0].args[0].split())
        assert "'pending'" not in sql
        assert "status = 'running'" in sql
        # The rejected NOT EXISTS same-repo-sibling gate must be gone too.
        assert "NOT EXISTS" not in sql
        assert "sibling" not in sql

    def test_stuck_query_identical_regardless_of_active_nodes_state(self):
        """
        Cluster-correctness / outage guard: since the stuck-index-blocking
        path no longer depends on active_nodes at all, its SQL shape (and
        behavior) must be IDENTICAL whether active_nodes is empty (a
        heartbeat outage) or populated -- no active_nodes-dependent branch
        exists to get this wrong during an outage.
        """
        pool_live, _, cur_live = _make_pool(fetchall=[[], []])
        hb_live = _make_heartbeat(active_nodes=["node-1", "node-2"])
        JobReconciliationService(pool_live, hb_live).sweep()

        pool_outage, _, cur_outage = _make_pool(fetchall=[[]])
        hb_outage = _make_heartbeat(active_nodes=[])
        JobReconciliationService(pool_outage, hb_outage).sweep()

        def _stuck_sql(cur):
            update_calls = [
                c
                for c in cur.execute.call_args_list
                if "UPDATE background_jobs" in c.args[0]
            ]
            stuck = [
                c
                for c in update_calls
                if "failed" in c.args[0] and "COALESCE" in c.args[0]
            ]
            assert stuck, "No stuck-index-blocking UPDATE found"
            return " ".join(stuck[0].args[0].split())

        assert _stuck_sql(cur_live) == _stuck_sql(cur_outage)

    def test_max_execution_time_and_exclusion_param_shape_unchanged(self):
        """The stuck-index-blocking query must still be fully parameterized
        (no f-string interpolation): max_execution_time as %s, and the
        clobber-safety exclusion list as job_id <> ALL(%s)."""
        pool, _, cur = _make_pool(
            fetchall=[
                [("j1", "dead-node")],
                [],
            ]
        )
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=999)

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
        params = stuck_calls[0].args[1]
        assert "job_id <> ALL" in sql
        assert 999 in params
        assert any(isinstance(p, (list, set, tuple)) and "j1" in p for p in params), (
            f"exclusion list missing j1: {params}"
        )


# ---------------------------------------------------------------------------
# sweep() — Bug #1312 behavioral proof (real predicate evaluation, not
# SQL-string-only assertions against a mocked cursor)
# ---------------------------------------------------------------------------


class TestSweepBug1312BehavioralPoolExhaustion:
    """
    Bug #1312 behavioral proof. Uses ``_make_behavioral_pool`` (a faithful
    in-memory evaluator of the real WHERE-clause semantics, not a mock that
    merely replays a scripted return value) to prove the FINAL PERSISTED
    STATE of rows after sweep() -- the exact category of proof the code
    review found missing from the original (rejected) fix.
    """

    def test_pending_job_queued_behind_other_repos_running_jobs_survives(self):
        """
        The core Bug #1312 scenario: 5 running jobs for OTHER repos
        (B-F, unrelated operation_types) occupy the entire bounded worker
        pool; a 6th job for repo A stays pending, older than
        max_execution_time. It must remain 'pending' after sweep() -- not
        wall-clock-failed merely for queue age.
        """
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        old = now - timedelta(seconds=3600)  # 1h old, past the 30-min default
        rows = [
            {
                "job_id": f"running-{repo}",
                "status": "running",
                "executing_node": "node-1",
                "started_at": old,
                "claimed_at": old,
                "created_at": old,
                "repo_alias": f"repo{repo}",
            }
            for repo in "BCDEF"
        ] + [
            {
                "job_id": "pending-repoA",
                "status": "pending",
                "executing_node": None,
                "started_at": None,
                "claimed_at": None,
                "created_at": old,
                "repo_alias": "repoA",
            }
        ]
        pool, _, cur, table = _make_behavioral_pool(rows, now)
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        pending_row = next(r for r in table.rows if r["job_id"] == "pending-repoA")
        assert pending_row["status"] == "pending", (
            "Bug #1312 regression: a legitimately-queued pending job "
            "(pool exhausted by OTHER repos' running jobs) was "
            "wall-clock-failed"
        )
        assert count == 0
        # The 5 legitimately-running jobs on OTHER repos must also survive
        # untouched (Bug #1218/#1310 -- no wall clock on live running work).
        for repo in "BCDEF":
            row = next(r for r in table.rows if r["job_id"] == f"running-{repo}")
            assert row["status"] == "running"

    def test_pending_job_with_null_repo_alias_also_survives(self):
        """A discovery-style job (repo_alias=None) sitting pending must
        also survive -- repo_alias is irrelevant to the corrected
        predicate, so NULL rows are handled uniformly and correctly."""
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        old = now - timedelta(seconds=3600)
        rows = [
            {
                "job_id": "pending-discovery",
                "status": "pending",
                "executing_node": None,
                "started_at": None,
                "claimed_at": None,
                "created_at": old,
                "repo_alias": None,
            }
        ]
        pool, _, cur, table = _make_behavioral_pool(rows, now)
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        svc.sweep()

        assert table.rows[0]["status"] == "pending"

    def test_genuinely_stuck_running_null_started_at_job_is_still_freed(self):
        """
        Bug #1141 preserved: a job that transitioned to 'running' (has an
        executing_node on a live node) but never actually recorded
        started_at -- a defensive anomaly catch, not a liveness question
        -- is still failed once older than max_execution_time, freeing
        idx_active_job_per_repo.
        """
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        old = now - timedelta(seconds=3600)
        rows = [
            {
                "job_id": "stuck-running-null-started",
                "status": "running",
                "executing_node": "node-1",
                "started_at": None,
                "claimed_at": old,
                "created_at": old,
            }
        ]
        pool, _, cur, table = _make_behavioral_pool(rows, now)
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert table.rows[0]["status"] == "failed"
        assert count == 1

    def test_running_job_valid_started_at_on_live_node_never_reaped_bug_1310(self):
        """Bug #1310 preserved: a running job with a valid started_at on a
        live node is never touched, however old (no wall clock on live,
        progressing indexing/golden-repo/SCIP work -- Bug #1218)."""
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        old = now - timedelta(seconds=99999)  # far beyond any reasonable timeout
        rows = [
            {
                "job_id": "live-long-running",
                "status": "running",
                "executing_node": "node-1",
                "started_at": old,
                "claimed_at": old,
                "created_at": old,
            }
        ]
        pool, _, cur, table = _make_behavioral_pool(rows, now)
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert table.rows[0]["status"] == "running"
        assert count == 0

    def test_dead_node_running_job_still_reclaimed_to_pending(self):
        """
        Sanity check that the behavioral harness's dead-node path still
        works: a running job whose executing_node is NOT in active_nodes
        is reset to 'pending' by Path 1, independent of this bug's fix.
        """
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        old = now - timedelta(seconds=3600)
        rows = [
            {
                "job_id": "dead-node-job",
                "status": "running",
                "executing_node": "node-gone",
                "started_at": old,
                "claimed_at": old,
                "created_at": old,
            }
        ]
        pool, _, cur, table = _make_behavioral_pool(rows, now)
        hb = _make_heartbeat(active_nodes=["node-1"])
        svc = JobReconciliationService(pool, hb, max_execution_time=1800)

        count = svc.sweep()

        assert table.rows[0]["status"] == "pending"
        assert table.rows[0]["executing_node"] is None
        assert count == 1
