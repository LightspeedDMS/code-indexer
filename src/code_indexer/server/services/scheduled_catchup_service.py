"""
Scheduled Catch-Up Service for Smart Description Generation.

Provides a background service that periodically scans for repositories
with fallback descriptions and attempts to generate proper Claude-based
descriptions when the API key becomes available.

Story #23 AC6: Scheduled Timer-Based Gap Scanning and Catch-Up
"""

import logging
import threading
import uuid
from typing import Optional
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


def get_claude_cli_manager():
    """
    Get the global ClaudeCliManager instance.

    Module-level wrapper enabling tests to patch
    ``scheduled_catchup_service.get_claude_cli_manager``.
    """
    from .claude_cli_manager import get_claude_cli_manager as _get
    return _get()


class ScheduledCatchupService:
    """
    Background service for scheduled catch-up processing.

    Periodically scans for repositories with fallback descriptions (_README.md files)
    and attempts to generate proper Claude-based descriptions.

    Args:
        enabled: Whether scheduled catch-up is enabled
        interval_minutes: Interval between catch-up runs in minutes
    """

    def __init__(
        self,
        enabled: bool = False,
        interval_minutes: int = 60,
        job_tracker=None,
    ):
        """
        Initialize the scheduled catch-up service.

        Args:
            enabled: Whether scheduled catch-up is enabled
            interval_minutes: Interval between catch-up runs in minutes
            job_tracker: Optional JobTracker for dashboard visibility (Story #314)
        """
        self._enabled = enabled
        self._interval_minutes = interval_minutes
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._job_tracker = job_tracker  # Story #314: dashboard visibility

    def start(self) -> None:
        """
        Start the scheduled catch-up background thread.

        If not enabled, this method does nothing.
        If already running, this method does nothing.
        """
        if not self._enabled:
            logger.info("Scheduled catch-up service is disabled, not starting")
            return

        if self._running:
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-069", "Scheduled catch-up service already running"
                )
            )
            return

        # Check if manager is available
        from .claude_cli_manager import get_claude_cli_manager

        manager = get_claude_cli_manager()
        if manager is None:
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-070",
                    "ClaudeCliManager not initialized, scheduled catch-up will check again on each run",
                )
            )

        # Start background thread
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ScheduledCatchupService",
            daemon=True,
        )
        self._thread.start()

        interval_display = f"{self._interval_minutes} minutes"
        if self._interval_minutes < 1:
            interval_display = f"{self._interval_minutes * 60:.1f} seconds"

        logger.info(
            f"Scheduled catch-up service started (interval: {interval_display})"
        )

    def stop(self) -> None:
        """
        Stop the scheduled catch-up background thread.

        If not running, this method does nothing.
        Blocks until the thread terminates (with timeout).
        """
        if not self._running:
            return

        logger.info("Stopping scheduled catch-up service")
        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning(
                    format_error_log(
                        "GIT-GENERAL-071",
                        "Scheduled catch-up service thread did not terminate within timeout",
                    )
                )

        logger.info("Scheduled catch-up service stopped")

    def _run_loop(self) -> None:
        """
        Main loop for scheduled catch-up processing.

        Runs periodically at the configured interval, calling process_all_fallbacks()
        on the global ClaudeCliManager.
        """
        interval_seconds = self._interval_minutes * 60

        while self._running and not self._stop_event.is_set():
            try:
                self._process_catchup()
            except Exception as e:
                logger.error(
                    format_error_log(
                        "GIT-GENERAL-072",
                        f"Scheduled catch-up processing failed: {e}",
                        exc_info=True,
                    )
                )

            # Wait for interval or until stopped
            self._stop_event.wait(timeout=interval_seconds)

    def _process_catchup(self) -> None:
        """
        Process catch-up for repositories with fallback descriptions.

        Uses the global ClaudeCliManager to process all fallbacks.
        Registers a scheduled_catchup job in JobTracker for dashboard visibility (Story #314).
        """
        from ..middleware.correlation import get_correlation_id

        manager = get_claude_cli_manager()
        if manager is None:
            logger.debug(
                "ClaudeCliManager not initialized, skipping scheduled catch-up"
            )
            return

        logger.info(
            "Running scheduled catch-up processing",
            extra={"correlation_id": get_correlation_id()},
        )

        # Story #314: Register scheduled_catchup job for dashboard visibility
        tracked_job_id = None
        if self._job_tracker is not None:
            try:
                tracked_job_id = f"scheduled-catchup-{uuid.uuid4().hex[:8]}"
                self._job_tracker.register_job(
                    tracked_job_id, "scheduled_catchup", username="system"
                )
                self._job_tracker.update_status(tracked_job_id, status="running")
            except Exception as e:
                logger.debug(f"Failed to register scheduled_catchup job: {e}")
                tracked_job_id = None

        try:
            # Pass skip_tracking=True to avoid double-tracking inside process_all_fallbacks
            result = manager.process_all_fallbacks(skip_tracking=True)

            if result.processed:
                logger.info(
                    f"Scheduled catch-up completed: processed {len(result.processed)} repos",
                    extra={"correlation_id": get_correlation_id()},
                )
            elif result.error:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-136",
                        f"Scheduled catch-up partially completed: {result.error}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
            else:
                logger.debug(
                    "Scheduled catch-up: no repos needed processing",
                    extra={"correlation_id": get_correlation_id()},
                )

            if tracked_job_id and self._job_tracker is not None:
                try:
                    self._job_tracker.complete_job(tracked_job_id)
                except Exception as e:
                    logger.debug(
                        f"Failed to complete tracked job {tracked_job_id}: {e}"
                    )

        except Exception as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-137",
                    f"Scheduled catch-up failed: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            if tracked_job_id and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(tracked_job_id, error=str(e))
                except Exception as e2:
                    logger.debug(
                        f"Failed to mark tracked job {tracked_job_id} as failed: {e2}"
                    )

    @property
    def is_running(self) -> bool:
        """Return whether the service is currently running."""
        return self._running

    @property
    def enabled(self) -> bool:
        """Return whether the service is enabled."""
        return self._enabled

    @property
    def interval_minutes(self) -> int:
        """Return the configured interval in minutes."""
        return self._interval_minutes
