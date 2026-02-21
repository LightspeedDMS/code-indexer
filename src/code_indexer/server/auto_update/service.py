"""AutoUpdateService - polling service for automatic CIDX server deployment."""

from code_indexer.server.middleware.correlation import get_correlation_id
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from .change_detector import ChangeDetector
    from .deployment_lock import DeploymentLock
    from .deployment_executor import DeploymentExecutor
from code_indexer.server.auto_update.deployment_executor import (
    LEGACY_REDEPLOY_MARKER,
    PENDING_REDEPLOY_MARKER,
)
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


class ServiceState(Enum):
    """Service state machine states."""

    IDLE = "idle"
    CHECKING = "checking"
    DEPLOYING = "deploying"
    RESTARTING = "restarting"


class AutoUpdateService:
    """Auto-update service for polling and deploying CIDX server updates."""

    def __init__(
        self,
        repo_path: Path,
        check_interval: int,
        lock_file: Optional[Path] = None,
    ):
        """Initialize AutoUpdateService.

        Args:
            repo_path: Path to git repository
            check_interval: Polling interval in seconds
            lock_file: Path to lock file (default: /tmp/cidx-auto-update.lock)
        """
        self.repo_path = repo_path
        self.check_interval = check_interval
        self.lock_file = lock_file or Path("/tmp/cidx-auto-update.lock")
        self.current_state = ServiceState.IDLE
        self.last_deployment: Optional[datetime] = None
        self.last_error: Optional[Exception] = None

        # Components injected for testing (must be set before calling poll_once)
        self.change_detector: Optional["ChangeDetector"] = None
        self.deployment_lock: Optional["DeploymentLock"] = None
        self.deployment_executor: Optional["DeploymentExecutor"] = None

    def transition_to(self, new_state: ServiceState) -> None:
        """Transition to a new state.

        Args:
            new_state: Target state to transition to
        """
        logger.info(
            f"State transition: {self.current_state.value} -> {new_state.value}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Record timestamp when entering DEPLOYING state
        if new_state == ServiceState.DEPLOYING:
            self.last_deployment = datetime.now()

        self.current_state = new_state

    def poll_once(self) -> None:
        """Execute one polling iteration.

        Checks for changes and triggers deployment if needed.
        Only runs when in IDLE state to prevent concurrent operations.

        Issue #154: Also checks for pending-redeploy marker and forces deployment
        if present, bypassing normal change detection flow.
        """
        # Validate components are injected before use
        assert (
            self.change_detector is not None
        ), "change_detector must be set before calling poll_once()"
        assert (
            self.deployment_lock is not None
        ), "deployment_lock must be set before calling poll_once()"
        assert (
            self.deployment_executor is not None
        ), "deployment_executor must be set before calling poll_once()"

        # Issue #154: Check for pending-redeploy marker FIRST (before state checks)
        # Backwards compatibility: migrate legacy marker path (v8.15.0 wrote to /var/lib/)
        if not PENDING_REDEPLOY_MARKER.exists() and LEGACY_REDEPLOY_MARKER.exists():
            logger.info(
                "Found legacy redeploy marker at old path, migrating",
                extra={"correlation_id": get_correlation_id()},
            )
            try:
                PENDING_REDEPLOY_MARKER.parent.mkdir(parents=True, exist_ok=True)
                PENDING_REDEPLOY_MARKER.touch()
                LEGACY_REDEPLOY_MARKER.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(
                    f"Could not migrate legacy marker: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )

        if PENDING_REDEPLOY_MARKER.exists():
            logger.info(
                "Pending redeploy marker found, forcing deployment",
                extra={"correlation_id": get_correlation_id()},
            )

            try:
                self.transition_to(ServiceState.DEPLOYING)
                success = self.deployment_executor.execute()

                if success:
                    self.transition_to(ServiceState.RESTARTING)
                    restart_ok = self.deployment_executor.restart_server()
                    if restart_ok:
                        logger.info(
                            "Forced redeployment and restart completed successfully",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        logger.error(
                            format_error_log(
                                "AUTO-UPDATE-010",
                                "Forced deployment succeeded but server restart FAILED",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                else:
                    logger.error(
                        format_error_log(
                            "AUTO-UPDATE-011",
                            "Forced redeployment failed",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            except Exception as e:
                logger.exception(
                    f"Forced redeployment error: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
                self.last_error = e

            finally:
                # Always remove marker to prevent infinite retry loops
                try:
                    PENDING_REDEPLOY_MARKER.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(
                        f"Could not remove redeploy marker: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                self.transition_to(ServiceState.IDLE)

            return

        # Skip if not in IDLE state
        if self.current_state != ServiceState.IDLE:
            logger.debug(
                f"Skipping poll - current state: {self.current_state.value}",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        try:
            # Transition to CHECKING state
            self.transition_to(ServiceState.CHECKING)

            # Check for changes
            has_changes = self.change_detector.has_changes()

            if not has_changes:
                # No changes - return to IDLE
                logger.debug(
                    "No changes detected",
                    extra={"correlation_id": get_correlation_id()},
                )
                self.transition_to(ServiceState.IDLE)
                return

            # Changes detected - attempt deployment
            logger.info(
                "Changes detected, attempting deployment",
                extra={"correlation_id": get_correlation_id()},
            )

            # Try to acquire deployment lock
            if not self.deployment_lock.acquire():
                logger.warning(
                    format_error_log(
                        "GIT-GENERAL-005",
                        "Another deployment in progress, skipping",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                self.transition_to(ServiceState.IDLE)
                return

            try:
                # Execute deployment
                self.transition_to(ServiceState.DEPLOYING)
                success = self.deployment_executor.execute()

                if success:
                    # Restart server after successful deployment
                    self.transition_to(ServiceState.RESTARTING)
                    restart_ok = self.deployment_executor.restart_server()
                    if restart_ok:
                        logger.info(
                            "Deployment and restart completed successfully",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        logger.error(
                            format_error_log(
                                "AUTO-UPDATE-012",
                                "Deployment succeeded but server restart FAILED",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                else:
                    logger.error(
                        format_error_log(
                            "GIT-GENERAL-006",
                            "Deployment failed",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            except Exception as e:
                # Record error and continue
                logger.exception(
                    f"Deployment error: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
                self.last_error = e

            finally:
                # Always release lock
                self.deployment_lock.release()
                # Return to IDLE state
                self.transition_to(ServiceState.IDLE)

        except Exception as e:
            # Catch any unexpected errors during polling
            logger.exception(
                f"Unexpected error during polling: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            self.last_error = e
            self.transition_to(ServiceState.IDLE)
