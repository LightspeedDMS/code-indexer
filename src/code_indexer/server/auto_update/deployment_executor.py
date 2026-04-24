"""DeploymentExecutor - deployment command execution for auto-update service.

v9.3.7: Verified self-restart + marker + forced-redeploy cycle on staging.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from code_indexer.server.utils.ripgrep_installer import RipgrepInstaller
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast
import hashlib
import json
import subprocess
import logging
import time
import sys
import os
import pwd
import shutil

import requests
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Bug #879: Honor CIDX_DATA_DIR env var so cidx-server (User=code-indexer,
# HOME=/opt/code-indexer) and cidx-auto-update (User=root, HOME=/root) resolve
# the IPC paths identically.  Falls back to Path.home()/.cidx-server so same-
# user dev installs continue to work without any configuration change.
_cidx_data_dir = Path(
    os.environ.get("CIDX_DATA_DIR", str(Path.home() / ".cidx-server"))
)

# Issue #154: Pending redeploy marker for self-healing Python environment
# Note: Using ~/.cidx-server/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
# and /var/lib/ is not writable by non-root service users
PENDING_REDEPLOY_MARKER = _cidx_data_dir / "pending-redeploy"
# Legacy marker path used in v8.15.0 (before path moved to ~/.cidx-server/)
LEGACY_REDEPLOY_MARKER = Path("/var/lib/cidx-pending-redeploy")
AUTO_UPDATE_SERVICE_NAME = "cidx-auto-update"
# Systemd unit directory — configurable for testing or non-standard deployments.
# Default is the Linux standard /etc/systemd/system.
SYSTEMD_UNIT_DIR = Path(os.environ.get("SYSTEMD_UNIT_DIR", "/etc/systemd/system"))

# Self-restart mechanism constants
# Note: Using ~/.cidx-server/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
# and /var/lib/ is not writable by non-root service users
AUTO_UPDATE_STATUS_FILE = _cidx_data_dir / "auto-update-status.json"
SYSTEMCTL_TIMEOUT_SECONDS = 30  # Timeout for systemctl restart operations

# Story #355: Signal-based server restart via auto-updater
# Server writes this file to request a restart; auto-updater detects and executes it.
# Using ~/.cidx-server/ to avoid systemd PrivateTmp=yes isolation issues.
RESTART_SIGNAL_PATH = _cidx_data_dir / "restart.signal"
# Signals older than this threshold (seconds) are treated as stale (from a previous crash)
# and deleted without triggering a restart. Set to 2x the typical poll interval.
RESTART_SIGNAL_STALENESS_THRESHOLD = 120

# Hnswlib fallback constants (Bug #160)
# Note: Using /var/tmp/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
HNSWLIB_FALLBACK_PATH = Path("/var/tmp/cidx-hnswlib")
HNSWLIB_REPO_URL = "https://github.com/LightspeedDMS/hnswlib.git"

# Bug #839: Claude CLI auto-update timeout constants
NPM_VERSION_TIMEOUT_SECONDS = 5  # How long to wait for `npm --version` probe
CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS = 180  # How long to wait for npm global install


class DeploymentExecutor:
    """Executes deployment commands: git pull, pip install, systemd restart.

    Story #734: Supports graceful drain mode during auto-update.
    """

    def __init__(
        self,
        repo_path: Path,
        branch: str = "master",
        service_name: str = "cidx-server",
        server_url: str = "http://localhost:8000",
        drain_timeout: int = 300,
        drain_poll_interval: int = 10,
    ):
        """Initialize DeploymentExecutor.

        Args:
            repo_path: Path to git repository
            branch: Git branch to pull from (default: master)
            service_name: Systemd service name (default: cidx-server)
            server_url: CIDX server URL for maintenance API (default: http://localhost:8000)
            drain_timeout: Max seconds to wait for drain (default: 300)
            drain_poll_interval: Seconds between drain status checks (default: 10)
        """
        self.repo_path = repo_path
        self.branch = branch
        self.service_name = service_name
        self.server_url = server_url
        self.drain_timeout = drain_timeout
        self.drain_poll_interval = drain_poll_interval

    def _get_auth_token(self) -> Optional[str]:
        """Generate JWT token directly using the server's JWT secret.

        The auto-updater runs as the same OS user as the server, so it can
        read the JWT secret file directly. This avoids needing to know the
        admin password or make HTTP calls for authentication.

        A fresh token is generated per call to avoid expiry during long
        deployments (Bug #243).

        Returns:
            JWT token string if generation successful, None on error
        """
        try:
            from code_indexer.server.utils.jwt_secret_manager import JWTSecretManager
            from code_indexer.server.auth.jwt_manager import JWTManager

            secret_manager = JWTSecretManager()
            secret_key = secret_manager.get_or_create_secret()

            jwt_manager = JWTManager(secret_key=secret_key, token_expiration_minutes=10)

            token: str = jwt_manager.create_token(
                {
                    "username": "admin",
                    "role": "admin",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            )

            logger.debug(
                "Generated JWT token for maintenance API",
                extra={"correlation_id": get_correlation_id()},
            )
            return token

        except FileNotFoundError:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-080",
                    "JWT secret file not found - server may not be initialized",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return None
        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-081",
                    f"Error generating JWT token: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return None

    def _enter_maintenance_mode(self) -> bool:
        """Enter maintenance mode via server API.

        Returns:
            True if successful, False on error (e.g., connection refused)
        """
        try:
            # Get authentication token
            token = self._get_auth_token()
            if not token:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-082",
                        "Could not obtain auth token for maintenance mode",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            url = f"{self.server_url}/api/admin/maintenance/enter"
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.post(url, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(
                    "Entered maintenance mode",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-001",
                    f"Failed to enter maintenance mode: {response.status_code}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        except requests.exceptions.ConnectionError:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-002",
                    "Could not connect to server for maintenance mode - proceeding anyway",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-003",
                    f"Error entering maintenance mode: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _get_drain_timeout(self) -> int:
        """Get drain timeout dynamically from server config (Bug #135).

        Queries /api/admin/maintenance/drain-timeout endpoint to get recommended
        drain timeout based on configured job timeouts. Falls back to 2 hours
        if server is unavailable or returns error.

        Returns:
            Drain timeout in seconds (from server or fallback value)
        """
        try:
            # Get authentication token
            token = self._get_auth_token()
            if not token:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-083",
                        "Could not obtain auth token for drain timeout - using fallback",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                fallback_timeout = 7200
                return fallback_timeout

            url = f"{self.server_url}/api/admin/maintenance/drain-timeout"
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                recommended_timeout = data.get("recommended_drain_timeout_seconds")

                if recommended_timeout and isinstance(recommended_timeout, int):
                    logger.info(
                        f"Using dynamic drain timeout from server: {recommended_timeout}s",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return int(recommended_timeout)  # Explicit cast for mypy

            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-029",
                    f"Server returned invalid drain timeout response: {response.status_code}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        except requests.exceptions.ConnectionError:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-030",
                    "Could not connect to server for drain timeout - using fallback",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-031",
                    f"Error getting drain timeout: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Fallback: 2 hours (reasonable for max job timeout of 1 hour)
        fallback_timeout = 7200
        logger.info(
            f"Using fallback drain timeout: {fallback_timeout}s",
            extra={"correlation_id": get_correlation_id()},
        )
        return fallback_timeout

    def _wait_for_drain(self) -> bool:
        """Wait for jobs to drain before restart.

        Bug #135: Uses dynamic timeout from server config instead of hardcoded value.
        Bug #882: Exits early if the server is unreachable for several
        STRICTLY CONSECUTIVE polls — if cidx-server is already down, there is
        nothing to drain, and holding the auto-update systemd unit past its
        TimeoutStartSec would kill the whole upgrade cycle. Any non-
        ConnectionError iteration outcome (HTTP response received, auth
        failure, generic exception) resets the counter so cumulative errors
        of mixed kinds do not trigger the early exit.

        Returns:
            True if drained (or server unreachable), False if timeout
        """
        # Get dynamic timeout from server
        drain_timeout = self._get_drain_timeout()

        start_time = time.time()
        # Bug #882: cap wasted time when cidx-server is unreachable. At the
        # default 10s poll interval this is ~30s — well under the 120s systemd
        # TimeoutStartSec budget on the auto-update unit.
        consecutive_conn_errors = 0
        max_consecutive_conn_errors = 3

        while time.time() - start_time < drain_timeout:
            try:
                # Get authentication token
                token = self._get_auth_token()
                if not token:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-084",
                            "Could not obtain auth token for drain status check",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    # Auth-token failure is not a connection failure — reset
                    # the consecutive counter so it does not trigger the
                    # Bug #882 early-exit path.
                    consecutive_conn_errors = 0
                    time.sleep(self.drain_poll_interval)
                    continue

                url = f"{self.server_url}/api/admin/maintenance/drain-status"
                headers = {"Authorization": f"Bearer {token}"}
                response = requests.get(url, headers=headers, timeout=10)
                # Server responded (even non-200) → reset the unreachable counter.
                consecutive_conn_errors = 0

                if response.status_code == 200:
                    data = response.json()
                    if data.get("drained", False):
                        logger.info(
                            "System drained, ready for restart",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return True

                    logger.info(
                        f"Waiting for drain: {data.get('running_jobs', 0)} running, "
                        f"{data.get('queued_jobs', 0)} queued",
                        extra={"correlation_id": get_correlation_id()},
                    )

            except requests.exceptions.ConnectionError:
                consecutive_conn_errors += 1
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-004",
                        f"Could not connect to server for drain status "
                        f"(consecutive failures: {consecutive_conn_errors})",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                if consecutive_conn_errors >= max_consecutive_conn_errors:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-140",
                            f"Server unreachable for {consecutive_conn_errors} "
                            "consecutive polls — assuming drained and proceeding "
                            "with deployment (Bug #882: avoids burning systemd "
                            "TimeoutStartSec budget).",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return True
            except Exception as e:
                # A non-connection exception breaks the "consecutive" chain.
                consecutive_conn_errors = 0
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-005",
                        f"Error checking drain status: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

            time.sleep(self.drain_poll_interval)

        logger.warning(
            format_error_log(
                "DEPLOY-GENERAL-006",
                f"Drain timeout ({drain_timeout}s) exceeded",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return False

    def _exit_maintenance_mode(self) -> bool:
        """Exit maintenance mode via server API.

        Returns:
            True if successful, False on error
        """
        try:
            # Get authentication token
            token = self._get_auth_token()
            if not token:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-085",
                        "Could not obtain auth token to exit maintenance mode",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            url = f"{self.server_url}/api/admin/maintenance/exit"
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.post(url, headers=headers, timeout=10)

            if response.status_code == 200:
                logger.info(
                    "Exited maintenance mode",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-007",
                    f"Failed to exit maintenance mode: {response.status_code}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-008",
                    f"Error exiting maintenance mode: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _get_running_jobs_for_logging(self) -> list:
        """Get running jobs from drain-status endpoint for logging.

        Story #734 AC4: Fetch job details to log when forcing restart.

        Returns:
            List of job dicts with job_id, operation_type, started_at, progress
        """
        try:
            # Get authentication token
            token = self._get_auth_token()
            if not token:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-086",
                        "Could not obtain auth token to fetch running jobs for logging",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return []

            url = f"{self.server_url}/api/admin/maintenance/drain-status"
            headers = {"Authorization": f"Bearer {token}"}
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                jobs: list = data.get("jobs", [])
                return jobs

            return []

        except requests.exceptions.ConnectionError:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-009",
                    "Could not connect to server to get running jobs",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return []
        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-010",
                    f"Error getting running jobs: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return []

    def git_pull(self) -> bool:
        """Execute git pull to update repository.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = subprocess.run(
                ["git", "pull", "origin", self.branch],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                env=build_non_interactive_git_env(),
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-011",
                        f"Git pull failed: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                f"Git pull successful: {result.stdout.strip()}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.exception(
                f"Git pull exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _ensure_submodule_safe_directory(self) -> bool:
        """Add submodule paths to git safe.directory config.

        Git's "dubious ownership" check applies to each git repository independently.
        Submodules are separate repositories and need their own safe.directory entries.

        Returns:
            True if successful or not needed, False on error
        """
        try:
            # Known submodule paths
            submodule_paths = [
                self.repo_path / "third_party" / "hnswlib",
            ]

            for submodule_path in submodule_paths:
                # Skip if submodule directory doesn't exist yet
                if not submodule_path.exists():
                    continue

                # Add to global safe.directory (runs as root, so use root's config)
                result = subprocess.run(
                    [
                        "git",
                        "config",
                        "--global",
                        "--add",
                        "safe.directory",
                        str(submodule_path),
                    ],
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    logger.warning(
                        f"Could not add submodule to safe.directory: {submodule_path}: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                else:
                    logger.info(
                        f"Added submodule to git safe.directory: {submodule_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )

            return True

        except Exception as e:
            logger.warning(
                f"Error configuring submodule safe.directory: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True  # Non-fatal, continue with deployment

    def _cleanup_submodule_state(self, submodule_path: str) -> bool:
        """Clean up partial submodule initialization state.

        Removes both the git modules directory and the worktree directory
        to allow fresh initialization. Uses sudo rm -rf since the service
        runs as root.

        Args:
            submodule_path: Relative path to submodule (e.g., "third_party/hnswlib")

        Returns:
            True if cleanup successful, False on error
        """
        try:
            git_modules_path = self.repo_path / ".git" / "modules" / submodule_path
            worktree_path = self.repo_path / submodule_path

            # Remove .git/modules/{submodule_path}
            result = subprocess.run(
                ["sudo", "rm", "-rf", str(git_modules_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-060",
                        f"Failed to remove git modules directory: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                f"Removed git modules directory: {git_modules_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Remove worktree directory
            result = subprocess.run(
                ["sudo", "rm", "-rf", str(worktree_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-061",
                        f"Failed to remove worktree directory: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                f"Removed worktree directory: {worktree_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-062",
                    f"Exception during submodule cleanup: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _is_recoverable_submodule_error(self, stderr: str) -> bool:
        """Check if submodule error is recoverable with cleanup and retry.

        Recoverable errors indicate partial initialization state that can be
        fixed by removing and retrying. Non-recoverable errors (network, auth)
        should not trigger retry.

        Args:
            stderr: Error output from git submodule command

        Returns:
            True if error is recoverable, False otherwise
        """
        recoverable_patterns = [
            "could not lock",  # Git error: "could not lock config file"
            "already exists",
            "could not get a repository handle",
            "worktree",
        ]

        non_recoverable_patterns = [
            "Could not resolve host",
            "unable to access",
            "Authentication failed",
        ]

        # Check non-recoverable first (takes precedence)
        for pattern in non_recoverable_patterns:
            if pattern in stderr:
                return False

        # Check recoverable patterns
        for pattern in recoverable_patterns:
            if pattern in stderr:
                return True

        return False

    def git_submodule_update(self) -> bool:
        """Initialize and update the hnswlib submodule only.

        Required for custom hnswlib build from third_party/hnswlib submodule.
        The custom build includes check_integrity() method for HNSW index validation.

        Note: Only initializes third_party/hnswlib, not test fixture submodules.
        Production servers don't need test-fixtures/* submodules, and initializing
        all submodules with --recursive causes safe.directory errors.

        Resilient to partial initialization state: If update fails with recoverable
        error (lock file, already exists, worktree config), cleans up state and
        retries once. Does not retry non-recoverable errors (network, auth).

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure submodule paths are in git safe.directory before update
            self._ensure_submodule_safe_directory()

            submodule_path = "third_party/hnswlib"

            # Attempt submodule update
            # Service runs as root, no sudo needed
            # Only initialize the specific submodule we need (not --recursive)
            result = subprocess.run(
                ["git", "submodule", "update", "--init", submodule_path],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                # Check if this is a recoverable error
                if self._is_recoverable_submodule_error(result.stderr):
                    logger.warning(
                        f"Submodule update failed with recoverable error, attempting cleanup and retry: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )

                    # Clean up partial state
                    if not self._cleanup_submodule_state(submodule_path):
                        logger.error(
                            format_error_log(
                                "DEPLOY-GENERAL-063",
                                "Failed to clean up submodule state, cannot retry",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        return False

                    # Retry once after cleanup
                    logger.info(
                        "Retrying submodule update after cleanup",
                        extra={"correlation_id": get_correlation_id()},
                    )

                    result = subprocess.run(
                        ["git", "submodule", "update", "--init", submodule_path],
                        cwd=self.repo_path,
                        capture_output=True,
                        text=True,
                    )

                    if result.returncode != 0:
                        logger.error(
                            format_error_log(
                                "DEPLOY-GENERAL-064",
                                f"Submodule update retry failed: {result.stderr}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        return False
                else:
                    # Non-recoverable error, don't retry
                    logger.error(
                        format_error_log(
                            "DEPLOY-GENERAL-040",
                            f"Git submodule update failed (non-recoverable): {result.stderr}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return False

            logger.info(
                f"Git submodule update successful: {result.stdout.strip() or 'submodules initialized'}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.exception(
                f"Git submodule update exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _clone_hnswlib_standalone(self) -> bool:
        """Clone hnswlib to standalone location as fallback when submodule fails.

        Bug #160: Fallback mechanism to bypass submodule lock file permission errors.
        Clones LightspeedDMS/hnswlib fork (has check_integrity method) to /var/tmp.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Remove existing directory if present (clean slate)
            if HNSWLIB_FALLBACK_PATH.exists():
                try:
                    shutil.rmtree(HNSWLIB_FALLBACK_PATH)
                    logger.info(
                        f"Removed existing fallback directory: {HNSWLIB_FALLBACK_PATH}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                except OSError as e:
                    logger.error(
                        format_error_log(
                            "DEPLOY-GENERAL-070",
                            f"Failed to remove existing fallback directory: {e}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return False

            # Add fallback path to git safe.directory
            result = subprocess.run(
                [
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "safe.directory",
                    str(HNSWLIB_FALLBACK_PATH),
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.warning(
                    f"Could not add fallback path to safe.directory: {result.stderr}",
                    extra={"correlation_id": get_correlation_id()},
                )
                # Not fatal, continue with clone

            # Clone hnswlib to fallback location
            result = subprocess.run(
                ["git", "clone", HNSWLIB_REPO_URL, str(HNSWLIB_FALLBACK_PATH)],
                capture_output=True,
                text=True,
                timeout=60,
                env=build_non_interactive_git_env(),
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-071",
                        f"Failed to clone hnswlib standalone: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                f"Successfully cloned hnswlib to fallback location: {HNSWLIB_FALLBACK_PATH}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except subprocess.TimeoutExpired:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-071",
                    "Hnswlib standalone clone timed out after 60 seconds",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-071",
                    f"Exception during hnswlib standalone clone: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_build_dependencies(self) -> bool:
        """Ensure C++ build dependencies are installed for compiling hnswlib.

        Required packages: gcc-c++ (g++), python3-devel, libgomp (OpenMP).
        Uses dnf with fallback to yum for compatibility with Rocky/Amazon Linux.

        Returns:
            True if dependencies are available, False on installation failure
        """
        # Check if g++ already exists (idempotent check)
        result = subprocess.run(
            ["which", "g++"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.debug(
                f"C++ compiler already available: {result.stdout.strip()}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        # g++ not found - install build dependencies
        logger.info(
            "C++ compiler not found, installing build dependencies",
            extra={"correlation_id": get_correlation_id()},
        )

        packages = ["gcc-c++", "python3-devel", "libgomp"]

        # Try dnf first (Rocky Linux 8+, Amazon Linux 2023)
        for pkg_manager in ["dnf", "yum"]:
            result = subprocess.run(
                ["which", pkg_manager],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                install_cmd = ["sudo", pkg_manager, "install", "-y"] + packages
                result = subprocess.run(
                    install_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                if result.returncode == 0:
                    logger.info(
                        f"Build dependencies installed via {pkg_manager}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return True
                else:
                    logger.warning(
                        f"{pkg_manager} install failed: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    # Try next package manager
                    continue

        logger.error(
            format_error_log(
                "DEPLOY-GENERAL-045",
                "Failed to install build dependencies - no compatible package manager",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return False

    def build_custom_hnswlib(self, hnswlib_path: Optional[Path] = None) -> bool:
        """Build and install custom hnswlib from specified path or default submodule.

        The custom hnswlib fork includes the check_integrity() method for HNSW
        index validation. This must be built from source and installed to replace
        the standard pip-installed hnswlib.

        Args:
            hnswlib_path: Path to hnswlib source directory. If None, uses default
                         submodule path (third_party/hnswlib).

        Returns:
            True if successful or submodule not present, False on build failure
        """
        if hnswlib_path is None:
            hnswlib_path = self.repo_path / "third_party" / "hnswlib"

        # Skip if submodule not initialized (not a fatal error)
        if not hnswlib_path.exists() or not (hnswlib_path / "setup.py").exists():
            logger.warning(
                "Custom hnswlib submodule not found, skipping build",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        # Ensure build dependencies are installed (idempotent)
        if not self._ensure_build_dependencies():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-046",
                    "Cannot build custom hnswlib - build dependencies unavailable",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        try:
            python_path = self._get_server_python()

            # Install pybind11 first - required because setup.py imports it at module level
            # Use sudo because pipx venv may be owned by root (e.g., /opt/pipx/venvs/)
            pybind_result = subprocess.run(
                [
                    "sudo",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                    "--break-system-packages",
                    "pybind11",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if pybind_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-047",
                        f"pybind11 installation failed: {pybind_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "pybind11 installed successfully",
                extra={"correlation_id": get_correlation_id()},
            )
            # Use sudo because pipx venv may be owned by root (e.g., /opt/pipx/venvs/)
            result = subprocess.run(
                [
                    "sudo",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                    "--break-system-packages",
                    "--force-reinstall",
                    "--no-deps",
                    ".",
                ],
                cwd=hnswlib_path,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for compilation
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-042",
                        f"Custom hnswlib build failed: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "Custom hnswlib build and install successful",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except subprocess.TimeoutExpired:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-043",
                    "Custom hnswlib build timed out after 5 minutes",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except Exception as e:
            logger.exception(
                f"Custom hnswlib build exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _build_hnswlib_with_fallback(self) -> bool:
        """Build custom hnswlib with fallback to standalone clone if submodule fails.

        Bug #160: Unified method that tries submodule first, then falls back to
        cloning hnswlib to standalone location if submodule has no setup.py.

        Strategy:
        1. Check if submodule path has setup.py
        2. If yes: build from submodule (normal path)
        3. If no: clone to fallback location and build from there

        Returns:
            True if either approach succeeds, False if both fail
        """
        submodule_path = self.repo_path / "third_party" / "hnswlib"
        submodule_setup_py = submodule_path / "setup.py"

        # Try submodule first if setup.py exists
        if submodule_setup_py.exists():
            logger.info(
                "Building hnswlib from submodule path",
                extra={"correlation_id": get_correlation_id()},
            )
            return self.build_custom_hnswlib(hnswlib_path=None)

        # Submodule doesn't have setup.py - use fallback approach
        logger.warning(
            "Submodule setup.py not found, attempting fallback clone",
            extra={"correlation_id": get_correlation_id()},
        )

        # Clone to standalone location
        if not self._clone_hnswlib_standalone():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-073",
                    "Both submodule and fallback clone approaches failed",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        # Build from fallback location
        logger.info(
            "Building hnswlib from fallback location",
            extra={"correlation_id": get_correlation_id()},
        )
        if not self.build_custom_hnswlib(hnswlib_path=HNSWLIB_FALLBACK_PATH):
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-072",
                    "Fallback build failed",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        logger.info(
            "Successfully built hnswlib from fallback location",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _get_server_python(self) -> str:
        """Extract Python interpreter from server's service file ExecStart line.

        Issue #154: Reads cidx-server.service to find the actual Python being used,
        so pip install targets the correct environment (e.g., pipx venv, not system Python).

        Returns:
            Python interpreter path from ExecStart, or sys.executable on error
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")
        try:
            result = subprocess.run(
                ["sudo", "cat", str(service_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logger.warning(
                    f"Could not read {service_path}, using sys.executable",
                    extra={"correlation_id": get_correlation_id()},
                )
                return sys.executable

            # Parse ExecStart line
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("ExecStart="):
                    exec_command = line.split("=", 1)[1].strip()
                    python_path = exec_command.split()[0]
                    if Path(python_path).exists():
                        logger.info(
                            f"Using server Python: {python_path}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return python_path

            logger.warning(
                "Could not parse ExecStart, using sys.executable",
                extra={"correlation_id": get_correlation_id()},
            )
            return sys.executable

        except Exception as e:
            logger.warning(
                f"Error reading service file: {e}, using sys.executable",
                extra={"correlation_id": get_correlation_id()},
            )
            return sys.executable

    def _read_service_file(self, service_path: Path) -> Optional[str]:
        """Read systemd service file content.

        Args:
            service_path: Path to service file

        Returns:
            Service file content as string, or None on error
        """
        try:
            result = subprocess.run(
                ["sudo", "cat", str(service_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logger.warning(
                    f"Could not read {service_path}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return None

            return result.stdout

        except Exception as e:
            logger.warning(
                f"Error reading {service_path}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

    def _write_service_file_and_reload(self, service_path: Path, content: str) -> bool:
        """Write systemd service file via sudo tee and reload daemon.

        Args:
            service_path: Path to service file
            content: New service file content

        Returns:
            True if successful, False on error
        """
        try:
            # Write via sudo tee
            result = subprocess.run(
                ["sudo", "tee", str(service_path)],
                input=content,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-032",
                        f"Failed to write {service_path}: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Reload systemd
            result = subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-033",
                        f"Failed to reload systemd: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-034",
                    f"Error writing service file: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_auto_updater_uses_server_python(self) -> bool:
        """Ensure auto-updater service uses same Python as main server.

        Issue #154: Self-healing mechanism to fix Python environment mismatches.
        If auto-updater uses different Python than server (e.g., /usr/bin/python3
        vs /opt/pipx/venvs/code-indexer/bin/python), updates the auto-updater
        service file and creates pending-redeploy marker.

        Returns:
            True if config is correct or was updated, False on error
        """
        try:
            server_python = self._get_server_python()
            auto_update_service = Path(
                f"/etc/systemd/system/{AUTO_UPDATE_SERVICE_NAME}.service"
            )

            # Read current service file
            current_content = self._read_service_file(auto_update_service)
            if current_content is None:
                return False

            # Check and update ExecStart line
            new_lines = []
            needs_update = False

            for line in current_content.splitlines():
                if line.strip().startswith("ExecStart="):
                    exec_part = line.split("=", 1)[1].strip()
                    current_python = exec_part.split()[0]

                    if current_python != server_python:
                        new_exec = exec_part.replace(current_python, server_python, 1)
                        new_lines.append(f"ExecStart={new_exec}")
                        needs_update = True
                        logger.info(
                            f"Updating auto-updater Python: {current_python} -> {server_python}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            if not needs_update:
                logger.info(
                    "Auto-updater already uses correct Python",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Write updated service file
            new_content = "\n".join(new_lines) + "\n"
            if not self._write_service_file_and_reload(
                auto_update_service, new_content
            ):
                return False

            # Create pending-redeploy marker
            PENDING_REDEPLOY_MARKER.parent.mkdir(parents=True, exist_ok=True)
            PENDING_REDEPLOY_MARKER.touch()
            logger.info(
                f"Created pending-redeploy marker: {PENDING_REDEPLOY_MARKER}",
                extra={"correlation_id": get_correlation_id()},
            )

            return True

        except Exception as e:
            logger.exception(
                f"Error ensuring auto-updater Python: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _get_server_data_dir(self) -> Optional[str]:
        """Resolve the server user's .cidx-server data directory path.

        Reads the server systemd service file, extracts the User= directive,
        and resolves that user's home directory via pwd.  Returns None when the
        server service file is absent (non-fatal fresh-install) OR when no
        User= directive is present (same-user deployment: both processes share
        the same home directory, so no CIDX_DATA_DIR injection is required).

        Returns:
            Absolute path string for the data dir when server has a User=
            directive; None when service file is absent OR has no User=
            directive (same-user deployment — no action needed).
        """
        server_service = Path(f"/etc/systemd/system/{self.service_name}.service")
        server_content = self._read_service_file(server_service)
        if server_content is None:
            return None  # Service file absent — non-fatal

        service_user = self._extract_service_user(server_content)
        if not service_user:
            # Same-user deployment: no User= directive means server runs as the
            # same user as the auto-updater, so Path.home() is identical in both
            # processes and no CIDX_DATA_DIR injection is required.
            return None

        user_home = Path(pwd.getpwnam(service_user).pw_dir)
        return str(user_home / ".cidx-server")

    def _inject_cidx_data_dir_into_content(
        self, content: str, expected_line: str
    ) -> str:
        """Build new service file content with CIDX_DATA_DIR injected.

        Strips any stale CIDX_DATA_DIR= entries, then inserts expected_line
        immediately after the last existing Environment= line.  When no
        Environment= lines are present, inserts after the [Service] header.

        Args:
            content: Current service file content.
            expected_line: The full Environment="CIDX_DATA_DIR=..." line to add.

        Returns:
            Updated service file content string (newline-terminated).
        """
        filtered = [
            line for line in content.splitlines() if "CIDX_DATA_DIR" not in line
        ]

        last_env_index = -1
        for i, line in enumerate(filtered):
            if line.strip().startswith("Environment="):
                last_env_index = i

        if last_env_index >= 0:
            insert_after = last_env_index
        else:
            # No Environment= lines: insert after [Service] header
            insert_after = next(
                (i for i, line in enumerate(filtered) if line.strip() == "[Service]"),
                len(filtered) - 1,
            )

        new_lines = []
        for i, line in enumerate(filtered):
            new_lines.append(line)
            if i == insert_after:
                new_lines.append(expected_line)

        return "\n".join(new_lines) + "\n"

    def _ensure_data_dir_env_var(self) -> bool:
        """Ensure auto-updater service has CIDX_DATA_DIR pointing at the
        server user's data directory.

        Bug #879: cidx-server (User=code-indexer) and cidx-auto-update
        (User=root) resolve Path.home() differently, breaking the IPC file
        contract.  This method self-heals the auto-updater service file.
        Error code DEPLOY-GENERAL-058 is emitted by execute() on failure.

        Returns:
            True if config is correct or was updated (or server service absent),
            False on error
        """
        try:
            correct_data_dir = self._get_server_data_dir()
            if correct_data_dir is None:
                return True  # Non-fatal: server service file absent (fresh install)

            auto_update_service = (
                SYSTEMD_UNIT_DIR / f"{AUTO_UPDATE_SERVICE_NAME}.service"
            )
            current_content = self._read_service_file(auto_update_service)
            if current_content is None:
                return False

            expected_line = f'Environment="CIDX_DATA_DIR={correct_data_dir}"'
            if expected_line in current_content:
                logger.debug(
                    "CIDX_DATA_DIR already correctly configured",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            logger.info(
                f"Injecting CIDX_DATA_DIR={correct_data_dir} into auto-updater service",
                extra={"correlation_id": get_correlation_id()},
            )
            new_content = self._inject_cidx_data_dir_into_content(
                current_content, expected_line
            )
            if not self._write_service_file_and_reload(
                auto_update_service, new_content
            ):
                return False

            if not self._restart_auto_update_service():
                return False
            return True

        except Exception as e:
            logger.exception(
                f"Error ensuring CIDX_DATA_DIR: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    # ------------------------------------------------------------------
    # Bug #897 mitigation 2: MALLOC_ARENA_MAX=2 in the server unit file
    # ------------------------------------------------------------------

    _MALLOC_ARENA_ENV_LINE = "Environment=MALLOC_ARENA_MAX=2"

    def _render_malloc_arena_max_content(self, content: str, inject: bool) -> str:
        """Return updated service file content with MALLOC_ARENA_MAX=2 added or removed.

        inject=True: line is inserted after the last existing Environment= line,
            or after [Service] if none exist, or appended when neither anchor exists.
        inject=False: all lines whose stripped form equals _MALLOC_ARENA_ENV_LINE
            are removed.

        Args:
            content: Current service file text.
            inject: True to add the line, False to remove it.

        Returns:
            Updated service file content (newline-terminated).
        """
        if not inject:
            filtered = [
                line
                for line in content.splitlines()
                if line.strip() != self._MALLOC_ARENA_ENV_LINE
            ]
            return "\n".join(filtered) + "\n"

        lines = content.splitlines()
        env_indices = [
            i for i, line in enumerate(lines) if line.strip().startswith("Environment=")
        ]
        service_indices = [
            i for i, line in enumerate(lines) if line.strip() == "[Service]"
        ]

        if env_indices:
            insert_after = env_indices[-1]
        elif service_indices:
            insert_after = service_indices[-1]
        else:
            # No usable anchor: append to the end of the file.
            return "\n".join(lines) + "\n" + self._MALLOC_ARENA_ENV_LINE + "\n"

        new_lines: list = []
        for i, line in enumerate(lines):
            new_lines.append(line)
            if i == insert_after:
                new_lines.append(self._MALLOC_ARENA_ENV_LINE)
        return "\n".join(new_lines) + "\n"

    def _ensure_malloc_arena_max(self) -> bool:
        """Bug #897 mitigation 2: idempotently manage MALLOC_ARENA_MAX=2 in the
        cidx-server systemd service file.

        Reads the `enable_malloc_arena_max` bootstrap flag from config.json and
        ensures the service file matches: inject when True, remove when False.
        Calls `sudo systemctl daemon-reload` after any write.

        Returns:
            True if service file is in the correct state or was corrected,
            False on read/write error (execute() logs DEPLOY-GENERAL-143).
        """
        try:
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config = ServerConfigManager(server_dir_path=str(_cidx_data_dir)).load_config()
            flag_enabled = bool(
                config and getattr(config, "enable_malloc_arena_max", False)
            )

            service_path = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
            current_content = self._read_service_file(service_path)
            if current_content is None:
                return False

            # Use line-level match to avoid false positives from comments/partial text.
            line_present = any(
                line.strip() == self._MALLOC_ARENA_ENV_LINE
                for line in current_content.splitlines()
            )
            if flag_enabled == line_present:
                logger.debug(
                    "MALLOC_ARENA_MAX already in correct state (flag=%s)",
                    flag_enabled,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            action = "Injecting" if flag_enabled else "Removing"
            logger.info(
                "%s MALLOC_ARENA_MAX=2 in %s",
                action,
                service_path,
                extra={"correlation_id": get_correlation_id()},
            )
            new_content = self._render_malloc_arena_max_content(
                current_content, flag_enabled
            )
            if not self._write_service_file_and_reload(service_path, new_content):
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-143",
                        f"Failed to write MALLOC_ARENA_MAX change to {service_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False
            return True

        except Exception as exc:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-143",
                    f"Error managing MALLOC_ARENA_MAX in service file: {exc}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def pip_install(self) -> bool:
        """Execute pip install to update dependencies.

        Issue #154: Uses _get_server_python() to install into the correct environment
        (e.g., pipx venv, not system Python).

        Returns:
            True if successful, False otherwise
        """
        try:
            python_path = self._get_server_python()
            # Use sudo because pipx venv may be owned by root (e.g., /opt/pipx/venvs/)
            result = subprocess.run(
                [
                    "sudo",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                    "--break-system-packages",
                    "-e",
                    ".",
                ],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-012",
                        f"Pip install failed: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "Pip install successful", extra={"correlation_id": get_correlation_id()}
            )
            return True

        except Exception as e:
            logger.exception(
                f"Pip install exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def restart_server(self) -> bool:
        """Restart CIDX server via systemctl with graceful drain.

        Story #734: Uses maintenance mode flow:
        1. Enter maintenance mode (stop accepting new jobs)
        2. Wait for drain (running jobs to complete)
        3. Restart server

        Returns:
            True if successful, False otherwise
        """
        # Step 1: Enter maintenance mode
        entered_maintenance = self._enter_maintenance_mode()
        if entered_maintenance:
            logger.info(
                "Maintenance mode entered, waiting for drain",
                extra={"correlation_id": get_correlation_id()},
            )

            # Step 2: Wait for drain
            drained = self._wait_for_drain()
            if not drained:
                # AC4: Log running jobs at WARNING level before forcing restart
                running_jobs = self._get_running_jobs_for_logging()
                for job in running_jobs:
                    job_id = job.get("job_id", "unknown")
                    operation_type = job.get("operation_type", "unknown")
                    started_at = job.get("started_at", "unknown")
                    progress = job.get("progress", 0)
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-013",
                            f"Forcing restart - running job: job_id={job_id}, "
                            f"operation_type={operation_type}, started_at={started_at}, "
                            f"progress={progress}%",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-014",
                        "Drain timeout exceeded, forcing restart",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
            else:
                logger.info(
                    "System drained successfully, proceeding with restart",
                    extra={"correlation_id": get_correlation_id()},
                )
        else:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-015",
                    "Could not enter maintenance mode, proceeding with restart",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 3: Execute restart
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.service_name],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-016",
                        f"Server restart failed: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "Server restarted successfully",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.exception(
                f"Server restart exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _ensure_workers_config(self) -> bool:
        """Ensure systemd service has --workers 1 configured.

        Single worker maintains in-memory cache coherency (HNSW, FTS, OmniCache).
        Multiple workers duplicate caches and break cursor-based pagination.

        Returns:
            True if config is correct or was updated, False on error
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")

        try:
            if not service_path.exists():
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-017",
                        f"Service file not found: {service_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Not an error if service doesn't exist yet

            content = service_path.read_text()

            # Check if --workers is already configured
            if "--workers" in content:
                logger.debug(
                    "Workers config already present in service file",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Add --workers 1 to ExecStart line
            if "ExecStart=" in content and "uvicorn" in content:
                # Find ExecStart line and add --workers 1
                lines = content.split("\n")
                updated_lines = []
                modified = False

                for line in lines:
                    if line.startswith("ExecStart=") and "uvicorn" in line:
                        # Add --workers 1 before any newline
                        line = line.rstrip() + " --workers 1"
                        modified = True
                    updated_lines.append(line)

                if modified:
                    new_content = "\n".join(updated_lines)
                    # Write via sudo
                    result = subprocess.run(
                        ["sudo", "tee", str(service_path)],
                        input=new_content,
                        capture_output=True,
                        text=True,
                    )

                    if result.returncode != 0:
                        logger.error(
                            format_error_log(
                                "DEPLOY-GENERAL-018",
                                f"Failed to update service file: {result.stderr}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        return False

                    # Reload systemd
                    subprocess.run(
                        ["sudo", "systemctl", "daemon-reload"],
                        capture_output=True,
                    )

                    logger.info(
                        "Added --workers 1 to service file",
                        extra={"correlation_id": get_correlation_id()},
                    )

            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-019",
                    f"Error checking workers config: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _extract_service_user(self, content: str) -> Optional[str]:
        """Extract User= value from service file content.

        Args:
            content: Service file content as string

        Returns:
            Service user name, or None if User= line not found
        """
        for line in content.split("\n"):
            if line.strip().startswith("User="):
                return line.split("=", 1)[1].strip()
        return None

    def _extract_working_directory(self, content: str) -> Path:
        """Extract WorkingDirectory= value from service file content.

        Args:
            content: Service file content as string

        Returns:
            Path from WorkingDirectory=, or self.repo_path if not found
        """
        for line in content.split("\n"):
            if line.strip().startswith("WorkingDirectory="):
                return Path(line.split("=", 1)[1].strip())
        return self.repo_path

    def _ensure_cidx_repo_root(self) -> bool:
        """Ensure systemd service has CIDX_REPO_ROOT environment variable configured.

        CIDX_REPO_ROOT is required for self-monitoring to detect repository root.
        Without it, self-monitoring fails with MONITOR-GENERAL-011 error.

        Returns:
            True if config is correct or was updated, False on error
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")

        try:
            if not service_path.exists():
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-022",
                        f"Service file not found: {service_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Not an error if service doesn't exist yet

            content = service_path.read_text()

            # Check if CIDX_REPO_ROOT is already configured
            if "CIDX_REPO_ROOT" in content:
                logger.debug(
                    "CIDX_REPO_ROOT already present in service file",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Find insertion point and add CIDX_REPO_ROOT
            lines = content.split("\n")

            # Extract WorkingDirectory from service file - this is the canonical repo path
            repo_root = self._extract_working_directory(content)

            new_env_line = f'Environment="CIDX_REPO_ROOT={repo_root}"'

            # First pass: find the index of the last Environment= line
            last_env_index = -1
            for i, line in enumerate(lines):
                if line.startswith("Environment="):
                    last_env_index = i

            # Second pass: build updated content with insertion
            updated_lines = []
            inserted = False

            for i, line in enumerate(lines):
                # Check if we need to insert before ExecStart (no Environment= lines case)
                if (
                    last_env_index == -1
                    and not inserted
                    and line.startswith("ExecStart=")
                ):
                    updated_lines.append(new_env_line)
                    inserted = True

                updated_lines.append(line)

                # Check if we need to insert after last Environment= line
                if last_env_index >= 0 and i == last_env_index and not inserted:
                    updated_lines.append(new_env_line)
                    inserted = True

            if not inserted:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-023",
                        "Could not find insertion point for CIDX_REPO_ROOT",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Not a fatal error

            new_content = "\n".join(updated_lines)
            result = subprocess.run(
                ["sudo", "tee", str(service_path)],
                input=new_content,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-024",
                        f"Failed to update service file: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True,
            )

            logger.info(
                f"Added CIDX_REPO_ROOT to service file: {self.repo_path}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-025",
                    f"Error checking CIDX_REPO_ROOT config: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_git_safe_directory(self) -> bool:
        """Ensure git safe.directory is configured for the service user.

        On production servers where the repo is owned by root but the service runs
        as a different user (e.g., code-indexer), git refuses to operate due to
        "dubious ownership" security check. This method configures safe.directory
        to allow git operations.

        Returns:
            True if config is correct or was updated or not needed, False on error
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")

        try:
            if not service_path.exists():
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-026",
                        f"Service file not found: {service_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Not a fatal error if service doesn't exist yet

            content = service_path.read_text()

            # Extract User from service file
            service_user = self._extract_service_user(content)

            # If no User= line, skip (service runs as current user)
            if not service_user:
                logger.debug(
                    "No User= line in service file, skipping git safe.directory config",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Extract WorkingDirectory from service file - this is the canonical repo path
            repo_root = self._extract_working_directory(content)

            # Check if already configured
            check_result = subprocess.run(
                [
                    "sudo",
                    "-u",
                    service_user,
                    "git",
                    "config",
                    "--global",
                    "--get-all",
                    "safe.directory",
                ],
                capture_output=True,
                text=True,
            )

            if check_result.returncode == 0:
                # Check if our repo path is in the output
                configured_paths = check_result.stdout.strip().split("\n")
                if str(repo_root) in configured_paths:
                    logger.debug(
                        f"Git safe.directory already configured for {service_user}: {repo_root}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return True

            # Add safe.directory configuration
            add_result = subprocess.run(
                [
                    "sudo",
                    "-u",
                    service_user,
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "safe.directory",
                    str(repo_root),
                ],
                capture_output=True,
                text=True,
            )

            if add_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-027",
                        f"Failed to add git safe.directory: {add_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                f"Added git safe.directory for {service_user}: {repo_root}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-028",
                    f"Error configuring git safe.directory: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_sudoers_restart(self) -> bool:
        """Ensure sudoers rule exists for service user to restart systemd service.

        On production servers where the service runs as a non-root user (e.g., jsbattig),
        the web diagnostics restart feature requires sudo privileges to run
        'systemctl restart cidx-server'. This method creates a sudoers rule to allow
        the service user to restart the service without a password prompt.

        Returns:
            True if rule exists or was created or not needed, False on error
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")
        sudoers_path = Path(f"/etc/sudoers.d/{self.service_name}")

        try:
            if not service_path.exists():
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-052",
                        f"Service file not found: {service_path}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Not a fatal error if service doesn't exist yet

            content = service_path.read_text()

            # Extract User from service file
            service_user = self._extract_service_user(content)

            # If no User= line, skip (service runs as current user)
            if not service_user:
                logger.debug(
                    "No User= line in service file, skipping sudoers restart config",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Check if sudoers rule already exists with correct content
            expected_rule = f"{service_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart {self.service_name}"

            # Use sudo to check /etc/sudoers.d/ (not readable by non-root)
            check_result = subprocess.run(
                ["sudo", "cat", str(sudoers_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check_result.returncode == 0:
                existing_content = check_result.stdout.strip()
                if existing_content == expected_rule:
                    logger.debug(
                        f"Sudoers restart rule already configured for {service_user}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return True

            # Create sudoers rule via sudo tee
            logger.info(
                f"Creating sudoers restart rule for {service_user}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Use sudo tee to write the sudoers file
            tee_result = subprocess.run(
                ["sudo", "tee", str(sudoers_path)],
                input=expected_rule,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if tee_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-053",
                        f"Failed to create sudoers rule: {tee_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Set correct permissions (0440)
            chmod_result = subprocess.run(
                ["sudo", "chmod", "0440", str(sudoers_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if chmod_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-054",
                        f"Failed to set sudoers permissions: {chmod_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Remove file with wrong permissions
                subprocess.run(
                    ["sudo", "rm", "-f", str(sudoers_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return False

            # Validate with visudo
            visudo_result = subprocess.run(
                ["sudo", "visudo", "-c", "-f", str(sudoers_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if visudo_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-055",
                        f"Sudoers validation failed: {visudo_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Remove invalid sudoers file
                subprocess.run(
                    ["sudo", "rm", "-f", str(sudoers_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return False

            logger.info(
                f"Created sudoers restart rule for {service_user}",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-056",
                    f"Error configuring sudoers restart rule: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _get_service_user_home(self) -> Optional[Path]:
        """Get home directory for the service user from systemd service file.

        Reads the systemd service file, extracts the User= value, and looks up
        the user's home directory using pwd.getpwnam.

        Returns:
            Path to service user's home directory, or None if:
            - Service file doesn't exist
            - No User= line in service file (service runs as current user)
            - User lookup fails
        """
        service_path = Path(f"/etc/systemd/system/{self.service_name}.service")

        try:
            if not service_path.exists():
                return None

            content = service_path.read_text()
            service_user = self._extract_service_user(content)

            if not service_user:
                return None

            # Look up user's home directory
            pw_record = pwd.getpwnam(service_user)
            return Path(pw_record.pw_dir)

        except (KeyError, FileNotFoundError, PermissionError) as e:
            logger.debug(
                f"Could not determine service user home: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

    def ensure_ripgrep(self) -> bool:
        """
        Ensure ripgrep is installed (x86_64 Linux only).

        Uses pre-compiled static MUSL binary from GitHub releases.
        Works on Amazon Linux, Rocky Linux, and Ubuntu without dependencies.

        Returns:
            True if ripgrep is available (already installed or successfully installed),
            False if installation failed or unsupported architecture.
        """
        home_dir = self._get_service_user_home()
        installer = RipgrepInstaller(home_dir=home_dir)
        return bool(installer.install())  # Explicit cast for mypy

    def _calculate_auto_update_hash(self) -> str:
        """Calculate SHA256 hash of all auto_update/*.py files.

        Used to detect when the auto-updater's own code has changed,
        triggering a self-restart to load the new code.

        Returns:
            SHA256 hex digest of concatenated file contents, or empty string if no files found
        """
        try:
            auto_update_dir = (
                self.repo_path / "src" / "code_indexer" / "server" / "auto_update"
            )
            py_files = sorted(auto_update_dir.glob("*.py"))

            if not py_files:
                return ""

            hasher = hashlib.sha256()
            for py_file in py_files:
                content = py_file.read_text()
                hasher.update(content.encode("utf-8"))

            return hasher.hexdigest()

        except Exception as e:
            logger.warning(
                f"Error calculating auto_update hash: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return ""

    def _write_status_file(self, status: str, details: str = "") -> None:
        """Write deployment status to AUTO_UPDATE_STATUS_FILE.

        Args:
            status: Status value (pending_restart, in_progress, success, failed)
            details: Optional details about the status
        """
        try:
            # Get current version from package
            try:
                from code_indexer import __version__

                version = __version__
            except ImportError:
                version = "unknown"

            status_data = {
                "status": status,
                "version": version,
                "timestamp": datetime.now().isoformat(),
                "details": details,
            }

            AUTO_UPDATE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(AUTO_UPDATE_STATUS_FILE, "w") as f:
                json.dump(status_data, f, indent=2)

            logger.debug(
                f"Wrote status file: {status}",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            logger.warning(
                f"Could not write status file: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    def _read_status_file(self) -> Optional[dict]:
        """Read deployment status from AUTO_UPDATE_STATUS_FILE.

        Returns:
            Status dict with keys: status, version, timestamp, details
            None if file doesn't exist or is corrupted
        """
        try:
            if not AUTO_UPDATE_STATUS_FILE.exists():
                return None

            with open(AUTO_UPDATE_STATUS_FILE, "r") as f:
                return cast(Optional[dict[Any, Any]], json.load(f))

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(
                f"Could not read status file: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

    def _should_retry_on_startup(self) -> bool:
        """Check if deployment should be retried based on status file.

        Called by run_once.py on startup to detect pending_restart or failed
        status from a previous run (e.g., after auto-updater self-restart).

        Returns:
            True if status is pending_restart or failed, False otherwise
        """
        status_data = self._read_status_file()

        if status_data is None:
            return False

        status = status_data.get("status")
        return status in ("pending_restart", "failed")

    def _restart_auto_update_service(self) -> bool:
        """Restart the cidx-auto-update systemd service.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", AUTO_UPDATE_SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-050",
                        f"Failed to restart auto-update service: {result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "Auto-update service restart initiated",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-051",
                    f"Exception restarting auto-update service: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_memory_overcommit(self) -> bool:
        """Ensure vm.overcommit_memory=1 is set for fork safety.

        Production servers with vm.overcommit_memory=0 (heuristic mode) refuse fork()
        when VmPeak exceeds CommitLimit, even though mmap'd memory is disk-backed.
        The CIDX server reaches ~57GB VmPeak from mmap'd HNSW indexes and SQLite DBs,
        causing all subprocess.run() calls to fail with OSError: [Errno 12] Cannot
        allocate memory.

        Setting vm.overcommit_memory=1 (always overcommit) allows fork() regardless
        of virtual memory size, which is safe because the child process (exec'd
        immediately via subprocess) never actually uses the parent's memory pages.

        Returns:
            True if already configured or successfully configured, False on error
        """
        try:
            # Check current value
            check_result = subprocess.run(
                ["sysctl", "-n", "vm.overcommit_memory"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check_result.stdout.strip() == "1":
                logger.debug(
                    "Memory overcommit already configured (vm.overcommit_memory=1)",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Write persistent sysctl config file
            write_result = subprocess.run(
                ["sudo", "tee", "/etc/sysctl.d/99-cidx-memory.conf"],
                input="vm.overcommit_memory = 1\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            if write_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-090",
                        f"Failed to write /etc/sysctl.d/99-cidx-memory.conf: {write_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Apply immediately
            apply_result = subprocess.run(
                ["sudo", "sysctl", "-p", "/etc/sysctl.d/99-cidx-memory.conf"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if apply_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-091",
                        f"Failed to apply sysctl config via sysctl -p: {apply_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            logger.info(
                "Configured vm.overcommit_memory=1 for fork safety",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-092",
                    f"Exception configuring memory overcommit: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_swap_file(self) -> bool:
        """Ensure a 4GB swap file exists as an OOM safety net.

        Creates /swapfile with proper permissions, formats it, enables it,
        and adds a persistent fstab entry so it survives reboots.

        This provides an additional safety net for OOM conditions when the
        server has large mmap'd virtual memory from HNSW indexes and SQLite DBs.

        Returns:
            True if swap exists or was successfully created, False on error
        """
        try:
            # Check if any swap is already active
            check_result = subprocess.run(
                ["swapon", "--show", "--noheadings"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check_result.stdout.strip():
                logger.debug(
                    f"Swap already configured: {check_result.stdout.strip()}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Allocate 4GB swap file
            fallocate_result = subprocess.run(
                ["sudo", "fallocate", "-l", "4G", "/swapfile"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if fallocate_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-093",
                        f"fallocate -l 4G /swapfile failed: {fallocate_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Set secure permissions (0600 - root only)
            chmod_result = subprocess.run(
                ["sudo", "chmod", "600", "/swapfile"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if chmod_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-094",
                        f"chmod 600 /swapfile failed: {chmod_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Format as swap
            mkswap_result = subprocess.run(
                ["sudo", "mkswap", "/swapfile"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if mkswap_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-095",
                        f"mkswap /swapfile failed: {mkswap_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Enable swap immediately
            swapon_result = subprocess.run(
                ["sudo", "swapon", "/swapfile"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if swapon_result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-096",
                        f"swapon /swapfile failed: {swapon_result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Add fstab entry for reboot persistence
            fstab_result = subprocess.run(
                ["cat", "/etc/fstab"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if "/swapfile" not in fstab_result.stdout:
                tee_result = subprocess.run(
                    ["sudo", "tee", "-a", "/etc/fstab"],
                    input="/swapfile none swap sw 0 0\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if tee_result.returncode != 0:
                    # Non-fatal: swap is active but will not survive reboot
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-097",
                            f"Failed to append /swapfile entry to /etc/fstab: {tee_result.stderr} "
                            "- swap is active but will not survive reboot",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    # Still return True - swap IS active

            logger.info(
                "Created and enabled 4GB swap file",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-098",
                    f"Exception creating swap file: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def execute(self) -> bool:
        """Execute complete deployment: git pull + pip install.

        Self-restart mechanism: Detects when auto-updater's own code changes
        and restarts the service to load new code (bootstrap problem solution).

        Returns:
            True if all steps successful, False otherwise
        """
        logger.info(
            "Starting deployment execution",
            extra={"correlation_id": get_correlation_id()},
        )

        # Step 0: Calculate hash of auto_update code BEFORE git pull
        hash_before = self._calculate_auto_update_hash()

        # Step 1: Git pull
        if not self.git_pull():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-020",
                    "Deployment failed at git pull step",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        # Step 1.1: Check if auto_update code itself changed
        hash_after = self._calculate_auto_update_hash()
        if hash_before and hash_after and hash_before != hash_after:
            logger.info(
                "Auto-updater code changed, initiating self-restart",
                extra={"correlation_id": get_correlation_id()},
            )
            # Bug #884: Smoke-test the new auto-updater code before self-restarting.
            # If the updated run_once.py has an import-time error, restarting the
            # service would crash-loop it.  Run a quick import check first.
            try:
                smoke = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from code_indexer.server.auto_update.run_once import main",
                    ],
                    timeout=10,
                    capture_output=True,
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-141",
                        "Self-restart smoke-test timed out (>10s) — aborting self-restart "
                        "(Bug #884): new auto-updater code may have import-time side effect.",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                self._write_status_file("failed", "self-restart smoke-test timed out")
                return False
            if smoke.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-142",
                        f"Self-restart smoke-test failed (rc={smoke.returncode}) — aborting "
                        f"self-restart (Bug #884). stderr: "
                        f"{smoke.stderr.decode('utf-8', errors='replace')}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                self._write_status_file(
                    "failed",
                    f"self-restart smoke-test failed rc={smoke.returncode}",
                )
                return False
            # Smoke passed — proceed with pending_restart + marker + systemctl restart
            self._write_status_file(
                "pending_restart", "Auto-updater code updated, restarting service"
            )
            # Create redeploy marker so the restarted instance continues deployment
            # (git pull will be a no-op, but pip install + ensure steps + server restart will run)
            try:
                PENDING_REDEPLOY_MARKER.parent.mkdir(parents=True, exist_ok=True)
                PENDING_REDEPLOY_MARKER.touch()
                logger.info(
                    "Created pending redeploy marker for post-restart deployment",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.warning(
                    f"Could not create redeploy marker: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            self._restart_auto_update_service()
            # Return True - deployment will continue after restart
            return True

        # Step 1.5: Git submodule update (for custom hnswlib build)
        # Note: Still attempt submodule update, but fallback handles failure
        if not self.git_submodule_update():
            logger.warning(
                "Git submodule update failed, fallback will attempt standalone clone",
                extra={"correlation_id": get_correlation_id()},
            )

        # Step 1.6: Build custom hnswlib with check_integrity() method (with fallback)
        # Bug #160: Uses fallback approach if submodule has no setup.py
        if not self._build_hnswlib_with_fallback():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-044",
                    "Deployment failed at custom hnswlib build step (both submodule and fallback)",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        # Step 2: Pip install
        if not self.pip_install():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-021",
                    "Deployment failed at pip install step",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        # Step 3: Story #30 AC4 - Ensure workers config
        self._ensure_workers_config()

        # Step 4: Bug #87 - Ensure CIDX_REPO_ROOT environment variable
        self._ensure_cidx_repo_root()

        # Step 5: Ensure git safe.directory configured
        self._ensure_git_safe_directory()

        # Step 6: Issue #154 - Ensure auto-updater uses server Python
        self._ensure_auto_updater_uses_server_python()

        # Step 6.5: Bug #879 - Ensure auto-updater has CIDX_DATA_DIR pointing at
        # server user's data dir so restart signal and redeploy marker paths match
        # across cidx-server (User=code-indexer) and cidx-auto-update (User=root).
        if not self._ensure_data_dir_env_var():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-058",
                    "CIDX_DATA_DIR could not be verified/injected — "
                    "restart signal and redeploy marker paths may diverge",
                ),
                extra={"correlation_id": get_correlation_id()},
            )

        # Step 6.6: Bug #897 - Idempotently inject or remove MALLOC_ARENA_MAX=2
        # from the cidx-server systemd unit file based on bootstrap config flag.
        if not self._ensure_malloc_arena_max():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-143",
                    "MALLOC_ARENA_MAX could not be verified — glibc arena cap not enforced",
                ),
                extra={"correlation_id": get_correlation_id()},
            )

        # Step 7: Ensure ripgrep is installed (Bug #157: log result)
        ripgrep_result = self.ensure_ripgrep()
        if ripgrep_result:
            logger.info(
                "Ripgrep installation successful",
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-035",
                    "Ripgrep installation failed",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 8: Ensure sudoers rule for server self-restart
        if not self._ensure_sudoers_restart():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-057",
                    "Sudoers restart rule could not be verified/created - "
                    "server self-restart may fail",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 9: Ensure vm.overcommit_memory=1 for fork safety
        if not self._ensure_memory_overcommit():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-099",
                    "Memory overcommit could not be configured - "
                    "subprocess fork may fail with ENOMEM on high VmPeak",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 10: Ensure swap file exists as safety net
        if not self._ensure_swap_file():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-100",
                    "Swap file could not be created - "
                    "no swap safety net for OOM conditions",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 11: Bug #839 - Keep Claude CLI at latest version (non-fatal)
        if not self._ensure_claude_cli_updated():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-106",
                    "Claude CLI update skipped or failed - "
                    "Claude CLI may be running a stale version",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        logger.info(
            "Deployment execution completed successfully",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _is_npm_available(self) -> bool:
        """Check if npm is installed and runnable.

        Returns True if `npm --version` exits 0 within NPM_VERSION_TIMEOUT_SECONDS.
        Returns False on any failure (missing binary, timeout, non-zero exit).
        """
        try:
            subprocess.run(
                ["npm", "--version"],
                capture_output=True,
                timeout=NPM_VERSION_TIMEOUT_SECONDS,
                check=True,
            )
            return True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            return False

    def _ensure_claude_cli_updated(self) -> bool:
        """Ensure Claude CLI is at latest version.

        Runs `npm install -g @anthropic-ai/claude-code@latest`. npm's end state is
        idempotent (repeated runs converge on the latest version).

        Non-fatal if npm is missing (logs WARNING, returns False, deploy continues).
        """
        if not self._is_npm_available():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-101",
                    "npm not found — Claude CLI update skipped",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        try:
            subprocess.run(
                ["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS,
                check=True,
            )
            logger.info(
                format_error_log(
                    "DEPLOY-GENERAL-102",
                    "Claude CLI updated via npm",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-103",
                    f"Claude CLI update failed: {e.stderr}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except subprocess.TimeoutExpired as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-104",
                    f"Claude CLI update timed out after {CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS}s: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except OSError as e:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-105",
                    f"Claude CLI update spawn failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
