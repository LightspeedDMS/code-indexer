"""DeploymentLock - deployment concurrency control via PID-based lock file."""

from code_indexer.server.middleware.correlation import get_correlation_id
from pathlib import Path
import os
import logging
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Bug #879 / Bug #1175: Honor CIDX_DATA_DIR so both cidx-server and cidx-auto-update
# (which may run as different OS users) resolve the lock path identically.
# NEVER put the lock under /tmp — systemd PrivateTmp=yes isolates /tmp per-service,
# causing PermissionError (EACCES) when the auto-updater tries to access cidx-server's
# private /tmp.
_cidx_data_dir = Path(
    os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server"))
)


def get_default_lock_path() -> Path:
    """Return the default lock file path anchored to CIDX_DATA_DIR.

    The lock file is placed in the data directory (not /tmp) so it is
    accessible to both the cidx-server and cidx-auto-update systemd units
    even when PrivateTmp=yes isolates each service's /tmp namespace.

    Returns:
        Path to the default lock file location.
    """
    return _cidx_data_dir / "cidx-auto-update.lock"


class DeploymentLock:
    """Manages deployment lock using PID-based lock file mechanism."""

    def __init__(self, lock_file: Path):
        """Initialize DeploymentLock.

        Args:
            lock_file: Path to lock file
        """
        self.lock_file = lock_file

    def _lock_file_exists(self) -> bool:
        try:
            return self.lock_file.exists()
        except OSError:
            return False

    def acquire(self) -> bool:
        """Attempt to acquire deployment lock.

        Returns:
            True if lock acquired, False if another deployment is in progress

        Raises:
            IOError: If lock file operations fail
        """
        # Check if lock file exists
        if self._lock_file_exists():
            # Read PID from lock file
            try:
                with open(self.lock_file, "r+") as f:
                    pid_str = f.read().strip()

                # Try to parse PID
                try:
                    pid = int(pid_str)
                except ValueError:
                    # Invalid PID - treat as stale
                    logger.warning(
                        format_error_log(
                            "GIT-GENERAL-001",
                            f"Invalid PID in lock file: {pid_str}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    self.lock_file.unlink()
                else:
                    # Check if process is alive
                    try:
                        os.kill(pid, 0)
                        # Process is alive - lock is held
                        logger.info(
                            f"Lock held by active process {pid}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return False
                    except OSError:
                        # Process is dead - stale lock
                        logger.info(
                            f"Removing stale lock (PID {pid})",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        self.lock_file.unlink()

            except IOError as e:
                logger.error(
                    format_error_log(
                        "GIT-GENERAL-002",
                        f"Error reading lock file: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                raise

        # Create lock file with current PID
        try:
            with open(self.lock_file, "w") as f:
                f.write(str(os.getpid()))
            logger.info(
                f"Lock acquired (PID {os.getpid()})",
                extra={"correlation_id": get_correlation_id()},
            )
            return True
        except OSError as e:
            # Fail-soft: under systemd PrivateTmp=yes the lock file path may be
            # unwritable (EACCES/PermissionError).  Losing best-effort mutual
            # exclusion is strictly better than a permanently un-updatable node —
            # the worst case is two overlapping deploys on the same node, which
            # systemctl restart handles safely.  NEVER re-raise here.
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-003",
                    f"Cannot create lock file (proceeding without lock): {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return True

    def release(self) -> None:
        """Release deployment lock by removing lock file.

        Does not raise exceptions if lock file doesn't exist or can't be deleted.
        """
        if not self._lock_file_exists():
            logger.debug(
                "Lock file doesn't exist, nothing to release",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        try:
            self.lock_file.unlink()
            logger.info("Lock released", extra={"correlation_id": get_correlation_id()})
        except (IOError, OSError, PermissionError) as e:
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-004",
                    f"Error removing lock file: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    def is_stale(self) -> bool:
        """Check if lock file represents a stale lock (process is dead).

        Returns:
            True if lock is stale, False if lock is active or doesn't exist
        """
        if not self._lock_file_exists():
            return False

        try:
            with open(self.lock_file, "r+") as f:
                pid_str = f.read().strip()

            # Try to parse PID
            try:
                pid = int(pid_str)
            except ValueError:
                # Invalid PID - consider stale
                return True

            # Check if process is alive
            try:
                os.kill(pid, 0)
                # Process is alive - not stale
                return False
            except OSError:
                # Process is dead - stale
                return True

        except IOError:
            # Can't read lock file - assume not stale
            return False
