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
        job = self._claimer.claim_next_job()
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
            self._refresh_scheduler.trigger_refresh_for_repo(repo_alias)
        else:
            raise ValueError(f"Unknown retryable job type: {op_type}")
