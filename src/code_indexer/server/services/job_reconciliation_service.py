"""
Job Reconciliation Service (Story #422).

Runs a background sweep every ``sweep_interval`` seconds (default 5 s) to
detect and reclaim jobs that have been abandoned by crashed cluster nodes.

Three reclaim conditions are checked on every sweep:

1. **Dead-node reclaim**: A job is in ``status='running'`` but its
   ``executing_node`` is NOT in the list of currently active nodes
   (as reported by :class:`NodeHeartbeatService`).  This means the node
   that claimed the job has crashed or gone offline.

2. **Execution-timeout reclaim**: A job is in ``status='running'`` and
   ``started_at`` is older than ``max_execution_time`` seconds
   (default 1800 s / 30 min).  This is a safety net for runaway jobs.

3. **Stuck index-blocking reclaim** (Bug #1141): A job is in ``status='pending'``
   or ``status='running'`` with a NULL ``started_at`` (so path 2 misses it)
   and is older than ``max_execution_time`` based on COALESCE(started_at,
   claimed_at, created_at).  These jobs are set to ``status='failed'``
   (not ``pending``) so the partial unique index ``idx_active_job_per_repo``
   is freed and a fresh job can be submitted.

Paths 1 & 2 reset jobs to ``status='pending'`` with ``executing_node=NULL``
and ``started_at=NULL`` so they can be re-claimed by a healthy node.

Path 3 uses ``status='failed'`` because setting to ``pending`` would keep
the job in the active index and block future submissions.

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
        reclaimed += self._reclaim_timed_out_jobs(reclaimed_ids)
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

    def _reclaim_timed_out_jobs(
        self, _reclaimed_ids_out: Optional[Set[str]] = None
    ) -> int:
        """
        Reset jobs that have been running longer than max_execution_time.

        Returns:
            Number of rows updated.
        """
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
                            WHERE  status     = 'running'
                              AND  started_at <= NOW() - %s * INTERVAL '1 second'
                            RETURNING job_id, executing_node, started_at
                            """,
                            (self._max_execution_time,),
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
                        "JobReconciliationService: deadlock in _reclaim_timed_out_jobs "
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
            logger.warning(
                "JobReconciliationService: reclaimed timed-out job %s "
                "(node: %s, started_at: %s) -> pending",
                row[0],
                row[1],
                row[2],
            )
        return len(rows)

    def _reclaim_stuck_index_blocking_jobs(self, reclaimed_ids: Set[str]) -> int:
        """Bug #1141: fail jobs stuck in an index-blocking active status.

        Jobs in 'pending' or 'running' with a NULL started_at (path 2 misses
        them) — or pending jobs that no path touches — can sit ACTIVE forever
        and block idx_active_job_per_repo, rejecting all new same-key jobs.
        Move them to terminal 'failed' (NOT 'pending', which would keep them
        active) so a fresh job can be submitted. Age uses
        COALESCE(started_at, claimed_at, created_at) so NULL started_at is
        handled. Excludes job_ids already reclaimed by the dead-node / timeout
        paths this sweep (clobber-safety — those were intentionally re-queued).
        """
        exclude = list(reclaimed_ids)
        base_sql = (
            "UPDATE background_jobs "
            "SET    status = 'failed', "
            "       error  = COALESCE(error, 'Reclaimed by JobReconciliationService: "
            "stuck in active state beyond max_execution_time (Bug #1141)') "
            "WHERE  status IN ('pending', 'running') "
            "  AND  COALESCE(started_at, claimed_at, created_at) "
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
