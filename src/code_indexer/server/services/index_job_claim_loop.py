"""
Per-pod index-job claim loop (Pod-pull: pod-pull work-stealing).

In cluster mode, memory-heavy index ops (POD_PULL_OPS: add_golden_repo,
provider_index_add, provider_temporal_index_rebuild, sync_repository,
change_branch) are registered PENDING in the shared Postgres background_jobs
queue by submit_job and are NOT dispatched to the submitting pod's in-memory
pool. This loop — started on EVERY postgres pod, not just the leader — claims
those rows via the memory-gated DistributedJobClaimer and executes them by
reconstructing the work from the row's ``metadata``.

Because the claimer's claim is memory-gated , a node under
memory pressure returns None and leaves the row PENDING for a pod with
headroom. FOR UPDATE SKIP LOCKED guarantees exactly one pod runs each row.

Contrast DistributedJobWorkerService: that runs on the LEADER only, re-executes
reclaimed refresh jobs, and fails non-retryable types. It now claims with
``exclude_types=POD_PULL_OPS`` so the two loops target disjoint row sets.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# op_type -> callable(metadata_dict) -> optional result dict.
DispatchTable = Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]]


class IndexJobClaimLoop:
    """Claims and executes pod-pull index jobs on this pod when it has headroom."""

    def __init__(
        self,
        claimer: Any,
        dispatch: DispatchTable,
        node_id: str,
        poll_interval: float = 5.0,
    ) -> None:
        """
        Args:
            claimer: DistributedJobClaimer for atomic, memory-gated claiming.
            dispatch: op_type -> executor(metadata) -> result. The claim is
                restricted to ``dispatch.keys()`` so only executable ops are
                pulled.
            node_id: this pod's node id (logging only; claimer owns ownership).
            poll_interval: seconds between claim attempts when idle.
        """
        self._claimer = claimer
        self._dispatch = dict(dispatch)
        self._node_id = node_id
        self._poll_interval = poll_interval
        self._job_types = sorted(self._dispatch.keys())
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # job_id currently executing on this pod (for release on shutdown).
        self._in_flight: Optional[str] = None
        self._in_flight_lock = threading.Lock()

    def start(self) -> None:
        """Start the claim loop thread (idempotent)."""
        if not self._dispatch:
            logger.info("IndexJobClaimLoop: no dispatchable ops — not starting")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="index-job-claim-loop"
        )
        self._thread.start()
        logger.info(
            "IndexJobClaimLoop: started (node=%s, ops=%s, interval=%.1fs)",
            self._node_id,
            ",".join(self._job_types),
            self._poll_interval,
        )

    def stop(self) -> None:
        """Signal stop, release any in-flight claim back to PENDING, and join."""
        self._stop_event.set()
        # Return an in-flight claim to the pool so another pod can pick it up
        # rather than waiting for dead-node reconciliation.
        with self._in_flight_lock:
            job_id = self._in_flight
        if job_id is not None:
            try:
                self._claimer.release_job(job_id)
            except Exception:  # noqa: BLE001 — best-effort on shutdown
                logger.warning(
                    "IndexJobClaimLoop: failed to release in-flight job %s",
                    job_id,
                    exc_info=True,
                )
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("IndexJobClaimLoop: stopped")

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                claimed = self._process_one_job()
            except Exception:  # noqa: BLE001 — loop must never die
                logger.exception("IndexJobClaimLoop: error in poll loop")
                claimed = False
            # If we just ran a job, immediately try for the next one (drain
            # fast); only sleep when the queue/headroom yielded nothing.
            if not claimed:
                self._stop_event.wait(timeout=self._poll_interval)

    def _process_one_job(self) -> bool:
        """Claim and execute at most one job. Returns True if one was executed."""
        job = self._claimer.claim_next_job(job_types=self._job_types)
        if job is None:
            # No pending pod-pull job, or this pod is memory-pressured (the
            # claimer's memory gate returned None) — leave rows for a pod w/ headroom.
            return False

        job_id = job["job_id"]
        op_type = job.get("operation_type", "")
        metadata = job.get("metadata") or {}
        executor = self._dispatch.get(op_type)

        if executor is None:
            # Should not happen (claim filtered to dispatch keys), but never
            # strand a claimed row: fail it so reconciliation doesn't loop.
            logger.error(
                "IndexJobClaimLoop: no executor for claimed op '%s' (job %s)",
                op_type,
                job_id,
            )
            self._claimer.fail_job(job_id, f"No pod-pull executor for {op_type}")
            return True

        with self._in_flight_lock:
            self._in_flight = job_id
        try:
            logger.info(
                "IndexJobClaimLoop: node %s executing %s (op=%s)",
                self._node_id,
                job_id,
                op_type,
            )
            result = executor(metadata)
            self._claimer.complete_job(job_id, result)
            logger.info("IndexJobClaimLoop: completed %s", job_id)
        except Exception as exc:  # noqa: BLE001 — surface as job failure
            logger.error(
                "IndexJobClaimLoop: job %s (op=%s) failed: %s",
                job_id,
                op_type,
                exc,
            )
            self._claimer.fail_job(job_id, str(exc))
        finally:
            with self._in_flight_lock:
                self._in_flight = None
        return True
