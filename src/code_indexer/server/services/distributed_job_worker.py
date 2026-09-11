"""
Distributed Job Worker Service (Bug #582).

Background thread that polls PostgreSQL for reclaimed/pending jobs and
re-executes them. Only handles idempotent job types that can be
reconstructed from operation_type + repo_alias.

Runs on the leader node only (gated by leader election callbacks).
"""

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Job types that can be safely re-executed from DB metadata
RETRYABLE_JOB_TYPES = {
    "global_repo_refresh",
    "refresh_golden_repo",
}


class DistributedJobWorkerService:
    """Polls PG for pending jobs and executes them."""

    def __init__(
        self,
        claimer: Any,
        refresh_scheduler: Any,
        poll_interval: int = 30,
    ) -> None:
        self._claimer = claimer
        self._refresh_scheduler = refresh_scheduler
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the worker thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="dist-job-worker"
        )
        self._thread.start()
        logger.info(
            "DistributedJobWorkerService: started (interval=%ds)",
            self._poll_interval,
        )

    def stop(self) -> None:
        """Stop the worker thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("DistributedJobWorkerService: stopped")

    def _poll_loop(self) -> None:
        """Main loop: claim and execute jobs."""
        while not self._stop_event.is_set():
            try:
                self._process_one_job()
            except Exception:
                logger.exception("DistributedJobWorkerService: error in poll loop")
            self._stop_event.wait(timeout=self._poll_interval)

    def _process_one_job(self) -> None:
        """Attempt to claim and execute one pending job."""
        # Pod-pull: never claim pod-pull index ops — those are owned by the
        # per-pod IndexJobClaimLoop. Without this exclusion the leader worker
        # (which fails any non-retryable type it claims) would fail the PENDING
        # add_golden_repo/sync/etc. rows before a pod could work-steal them.
        from code_indexer.server.repositories.background_jobs import POD_PULL_OPS

        job = self._claimer.claim_next_job(exclude_types=sorted(POD_PULL_OPS))
        if job is None:
            return

        job_id = job.get("job_id", "?")
        op_type = job.get("operation_type", "")
        repo_alias = job.get("repo_alias", "")

        logger.info(
            "DistributedJobWorkerService: claimed job %s (type=%s, repo=%s)",
            job_id,
            op_type,
            repo_alias,
        )

        if op_type not in RETRYABLE_JOB_TYPES:
            logger.warning(
                "DistributedJobWorkerService: job %s has non-retryable "
                "type '%s', marking failed",
                job_id,
                op_type,
            )
            self._claimer.fail_job(job_id, f"Non-retryable operation_type: {op_type}")
            return

        try:
            self._execute_retryable_job(job_id, op_type, repo_alias)
            self._claimer.complete_job(job_id)
            logger.info("DistributedJobWorkerService: completed job %s", job_id)
        except Exception as exc:
            logger.error(
                "DistributedJobWorkerService: job %s failed: %s",
                job_id,
                exc,
            )
            self._claimer.fail_job(job_id, str(exc))

    def _execute_retryable_job(
        self, job_id: str, op_type: str, repo_alias: str
    ) -> None:
        """Execute a retryable job based on operation_type."""
        if op_type in ("global_repo_refresh", "refresh_golden_repo"):
            if not repo_alias:
                raise ValueError(f"Job {job_id}: repo_alias is required for {op_type}")
            # Bug #1839: the claim itself (claim_next_job's UPDATE) already
            # occupies this job's idx_active_job_per_repo dedup slot. Calling
            # trigger_refresh_for_repo() here would re-enter the SUBMISSION
            # path (BackgroundJobManager.submit_job), which tries to
            # register a SECOND row for the same (operation_type,
            # repo_alias) pair and collides with our own claimed row --
            # DuplicateJobError naming job_id itself. execute_refresh_for_
            # claimed_job() performs the refresh WORK directly instead,
            # exactly like a normally-submitted job's worker closure does.
            result = self._refresh_scheduler.execute_refresh_for_claimed_job(
                repo_alias,
                progress_callback=self._make_progress_callback(job_id),
            )
            # Mirror BackgroundJobManager._execute_job's own interpretation
            # of the refresh result dict (Story #1586 Finding 2): a real
            # failure can return {"success": False, ...} WITHOUT raising
            # (e.g. an integrity-gate skip). Raise here so _process_one_job's
            # existing except block marks the job failed exactly once.
            if isinstance(result, dict) and result.get("success") is False:
                failure_detail = result.get("message") or result.get("error")
                raise RuntimeError(failure_detail or f"Refresh failed for {repo_alias}")
        else:
            raise ValueError(f"Unknown retryable job type: {op_type}")

    def _make_progress_callback(self, job_id: str) -> Optional[Any]:
        """Build a progress_callback forwarding into the claimer's shared DB
        row, so a claimed refresh job's progress is visible on the dashboard
        the same way a normally-submitted refresh job's progress is.

        Returns None when the claimer has no update_progress (e.g. a
        minimal test double) so RefreshScheduler treats it as "no callback".
        """
        update_progress = getattr(self._claimer, "update_progress", None)
        if update_progress is None:
            return None

        def _progress_callback(
            progress: int,
            phase: Optional[str] = None,
            detail: Optional[str] = None,
        ) -> None:
            try:
                update_progress(job_id, progress, phase=phase, detail=detail)
            except Exception:
                logger.debug(
                    "DistributedJobWorkerService: progress update failed for %s",
                    job_id,
                    exc_info=True,
                )

        return _progress_callback
