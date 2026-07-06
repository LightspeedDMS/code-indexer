"""
Job Reconciliation Service (Story #422).

Runs a background sweep every ``sweep_interval`` seconds (default 5 s) to
detect and reclaim jobs that have been abandoned by crashed cluster nodes.

Two reclaim conditions are checked on every sweep:

1. **Dead-node reclaim**: A job is in ``status='running'`` but its
   ``executing_node`` is NOT in the list of currently active nodes
   (as reported by :class:`NodeHeartbeatService`).  This means the node
   that claimed the job has crashed or gone offline.  This is the ONLY
   legitimate mechanism for reclaiming a running job — a running job on
   a LIVE node is NEVER reclaimed, however long it has been running.
   Bug #1218: the indexing / golden-repo-registration / SCIP path carries
   no wall-clock timeout because a large repo legitimately takes hours.

2. **Stuck running-anomaly reclaim** (Bug #1141, narrowed by Bug #1312):
   A job is in ``status='running'`` with a NULL ``started_at`` — i.e. it
   was claimed (an ``executing_node`` was assigned) but never actually
   recorded having started — and is older than ``max_execution_time``
   based on COALESCE(claimed_at, created_at).  This is a defensive catch
   for a claim/execution defect: the normal claim paths
   (``DistributedJobClaimer.claim_next_job()``,
   ``BackgroundJobManager._execute_job()``) always set ``started_at``
   atomically with ``status='running'``, so this row shape should not
   occur via the normal path.  These jobs are set to ``status='failed'``
   (not ``pending``) so the partial unique index
   ``idx_active_job_per_repo`` is freed and a fresh job can be submitted.
   The ``started_at IS NULL`` guard is load-bearing (Bug #1310): it
   ensures this path can never touch a job that has a valid
   ``started_at``, i.e. a job that is genuinely running and progressing
   on a live node.

Path 1 resets jobs to ``status='pending'`` with ``executing_node=NULL``
and ``started_at=NULL`` so they can be re-claimed by a healthy node.

Path 2 uses ``status='failed'`` because setting to ``pending`` would keep
the job in the active index and block future submissions.

Bug #1310 history: a prior "Path 2" (``_reclaim_timed_out_jobs``) used to
reset ANY ``status='running'`` job older than ``max_execution_time`` to
``pending`` regardless of node liveness — a direct violation of the
Bug #1218 no-timeout invariant that killed live, progressing golden-repo
temporal-index jobs at the 30-minute default. It has been removed; the
dead-node path above is the sole, correct replacement.

Bug #1312 history: Bug #1141's original Path 2 also covered
``status='pending'`` via the same COALESCE-age wall clock. That is wrong:
a ``pending`` job's ``started_at`` is unconditionally NULL, so Bug #1310's
guard never excluded it, and a job legitimately waiting in
``BackgroundJobManager``'s bounded worker pool (default
``max_concurrent_background_jobs=5`` — see
``utils/config_manager.py`` / ``repositories/background_jobs.py``) for
capacity occupied by OTHER repos' long-running jobs (which legitimately
run for hours per Bug #1218) would be wall-clock-failed the moment it
crossed ``max_execution_time`` (default 30 min), even though nothing
about it is actually stuck.

A first attempt at fixing this (rejected in code review) tried to gate
the ``pending`` sub-case on the absence of a live sibling ``running`` job
for the *same* ``repo_alias``. That was factually wrong on two counts,
established by reading the codebase rather than assuming:

- ``idx_active_job_per_repo`` already forbids two active rows sharing the
  exact same ``(operation_type, repo_alias)`` key, so a pending job's own
  key can never be blocked by a same-key sibling — the actual blocker in
  the reported scenario is generic worker-pool exhaustion by jobs for
  OTHER repos entirely, which a same-repo_alias sibling check cannot see.
- It never protected ``repo_alias IS NULL`` jobs (e.g. discovery jobs),
  since ``sibling.repo_alias = background_jobs.repo_alias`` is UNKNOWN
  (neither true nor false) in SQL when both sides are NULL.

**Corrected fix**: the ``pending`` sub-case is removed from Path 2
entirely — ``_reclaim_stuck_index_blocking_jobs`` targets
``status = 'running'`` only (never ``status IN ('pending', 'running')``),
unconditionally, regardless of ``active_nodes``. A ``pending`` row is
NEVER wall-clock-failed by ``JobReconciliationService``, no matter how
old — this removes the false-failure uniformly, including the
pool-exhaustion-behind-other-repos case and the ``repo_alias IS NULL``
case (both handled correctly now since the predicate no longer
references ``repo_alias`` at all).

This does not reopen Bug #1141: a genuinely abandoned ``pending`` row
(the ``reap_activated_repos`` / ``lifecycle_backfill`` scenario from that
bug, where the submitting node's own in-process worker-pool queue — see
``BackgroundJobManager._pending_job_queue`` — is lost on a crash/restart
and no node will ever dequeue that specific row) is still independently
reclaimed by ``DistributedJobWorkerService`` (Bug #582), which is wired
alongside this service in the SAME leader-gated cluster-services block
in ``startup/lifespan.py`` (``_on_become_leader()`` starts/stops both
``JobReconciliationService`` and ``DistributedJobWorkerService``
together). Its poll loop unconditionally claims the cluster-wide oldest
``pending`` row via ``DistributedJobClaimer.claim_next_job()`` and
immediately calls ``fail_job()`` — freeing ``idx_active_job_per_repo`` —
whenever ``operation_type`` is not in its ``RETRYABLE_JOB_TYPES``
allow-list; both ``reap_activated_repos`` and ``lifecycle_backfill``
(the two types named in Bug #1141) are non-retryable, so they are
cleared this way whenever a leader is active. This module therefore does
not need to (and must not) duplicate that liveness-free, owner-free
reclaim for ``pending`` rows itself.

This module is cluster-only and must only be loaded when
storage_mode="postgres".  No SQLite dependency.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, List, Optional, Set

logger = logging.getLogger(__name__)

# Default sweep cadence in seconds.
_DEFAULT_SWEEP_INTERVAL = 5

# Default maximum time a job may stay in 'running' state before being
# considered hung and reclaimed (30 minutes).
_DEFAULT_MAX_EXECUTION_TIME = 1800

# Bug #543: Max retries for deadlock (SQLSTATE 40P01) during reconciliation.
_DEADLOCK_MAX_RETRIES = 3

# Extra seconds added to sweep_interval when joining the thread on stop().
_THREAD_JOIN_GRACE_SECONDS = 5


class JobReconciliationService:
    """
    Periodic sweep service that reclaims abandoned running jobs.

    Usage::

        service = JobReconciliationService(
            pool=pool,
            heartbeat_service=heartbeat,
        )
        service.start()
        ...
        service.stop()
    """

    def __init__(
        self,
        pool: Any,
        heartbeat_service: Any,
        sweep_interval: int = _DEFAULT_SWEEP_INTERVAL,
        max_execution_time: int = _DEFAULT_MAX_EXECUTION_TIME,
    ) -> None:
        """
        Initialise the service.

        Args:
            pool:              A ConnectionPool instance.
            heartbeat_service: A :class:`NodeHeartbeatService` (or compatible
                               object with a ``get_active_nodes()`` method).
            sweep_interval:    Seconds between reconciliation sweeps
                               (default 5).
            max_execution_time: Seconds after which a running job is
                                considered hung and reclaimed
                                (default 1800 = 30 min).
        """
        self._pool = pool
        self._heartbeat_service = heartbeat_service
        self._sweep_interval = sweep_interval
        self._max_execution_time = max_execution_time
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the background reconciliation sweep thread.

        Idempotent — if the thread is already running this is a no-op.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("JobReconciliationService: already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sweep_loop,
            daemon=True,
            name="JobReconciliation",
        )
        self._thread.start()
        logger.info(
            "JobReconciliationService: started "
            "(sweep_interval=%ds, max_execution_time=%ds)",
            self._sweep_interval,
            self._max_execution_time,
        )

    def stop(self) -> None:
        """Stop the sweep thread gracefully."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self._sweep_interval + _THREAD_JOIN_GRACE_SECONDS)
        self._thread = None
        logger.info("JobReconciliationService: stopped")

    def sweep(self) -> int:
        """
        Execute one reconciliation sweep and return the number of jobs reclaimed.

        This method is also exposed directly for unit testing.
        """
        active_nodes = self._heartbeat_service.get_active_nodes()
        reclaimed = 0
        reclaimed_ids: Set[str] = set()
        reclaimed += self._reclaim_dead_node_jobs(active_nodes, reclaimed_ids)
        reclaimed += self._reclaim_stuck_index_blocking_jobs(reclaimed_ids)
        if reclaimed:
            logger.info(
                "JobReconciliationService: reclaimed %d job(s) this sweep",
                reclaimed,
            )
        return reclaimed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reclaim_dead_node_jobs(
        self, active_nodes: List[str], _reclaimed_ids_out: Optional[Set[str]] = None
    ) -> int:
        """
        Reset jobs whose executing_node is not in the active nodes list.

        When no active nodes exist (empty list), no dead-node reclaim is
        performed — this prevents wiping all running jobs during a transient
        heartbeat outage.

        Returns:
            Number of rows updated.
        """
        if not active_nodes:
            logger.debug(
                "JobReconciliationService: active_nodes list is empty; "
                "skipping dead-node reclaim to avoid false positives"
            )
            return 0

        # Bug #537: Add grace period — only reclaim jobs where claimed_at
        # is old enough that the node genuinely appears dead, not just
        # experiencing a brief heartbeat delay (GC pause, connection contention).
        grace_seconds = self._sweep_interval * 3  # 3x sweep interval as grace
        rows = []
        for attempt in range(_DEADLOCK_MAX_RETRIES):
            try:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE background_jobs
                            SET    status         = 'pending',
                                   executing_node = NULL,
                                   started_at     = NULL,
                                   claimed_at     = NULL
                            WHERE  status         = 'running'
                              AND  executing_node IS NOT NULL
                              AND  executing_node != ALL(%s)
                              AND  (claimed_at IS NULL
                                    OR claimed_at < NOW() - %s * INTERVAL '1 second')
                            RETURNING job_id, executing_node
                            """,
                            (active_nodes, grace_seconds),
                        )
                        rows = cur.fetchall()
                    conn.commit()
                break  # Success — exit retry loop
            except Exception as exc:
                # Bug #543: Catch deadlock (SQLSTATE 40P01) and retry with jitter
                sqlstate = getattr(exc, "sqlstate", None)
                if sqlstate == "40P01" and attempt < _DEADLOCK_MAX_RETRIES - 1:
                    jitter = random.uniform(0.1, 0.5)
                    logger.warning(
                        "JobReconciliationService: deadlock in _reclaim_dead_node_jobs "
                        "(attempt %d/%d), retrying in %.2fs",
                        attempt + 1,
                        _DEADLOCK_MAX_RETRIES,
                        jitter,
                    )
                    time.sleep(jitter)
                else:
                    raise

        if _reclaimed_ids_out is not None:
            _reclaimed_ids_out.update(r[0] for r in rows)

        for row in rows:
            logger.info(
                "JobReconciliationService: reclaimed job %s (dead node: %s) -> pending",
                row[0],
                row[1],
            )
        return len(rows)

    def _reclaim_stuck_index_blocking_jobs(self, reclaimed_ids: Set[str]) -> int:
        """Bug #1141, narrowed by Bug #1312: fail RUNNING jobs stuck in an
        index-blocking active status with no recorded start time.

        A job in ``status='running'`` with a NULL ``started_at`` was
        claimed (an ``executing_node`` was assigned) but never actually
        recorded starting — a defensive anomaly catch, since the normal
        claim paths (``DistributedJobClaimer.claim_next_job()``,
        ``BackgroundJobManager._execute_job()``) always set ``started_at``
        atomically with ``status='running'``. Such a row can sit ACTIVE
        forever and block ``idx_active_job_per_repo``, rejecting all new
        same-key jobs. Move it to terminal ``'failed'`` (NOT ``'pending'``,
        which would keep it active) so a fresh job can be submitted. Age
        uses COALESCE(claimed_at, created_at) since started_at is NULL by
        definition here.

        Bug #1310: the ``started_at IS NULL`` guard is load-bearing — it is
        what makes this path NEVER touch a job that has actually started
        running (a valid started_at means the job is live and progressing
        on whichever node claimed it; only the dead-node/heartbeat path may
        reclaim that job).

        Bug #1312: ``status='pending'`` jobs are DELIBERATELY EXCLUDED from
        this path — the WHERE clause below is ``status = 'running'`` only,
        never ``IN ('pending', 'running')``. A pending job's ``started_at``
        is unconditionally NULL, so any age-based inclusion of ``pending``
        here would wall-clock-fail a job merely waiting for worker-pool
        capacity (occupied by OTHER repos' hours-long jobs — Bug #1218)
        rather than genuinely abandoned, and there is no column recording
        pending-row ownership to gate it on reliably. See the module
        docstring for why a genuinely-abandoned pending row is still
        cleared, via ``DistributedJobWorkerService`` (Bug #582), without
        this path's help. Excludes job_ids already reclaimed by the
        dead-node path this sweep (clobber-safety — those were intentionally
        re-queued to pending).
        """
        exclude = list(reclaimed_ids)
        base_sql = (
            "UPDATE background_jobs "
            "SET    status = 'failed', "
            "       error  = COALESCE(error, 'Reclaimed by JobReconciliationService: "
            "stuck in active state beyond max_execution_time (Bug #1141)') "
            "WHERE  status = 'running' "
            "  AND  started_at IS NULL "
            "  AND  COALESCE(claimed_at, created_at) "
            "       <= NOW() - %s * INTERVAL '1 second' "
        )
        params: List[Any] = [self._max_execution_time]
        if exclude:
            base_sql += "  AND  job_id <> ALL(%s) "
            params.append(exclude)

        sql = base_sql + "RETURNING job_id, status, executing_node"

        rows = []
        for attempt in range(_DEADLOCK_MAX_RETRIES):
            try:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, tuple(params))
                        rows = cur.fetchall()
                    conn.commit()
                break
            except Exception as exc:
                sqlstate = getattr(exc, "sqlstate", None)
                if sqlstate == "40P01" and attempt < _DEADLOCK_MAX_RETRIES - 1:
                    jitter = random.uniform(0.1, 0.5)
                    logger.warning(
                        "JobReconciliationService: deadlock in "
                        "_reclaim_stuck_index_blocking_jobs (attempt %d/%d), "
                        "retrying in %.2fs",
                        attempt + 1,
                        _DEADLOCK_MAX_RETRIES,
                        jitter,
                    )
                    time.sleep(jitter)
                else:
                    raise

        for row in rows:
            logger.warning(
                "JobReconciliationService: failed stuck index-blocking job %s "
                "(status was %s, node: %s) -> failed (Bug #1141)",
                row[0],
                row[1],
                row[2],
            )
        return len(rows)

    def _sweep_loop(self) -> None:
        """Background loop: run sweep() every sweep_interval seconds."""
        while not self._stop_event.is_set():
            try:
                self.sweep()
            except Exception:
                logger.exception(
                    "JobReconciliationService: unexpected error in sweep loop"
                )
            self._stop_event.wait(self._sweep_interval)
