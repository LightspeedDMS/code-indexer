"""
Self-Monitoring Service for CIDX Server.

Story #72 - Epic #71: CIDX Self-Monitoring

Provides scheduled automatic log analysis using Claude CLI to detect issues,
create bug reports, and maintain operational excellence.
"""

import logging
import threading
from typing import Optional, TYPE_CHECKING

from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager

logger = logging.getLogger(__name__)


class SelfMonitoringService:
    """
    Background service for scheduled self-monitoring log analysis.

    Periodically scans server logs for errors and anomalies, using Claude CLI
    to analyze logs and create GitHub issues for problems requiring attention.

    Args:
        enabled: Whether self-monitoring is enabled
        cadence_minutes: Interval between scans in minutes
        job_manager: BackgroundJobManager instance for job submission
    """

    def __init__(
        self,
        enabled: bool = False,
        cadence_minutes: int = 60,
        job_manager: Optional["BackgroundJobManager"] = None,
        db_path: Optional[str] = None,
        log_db_path: Optional[str] = None,
        github_repo: Optional[str] = None,
        prompt_template: str = "",
        model: str = "opus",
        repo_root: Optional[str] = None,
        github_token: Optional[str] = None,
        server_name: Optional[str] = None,
    ):
        """
        Initialize the self-monitoring service.

        Args:
            enabled: Whether self-monitoring is enabled
            cadence_minutes: Interval between scans in minutes
            job_manager: BackgroundJobManager for job submission
            db_path: Path to self-monitoring SQLite database
            log_db_path: Path to logs database
            github_repo: GitHub repository in format "owner/repo"
            prompt_template: Template string for Claude prompt (empty = use default)
            model: Claude model to use (opus, sonnet, haiku)
            repo_root: Path to repo root for Claude to run in (working directory)
            github_token: GitHub token for authentication (optional, Bug #87)
            server_name: Server display name for issue identification (optional, Bug #87)
        """
        self._enabled = enabled
        self._cadence_minutes = cadence_minutes
        self._job_manager = job_manager
        self._db_path = db_path
        self._log_db_path = log_db_path
        self._github_repo = github_repo
        self._prompt_template = prompt_template
        self._model = model
        self._repo_root = repo_root
        self._github_token = github_token
        self._server_name = server_name
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """
        Start the self-monitoring background thread.

        If not enabled, this method does nothing.
        If already running, this method does nothing.
        """
        if not self._enabled:
            logger.info("Self-monitoring service is disabled, not starting")
            return

        if self._running:
            logger.warning(
                format_error_log(
                    "MONITOR-GENERAL-001",
                    "Self-monitoring service already running",
                )
            )
            return

        if self._job_manager is None:
            logger.warning(
                format_error_log(
                    "MONITOR-GENERAL-002",
                    "BackgroundJobManager not initialized, self-monitoring cannot start",
                )
            )
            return

        # Start background thread
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="SelfMonitoringService",
            daemon=True,
        )
        self._thread.start()

        interval_display = f"{self._cadence_minutes} minutes"
        if self._cadence_minutes < 1:
            interval_display = f"{self._cadence_minutes * 60:.1f} seconds"

        logger.info(f"Self-monitoring service started (interval: {interval_display})")

    def stop(self) -> None:
        """
        Stop the self-monitoring background thread.

        If not running, this method does nothing.
        Blocks until the thread terminates (with timeout).
        """
        if not self._running:
            return

        logger.info("Stopping self-monitoring service")
        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning(
                    format_error_log(
                        "MONITOR-GENERAL-003",
                        "Self-monitoring service thread did not terminate within timeout",
                    )
                )

        logger.info("Self-monitoring service stopped")

    def _run_loop(self) -> None:
        """
        Main loop for self-monitoring processing.

        Runs periodically at the configured interval, submitting jobs to the
        background job queue for log analysis.
        """
        interval_seconds = self._cadence_minutes * 60

        while self._running and not self._stop_event.is_set():
            try:
                self._submit_scan_job()
            except Exception as e:
                logger.error(
                    format_error_log(
                        "MONITOR-GENERAL-004",
                        f"Self-monitoring scan submission failed: {e}",
                    ),
                    exc_info=True,
                )

            # Wait for interval or until stopped
            self._stop_event.wait(timeout=interval_seconds)

    def _submit_scan_job(self) -> None:
        """
        Submit a self-monitoring scan job to the background job queue.

        The job is tagged with operation_type='self_monitoring' and submitted
        as a system user job.
        """
        if self._job_manager is None:
            logger.warning(
                format_error_log(
                    "MONITOR-GENERAL-005",
                    "Cannot submit scan job: BackgroundJobManager not available",
                )
            )
            return

        logger.info("Submitting self-monitoring scan job")

        # Submit job to background job manager
        # The _execute_scan method creates LogScanner and runs the actual analysis
        try:
            job_id = self._job_manager.submit_job(
                operation_type="self_monitoring",
                func=self._execute_scan,
                submitter_username="system",
                is_admin=True,
                repo_alias=self._github_repo,
            )
            logger.info(f"Self-monitoring scan job submitted: {job_id}")
        except Exception as e:
            logger.error(
                format_error_log(
                    "MONITOR-GENERAL-006",
                    f"Failed to submit self-monitoring scan job: {e}",
                ),
                exc_info=True,
            )
            raise

    def _execute_scan(self) -> dict:
        """
        Execute actual log analysis scan using LogScanner (Bug #87).

        Returns:
            Scan result dictionary with status and metrics
        """
        logger.debug(f"[SELF-MON-DEBUG] _execute_scan: Entry - db_path={self._db_path}, log_db_path={self._log_db_path}, github_repo={self._github_repo}")

        # Validate required configuration
        if not self._db_path or not self._log_db_path:
            error_msg = "Database paths not configured: db_path and log_db_path are required"
            logger.debug(f"[SELF-MON-DEBUG] _execute_scan: Config validation failed - db_path={self._db_path}, log_db_path={self._log_db_path}")
            logger.error(
                format_error_log(
                    "MONITOR-GENERAL-010",
                    error_msg,
                )
            )
            return {"status": "FAILURE", "error": error_msg}

        if not self._github_repo:
            error_msg = "GitHub repository not configured: github_repo is required"
            logger.debug("[SELF-MON-DEBUG] _execute_scan: Config validation failed - github_repo is None")
            logger.error(
                format_error_log(
                    "MONITOR-GENERAL-011",
                    error_msg,
                )
            )
            return {"status": "FAILURE", "error": error_msg}

        logger.debug(f"[SELF-MON-DEBUG] _execute_scan: Config validated - model={self._model}, repo_root={self._repo_root}, server_name={self._server_name}")

        from code_indexer.server.self_monitoring.scanner import LogScanner
        from code_indexer.server.self_monitoring.prompts import get_default_prompt
        import uuid

        # Load default prompt if not configured
        prompt = self._prompt_template or get_default_prompt()
        logger.debug(f"[SELF-MON-DEBUG] _execute_scan: Prompt loaded - using_default={not self._prompt_template}, length={len(prompt)}")

        # Generate unique scan ID
        scan_id = str(uuid.uuid4())
        logger.debug(f"[SELF-MON-DEBUG] _execute_scan: Generated scan_id={scan_id}")

        # Create scanner instance
        logger.debug("[SELF-MON-DEBUG] _execute_scan: Creating LogScanner instance")
        scanner = LogScanner(
            db_path=self._db_path,
            scan_id=scan_id,
            github_repo=self._github_repo,
            log_db_path=self._log_db_path,
            prompt_template=prompt,
            model=self._model,
            repo_root=self._repo_root,
            github_token=self._github_token,
            server_name=self._server_name
        )

        # Execute scan (this handles all workflow including creating scan record)
        logger.debug("[SELF-MON-DEBUG] _execute_scan: Calling scanner.execute_scan()")
        result = scanner.execute_scan()
        logger.debug(f"[SELF-MON-DEBUG] _execute_scan: scanner.execute_scan() returned - status={result.get('status')}")
        return result

    @property
    def is_running(self) -> bool:
        """Return whether the service is currently running."""
        return self._running

    @property
    def enabled(self) -> bool:
        """Return whether the service is enabled."""
        return self._enabled

    @property
    def cadence_minutes(self) -> int:
        """Return the configured interval in minutes."""
        return self._cadence_minutes

    def trigger_scan(self) -> dict:
        """
        Manually trigger a self-monitoring scan (Story #75 AC2).

        Submits a scan job to the background job queue. If a scan is already
        running, the new scan is queued (not rejected) for sequential execution.

        Returns:
            Dictionary with status and scan_id on success, or error message on failure.
            Success: {"status": "queued", "scan_id": "..."}
            Error: {"status": "error", "error": "..."}
        """
        logger.debug(f"[SELF-MON-DEBUG] trigger_scan: Entry - enabled={self._enabled}, job_manager={self._job_manager is not None}")

        if not self._enabled:
            logger.debug("[SELF-MON-DEBUG] trigger_scan: Self-monitoring not enabled")
            logger.warning(
                format_error_log(
                    "MONITOR-GENERAL-007",
                    "Manual scan trigger rejected: self-monitoring not enabled",
                )
            )
            return {
                "status": "error",
                "error": "Self-monitoring is not enabled"
            }

        if self._job_manager is None:
            logger.debug("[SELF-MON-DEBUG] trigger_scan: Job manager not available")
            logger.warning(
                format_error_log(
                    "MONITOR-GENERAL-008",
                    "Manual scan trigger rejected: job manager not available",
                )
            )
            return {
                "status": "error",
                "error": "Job manager not available"
            }

        try:
            logger.info("Manual scan trigger: submitting self-monitoring scan job")
            logger.debug(f"[SELF-MON-DEBUG] trigger_scan: Submitting job - github_repo={self._github_repo}")

            # Submit job to background job manager
            # Concurrency is handled by the job queue itself - multiple scans
            # can be queued, but only one executes at a time
            job_id = self._job_manager.submit_job(
                operation_type="self_monitoring",
                func=self._execute_scan,
                submitter_username="system",
                is_admin=True,
                repo_alias=self._github_repo,
            )

            logger.info(f"Manual scan trigger: job submitted with ID {job_id}")
            logger.debug(f"[SELF-MON-DEBUG] trigger_scan: Job submitted successfully - job_id={job_id}")

            return {
                "status": "queued",
                "scan_id": job_id
            }

        except Exception as e:
            logger.debug(f"[SELF-MON-DEBUG] trigger_scan: Exception caught - {type(e).__name__}: {e}")
            logger.error(
                format_error_log(
                    "MONITOR-GENERAL-009",
                    f"Manual scan trigger failed: {e}",
                ),
                exc_info=True,
            )
            return {
                "status": "error",
                "error": str(e)
            }
