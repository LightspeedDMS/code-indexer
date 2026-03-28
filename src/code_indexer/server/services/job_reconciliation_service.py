"""
Job Reconciliation Service (Story #422).

Runs a background sweep every ``sweep_interval`` seconds (default 5 s) to
detect and reclaim jobs that have been abandoned by crashed cluster nodes.

Two reclaim conditions are checked on every sweep:

1. **Dead-node reclaim**: A job is in ``status='running'`` but its
   ``executing_node`` is NOT in the list of currently active nodes
   (as reported by :class:`NodeHeartbeatService`).  This means the node
   that claimed the job has crashed or gone offline.

2. **Execution-timeout reclaim**: A job is in ``status='running'`` and
   ``started_at`` is older than ``max_execution_time`` seconds
   (default 1800 s / 30 min).  This is a safety net for runaway jobs.

Reclaimed jobs are reset to ``status='pending'`` with
``executing_node=NULL`` and ``started_at=NULL`` so they can be re-claimed
by a healthy node.

This module is cluster-only and must only be loaded when
storage_mode="postgres".  No SQLite dependency.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Default sweep cadence in seconds.
_DEFAULT_SWEEP_INTERVAL = 5

# Default maximum time a job may stay in 'running' state before being
# considered hung and reclaimed (30 minutes).
_DEFAULT_MAX_EXECUTION_TIME = 1800

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
        reclaimed += self._reclaim_dead_node_jobs(active_nodes)
        reclaimed += self._reclaim_timed_out_jobs()
        if reclaimed:
            logger.info(
                "JobReconciliationService: reclaimed %d job(s) this sweep",
                reclaimed,
            )
        return reclaimed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reclaim_dead_node_jobs(self, active_nodes: List[str]) -> int:
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

        for row in rows:
            logger.info(
                "JobReconciliationService: reclaimed job %s (dead node: %s) -> pending",
                row[0],
                row[1],
            )
        return len(rows)

    def _reclaim_timed_out_jobs(self) -> int:
        """
        Reset jobs that have been running longer than max_execution_time.

        Returns:
            Number of rows updated.
        """
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

        for row in rows:
            logger.warning(
                "JobReconciliationService: reclaimed timed-out job %s "
                "(node: %s, started_at: %s) -> pending",
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
