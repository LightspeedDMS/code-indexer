"""
Global Repos Lifecycle Manager for CIDX Server.

Manages the lifecycle of all global repository background services:
- QueryTracker: Reference counting for active queries
- CleanupManager: Automatic deletion of old index versions
- RefreshScheduler: Periodic repository refresh scheduling

Coordinates startup, shutdown, and graceful cleanup of these services.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager
    from code_indexer.server.utils.config_manager import ServerResourceConfig

from ...global_repos.query_tracker import QueryTracker
from ...global_repos.cleanup_manager import CleanupManager
from ...global_repos.refresh_scheduler import RefreshScheduler
from ...global_repos.shared_operations import GlobalRepoOperations


logger = logging.getLogger(__name__)


class GlobalReposLifecycleManager:
    """
    Lifecycle manager for global repository background services.

    Coordinates the startup and shutdown of:
    - QueryTracker (singleton)
    - CleanupManager (depends on QueryTracker)
    - RefreshScheduler (depends on QueryTracker and CleanupManager)

    Ensures proper initialization order and graceful shutdown.
    """

    def __init__(
        self,
        golden_repos_dir: str,
        background_job_manager: Optional["BackgroundJobManager"] = None,
        resource_config: Optional["ServerResourceConfig"] = None,
        job_tracker=None,
    ):
        """
        Initialize the lifecycle manager.

        Args:
            golden_repos_dir: Path to golden repos directory
            background_job_manager: Optional job manager for dashboard visibility (server mode)
            resource_config: Optional resource configuration (timeouts, etc.)
            job_tracker: Optional JobTracker for dashboard visibility (Story #314)
        """
        self.golden_repos_dir = Path(golden_repos_dir)

        # Ensure directory structure exists
        self.golden_repos_dir.mkdir(parents=True, exist_ok=True)

        # Create singleton QueryTracker
        self.query_tracker = QueryTracker()

        # Create CleanupManager with QueryTracker dependency
        self.cleanup_manager = CleanupManager(
            query_tracker=self.query_tracker,
            check_interval=1.0,  # Check every second
            job_tracker=job_tracker,
        )

        # Create GlobalRepoOperations for config access
        self.global_ops = GlobalRepoOperations(str(self.golden_repos_dir))

        # Create RefreshScheduler with all dependencies
        self.refresh_scheduler = RefreshScheduler(
            golden_repos_dir=str(self.golden_repos_dir),
            config_source=self.global_ops,
            query_tracker=self.query_tracker,
            cleanup_manager=self.cleanup_manager,
            background_job_manager=background_job_manager,
            resource_config=resource_config,
        )

        # Track running state
        self._running = False
        self._job_tracker = job_tracker  # Story #314: dashboard visibility

        logger.debug(
            f"GlobalReposLifecycleManager initialized for {self.golden_repos_dir}",
            extra={"correlation_id": get_correlation_id()},
        )

    def is_running(self) -> bool:
        """
        Check if lifecycle manager is running.

        Returns:
            True if background services are active
        """
        return self._running

    def start(self) -> None:
        """
        Start all background services.

        Startup order:
        1. CleanupManager (depends on QueryTracker)
        2. RefreshScheduler (depends on QueryTracker and CleanupManager)

        Idempotent: Safe to call multiple times
        """
        if self._running:
            logger.debug(
                "GlobalReposLifecycleManager already running",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        logger.info(
            "Starting global repos background services",
            extra={"correlation_id": get_correlation_id()},
        )

        # Start CleanupManager first
        self.cleanup_manager.start()
        logger.debug(
            "CleanupManager started", extra={"correlation_id": get_correlation_id()}
        )

        # Start RefreshScheduler
        self.refresh_scheduler.start()
        logger.debug(
            "RefreshScheduler started", extra={"correlation_id": get_correlation_id()}
        )

        # Trigger reconciliation in background thread (non-blocking, Story #236)
        # Failures must not block startup per AC7
        def _run_reconcile() -> None:
            # Story #314: Register startup_reconcile job for dashboard visibility
            tracked_job_id = None
            if self._job_tracker is not None:
                try:
                    tracked_job_id = f"startup-reconcile-{uuid.uuid4().hex[:8]}"
                    self._job_tracker.register_job(
                        tracked_job_id, "startup_reconcile", username="system", repo_alias="server"
                    )
                    self._job_tracker.update_status(tracked_job_id, status="running")
                except Exception as e:
                    logger.debug(f"Failed to register startup_reconcile job: {e}")
                    tracked_job_id = None

            try:
                logger.info(
                    "Starting golden repos reconciliation",
                    extra={"correlation_id": get_correlation_id()},
                )
                self.refresh_scheduler.reconcile_golden_repos()
                logger.info(
                    "Golden repos reconciliation completed",
                    extra={"correlation_id": get_correlation_id()},
                )
                if tracked_job_id and self._job_tracker is not None:
                    try:
                        self._job_tracker.complete_job(tracked_job_id)
                    except Exception as e:
                        logger.debug(
                            f"Failed to complete startup_reconcile job {tracked_job_id}: {e}"
                        )
            except Exception as exc:
                logger.warning(
                    f"Golden repos reconciliation failed during startup (non-fatal): {exc}",
                    extra={"correlation_id": get_correlation_id()},
                )
                if tracked_job_id and self._job_tracker is not None:
                    try:
                        self._job_tracker.fail_job(tracked_job_id, error=str(exc))
                    except Exception as e:
                        logger.debug(
                            f"Failed to mark startup_reconcile job {tracked_job_id} as failed: {e}"
                        )

        reconcile_thread = threading.Thread(
            target=_run_reconcile,
            name="golden-repos-reconcile",
            daemon=True,
        )
        reconcile_thread.start()
        logger.debug(
            "Golden repos reconciliation thread started",
            extra={"correlation_id": get_correlation_id()},
        )

        self._running = True
        logger.info(
            "Global repos background services started successfully",
            extra={"correlation_id": get_correlation_id()},
        )

    def stop(self) -> None:
        """
        Stop all background services gracefully.

        Shutdown order (reverse of startup):
        1. RefreshScheduler
        2. CleanupManager

        Waits for threads to exit gracefully.

        Idempotent: Safe to call multiple times
        """
        if not self._running:
            logger.debug(
                "GlobalReposLifecycleManager already stopped",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        logger.info(
            "Stopping global repos background services",
            extra={"correlation_id": get_correlation_id()},
        )

        # Stop RefreshScheduler first (reverse order)
        self.refresh_scheduler.stop()
        logger.debug(
            "RefreshScheduler stopped", extra={"correlation_id": get_correlation_id()}
        )

        # Stop CleanupManager
        self.cleanup_manager.stop()
        logger.debug(
            "CleanupManager stopped", extra={"correlation_id": get_correlation_id()}
        )

        self._running = False
        logger.info(
            "Global repos background services stopped successfully",
            extra={"correlation_id": get_correlation_id()},
        )
