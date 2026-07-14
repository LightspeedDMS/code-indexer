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
import tarfile
import tempfile
import platform

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

# Bug #1320 Part B: fixed, documented installation location of the co-located
# CoW Storage Daemon's own config file. This is a stable install-location
# constant (not an environment-specific storage value) — used ONLY to
# auto-detect cow_daemon.daemon_storage_path (the `base_path` field) on the
# daemon-HOST node. Overridable via env var for testing only, mirroring the
# SYSTEMD_UNIT_DIR pattern above.
COW_DAEMON_HOST_CONFIG_PATH = Path(
    os.environ.get(
        "CIDX_COW_DAEMON_HOST_CONFIG_PATH", "/etc/cow-storage-daemon/config.json"
    )
)

# Self-restart mechanism constants
# Note: Using ~/.cidx-server/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
# and /var/lib/ is not writable by non-root service users
AUTO_UPDATE_STATUS_FILE = _cidx_data_dir / "auto-update-status.json"
# Timeout for ALL systemd/sudo control-plane operations: daemon-reload,
# systemctl restart, sudoers cat/tee/chmod/rm/visudo, unit-file read/write.
# 120s (not the previous 30s/10s) because the FIRST auto-update deploy on a
# freshly-built host compiles hnswlib and installs rustup/ripgrep/Claude-CLI/
# pace-maker -- CPU-heavy work that transiently starves systemd/sudo
# (pam_systemd blocks on a busy PID 1). A tighter timeout here raised
# subprocess.TimeoutExpired, which a broad `except Exception` swallowed,
# silently skipping real config steps (DEPLOY-GENERAL-034, -056).
SYSTEMD_OP_TIMEOUT_SECONDS = 120

# Story #355: Signal-based server restart via auto-updater
# Server writes this file to request a restart; auto-updater detects and executes it.
# Using ~/.cidx-server/ to avoid systemd PrivateTmp=yes isolation issues.
RESTART_SIGNAL_PATH = _cidx_data_dir / "restart.signal"
# Signals older than this threshold (seconds) are treated as stale (from a previous crash)
# and deleted without triggering a restart. Set to 2x the typical poll interval.
RESTART_SIGNAL_STALENESS_THRESHOLD = 120

# Story #1198 MAJOR-M2: launch.json written by ConfigService.materialize_launch_config()
# to capture the TARGET launch parameters (workers, log_level, host, port,
# target_restart_generation).  The auto-updater reads this file before restarting
# uvicorn so that the restarted process picks up any pending config changes.
LAUNCH_CONFIG_PATH = _cidx_data_dir / "launch.json"
# applied_launch.json written by the auto-updater AFTER it has applied a launch
# config (started uvicorn with the values from launch.json).  Consumers such as
# applied_worker_count.py read this to determine the APPLIED (running) count.
APPLIED_LAUNCH_CONFIG_PATH = _cidx_data_dir / "applied_launch.json"


# Canonical server defaults mirroring ServerConfig field declarations (config_manager.py).
# Used by _resolve_launch_values so defaults stay centralized; never hardcode these inline.
def _get_server_config_defaults() -> tuple:
    """Return (host, port, workers) defaults from ServerConfig field declarations."""
    from code_indexer.server.utils.config_manager import ServerConfig as _SC
    import dataclasses as _dc

    _fields = {f.name: f.default for f in _dc.fields(_SC)}
    return _fields["host"], _fields["port"], _fields["workers"]


_LAUNCH_DEFAULT_HOST, _LAUNCH_DEFAULT_PORT, _LAUNCH_DEFAULT_WORKERS = (
    _get_server_config_defaults()
)

# Hnswlib fallback constants (Bug #160)
# Note: Using /var/tmp/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
HNSWLIB_FALLBACK_PATH = Path("/var/tmp/cidx-hnswlib")
HNSWLIB_REPO_URL = "https://github.com/LightspeedDMS/hnswlib.git"
# Bug #1392: quick `python -c` probe for check_integrity/repair_orphans,
# same budget as the existing _hnswlib_importable() import probe.
HNSWLIB_CAPABILITY_PROBE_TIMEOUT_SECONDS = 10

# Bug #839: Claude CLI auto-update timeout constants
NPM_VERSION_TIMEOUT_SECONDS = 5  # How long to wait for `npm --version` probe
CLAUDE_CLI_UPDATE_TIMEOUT_SECONDS = 180  # How long to wait for npm global install
# Story #845: Codex CLI install timeout constants
CODEX_CLI_INSTALL_TIMEOUT_SECONDS = 300  # npm install can be slow; generous budget
CODEX_VERSION_PROBE_TIMEOUT_SECONDS = 10  # Quick binary probe after install
# Claude CLI install constants (same pattern as CODEX_CLI_INSTALL_TIMEOUT_SECONDS above)
CLAUDE_INSTALL_URL = "https://claude.ai/install.sh"
CLAUDE_INSTALL_TIMEOUT_SECONDS = (
    300  # curl + sh pipeline; generous budget matches Codex
)

# scip-python install constants (same pattern as CODEX_CLI_INSTALL_TIMEOUT_SECONDS
# above). scip-python (npm package @sourcegraph/scip-python) is the SCIP indexer
# binary for Python projects (src/code_indexer/scip/indexers/python.py); without
# it, SCIP indexing fails with "[Errno 2] No such file or directory: 'scip-python'".
SCIP_PYTHON_PACKAGE = "@sourcegraph/scip-python"
SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS = 300  # npm install can be slow; generous budget

# Story #997: Pace-maker install/update constants
PACE_MAKER_REPO_URL = "https://github.com/LightspeedDMS/claude-pace-maker.git"
PACE_MAKER_GIT_TIMEOUT = 60
PACE_MAKER_INSTALL_TIMEOUT = 120
PACE_MAKER_CMD_TIMEOUT = 10

# Step 15: FHS-mandated standard system PATH components for Linux (POSIX).
# These directories are identical across all supported distributions (RHEL, Ubuntu, Debian).
# Mirrors the convention used for /usr/bin/systemctl elsewhere in this file.
SYSTEMD_DEFAULT_PATH_SUFFIX = "/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"

# Step 16: Story #1024 - Rust toolchain + xray-cli build constants
# M5: Two-step install: curl downloads the installer, sh runs it via stdin (no shell=True).
RUSTUP_INSTALLER_URL = "https://sh.rustup.rs"
RUSTUP_CURL_ARGS = [
    "curl",
    "--proto",
    "=https",
    "--tlsv1.2",
    "-sSf",
    RUSTUP_INSTALLER_URL,
]
RUSTUP_SH_ARGS = ["sh", "-s", "--", "-y", "--default-toolchain", "stable"]
RUSTUP_INSTALL_TIMEOUT_SECONDS = 300  # curl + sh pipeline; generous budget
RUSTUP_CMD_TIMEOUT_SECONDS = 30  # rustup default stable; quick
CARGO_BUILD_TIMEOUT_SECONDS = 600  # 10 min for first-time xray-cli build
RUSTC_VERSION_TIMEOUT_SECONDS = 10  # quick binary probe

# System-wide Rust installation directory — accessible by all OS users.
# The auto-updater runs as root but cidx-server runs as code-indexer;
# /root/.cargo is unreachable (0550), so we install to /opt/rust.
RUST_SYSTEM_DIR = Path("/opt/rust")

# BUG #1318: Node.js toolchain provisioning constants. Node.js/npm is not
# installed on any server node, so ensure_scip_python() and the Codex CLI
# install both fail with "npm not available on PATH". Fix: provision a
# pinned Node.js LTS release to a system-wide dir the SAME way Rust is
# provisioned to RUST_SYSTEM_DIR (/opt/rust) — a static official tarball
# (not a distro package, avoiding dnf/apt version drift) installed to a
# dir reachable by both the (often root) auto-updater and the code-indexer
# service user running cidx-server's child index subprocesses.
NODEJS_VERSION = "22.11.0"  # current LTS ("Jod") at time of writing
NODEJS_DIST_URL = (
    f"https://nodejs.org/dist/v{NODEJS_VERSION}/node-v{NODEJS_VERSION}-linux-x64.tar.xz"
)
NODEJS_INSTALL_DIR = Path("/opt/node")
NODEJS_INSTALL_TIMEOUT_SECONDS = 300  # curl download + tar extract; generous budget
NODEJS_VERSION_PROBE_TIMEOUT_SECONDS = 10  # quick binary probe

# Maximum bytes of subprocess stderr captured in warning log messages.
MAX_ERROR_SNIPPET_LENGTH = 200


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

    def _pip_supports_break_system_packages(
        self, python_path: str, use_sudo: bool = False
    ) -> bool:
        """Return True if the pip that will run the install supports --break-system-packages.

        The flag was introduced in pip 23.0.1.  On stock Rocky 9 the system pip
        is 21.3.1, which rejects the flag with "no such option", causing the
        hnswlib build step to fail (Bug #1234).

        IMPORTANT (live-VM fix): the probe MUST use the same privilege context as
        the install.  Both build_custom_hnswlib and pip_install run their installs
        via ``sudo python3 -m pip install ...``.  On Rocky 9 the non-sudo user pip
        may be 26.x (probe would return True) while the sudo/system pip is 21.3.1
        (should return False).  Pass ``use_sudo=True`` when the corresponding
        install command starts with ``sudo``.

        Args:
            python_path: Path to the Python interpreter whose pip to probe.
            use_sudo: When True, prefix the pip --version probe with ``sudo`` so
                      the probe resolves the same pip binary the install will use.

        Returns:
            True if pip version >= 23.0.1, False otherwise or on any error.
            Conservatively returns False on any parse/subprocess failure so that
            the flag is silently omitted rather than breaking the install.
        """
        try:
            cmd = (["sudo"] if use_sudo else []) + [
                python_path,
                "-m",
                "pip",
                "--version",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False

            # Output format: "pip X.Y.Z from /path (python N.M)"
            parts = result.stdout.strip().split()
            if len(parts) < 2 or parts[0] != "pip":
                return False

            raw_version = parts[1]
            # Parse major.minor.patch (or major.minor) — ignore pre/post suffixes
            version_parts = raw_version.split(".")
            major = int(version_parts[0])
            minor = int(version_parts[1]) if len(version_parts) > 1 else 0
            patch = int(version_parts[2]) if len(version_parts) > 2 else 0

            # Minimum version that supports --break-system-packages: 23.0.1
            if major > 23:
                return True
            if major == 23:
                if minor > 0:
                    return True
                if minor == 0 and patch >= 1:
                    return True
            return False

        except Exception:
            # Swallow all errors — conservatively omit the flag
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

    def _deploy_tmpdir(self) -> str:
        """Return a writable temp directory for deploy pip installs under sudo + PrivateTmp.

        Bug #1243: Under systemd PrivateTmp=yes, pip's tempfile.gettempdir() finds no
        usable temp dir (private /tmp isolated from sudo context, /var/tmp also isolated,
        CWD / not writable, no TMPDIR env var set by the service unit).  This causes
        FileNotFoundError: No usable temporary directory found in ['/tmp', '/var/tmp',
        '/usr/tmp', '/'] on every pip build attempt, dead-looping the auto-updater.

        Solution: use a path under _cidx_data_dir (.deploy-tmp) which is NOT under /tmp
        and therefore unaffected by PrivateTmp isolation.  Pass it to pip via sudo `env`
        (the POSIX env utility): sudo's env_reset strips inherited env vars but does NOT
        strip env vars injected by a child `env` command, so `sudo env TMPDIR=<path> pip`
        receives a writable TMPDIR regardless of the PrivateTmp setting.

        Returns:
            Absolute path string to the deploy temp directory (created if absent).
        """
        deploy_tmp = _cidx_data_dir / ".deploy-tmp"
        deploy_tmp.mkdir(parents=True, exist_ok=True)
        return str(deploy_tmp)

    def _is_user_install(self, python_path: str) -> bool:
        """Return True if pip can install code-indexer WITHOUT sudo (user install).

        Bug #1245 (v11.10.0 fix was INCOMPLETE -- proven on the live staging
        cluster): the original probe recognized a user install ONLY when
        code_indexer.__file__ contained the substring "/.local/". Staging
        nodes run an EDITABLE install at a jsbattig-owned path such as
        /home/jsbattig/code-indexer/src/code_indexer/__init__.py -- which
        has NO "/.local/" segment at all. The substring-only probe returned
        False there -> use_sudo=True -> sudo's root pip targeted
        /root/.local and /root/.cache/pip/wheels, both READ-ONLY on the
        immutable host -> fatal "Pip install failed" -> the auto-updater
        dead-looped forever, even though the auto-updater's OWN process user
        (jsbattig, non-root) already owns and can write the install dir.

        Re-fix: key on WRITABILITY instead of the "/.local/" substring. pip
        (editable `-e .` plus dependency wheel builds such as hnswlib)
        writes the install directory itself, the user pip cache, and the
        user site-packages. If the CURRENT process user can already write
        the install directory, sudo is not merely unnecessary -- it is
        actively WRONG: it escalates to root and points pip at root's
        separate (often read-only) cache/site-packages instead.
        os.access(W_OK) correctly classifies every layout that matters: an
        editable-home install (writable -> no sudo), a ~/.local install
        (writable -> no sudo; the "/.local/" substring is also kept as a
        fast-path signal so a probe that can list the file but not stat its
        parent still classifies correctly), a genuine root-owned system
        install (not writable as the service user -> sudo), and the
        auto-updater running as root itself (writable -> sudo not needed,
        already root).

        Conservative: returns False (use sudo) on any subprocess failure,
        empty probe output, or writability-check error. Each conservative
        branch is DEBUG-logged for operator visibility into why sudo was
        chosen.

        Args:
            python_path: Path to the Python interpreter to probe.

        Returns:
            True if code_indexer.__file__ is under ~/.local/ OR its
            containing directory is writable by the current process user
            (user install, no sudo). False otherwise, or on any probe/check
            failure (system install, use sudo).
        """
        try:
            result = subprocess.run(
                [
                    python_path,
                    "-c",
                    "import code_indexer; print(code_indexer.__file__)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.debug(
                    f"_is_user_install probe failed (rc={result.returncode}): "
                    f"{result.stderr.strip()}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return False
            install_path = result.stdout.strip()
            if not install_path:
                logger.debug(
                    "_is_user_install probe returned empty output; "
                    "conservatively treating as system install",
                    extra={"correlation_id": get_correlation_id()},
                )
                return False
        except Exception as e:
            logger.debug(
                f"_is_user_install probe raised an exception: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        if "/.local/" in install_path:
            return True

        try:
            install_dir = Path(install_path).resolve().parent
            return os.access(install_dir, os.W_OK)
        except Exception as e:
            logger.debug(
                f"_is_user_install writability check failed for {install_path}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _get_cli_python_interpreter(self) -> Optional[str]:
        """Resolve the Python interpreter the system-wide `cidx` CLI runs under.

        Bug #1392: the CLI's system-wide Python environment is SEPARATE from
        the server's own pipx venv (`_get_server_python()`) -- real `cidx`
        indexing subprocesses run under this interpreter, not the server's.
        Resolved dynamically via the `cidx` console-script's shebang line
        (never hardcoded), so this works regardless of install layout
        (system pip, pipx, venv, user install).

        Returns:
            Absolute path to the CLI's Python interpreter, or None if it
            cannot be resolved (e.g. no system-wide `cidx` installed yet --
            a legitimate non-error state, not a failure).
        """
        cidx_bin = shutil.which("cidx")
        if cidx_bin is None:
            logger.debug(
                "No system-wide 'cidx' CLI entrypoint found on PATH",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

        try:
            first_line = Path(cidx_bin).read_text().splitlines()[0]
        except Exception as e:
            logger.debug(
                f"Could not read cidx entrypoint {cidx_bin}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

        if not first_line.startswith("#!"):
            logger.debug(
                f"cidx entrypoint {cidx_bin} has no shebang line",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

        shebang = first_line[2:].strip()
        tokens = shebang.split()
        if not tokens:
            logger.debug(
                f"cidx entrypoint {cidx_bin} has an empty shebang",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

        if Path(tokens[0]).name == "env" and len(tokens) > 1:
            resolved = shutil.which(tokens[1])
        else:
            resolved = tokens[0]

        if resolved is None or not Path(resolved).exists():
            logger.debug(
                f"cidx entrypoint {cidx_bin} shebang resolved to a "
                f"nonexistent interpreter: {resolved}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

        return resolved

    def _hnswlib_has_full_capability(self, python_path: str) -> bool:
        """Return True if the given python's hnswlib has the custom fork's methods.

        Bug #1392: stricter than `_hnswlib_importable()` -- a stock PyPI
        hnswlib IS importable (that is exactly the bug), so a plain import
        probe cannot detect drift. This probes for `check_integrity` and
        `repair_orphans` specifically, mirroring
        `HNSWIndexManager._ensure_hnswlib_capability()`'s own check.

        Args:
            python_path: Path to the Python interpreter to probe.

        Returns:
            True if both fork methods are present (rc=0), False otherwise
            (including on any probe exception, which is DEBUG-logged).
        """
        try:
            result = subprocess.run(
                [
                    python_path,
                    "-c",
                    "import hnswlib, sys; sys.exit(0 if hasattr(hnswlib.Index, "
                    "'check_integrity') and hasattr(hnswlib.Index, "
                    "'repair_orphans') else 1)",
                ],
                capture_output=True,
                text=True,
                timeout=HNSWLIB_CAPABILITY_PROBE_TIMEOUT_SECONDS,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(
                f"_hnswlib_has_full_capability probe raised an exception for "
                f"{python_path}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _get_python_site_packages(self, python_path: str) -> Optional[str]:
        """Return the given python's primary site-packages path, or None.

        Bug #1392: diagnostic-only -- used purely to make
        `_ensure_cli_hnswlib_capability()`'s failure message actionable (name
        the site-packages path found deficient). Does NOT affect where pip
        installs (pip resolves that correctly for whatever interpreter it is
        invoked with); this is logging context only.

        Args:
            python_path: Path to the Python interpreter to probe.

        Returns:
            The stripped site-packages path, or None on any failure (DEBUG-logged).
        """
        try:
            result = subprocess.run(
                [python_path, "-c", "import site; print(site.getsitepackages()[0])"],
                capture_output=True,
                text=True,
                timeout=HNSWLIB_CAPABILITY_PROBE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                logger.debug(
                    f"_get_python_site_packages probe failed (rc={result.returncode}) "
                    f"for {python_path}: {result.stderr}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return None
            output = result.stdout.strip()
            return output if output else None
        except Exception as e:
            logger.debug(
                f"_get_python_site_packages probe raised an exception for "
                f"{python_path}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
            return None

    def _cli_hnswlib_failure_message(self, cli_python: str) -> str:
        """Build the actionable error message for a failed CLI hnswlib sync.

        Bug #1392: names the interpreter, its site-packages (if resolvable),
        the expected fork commit, and the docs rebuild procedure -- split out
        of `_ensure_cli_hnswlib_capability()` to keep that method short.
        """
        site_packages = self._get_python_site_packages(cli_python)
        expected_commit = self._get_hnswlib_submodule_commit()
        return (
            "Failed to sync custom hnswlib fork into the CLI's system-wide "
            f"Python environment. Interpreter: {cli_python}. Site-packages: "
            f"{site_packages or 'unresolvable'}. Expected fork commit: "
            f"{expected_commit or 'unknown'}. Real cidx indexing subprocesses "
            "under this interpreter may fail with AttributeError on "
            "check_integrity()/repair_orphans(). See "
            "docs/hnswlib-custom-build.md for the manual rebuild procedure."
        )

    def _ensure_cli_hnswlib_capability(self) -> bool:
        """Ensure the CLI's system-wide Python env has the custom hnswlib fork.

        Bug #1392: the CLI's system-wide Python environment is SEPARATE from
        the server's own pipx venv, and was previously never synced by the
        deploy pipeline, so it could silently drift to a stock PyPI hnswlib.

        Orchestration: resolve CLI interpreter (None -> nothing to sync yet)
        -> skip if already fully capable (idempotent) -> else build -> loud
        actionable error on failure (see `_cli_hnswlib_failure_message`).

        Returns:
            True if synced, already capable, or nothing to sync; False if
            the build genuinely failed (non-fatal to the overall deploy).
        """
        cli_python = self._get_cli_python_interpreter()
        if cli_python is None:
            logger.info(
                "No system-wide 'cidx' CLI entrypoint found on PATH -- "
                "skipping CLI hnswlib capability sync (nothing to sync yet)",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if self._hnswlib_has_full_capability(cli_python):
            logger.info(
                f"CLI Python environment ({cli_python}) already has hnswlib "
                "check_integrity/repair_orphans -- skipping rebuild",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if self._build_hnswlib_with_fallback(python_path=cli_python):
            logger.info(
                f"Successfully synced custom hnswlib fork into CLI Python "
                f"environment ({cli_python})",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        logger.error(
            format_error_log(
                "DEPLOY-GENERAL-209",
                self._cli_hnswlib_failure_message(cli_python),
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return False

    def _hnswlib_importable(self, python_path: str) -> bool:
        """Return True if hnswlib can be imported by the server python.

        Bug #1245: Used to (a) skip an unnecessary rebuild and (b) demote a
        failed rebuild to WARNING when the existing module still works.

        Args:
            python_path: Path to the Python interpreter to probe.

        Returns:
            True if `import hnswlib` succeeds (rc=0), False otherwise.
        """
        try:
            result = subprocess.run(
                [python_path, "-c", "import hnswlib"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_hnswlib_submodule_commit(self) -> Optional[str]:
        """Return the current hnswlib submodule commit hash from git, or None if unavailable.

        Bug #1245: Used to detect whether the submodule has changed since the last
        successful build, so an up-to-date importable hnswlib can skip the rebuild.

        Returns:
            The commit hash string, or None on any failure.
        """
        try:
            result = subprocess.run(
                ["git", "ls-files", "-s", "third_party/hnswlib"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            # Output format: "<mode> <hash> <stage>\t<path>"
            parts = result.stdout.strip().split()
            if len(parts) < 2:
                return None
            return parts[1]
        except Exception:
            return None

    def _get_last_built_hnswlib_commit(self) -> Optional[str]:
        """Return the commit hash recorded from the last successful hnswlib build.

        Bug #1245: Persisted to _cidx_data_dir/hnswlib-last-built-commit so that
        the rebuild-skip check survives process restarts.

        Returns:
            The stored commit hash string, or None if absent/unreadable.
        """
        try:
            path = _cidx_data_dir / "hnswlib-last-built-commit"
            if not path.exists():
                return None
            value = path.read_text().strip()
            return value if value else None
        except Exception:
            return None

    def _save_last_built_hnswlib_commit(self, commit: str) -> None:
        """Persist the commit hash of a successful hnswlib build for future skip checks.

        Bug #1245: Written atomically after build_custom_hnswlib() succeeds so the
        next deploy can compare against it to skip an unchanged rebuild.

        Args:
            commit: The hnswlib submodule commit hash to record.
        """
        try:
            path = _cidx_data_dir / "hnswlib-last-built-commit"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(commit)
            logger.debug(
                f"Saved hnswlib last-built commit: {commit[:8]}",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                f"Could not save hnswlib built-commit marker: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    def build_custom_hnswlib(
        self,
        hnswlib_path: Optional[Path] = None,
        python_path: Optional[str] = None,
    ) -> bool:
        """Build and install custom hnswlib from specified path or default submodule.

        The custom hnswlib fork includes the check_integrity() method for HNSW
        index validation. This must be built from source and installed to replace
        the standard pip-installed hnswlib.

        Args:
            hnswlib_path: Path to hnswlib source directory. If None, uses default
                         submodule path (third_party/hnswlib).
            python_path: Target Python interpreter to build/install into. If
                         None (default), resolves via `self._get_server_python()`
                         as before (Bug #1392: callers targeting a DIFFERENT
                         environment -- e.g. the CLI's system-wide python --
                         pass it explicitly here).

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
            if python_path is None:
                python_path = self._get_server_python()
            # Bug #1245: Skip rebuild when hnswlib is already importable and the
            # submodule commit is unchanged since the last successful build.
            # Bug #1392 remediation: the last-built-commit marker is GLOBAL,
            # not keyed per-interpreter -- a build against a DIFFERENT
            # interpreter (e.g. the server's) can have already written it for
            # the current commit. Gating the skip on plain importability
            # (_hnswlib_importable) is therefore insufficient: a stock PyPI
            # hnswlib on THIS interpreter is also importable, so the skip
            # would falsely fire and leave this interpreter's install
            # un-rebuilt. _hnswlib_has_full_capability() probes for the
            # fork's own check_integrity/repair_orphans methods, so it can
            # only skip when THIS interpreter's install genuinely already has
            # the fork.
            current_commit = self._get_hnswlib_submodule_commit()
            last_built_commit = self._get_last_built_hnswlib_commit()
            if self._hnswlib_has_full_capability(python_path):
                if current_commit is not None and current_commit == last_built_commit:
                    logger.info(
                        f"hnswlib already has full fork capability and submodule "
                        f"commit unchanged ({current_commit[:8]}); skipping rebuild",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return True

            # Bug #1243: pass TMPDIR through sudo via the `env` utility so pip can find
            # a writable temp dir under systemd PrivateTmp=yes.  sudo's env_reset strips
            # inherited env vars but does NOT strip vars set by a child `env` command, so
            # `sudo env TMPDIR=<dir> python3 -m pip install ...` receives a usable TMPDIR.
            tmpdir = self._deploy_tmpdir()
            # Bug #1245: Use sudo only for system installs. For a user-install layout
            # (code-indexer in ~/.local), sudo would target /root/.local — the wrong
            # site-packages and read-only on immutable hosts.
            use_sudo = not self._is_user_install(python_path)
            # Bug #1234: Probe with use_sudo matching the install so we test the SAME
            # pip binary (system pip for sudo installs, user pip for user installs).
            break_sys_pkg = self._pip_supports_break_system_packages(
                python_path, use_sudo=use_sudo
            )

            # Install pybind11 first - required because setup.py imports it at module level.
            # Bug #1243: For sudo installs, env TMPDIR= passes the temp dir through
            # sudo's env_reset. For user installs, the environment is inherited as-is.
            pybind11_cmd = (
                [
                    "sudo",
                    "env",
                    f"TMPDIR={tmpdir}",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                ]
                if use_sudo
                else [python_path, "-m", "pip", "install"]
            )
            if break_sys_pkg:
                pybind11_cmd.append("--break-system-packages")
            pybind11_cmd.append("pybind11")
            pybind_result = subprocess.run(
                pybind11_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # Belt-and-suspenders: if pip still rejects the flag, retry without it.
            if (
                pybind_result.returncode != 0
                and "--break-system-packages" in pybind11_cmd
                and "no such option" in pybind_result.stderr
            ):
                logger.warning(
                    "pybind11 install rejected --break-system-packages; retrying without flag",
                    extra={"correlation_id": get_correlation_id()},
                )
                retry_cmd = [c for c in pybind11_cmd if c != "--break-system-packages"]
                pybind_result = subprocess.run(
                    retry_cmd,
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
            # Bug #1243: For sudo installs, env TMPDIR= passes the temp dir through
            # sudo's env_reset. For user installs, the environment is inherited as-is.
            hnswlib_cmd = (
                [
                    "sudo",
                    "env",
                    f"TMPDIR={tmpdir}",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                ]
                if use_sudo
                else [python_path, "-m", "pip", "install"]
            )
            if break_sys_pkg:
                hnswlib_cmd.append("--break-system-packages")
            hnswlib_cmd.extend(["--force-reinstall", "--no-deps", "."])
            result = subprocess.run(
                hnswlib_cmd,
                cwd=hnswlib_path,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for compilation
            )

            # Belt-and-suspenders: if pip still rejects the flag, retry without it.
            if (
                result.returncode != 0
                and "--break-system-packages" in hnswlib_cmd
                and "no such option" in result.stderr
            ):
                logger.warning(
                    "hnswlib install rejected --break-system-packages; retrying without flag",
                    extra={"correlation_id": get_correlation_id()},
                )
                retry_cmd = [c for c in hnswlib_cmd if c != "--break-system-packages"]
                result = subprocess.run(
                    retry_cmd,
                    cwd=hnswlib_path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

            if result.returncode != 0:
                # Bug #1245: Non-fatal when hnswlib is still importable (existing module works).
                # Bug #1392 remediation: "still importable" is NOT sufficient --
                # a stock PyPI hnswlib is importable too, so demoting on plain
                # importability would falsely report success for a target that
                # genuinely lacks the fork's check_integrity/repair_orphans
                # methods. Only demote to non-fatal when the fork's own
                # capability is confirmed present.
                if self._hnswlib_has_full_capability(python_path):
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-042",
                            f"Custom hnswlib build failed but the fork's capability "
                            f"is still present; continuing deploy: "
                            f"{result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return True
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
            # Bug #1245: Record successful build commit for future skip checks.
            if current_commit:
                self._save_last_built_hnswlib_commit(current_commit)
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

    def _build_hnswlib_with_fallback(self, python_path: Optional[str] = None) -> bool:
        """Build custom hnswlib with fallback to standalone clone if submodule fails.

        Bug #160: Unified method that tries submodule first, then falls back to
        cloning hnswlib to standalone location if submodule has no setup.py.

        Strategy:
        1. Check if submodule path has setup.py
        2. If yes: build from submodule (normal path)
        3. If no: clone to fallback location and build from there

        Args:
            python_path: Target Python interpreter to build/install into,
                         threaded through to build_custom_hnswlib(). If None
                         (default), build_custom_hnswlib() resolves via
                         `self._get_server_python()` as before (Bug #1392:
                         callers targeting the CLI's system-wide python pass
                         it explicitly here).

        Returns:
            True if either approach succeeds, False if both fail
        """
        submodule_path = self.repo_path / "third_party" / "hnswlib"
        submodule_setup_py = submodule_path / "setup.py"
        # Bug #1392: only pass python_path through when explicitly given, so
        # the call shape when it is None (the default) is byte-identical to
        # pre-#1392 behavior -- existing tests assert the exact call args.
        extra_kwargs = {} if python_path is None else {"python_path": python_path}

        # Try submodule first if setup.py exists
        if submodule_setup_py.exists():
            logger.info(
                "Building hnswlib from submodule path",
                extra={"correlation_id": get_correlation_id()},
            )
            return self.build_custom_hnswlib(hnswlib_path=None, **extra_kwargs)

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
        if not self.build_custom_hnswlib(
            hnswlib_path=HNSWLIB_FALLBACK_PATH, **extra_kwargs
        ):
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
                timeout=SYSTEMD_OP_TIMEOUT_SECONDS,
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
                timeout=SYSTEMD_OP_TIMEOUT_SECONDS,
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

    def _run_systemd_op_with_retry(
        self,
        cmd: list,
        *,
        input: Optional[str] = None,
        timeout: int = SYSTEMD_OP_TIMEOUT_SECONDS,
        max_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
    ) -> subprocess.CompletedProcess:
        """Run a systemd/sudo control-plane subprocess with retry-on-timeout.

        On a freshly-built host, the FIRST auto-update deploy is CPU-heavy
        (hnswlib compile, rustup/ripgrep/Claude-CLI/pace-maker install) and
        transiently starves systemd/sudo (pam_systemd blocks on a busy PID 1).
        A single 30s/10s timeout could raise subprocess.TimeoutExpired even
        though the operation would have succeeded moments later. This helper
        retries ONLY on subprocess.TimeoutExpired -- a completed process with
        ANY returncode (including nonzero) is returned immediately on the
        first attempt that does not raise; nonzero returncode is a real
        failure, not a transient one, so it is never retried.

        Args:
            cmd: Command argv list to execute.
            input: Optional stdin text (e.g. for `sudo tee`).
            timeout: Per-attempt timeout in seconds.
            max_attempts: Maximum number of attempts before giving up.
            retry_delay_seconds: Seconds to sleep between attempts.

        Returns:
            The subprocess.CompletedProcess from the first attempt that does
            not raise subprocess.TimeoutExpired.

        Raises:
            subprocess.TimeoutExpired: If every attempt times out.
        """
        last_timeout_error: Optional[subprocess.TimeoutExpired] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return subprocess.run(
                    cmd,
                    input=input,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as e:
                last_timeout_error = e
                if attempt < max_attempts:
                    logger.warning(
                        f"systemd/sudo op timed out after {timeout}s "
                        f"(attempt {attempt}/{max_attempts}), retrying: {cmd}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                    time.sleep(retry_delay_seconds)

        assert last_timeout_error is not None
        raise last_timeout_error

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
            result = self._run_systemd_op_with_retry(
                ["sudo", "tee", str(service_path)],
                input=content,
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
            result = self._run_systemd_op_with_retry(
                ["sudo", "systemctl", "daemon-reload"],
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

            config = ServerConfigManager(
                server_dir_path=str(_cidx_data_dir)
            ).load_config()
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
            # Bug #1243: pass TMPDIR through sudo via the `env` utility so pip can find
            # a writable temp dir under systemd PrivateTmp=yes.
            tmpdir = self._deploy_tmpdir()
            # Bug #1245: Use sudo only for system installs. For a user-install layout
            # (code-indexer in ~/.local), sudo would target /root/.local — the wrong
            # site-packages and read-only on immutable hosts.
            use_sudo = not self._is_user_install(python_path)
            # Bug #1234: Probe with use_sudo matching the install so we test the SAME
            # pip binary (system pip for sudo installs, user pip for user installs).
            # Bug #1243: For sudo installs, env TMPDIR= passes the temp dir through
            # sudo's env_reset. For user installs, the environment is inherited as-is.
            pip_cmd = (
                [
                    "sudo",
                    "env",
                    f"TMPDIR={tmpdir}",
                    python_path,
                    "-m",
                    "pip",
                    "install",
                ]
                if use_sudo
                else [python_path, "-m", "pip", "install"]
            )
            if self._pip_supports_break_system_packages(python_path, use_sudo=use_sudo):
                pip_cmd.append("--break-system-packages")
            pip_cmd.extend(["-e", "."])
            result = subprocess.run(
                pip_cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
            )

            # Belt-and-suspenders: if pip still rejects the flag, retry without it.
            if (
                result.returncode != 0
                and "--break-system-packages" in pip_cmd
                and "no such option" in result.stderr
            ):
                logger.warning(
                    "pip install rejected --break-system-packages; retrying without flag",
                    extra={"correlation_id": get_correlation_id()},
                )
                retry_cmd = [c for c in pip_cmd if c != "--break-system-packages"]
                result = subprocess.run(
                    retry_cmd,
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

    def _read_launch_source(self, mode: str) -> Optional[dict]:
        """Read launch config JSON for the given mode.

        Story #1196 (next-release cleanup): the config.json rung is removed
        from BOTH modes.

        APPLY:  reads LAUNCH_CONFIG_PATH; missing/corrupt → returns {} (falls
                through directly to ServerConfig defaults; APPLY's job is to
                apply the TARGET).
        DEPLOY: reads APPLIED_LAUNCH_CONFIG_PATH. Both the MISSING and CORRUPT cases
            return None so the caller PRESERVES the live ExecStart unchanged — a routine
            code deploy must never rewrite the live unit from a stale config. The live
            ExecStart is the confirmed running state (e.g. --host 0.0.0.0 bound so HAProxy
            on another host can reach this node); falling through to ServerConfig
            defaults would risk rewriting --host 0.0.0.0 → 127.0.0.1 (the
            ServerConfig default), dropping the node off the load balancer (a confirmed
            production-outage path). A present+valid applied_launch.json is used normally.
        """
        source_path = (
            LAUNCH_CONFIG_PATH if mode == "APPLY" else APPLIED_LAUNCH_CONFIG_PATH
        )
        if not source_path.exists():
            if mode == "DEPLOY":
                # DEPLOY + missing applied_launch.json: preserve the live ExecStart.
                # The live unit IS the confirmed running state (e.g. --host 0.0.0.0 bound
                # so HAProxy on another host can reach this node). Falling through to
                # ServerConfig defaults risks rewriting --host 0.0.0.0
                # to 127.0.0.1 (the ServerConfig default), dropping the node off the
                # load balancer (production outage).
                # Treat identically to the CORRUPT case: return None so the caller
                # preserves the live unit unchanged.
                logger.debug(
                    "DEPLOY: applied_launch.json missing — preserving live ExecStart "
                    "(same as corrupt path; live unit is the confirmed running state)",
                    extra={"correlation_id": get_correlation_id()},
                )
                return None
            return {}
        try:
            return dict(json.loads(source_path.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            if mode == "DEPLOY":
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-199",
                        f"DEPLOY: applied_launch.json corrupt — preserving live ExecStart: {exc}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return None
            logger.debug(
                f"APPLY: launch.json unreadable ({exc}); falling through to ServerConfig defaults",
                extra={"correlation_id": get_correlation_id()},
            )
            return {}

    def _fill_from_live_execstart(
        self,
        mode: str,
        host: Optional[str],
        port: Optional[int],
        workers: Optional[int],
    ) -> tuple:
        """Fill any still-None DEPLOY values from the live ExecStart.

        Story #1196 AC2 (FIX-1 / MAJOR-M1): the config.json rung is removed
        from BOTH modes.
          APPLY:  missing values are left None here so the caller applies the
                  ServerConfig defaults directly -- no fallback source between
                  launch.json and the literal defaults.
          DEPLOY: missing values (a partially-populated applied_launch.json)
                  are filled from the CURRENT live-unit ExecStart -- the
                  confirmed running state -- never from config.json, before
                  falling through to ServerConfig defaults.
        """
        if mode != "DEPLOY" or all(v is not None for v in (host, port, workers)):
            return host, port, workers
        live = read_execstart_flags(self.service_name)
        if host is None:
            host = live.get("host")
        if port is None:
            port = live.get("port")
        if workers is None:
            workers = live.get("workers")
        return host, port, workers

    def _resolve_launch_values(self, mode: str) -> Optional[dict]:
        """Return resolved {host, port, workers} or None (DEPLOY: missing/corrupt source).

        For APPLY mode, also includes applied_restart_generation sourced from
        launch.json's target_restart_generation (defaults to 0 when absent).
        DEPLOY mode is unaffected: it reads applied_launch.json which has no generation
        field, and the ExecStart rewrite must not receive that field.
        """
        raw = self._read_launch_source(mode)
        if raw is None:
            return (
                None  # DEPLOY: corrupt/missing applied_launch.json — preserve ExecStart
            )

        host, port, workers = raw.get("host"), raw.get("port"), raw.get("workers")
        host, port, workers = self._fill_from_live_execstart(mode, host, port, workers)
        result: dict = {
            "host": host if host is not None else _LAUNCH_DEFAULT_HOST,
            "port": int(port) if port is not None else _LAUNCH_DEFAULT_PORT,
            "workers": max(
                1, int(workers) if workers is not None else _LAUNCH_DEFAULT_WORKERS
            ),
        }
        if mode == "APPLY":
            # APPLY reads launch.json; target_restart_generation is written by Story #1198.
            # Story #1200 reads applied_restart_generation to detect pending restart loops.
            # Default 0 matches COALESCE(applied, 0) semantics in #1200 AC1/AC5.
            result["applied_restart_generation"] = int(
                raw.get("target_restart_generation") or 0
            )
        return result

    @staticmethod
    def _is_cidx_execstart(line: str) -> bool:
        """Detection predicate covering both ExecStart shapes (CRITICAL-D).

        The old uvicorn-only gate silently skipped installer-shape units that use
        'code_indexer.server.main' instead of 'uvicorn'. This predicate covers both.
        """
        return line.startswith("ExecStart=") and (
            "code_indexer.server.main" in line or "uvicorn" in line
        )

    @staticmethod
    def _read_flag(line: str, flag: str) -> "Optional[str]":
        """Extract the value of a flag from an ExecStart line (Bug #1232).

        Uses the same bounded-token regex as _rewrite_flag so '--workers 1'
        is never confused with '--workers 10'.

        Returns the string value following flag, or None if flag is absent.
        """
        import re as _re

        bounded = _re.compile(r"(?<!\S)" + _re.escape(flag) + r"\s+(\S+)")
        m = bounded.search(line)
        return m.group(1) if m else None

    @staticmethod
    def _rewrite_flag(line: str, flag: str, value: str) -> tuple:
        """Token-bounded in-place flag rewrite (Bug #1183 idiom).

        Returns (new_line, was_modified). Exact match → no-op.
        Differing value → bounded replace. Absent flag → append.
        Never confuses '--workers 1' with '--workers 10'.
        """
        import re as _re

        exact = _re.compile(
            r"(?<!\S)" + _re.escape(flag) + r"\s+" + _re.escape(value) + r"(?!\S)"
        )
        if exact.search(line):
            return line, False

        bounded = _re.compile(r"(?<!\S)" + _re.escape(flag) + r"\s+\S+")
        if bounded.search(line):
            return bounded.sub(f"{flag} {value}", line), True

        return line.rstrip() + f" {flag} {value}", True

    def _read_cidx_service_lines(self, service_path: "Path") -> Optional[list]:
        """Read service file lines; return them only if a cidx ExecStart is present.

        Returns None when the file is missing or contains no cidx ExecStart line.
        """
        if not service_path.exists():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-017",
                    f"Service file not found: {service_path}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return None
        lines = service_path.read_text().split("\n")
        if not any(self._is_cidx_execstart(ln) for ln in lines):
            logger.debug(
                "No cidx ExecStart found; skipping launch config rewrite",
                extra={"correlation_id": get_correlation_id()},
            )
            return None
        return lines

    def _rewrite_execstart_lines(
        self, lines: list, host: str, port: int, workers: int
    ) -> tuple:
        """Rewrite --host/--port/--workers on cidx ExecStart lines only.

        Returns (updated_lines, was_modified). Never writes --log-level (CRITICAL-A).
        """
        updated, modified = [], False
        for line in lines:
            if self._is_cidx_execstart(line):
                line, c1 = self._rewrite_flag(line, "--host", str(host))
                line, c2 = self._rewrite_flag(line, "--port", str(port))
                line, c3 = self._rewrite_flag(line, "--workers", str(workers))
                modified = modified or c1 or c2 or c3
            updated.append(line)
        return updated, modified

    def _write_and_reload_service(self, service_path: "Path", lines: list) -> bool:
        """Write lines to service_path via sudo tee, then daemon-reload.

        Returns True on tee success; daemon-reload failure is logged but non-fatal.
        """
        tee = subprocess.run(
            ["sudo", "tee", str(service_path)],
            input="\n".join(lines),
            capture_output=True,
            text=True,
        )
        if tee.returncode != 0:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-018",
                    f"sudo tee failed updating service file: {tee.stderr}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        reload = subprocess.run(
            ["sudo", "systemctl", "daemon-reload"],
            capture_output=True,
            text=True,
        )
        if reload.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-173",
                    f"daemon-reload failed after launch config update: {reload.stderr}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
        return True

    def _ensure_launch_config(self, mode: str) -> Optional[dict]:
        """Rewrite --host/--port/--workers in the live systemd ExecStart.

        APPLY:  returns snapshot {host, port, workers} on success; None on failure.
        DEPLOY: rewrites ExecStart then always returns None (MAJOR-M5).
        """
        if mode not in {"APPLY", "DEPLOY"}:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-200",
                    f"_ensure_launch_config: invalid mode={mode!r}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return None
        try:
            values = self._resolve_launch_values(mode)
            if values is None:
                return None
            host, port, workers = values["host"], values["port"], values["workers"]
            service_path = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
            lines = self._read_cidx_service_lines(service_path)
            if lines is None:
                return None
            updated, modified = self._rewrite_execstart_lines(
                lines, host, port, workers
            )
            snapshot: dict = {"host": host, "port": port, "workers": workers}
            if "applied_restart_generation" in values:
                snapshot["applied_restart_generation"] = values[
                    "applied_restart_generation"
                ]
            if not modified:
                logger.debug(
                    f"ExecStart already matches ({host}:{port} workers={workers}); no-op",
                    extra={"correlation_id": get_correlation_id()},
                )
                return snapshot if mode == "APPLY" else None
            if not self._write_and_reload_service(service_path, updated):
                return None
            logger.info(
                f"Rewrote ExecStart: --host {host} --port {port} --workers {workers} "
                f"(mode={mode})",
                extra={"correlation_id": get_correlation_id()},
            )
            return snapshot if mode == "APPLY" else None
        except Exception as exc:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-019",
                    f"Error in _ensure_launch_config(mode={mode}): {exc}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return None

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
            check_result = self._run_systemd_op_with_retry(
                ["sudo", "cat", str(sudoers_path)],
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
            tee_result = self._run_systemd_op_with_retry(
                ["sudo", "tee", str(sudoers_path)],
                input=expected_rule,
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
            chmod_result = self._run_systemd_op_with_retry(
                ["sudo", "chmod", "0440", str(sudoers_path)],
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
                self._run_systemd_op_with_retry(
                    ["sudo", "rm", "-f", str(sudoers_path)],
                )
                return False

            # Validate with visudo
            visudo_result = self._run_systemd_op_with_retry(
                ["sudo", "visudo", "-c", "-f", str(sudoers_path)],
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
                self._run_systemd_op_with_retry(
                    ["sudo", "rm", "-f", str(sudoers_path)],
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
            # Read version from disk so it reflects the newly-deployed code after
            # git pull, rather than the stale cached import in sys.modules.
            import re as _re

            version = "unknown"
            init_py = self.repo_path / "src" / "code_indexer" / "__init__.py"
            try:
                text = init_py.read_text()
                m = _re.search(
                    r'^__version__\s*=\s*["\']([^"\']+)["\']', text, _re.MULTILINE
                )
                if m:
                    version = m.group(1)
                else:
                    raise ValueError("no __version__ line found")
            except Exception:
                # Fall back to cached import when file is missing or unparseable
                try:
                    from code_indexer import __version__

                    version = __version__
                except ImportError:
                    pass

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
                timeout=SYSTEMD_OP_TIMEOUT_SECONDS,
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
            check_result = self._run_systemd_op_with_retry(
                ["sysctl", "-n", "vm.overcommit_memory"],
            )
            if check_result.stdout.strip() == "1":
                logger.debug(
                    "Memory overcommit already configured (vm.overcommit_memory=1)",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Write persistent sysctl config file
            write_result = self._run_systemd_op_with_retry(
                ["sudo", "tee", "/etc/sysctl.d/99-cidx-memory.conf"],
                input="vm.overcommit_memory = 1\n",
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
            apply_result = self._run_systemd_op_with_retry(
                ["sudo", "sysctl", "-p", "/etc/sysctl.d/99-cidx-memory.conf"],
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

        Bug #1254: best-effort / non-fatal. Swap is an OOM optimization, not
        a correctness requirement -- the server runs correctly without it.
        On read-only/immutable hosts (e.g. "/" mounted read-only) fallocate/
        chmod/mkswap/swapon legitimately cannot succeed. Any subprocess
        failure (or unexpected exception) here is logged at WARNING and the
        method still returns True so deployment proceeds to the restart.

        Returns:
            True always (best-effort) -- swap setup failures are logged at
            WARNING and never block deployment.
        """
        try:
            # Check if any swap is already active
            check_result = self._run_systemd_op_with_retry(
                ["swapon", "--show", "--noheadings"],
            )
            if check_result.stdout.strip():
                logger.debug(
                    f"Swap already configured: {check_result.stdout.strip()}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # Allocate 4GB swap file
            fallocate_result = self._run_systemd_op_with_retry(
                ["sudo", "fallocate", "-l", "4G", "/swapfile"],
            )
            if fallocate_result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-093",
                        f"fallocate -l 4G /swapfile failed: {fallocate_result.stderr} "
                        "- swap is an OOM optimization, continuing without it",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Bug #1254: non-fatal, swap is best-effort

            # Set secure permissions (0600 - root only)
            chmod_result = self._run_systemd_op_with_retry(
                ["sudo", "chmod", "600", "/swapfile"],
            )
            if chmod_result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-094",
                        f"chmod 600 /swapfile failed: {chmod_result.stderr} "
                        "- swap is an OOM optimization, continuing without it",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Bug #1254: non-fatal, swap is best-effort

            # Format as swap
            mkswap_result = self._run_systemd_op_with_retry(
                ["sudo", "mkswap", "/swapfile"],
            )
            if mkswap_result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-095",
                        f"mkswap /swapfile failed: {mkswap_result.stderr} "
                        "- swap is an OOM optimization, continuing without it",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Bug #1254: non-fatal, swap is best-effort

            # Enable swap immediately
            swapon_result = self._run_systemd_op_with_retry(
                ["sudo", "swapon", "/swapfile"],
            )
            if swapon_result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-096",
                        f"swapon /swapfile failed: {swapon_result.stderr} "
                        "- swap is an OOM optimization, continuing without it",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True  # Bug #1254: non-fatal, swap is best-effort

            # Add fstab entry for reboot persistence
            fstab_result = self._run_systemd_op_with_retry(
                ["cat", "/etc/fstab"],
            )
            if "/swapfile" not in fstab_result.stdout:
                tee_result = self._run_systemd_op_with_retry(
                    ["sudo", "tee", "-a", "/etc/fstab"],
                    input="/swapfile none swap sw 0 0\n",
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
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-098",
                    f"Exception creating swap file: {e} "
                    "- swap is an OOM optimization, continuing without it",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return True  # Bug #1254: non-fatal, swap is best-effort

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

        # Bug #1251: Set TMPDIR for the entire deploy process BEFORE any
        # subprocess is spawned. Bug #1243 set TMPDIR only on the explicit
        # `sudo env TMPDIR=...` prefix used by the SUDO pip path; Bug #1245's
        # writability fix routes editable-home installs (no /.local/ segment,
        # writable directory -- the staging cluster layout) through the
        # NO-SUDO command, which carried no TMPDIR at all. Under systemd
        # PrivateTmp=yes + Python 3.12, the auto-updater's private /tmp is
        # isolated and unusable, so pip's tempfile.gettempdir() raises
        # FileNotFoundError at the pybind11/hnswlib build step, dead-looping
        # the auto-updater (same self-perpetuating class as #1182/#1243/#1245).
        #
        # Mutating os.environ here -- rather than threading an explicit env=
        # kwarg through every no-sudo call site -- is sufficient and
        # exhaustive: every subprocess.run() invocation in this module either
        # omits env= entirely (inherits the live process environment, which
        # now carries TMPDIR) or explicitly builds its env from
        # os.environ.copy() / dict(os.environ) (build_non_interactive_git_env()
        # for git operations, the self-restart smoke test, the pace-maker
        # no-sudo install, the Rust toolchain install + cargo build). So this
        # single early mutation propagates TMPDIR to every no-sudo child
        # process without requiring any per-call-site change.
        #
        # The SUDO path is unchanged: sudo's env_reset strips inherited
        # environment variables, so it still requires (and keeps) the
        # explicit ["sudo", "env", f"TMPDIR={tmpdir}", ...] prefix from
        # Bug #1243.
        os.environ["TMPDIR"] = self._deploy_tmpdir()

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

        # Step 1.7 (Bug #1392): Ensure the CLI's SEPARATE system-wide Python
        # environment also has the custom hnswlib fork -- every real cidx CLI
        # indexing subprocess runs under that interpreter, not the server's
        # own pipx venv, and the two have been observed to drift apart in
        # production (Bug #1392: AttributeError: 'hnswlib.Index' object has
        # no attribute 'check_integrity' on every fleet-wide refresh).
        #
        # Non-fatal (unlike Step 1.6): this targets a wholly INDEPENDENT
        # Python environment from the one cidx-server itself runs under, so a
        # failure here (e.g. missing compiler, transient clone failure) must
        # not block the server's own restart/config steps, which are
        # unaffected by it. The runtime capability check added in
        # storage/hnsw_index_manager.py is the actual last-line-of-defense:
        # it fails loudly and immediately the moment real indexing is
        # attempted post-deploy, so this step is preventative, not the sole
        # safety net. Hard-aborting the ENTIRE deploy over an unrelated
        # environment's compile failure would repeat the exact "deploy
        # dead-loop over one unrelated environment" failure class this file
        # has already fixed multiple times (Bug #1243/#1245/#1234) -- just
        # relocated to a second Python environment.
        if not self._ensure_cli_hnswlib_capability():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-209",
                    "CLI system-wide hnswlib capability sync failed -- "
                    "indexing subprocesses may still fail with AttributeError "
                    "on check_integrity()/repair_orphans(). See "
                    "docs/hnswlib-custom-build.md.",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

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

        # Step 3: Story #1199 - Ensure launch config (host/port/workers) from applied_launch.json.
        # DEPLOY mode reads applied_launch.json → parse/preserve the live ExecStart → ServerConfig
        # defaults (NEVER launch.json/TARGET; the config.json rung was removed in Story #1196)
        # so a routine code deploy preserves the last operator-applied launch config without
        # auto-applying a saved-but-unconfirmed TARGET change (decision #3).
        # Uses the broadened ExecStart predicate (CRITICAL-D) that covers both installer-shape
        # (code_indexer.server.main) and uvicorn-shape units.
        self._ensure_launch_config("DEPLOY")

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

        # Step 6.65: BUG #1318 - Provision Node.js/npm BEFORE the npm-dependent
        # steps below (Codex CLI install, scip-python install) so both can
        # actually find npm on PATH instead of silently no-op'ing.
        if not self.ensure_nodejs():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    "Node.js provisioning could not be verified/installed — "
                    "npm-dependent features (scip-python, Codex CLI) will be "
                    "unavailable",
                ),
                extra={"correlation_id": get_correlation_id()},
            )

        # Step 6.7: Story #845 - Idempotently install/update Codex CLI (optional feature)
        if not self._ensure_codex_cli_installed():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-144",
                    "Codex CLI could not be verified/installed — feature effectively disabled",
                    extra={"correlation_id": get_correlation_id()},
                )
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

        # Step 7.1: Ensure scip-python is installed for SCIP-based code intelligence
        # (mirrors the ripgrep step above; non-fatal — SCIP indexing is degraded,
        # not the whole deploy, when this fails).
        scip_python_result = self.ensure_scip_python()
        if scip_python_result:
            logger.info(
                "scip-python installation successful",
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-201",
                    "scip-python installation could not be verified/installed — "
                    "SCIP indexing for Python projects will be unavailable",
                ),
                extra={"correlation_id": get_correlation_id()},
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

        # Step 12: Story #997 - Keep pace-maker installed/updated (non-fatal)
        if not self._ensure_pace_maker_installed():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-158",
                    "Pace-maker install/update skipped or failed - "
                    "pace-maker pacing enforcement may be unavailable",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 13: Initial Claude CLI installer (non-fatal).
        # Step 11 updates an existing install via npm; this step installs
        # from scratch via the official installer when claude is not on PATH.
        if not self._ensure_claude_cli_installed():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-164",
                    "Claude CLI install skipped or failed - "
                    "research assistant feature may be unavailable",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 14: Cluster mode only - set up NFS symlinks for Claude home and research data (non-fatal)
        if not self._ensure_nfs_research_symlinks():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-165",
                    "NFS research symlink setup failed - "
                    "research sessions may not be accessible across cluster nodes",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 14.5: Bug #1052 - Ensure activated-repos symlink for CoW-daemon cluster (non-fatal)
        if not self._ensure_activated_repos_symlink_for_cow_daemon():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-166",
                    "activated-repos symlink setup failed - "
                    "CoW-daemon activation may not function correctly",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 14.6: Bug #1320 Part B - Ensure cow_daemon.daemon_storage_path is
        # populated on CoW-daemon cluster nodes (non-fatal; leaves unset + logs
        # when no source resolves, letting Part A's guard fail loud at publish
        # time instead of silently guessing a value).
        if not self._ensure_daemon_storage_path():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-206",
                    "daemon_storage_path setup failed - "
                    "CoW-daemon versioned-snapshot publish may fail loud "
                    "with a path-translation error",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 14.7: Bug #1337 - Ensure golden-repos symlink for CoW-daemon
        # cluster (non-fatal). Runs AFTER Step 14.6 so it can use a
        # freshly-resolved cow_daemon.daemon_storage_path for the co-located
        # daemon-host target form.
        if not self._ensure_golden_repos_symlink_for_cow_daemon():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-208",
                    "golden-repos symlink setup failed - "
                    "per-user CoW-daemon activation may not function correctly",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 15: Ensure ~/.local/bin is in PATH in the systemd service unit (non-fatal)
        if not self._ensure_systemd_claude_path():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-171",
                    "systemd PATH update failed - "
                    "Claude CLI may not be found when invoked via systemd service",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Step 16: Story #1024 - Ensure Rust toolchain + build xray-cli (FATAL)
        if not self._ensure_rust_toolchain():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    "Rust toolchain provisioning failed - "
                    "xray native backend will not function. "
                    "Deployment cannot continue.",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

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

    def _run_codex_npm_install(self) -> bool:
        """Run `npm install -g @openai/codex` and return True on success.

        Handles nonzero returncode, TimeoutExpired, and OSError as
        WARNING + return False. Never raises.
        """
        try:
            result = subprocess.run(
                ["npm", "install", "-g", "@openai/codex"],
                capture_output=True,
                text=True,
                timeout=CODEX_CLI_INSTALL_TIMEOUT_SECONDS,
                shell=False,
            )
            logger.debug(
                "npm install @openai/codex stdout: %s",
                result.stdout,
                extra={"correlation_id": get_correlation_id()},
            )
            if result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-144",
                        f"npm install @openai/codex failed (exit {result.returncode}): "
                        f"{result.stderr}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-144",
                    f"npm install @openai/codex timed out after {CODEX_CLI_INSTALL_TIMEOUT_SECONDS}s",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except OSError as exc:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-144",
                    f"npm install @openai/codex could not be spawned: {exc}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _probe_codex_version(self) -> None:
        """Run `codex --version` and log the result. Never raises.

        Logs INFO on clean exit, WARNING on nonzero returncode,
        FileNotFoundError, or TimeoutExpired. All outcomes are non-fatal.
        """
        try:
            probe = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                text=True,
                timeout=CODEX_VERSION_PROBE_TIMEOUT_SECONDS,
                shell=False,
            )
            if probe.returncode == 0:
                logger.info(
                    "Codex CLI installed: %s",
                    probe.stdout.strip(),
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.warning(
                    "Codex CLI installed but 'codex --version' returned exit %d — "
                    "binary may need PATH refresh",
                    probe.returncode,
                    extra={"correlation_id": get_correlation_id()},
                )
        except FileNotFoundError:
            logger.warning(
                "Codex CLI installed successfully but 'codex' binary not found "
                "on PATH immediately after install — PATH may need refresh",
                extra={"correlation_id": get_correlation_id()},
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Codex CLI version probe timed out after %ds — "
                "install may have succeeded; binary availability unconfirmed",
                CODEX_VERSION_PROBE_TIMEOUT_SECONDS,
                extra={"correlation_id": get_correlation_id()},
            )

    def _ensure_pace_maker_installed(self) -> bool:
        """Story #997: Ensure pace-maker is cloned, installed, and bootstrap config records path.

        Fresh install: clone + install.sh + set master OFF.
        Update: git pull + install.sh. Config NOT touched on update.
        Non-fatal: failures return False, deployment continues.
        """
        try:
            service_path = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
            server_user = None
            user_home = Path.home()

            if service_path.exists():
                content = service_path.read_text()
                server_user = self._extract_service_user(content)

            if server_user:
                try:
                    user_home = Path(pwd.getpwnam(server_user).pw_dir)
                except KeyError:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-150",
                            f"Server user {server_user!r} not found in passwd — "
                            "using current HOME for pace-maker clone",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            clone_path = user_home / "claude-pace-maker"
            is_fresh = not (clone_path / ".git").exists()

            # Clone or pull
            if is_fresh:
                result = subprocess.run(
                    ["git", "clone", PACE_MAKER_REPO_URL, str(clone_path)],
                    capture_output=True,
                    text=True,
                    timeout=PACE_MAKER_GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-151",
                            f"pace-maker git clone failed: {result.stderr[:200]}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return False
            else:
                result = subprocess.run(
                    ["git", "-C", str(clone_path), "pull"],
                    capture_output=True,
                    text=True,
                    timeout=PACE_MAKER_GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-152",
                            f"pace-maker git pull failed: {result.stderr[:200]}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    return False

            # Run install.sh (idempotent)
            # When running via sudo the env dict is NOT inherited by the child
            # process, so we inject NONINTERACTIVE=1 directly into the command.
            if server_user:
                install_cmd = [
                    "sudo",
                    "-u",
                    server_user,
                    "env",
                    "NONINTERACTIVE=1",
                    "bash",
                    str(clone_path / "install.sh"),
                ]
                install_env = None
            else:
                install_cmd = ["bash", str(clone_path / "install.sh")]
                install_env = os.environ.copy()
                install_env["NONINTERACTIVE"] = "1"

            result = subprocess.run(
                install_cmd,
                capture_output=True,
                text=True,
                timeout=PACE_MAKER_INSTALL_TIMEOUT,
                env=install_env,
            )
            if result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-153",
                        f"pace-maker install.sh failed: {result.stderr[:200]}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

            # Fresh install only: set master switch OFF
            if is_fresh:
                off_cmd = ["pace-maker", "off"]
                if server_user:
                    off_cmd = ["sudo", "-u", server_user] + off_cmd
                result = subprocess.run(
                    off_cmd,
                    capture_output=True,
                    text=True,
                    timeout=PACE_MAKER_CMD_TIMEOUT,
                )
                if result.returncode != 0:
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-154",
                            f"pace-maker off failed: {result.stderr[:200]}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            # Record clone path in bootstrap config
            try:
                import json

                config_path = _cidx_data_dir / "config.json"
                config_dict: dict = {}
                if config_path.exists():
                    with open(config_path) as f:
                        config_dict = json.load(f)
                config_dict["pace_maker_clone_path"] = str(clone_path)
                _cidx_data_dir.mkdir(parents=True, exist_ok=True)
                with open(config_path, "w") as f:
                    json.dump(config_dict, f, indent=2)
                    f.write("\n")
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-155",
                        f"Failed to record pace_maker_clone_path in config: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

            logger.info(
                "pace-maker %s completed successfully",
                "installed" if is_fresh else "updated",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except subprocess.TimeoutExpired as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-156",
                    f"pace-maker operation timed out: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-157",
                    f"pace-maker install/update failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_codex_cli_installed(self) -> bool:
        """Story #845: idempotently install/update @openai/codex via npm.

        - If npm is not on PATH, logs WARNING and returns True (optional-feature
          semantics — CIDX must not fail when npm is absent).
        - Otherwise runs `npm install -g @openai/codex` via _run_codex_npm_install().
        - On install success, probes `codex --version` via _probe_codex_version()
          and logs result at INFO. Probe failures are non-fatal.
        - Never raises. Returns False only when install itself fails or times out.

        Returns:
            True if npm was absent (optional skip) or install succeeded.
            False if npm install returned nonzero, timed out, or failed to spawn
            (DEPLOY-GENERAL-144 logged by _run_codex_npm_install).
        """
        if shutil.which("npm") is None:
            logger.warning(
                "npm not available on PATH; skipping Codex CLI install — "
                "feature effectively disabled",
                extra={"correlation_id": get_correlation_id()},
            )
            return True
        if not self._run_codex_npm_install():
            return False
        self._probe_codex_version()
        return True

    def _check_node_installed(self) -> bool:
        """BUG #1318: Return True if Node.js is already installed and runnable.

        Checks NODEJS_INSTALL_DIR/bin/node first (our own provisioning
        target) by actually invoking it with --version, then falls back to
        shutil.which("node") to detect a system-installed Node.js elsewhere
        on PATH. Never raises.
        """
        node_bin = NODEJS_INSTALL_DIR / "bin" / "node"
        if node_bin.exists():
            try:
                result = subprocess.run(
                    [str(node_bin), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=NODEJS_VERSION_PROBE_TIMEOUT_SECONDS,
                )
                if result.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        return shutil.which("node") is not None

    def _download_nodejs_tarball(self, dest_path: Path) -> bool:
        """BUG #1318: Download the pinned Node.js LTS tarball via curl.

        Mirrors the Rust toolchain's curl usage (no shell=True). Handles
        nonzero returncode, TimeoutExpired, and OSError as WARNING +
        return False. Never raises.
        """
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--proto",
                    "=https",
                    "--tlsv1.2",
                    "-sSfL",
                    "-o",
                    str(dest_path),
                    NODEJS_DIST_URL,
                ],
                capture_output=True,
                text=True,
                timeout=NODEJS_INSTALL_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"Node.js tarball download failed: {exc}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        if result.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"Node.js tarball download failed (curl exit "
                    f"{result.returncode}): "
                    f"{result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        return True

    def _extract_nodejs_tarball(self, tar_path: Path, install_dir: Path) -> bool:
        """BUG #1318: Safely extract the Node.js tarball into install_dir.

        tarfile has no --strip-components, so the top-level
        node-v{NODEJS_VERSION}-linux-x64/ directory shipped by the official
        dist is extracted to a temp dir first, then its contents are moved
        up into install_dir. Path-traversal validation mirrors
        RipgrepInstaller._safe_extract_tar. Never raises.
        """
        expected_top_dir = f"node-v{NODEJS_VERSION}-linux-x64"
        try:
            with tempfile.TemporaryDirectory() as tmp_extract_dir:
                abs_dest = os.path.abspath(tmp_extract_dir)
                with tarfile.open(tar_path, "r:xz") as tar:
                    for member in tar.getmembers():
                        member_path = os.path.join(tmp_extract_dir, member.name)
                        abs_path = os.path.abspath(member_path)
                        if (
                            not abs_path.startswith(abs_dest + os.sep)
                            and abs_path != abs_dest
                        ):
                            raise ValueError(
                                f"Path traversal detected in tar: {member.name}"
                            )
                    tar.extractall(tmp_extract_dir, filter="data")

                extracted_root = Path(tmp_extract_dir) / expected_top_dir
                if not extracted_root.is_dir():
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-202",
                            f"Extracted Node.js tarball missing expected "
                            f"directory {expected_top_dir}",
                        ),
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return False

                for item in extracted_root.iterdir():
                    shutil.move(str(item), str(install_dir / item.name))
            return True
        except (OSError, tarfile.TarError, ValueError) as exc:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"Node.js tarball extraction failed: {exc}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _add_nodejs_bin_to_process_path(self) -> None:
        """BUG #1318: Prepend NODEJS_INSTALL_DIR/bin to THIS process's PATH.

        ensure_scip_python()/_ensure_codex_cli_installed() call
        shutil.which("npm")/subprocess.run(["npm", ...]) without an
        explicit env= kwarg, relying on the inherited process environment
        (unlike the Rust toolchain build, which passes an explicit env
        dict). Updating os.environ here — not just the systemd unit file —
        is what lets those SAME-execute()-run steps find npm immediately
        after ensure_nodejs() provisions it. Idempotent: never duplicates
        the segment. Never raises.
        """
        node_bin = str(NODEJS_INSTALL_DIR / "bin")
        current_path = os.environ.get("PATH", "")
        segments = current_path.split(":") if current_path else []
        if node_bin in segments:
            return
        os.environ["PATH"] = f"{node_bin}:{current_path}" if current_path else node_bin

    def _run_scip_python_npm_install(self) -> bool:
        """Run `npm install -g @sourcegraph/scip-python`; return True on success.

        Handles nonzero returncode, TimeoutExpired, and OSError as
        WARNING + return False. Never raises. Mirrors
        _run_codex_npm_install().
        """
        try:
            result = subprocess.run(
                ["npm", "install", "-g", SCIP_PYTHON_PACKAGE],
                capture_output=True,
                text=True,
                timeout=SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS,
                shell=False,
            )
            logger.debug(
                "npm install %s stdout: %s",
                SCIP_PYTHON_PACKAGE,
                result.stdout,
                extra={"correlation_id": get_correlation_id()},
            )
            if result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-201",
                        f"npm install {SCIP_PYTHON_PACKAGE} failed "
                        f"(exit {result.returncode}): {result.stderr}",
                    ),
                    extra={"correlation_id": get_correlation_id()},
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-201",
                    f"npm install {SCIP_PYTHON_PACKAGE} timed out after "
                    f"{SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS}s",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        except OSError as exc:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-201",
                    f"npm install {SCIP_PYTHON_PACKAGE} could not be spawned: {exc}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

    def _provision_nodejs_install_dir(self) -> bool:
        """BUG #1318: sudo mkdir -p + sudo chown NODEJS_INSTALL_DIR so the
        current (unprivileged) user can extract the tarball into it.
        Mirrors the mkdir/chown block in _ensure_rust_toolchain(). Returns
        False (with WARNING) on either subprocess failure; never raises.
        """
        mkdir_result = subprocess.run(
            ["sudo", "mkdir", "-p", str(NODEJS_INSTALL_DIR)],
            capture_output=True,
            text=True,
        )
        if mkdir_result.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"sudo mkdir -p {NODEJS_INSTALL_DIR} failed: "
                    f"{mkdir_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        uid_gid = f"{os.getuid()}:{os.getgid()}"
        chown_result = subprocess.run(
            ["sudo", "chown", "-R", uid_gid, str(NODEJS_INSTALL_DIR)],
            capture_output=True,
            text=True,
        )
        if chown_result.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"sudo chown -R {uid_gid} {NODEJS_INSTALL_DIR} failed: "
                    f"{chown_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        return True

    def ensure_nodejs(self) -> bool:
        """BUG #1318: Ensure a pinned Node.js LTS toolchain is provisioned
        (system-wide, at NODEJS_INSTALL_DIR — mirrors RUST_SYSTEM_DIR) so
        npm is available for scip-python / Codex CLI installation.

        Idempotent (skips when already installed). Non-fatal: any failure
        logs WARNING and returns False; never raises. On success (or when
        already installed) wires NODEJS_INSTALL_DIR/bin onto this
        process's PATH (so the SAME execute() run's npm-dependent steps
        find it) and the systemd unit PATH (for the restarted server).
        """
        if self._check_node_installed():
            logger.info(
                "Node.js already installed, skipping provisioning",
                extra={"correlation_id": get_correlation_id()},
            )
            self._add_nodejs_bin_to_process_path()
            self._ensure_systemd_node_path()
            return True

        if platform.machine() != "x86_64":
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    f"Node.js pinned tarball only available for x86_64, "
                    f"found {platform.machine()}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        if not self._provision_nodejs_install_dir():
            return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            tar_path = Path(tmp_dir) / "node.tar.xz"
            if not self._download_nodejs_tarball(tar_path):
                return False
            if not self._extract_nodejs_tarball(tar_path, NODEJS_INSTALL_DIR):
                return False

        if not self._check_node_installed():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-202",
                    "Node.js installation verification failed after extraction",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        logger.info(
            f"Node.js {NODEJS_VERSION} installed successfully at {NODEJS_INSTALL_DIR}",
            extra={"correlation_id": get_correlation_id()},
        )
        self._add_nodejs_bin_to_process_path()
        self._ensure_systemd_node_path()
        return True

    def ensure_scip_python(self) -> bool:
        """Idempotently install scip-python via npm for SCIP-based indexing.

        scip-python (npm package @sourcegraph/scip-python) is the SCIP
        indexer binary invoked by PythonIndexer for `cidx scip generate` /
        add_golden_repo_index(index_type="scip"). Without it, SCIP indexing
        fails with "[Errno 2] No such file or directory: 'scip-python'".

        Mirrors ensure_ripgrep() / _ensure_codex_cli_installed(): checks
        shutil.which("scip-python") first (skip if already present), then
        requires npm and delegates the install to
        _run_scip_python_npm_install(). Unlike the optional Codex CLI,
        scip-python is required for SCIP indexing, so a missing npm is
        reported as a failed provisioning attempt (WARNING + False) rather
        than silently "ok".

        Returns:
            True if already installed or the npm install succeeded.
            False if it could not be provisioned (npm absent, install
            failed, timed out, or could not be spawned).
        """
        if shutil.which("scip-python") is not None:
            logger.info(
                "scip-python already installed, skipping install",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if shutil.which("npm") is None:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-201",
                    "npm not available on PATH; cannot install scip-python — "
                    "SCIP indexing for Python projects will be unavailable",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        if not self._run_scip_python_npm_install():
            return False
        logger.info(
            "scip-python installed successfully via npm",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _ensure_claude_cli_installed(self) -> bool:
        """Idempotently install Claude CLI if not already present on PATH.

        Checks shutil.which("claude") first — if found, skips installation.
        Otherwise runs the official installer via curl + sh pipeline.

        Non-fatal: failures return False with a WARNING; deployment continues.

        Returns:
            True if claude was already installed or installation succeeded.
            False if the installer returned nonzero, timed out, or raised.
        """
        if shutil.which("claude") is not None:
            logger.info(
                "Claude CLI already installed, skipping install",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        logger.info(
            "Claude CLI not found on PATH, running installer",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            result = subprocess.run(
                ["sh", "-c", f"curl -fsSL {CLAUDE_INSTALL_URL} | sh"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_INSTALL_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-160",
                        f"Claude CLI installer failed (rc={result.returncode}): "
                        f"{result.stderr[:200]}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False
            logger.info(
                "Claude CLI installed successfully",
                extra={"correlation_id": get_correlation_id()},
            )
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-161",
                    f"Claude CLI installer timed out after {CLAUDE_INSTALL_TIMEOUT_SECONDS}s",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-162",
                    f"Claude CLI install failed with exception: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    @staticmethod
    def _validate_nfs_mount_point(mount_point: str) -> Path:
        """Validate and return the NFS mount point as an absolute Path.

        Raises:
            ValueError: If mount_point is empty or not an absolute path.
        """
        if not mount_point:
            raise ValueError("mount_point is empty")
        p = Path(mount_point)
        if not p.is_absolute():
            raise ValueError(f"mount_point must be absolute, got: {mount_point!r}")
        return p

    def _ensure_single_nfs_symlink(self, local_path: Path, nfs_target: Path) -> None:
        """Idempotently create one NFS symlink for a research data directory.

        If local_path is already a correct symlink, skips. If it is a regular
        directory, migrates contents to nfs_target (skipping collisions) then
        removes the dir. Finally creates the symlink.
        """
        if local_path.is_symlink():
            if local_path.readlink() == nfs_target:
                logger.debug(
                    f"NFS symlink already correct: {local_path} -> {nfs_target}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return
            local_path.unlink()

        nfs_target.mkdir(parents=True, exist_ok=True)

        if local_path.exists() and local_path.is_dir():
            for item in local_path.iterdir():
                if item.is_symlink():
                    item.unlink()
                    continue
                dest = nfs_target / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            try:
                shutil.rmtree(str(local_path))
            except OSError as exc:
                logger.warning(
                    f"Could not fully remove {local_path} after migration: {exc}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return

        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.symlink_to(nfs_target)
        logger.info(
            f"Created NFS research symlink: {local_path} -> {nfs_target}",
            extra={"correlation_id": get_correlation_id()},
        )

    def _consolidate_old_claude_projects(self, nfs_base: Path) -> None:
        """Migrate data from old .claude-projects/ NFS layout to .claude/projects/.

        Previous Step 14 stored projects at {nfs_base}/.claude-projects/.
        New layout stores everything under {nfs_base}/.claude/.
        This moves data from old location to new, preserving existing data.
        Files that already exist at the destination are skipped (no overwrite).
        The old directory is removed after migration; if items remain due to
        collisions the rmdir fails and a debug log is emitted for manual cleanup.
        """
        old_projects = nfs_base / ".claude-projects"
        new_projects = nfs_base / ".claude" / "projects"

        if not old_projects.exists() or not old_projects.is_dir():
            return

        new_projects.mkdir(parents=True, exist_ok=True)
        for item in old_projects.iterdir():
            dest = new_projects / item.name
            if not dest.exists():
                shutil.move(str(item), str(dest))

        try:
            old_projects.rmdir()
        except OSError as exc:
            logger.debug(
                f"Old .claude-projects/ not empty after migration (collision items remain); "
                f"leaving for manual cleanup: {exc}",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        logger.info(
            f"Consolidated old .claude-projects/ into {new_projects}",
            extra={"correlation_id": get_correlation_id()},
        )

    def _ensure_nfs_research_symlinks(self) -> bool:
        """Idempotently set up NFS symlinks for research session data in cluster mode.

        Only runs when storage_mode == 'postgres' AND ontap.mount_point is a
        valid absolute path.

        Creates two symlinks so any cluster node shares Claude state and research data:
          ~/.claude/              -> {mount_point}/.claude/
          ~/.cidx-server/research/ -> {mount_point}/.cidx-research/

        Non-fatal: any exception returns False with a WARNING.
        """
        try:
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config = ServerConfigManager(
                server_dir_path=str(_cidx_data_dir)
            ).load_config()

            if not config or getattr(config, "storage_mode", "") != "postgres":
                logger.debug(
                    "NFS research symlinks: not in cluster mode, skipping",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            ontap = getattr(config, "ontap", None)
            if not ontap:
                logger.debug(
                    "NFS research symlinks: no ontap config, skipping",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            try:
                nfs_base = self._validate_nfs_mount_point(
                    getattr(ontap, "mount_point", "")
                )
            except ValueError:
                logger.debug(
                    "NFS research symlinks: mount_point not set or invalid, skipping",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            home = Path.home()

            # Consolidate old .claude-projects/ layout if present
            self._consolidate_old_claude_projects(nfs_base)

            for local_path, nfs_target in [
                (home / ".claude", nfs_base / ".claude"),
                (home / ".cidx-server" / "research", nfs_base / ".cidx-research"),
            ]:
                self._ensure_single_nfs_symlink(local_path, nfs_target)

            return True

        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-163",
                    f"NFS research symlink setup failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    def _ensure_activated_repos_symlink_for_cow_daemon(self) -> bool:
        """Bug #1052: Idempotently set up activated-repos symlink for CoW-daemon cluster nodes.

        On CoW-daemon deployments, ~/.cidx-server/data/activated-repos must be a
        symlink to {cow_daemon.mount_point}/activated-repos so that
        CowDaemonBackend.create_clone_at_path() accepts destination paths as valid.

        Story #1034 set up golden-repos but never activated-repos. This step closes
        that gap so fresh cluster nodes provision correctly without manual SSH.

        Non-fatal: any exception returns False with a WARNING.
        Returns True in all handled cases (no-op, symlink created, real-dir warning).
        """
        try:
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config = ServerConfigManager(
                server_dir_path=str(_cidx_data_dir)
            ).load_config()

            clone_backend = getattr(config, "clone_backend", "local") or "local"
            if clone_backend != "cow-daemon":
                logger.debug(
                    "Bug #1052: clone_backend=%r, not cow-daemon — skipping activated-repos symlink setup",
                    clone_backend,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            cow_cfg = getattr(config, "cow_daemon", None)
            if cow_cfg is None or not getattr(cow_cfg, "mount_point", ""):
                logger.warning(
                    "Bug #1052: clone_backend=cow-daemon but cow_daemon config missing or "
                    "mount_point empty — skipping activated-repos symlink setup",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            target = Path(cow_cfg.mount_point) / "activated-repos"
            link_path = _cidx_data_dir / "data" / "activated-repos"

            if link_path.is_symlink():
                current_target = os.readlink(str(link_path))
                if current_target == str(target):
                    logger.debug(
                        "Bug #1052: activated-repos symlink already correct: %s -> %s",
                        link_path,
                        target,
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return True
                logger.warning(
                    "Bug #1052: activated-repos symlink points to %s but expected %s "
                    "— manual review needed",
                    current_target,
                    target,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            if link_path.exists():
                # Real directory — do NOT move user data from the auto-updater.
                logger.warning(
                    "Bug #1052: activated-repos exists as real directory with content; "
                    "manual migration required to enable CoW activation. "
                    "Run: sudo systemctl stop cidx-server && "
                    "mv %s %s.legacy.bug1052 && "
                    "mkdir -p %s && "
                    "ln -s %s %s && "
                    "sudo systemctl start cidx-server",
                    link_path,
                    link_path,
                    target,
                    target,
                    link_path,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            # link_path missing — create symlink
            target.mkdir(parents=True, exist_ok=True)
            link_path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(target), str(link_path))
            logger.info(
                "Bug #1052: created activated-repos symlink %s -> %s",
                link_path,
                target,
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-167",
                    f"Bug #1052: activated-repos symlink setup failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    @staticmethod
    def _resolve_daemon_storage_path() -> Optional[str]:
        """Bug #1320 Part B: resolve the CoW daemon's local storage root
        (base_path) using a fixed priority order. NEVER hardcodes or guesses
        an environment-specific path — returns None when no source resolves.

        Priority order:
          1. CIDX_COW_DAEMON_STORAGE_PATH env var (explicit operator override,
             set by the installer's --cow-daemon-storage-path flag or by the
             operator directly on the auto-updater's environment).
          2. Co-located CoW daemon config `base_path` field at
             COW_DAEMON_HOST_CONFIG_PATH — only present/readable on the
             daemon-HOST node (the daemon exposes no API reporting its own
             base_path, so this is the only runtime-derivable source besides
             the explicit override).

        Returns:
            The resolved absolute path string, or None if neither source
            is available/valid.
        """
        env_value = os.environ.get("CIDX_COW_DAEMON_STORAGE_PATH", "").strip()
        if env_value:
            return env_value

        try:
            if COW_DAEMON_HOST_CONFIG_PATH.is_file():
                with open(COW_DAEMON_HOST_CONFIG_PATH) as f:
                    daemon_config = json.load(f)
                base_path = daemon_config.get("base_path", "")
                if isinstance(base_path, str) and base_path.strip():
                    return base_path.strip()
        except (OSError, json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-203",
                    "Bug #1320: could not read co-located CoW daemon config "
                    f"at {COW_DAEMON_HOST_CONFIG_PATH} for daemon_storage_path "
                    f"auto-detect: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        return None

    def _ensure_daemon_storage_path(self) -> bool:
        """Bug #1320 Part B: idempotently populate cow_daemon.daemon_storage_path
        in config.json so CowDaemonBackend can translate CIDX paths (mount_point
        view) to the daemon's local filesystem paths (storage_path view).

        Part A (already shipped) made CowDaemonBackend._translate_to_daemon_path
        raise a clear ValueError instead of silently emitting an untranslatable
        NFS path when this field is empty/null. This step is what actually
        populates the value, using _resolve_daemon_storage_path() (env var,
        then co-located daemon config — never a hardcoded default).

        VALUE-AWARE idempotent (Bug #1183 style): only writes when the field
        is currently missing or empty. NEVER overwrites an existing non-empty
        value, even if a different value would be freshly resolved — an
        operator or a prior run may have set it deliberately.

        Non-fatal in all handled cases (no-op, write, or unresolved-leave-
        unset all return True); only an unexpected exception while reading or
        writing config.json returns False.
        """
        try:
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config = ServerConfigManager(
                server_dir_path=str(_cidx_data_dir)
            ).load_config()
            if config is None:
                logger.debug(
                    "Bug #1320: config.json absent — skipping daemon_storage_path setup",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            clone_backend = getattr(config, "clone_backend", "local") or "local"
            if clone_backend != "cow-daemon":
                logger.debug(
                    "Bug #1320: clone_backend=%r, not cow-daemon — skipping "
                    "daemon_storage_path setup",
                    clone_backend,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            cow_cfg = getattr(config, "cow_daemon", None)
            if cow_cfg is None:
                logger.warning(
                    "Bug #1320: clone_backend=cow-daemon but cow_daemon config "
                    "missing — skipping daemon_storage_path setup",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            existing_value = (
                getattr(cow_cfg, "daemon_storage_path", None) or ""
            ).strip()
            if existing_value:
                logger.debug(
                    "Bug #1320: cow_daemon.daemon_storage_path already set "
                    "(%s) — no-op",
                    existing_value,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            resolved_value = self._resolve_daemon_storage_path()
            if not resolved_value:
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-204",
                        "Bug #1320: cow_daemon.daemon_storage_path is unset and "
                        "could not be auto-resolved (no CIDX_COW_DAEMON_STORAGE_PATH "
                        "env var, no readable co-located CoW daemon config at "
                        f"{COW_DAEMON_HOST_CONFIG_PATH}) — leaving unset; "
                        "CowDaemonBackend will fail loud on the next "
                        "versioned-snapshot publish that needs path translation",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return True

            config_path = _cidx_data_dir / "config.json"
            with open(config_path) as f:
                config_dict = json.load(f)
            cow_daemon_dict = config_dict.get("cow_daemon") or {}
            cow_daemon_dict["daemon_storage_path"] = resolved_value
            config_dict["cow_daemon"] = cow_daemon_dict
            _cidx_data_dir.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w") as f:
                json.dump(config_dict, f, indent=2)
                f.write("\n")

            logger.info(
                "Bug #1320: set cow_daemon.daemon_storage_path=%s in config.json",
                resolved_value,
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-205",
                    f"Bug #1320: daemon_storage_path setup failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    @staticmethod
    def _resolve_golden_repos_symlink_target(cow_cfg: Any) -> Path:
        """Bug #1337: node-aware golden-repos symlink target.

        Mirrors _resolve_daemon_storage_path's own co-located-daemon-host
        detection (presence of the daemon's own config file at
        COW_DAEMON_HOST_CONFIG_PATH — only readable on that node): use the
        daemon-local form there (no bind-mount indirection needed); use the
        mount_point form on every other (NFS-client) node. Uses safe
        attribute access throughout — never raises on a malformed cow_cfg.
        """
        daemon_storage_path = (
            getattr(cow_cfg, "daemon_storage_path", None) or ""
        ).strip()
        mount_point = getattr(cow_cfg, "mount_point", None) or ""
        is_daemon_host = COW_DAEMON_HOST_CONFIG_PATH.is_file()
        if is_daemon_host and daemon_storage_path:
            return Path(daemon_storage_path) / "golden-repos"
        return Path(mount_point) / "golden-repos"

    @staticmethod
    def _remove_if_empty_dir(path: Path) -> bool:
        """Bug #1337: remove *path* iff it is an empty directory. Returns True
        if removed (safe to replace with a symlink), False if it has content
        or a race made it non-empty/non-removable (leave untouched, caller
        warns)."""
        try:
            if any(path.iterdir()):
                return False
            path.rmdir()
            return True
        except OSError:
            return False

    @staticmethod
    def _reconcile_existing_golden_repos_symlink(link_path: Path, target: Path) -> bool:
        """Bug #1337: an existing golden-repos symlink is already correct
        (no-op) or points elsewhere (WARNING, never silently rewritten).
        Returns False (non-fatal, logged) if the symlink cannot be read."""
        try:
            current_target = os.readlink(str(link_path))
        except OSError as e:
            logger.warning(
                "Bug #1337: failed to read golden-repos symlink %s: %s",
                link_path,
                e,
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        if current_target == str(target):
            logger.debug(
                "Bug #1337: golden-repos symlink already correct: %s -> %s",
                link_path,
                target,
                extra={"correlation_id": get_correlation_id()},
            )
            return True
        logger.warning(
            "Bug #1337: golden-repos symlink points to %s but expected %s "
            "— manual review needed",
            current_target,
            target,
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    @staticmethod
    def _warn_golden_repos_real_dir_with_content(link_path: Path, target: Path) -> None:
        """Bug #1337: golden-repos is a real directory WITH content — never
        move production data unattended; log the manual migration steps."""
        logger.warning(
            "Bug #1337: golden-repos exists as real directory with content; "
            "manual migration required to enable per-user CoW activation. "
            "Run: sudo systemctl stop cidx-server && "
            "mv %s %s.legacy.bug1337 && mkdir -p %s && ln -s %s %s && "
            "sudo systemctl start cidx-server",
            link_path,
            link_path,
            target,
            target,
            link_path,
            extra={"correlation_id": get_correlation_id()},
        )

    @staticmethod
    def _create_golden_repos_symlink(link_path: Path, target: Path) -> None:
        """Bug #1337: create *target* (if missing) and symlink link_path -> target."""
        target.mkdir(parents=True, exist_ok=True)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(link_path))
        logger.info(
            "Bug #1337: created golden-repos symlink %s -> %s",
            link_path,
            target,
            extra={"correlation_id": get_correlation_id()},
        )

    def _ensure_golden_repos_symlink_for_cow_daemon(self) -> bool:
        """Bug #1337: idempotently set up golden-repos as a symlink into the
        CoW storage tree on CoW-daemon cluster nodes, so per-user
        activation's CowDaemonBackend.create_clone_at_path() can translate
        the path (mirrors the Bug #1052 activated-repos twin).

        Non-fatal: any exception returns False with a WARNING. Returns True
        in all handled cases (no-op, symlink created/converted, real-dir-
        with-content warning, unexpected-symlink warning).
        """
        try:
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config = ServerConfigManager(
                server_dir_path=str(_cidx_data_dir)
            ).load_config()

            clone_backend = getattr(config, "clone_backend", "local") or "local"
            if clone_backend != "cow-daemon":
                logger.debug(
                    "Bug #1337: clone_backend=%r, not cow-daemon — skipping "
                    "golden-repos symlink setup",
                    clone_backend,
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            cow_cfg = getattr(config, "cow_daemon", None)
            if cow_cfg is None or not getattr(cow_cfg, "mount_point", ""):
                logger.warning(
                    "Bug #1337: clone_backend=cow-daemon but cow_daemon config "
                    "missing or mount_point empty — skipping golden-repos "
                    "symlink setup",
                    extra={"correlation_id": get_correlation_id()},
                )
                return True

            target = self._resolve_golden_repos_symlink_target(cow_cfg)
            link_path = _cidx_data_dir / "data" / "golden-repos"

            if link_path.is_symlink():
                return self._reconcile_existing_golden_repos_symlink(link_path, target)

            if link_path.exists() and not self._remove_if_empty_dir(link_path):
                self._warn_golden_repos_real_dir_with_content(link_path, target)
                return True

            self._create_golden_repos_symlink(link_path, target)
            return True

        except Exception as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-207",
                    f"Bug #1337: golden-repos symlink setup failed: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

    @staticmethod
    def _build_updated_service_content(content: str, local_bin: str) -> str:
        """Return updated systemd service content with local_bin prepended to PATH.

        Scans for an Environment="PATH=..." line:
          - Already has local_bin as an exact segment: returns content unchanged.
          - Has PATH line but missing local_bin: replaces that line in-place,
            preserving the original line's indentation and line ending.
          - No PATH line: appends a full default PATH line at end.

        Raises TypeError if either argument is not a str.
        Raises ValueError if local_bin is empty.
        """
        if not isinstance(content, str) or not isinstance(local_bin, str):
            raise TypeError("content and local_bin must be str")
        if not local_bin:
            raise ValueError("local_bin must not be empty")

        lines = content.splitlines(keepends=True)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('Environment="PATH='):
                continue
            path_value = stripped[len('Environment="PATH=') : -1]
            if local_bin in path_value.split(":"):
                return content
            # Preserve original leading whitespace and trailing line ending
            indent = line[: len(line) - len(line.lstrip())]
            ending = line[len(line.rstrip("\r\n")) :]
            lines[i] = f'{indent}Environment="PATH={local_bin}:{path_value}"{ending}'
            return "".join(lines)

        if not content.endswith("\n"):
            content += "\n"
        return (
            content + f'Environment="PATH={local_bin}:{SYSTEMD_DEFAULT_PATH_SUFFIX}"\n'
        )

    @staticmethod
    def _remove_path_segment(content: str, segment: str) -> str:
        """Remove a specific PATH segment from Environment="PATH=..." line.

        Returns content unchanged if the segment is not present.
        """
        lines = content.splitlines(keepends=True)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('Environment="PATH='):
                continue
            path_value = stripped[len('Environment="PATH=') : -1]
            segments = path_value.split(":")
            if segment not in segments:
                return content
            segments = [s for s in segments if s != segment]
            indent = line[: len(line) - len(line.lstrip())]
            ending = line[len(line.rstrip("\r\n")) :]
            lines[i] = f'{indent}Environment="PATH={":".join(segments)}"{ending}'
            return "".join(lines)
        return content

    @staticmethod
    def _line_parts(line: str) -> tuple[str, str]:
        """Return (indent, line_ending) for a line, preserving whitespace on both ends."""
        indent = line[: len(line) - len(line.lstrip())]
        ending = line[len(line.rstrip("\r\n")) :]
        return indent, ending

    @staticmethod
    def _ensure_systemd_env_var(content: str, key: str, value: str) -> str:
        """Ensure Environment="KEY=VALUE" exists in systemd unit content.

        If key already has correct value, returns unchanged.
        If key exists with wrong value, updates in place.
        If key not found, inserts after the last Environment= line.
        If no Environment= lines exist, appends to end of content.
        """
        target = f'Environment="{key}={value}"'
        lines = content.splitlines(keepends=True)

        last_env_idx = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f'Environment="{key}='):
                # Key exists — check value
                if stripped == target:
                    return content  # Already correct
                # Wrong value — update in place
                indent, ending = DeploymentExecutor._line_parts(line)
                lines[i] = f"{indent}{target}{ending}"
                return "".join(lines)
            if stripped.startswith("Environment="):
                last_env_idx = i

        # Key not found — insert after last Environment= line
        if last_env_idx >= 0:
            ref_line = lines[last_env_idx]
            indent, ending = DeploymentExecutor._line_parts(ref_line)
            lines.insert(last_env_idx + 1, f"{indent}{target}{ending}")
        else:
            # No Environment= lines at all — append to end, ensuring trailing newline
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append(f"{target}\n")

        return "".join(lines)

    def _write_service_file_via_sudo(self, service_file: Path, content: str) -> bool:
        """Write content to a systemd service file via sudo tee. Returns True on success."""
        try:
            result = subprocess.run(
                ["sudo", "tee", str(service_file)],
                input=content,
                capture_output=True,
                text=True,
            )
        except OSError as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-169",
                    f"sudo tee could not be invoked for {service_file}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        if result.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-169",
                    f"sudo tee failed writing {service_file}: "
                    f"{result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        return True

    def _reload_systemd_daemon(self) -> bool:
        """Run sudo systemctl daemon-reload. Returns True on success."""
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-170",
                    f"systemctl daemon-reload could not be invoked: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        if result.returncode != 0:
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-170",
                    f"systemctl daemon-reload failed: "
                    f"{result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False
        return True

    def _ensure_systemd_claude_path(self) -> bool:
        """Ensure the cidx-server systemd service unit has ~/.local/bin in PATH.

        Non-fatal: returns False with WARNING when the service file is missing
        or any subprocess call fails.
        """
        service_file = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
        if not service_file.exists():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-168",
                    f"Systemd service file not found: {service_file}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        local_bin = str(Path.home() / ".local" / "bin")
        original = service_file.read_text()
        updated = self._build_updated_service_content(original, local_bin)

        if updated == original:
            logger.debug(
                f"PATH already contains {local_bin} in {service_file}, skipping",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if not self._write_service_file_via_sudo(service_file, updated):
            return False

        if not self._reload_systemd_daemon():
            return False

        logger.info(
            f"Updated PATH in {service_file} to include {local_bin}",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _ensure_systemd_rust_path(self) -> bool:
        """Ensure the cidx-server systemd service unit has /opt/rust/bin in PATH.

        Also removes stale /root/.cargo/bin entries from prior deployments.
        Non-fatal: returns False with WARNING when the service file is missing
        or any subprocess call fails.
        """
        service_file = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
        if not service_file.exists():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-168",
                    f"Systemd service file not found: {service_file}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        rust_bin = str(RUST_SYSTEM_DIR / "bin")
        original = service_file.read_text()

        # Step 1: Remove stale /root/.cargo/bin if present
        updated = self._remove_path_segment(
            original, str(Path("/root") / ".cargo" / "bin")
        )

        # Step 2: Prepend /opt/rust/bin if not already present
        updated = self._build_updated_service_content(updated, rust_bin)

        # Step 3: Ensure RUSTUP_HOME so rustup proxy finds the toolchain
        updated = self._ensure_systemd_env_var(
            updated, "RUSTUP_HOME", str(RUST_SYSTEM_DIR)
        )

        # Step 4: Ensure CARGO_HOME for cargo binaries
        updated = self._ensure_systemd_env_var(
            updated, "CARGO_HOME", str(RUST_SYSTEM_DIR)
        )

        if updated == original:
            logger.debug(
                f"PATH already contains {rust_bin} in {service_file}, skipping",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if not self._write_service_file_via_sudo(service_file, updated):
            return False

        if not self._reload_systemd_daemon():
            return False

        logger.info(
            f"Updated PATH in {service_file} to include {rust_bin}",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _ensure_systemd_node_path(self) -> bool:
        """BUG #1318: Ensure the cidx-server systemd service unit has
        NODEJS_INSTALL_DIR/bin (/opt/node/bin) in PATH.

        Mirrors _ensure_systemd_rust_path() exactly. Non-fatal: returns
        False with WARNING when the service file is missing or any
        subprocess call fails.
        """
        service_file = SYSTEMD_UNIT_DIR / f"{self.service_name}.service"
        if not service_file.exists():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-168",
                    f"Systemd service file not found: {service_file}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        node_bin = str(NODEJS_INSTALL_DIR / "bin")
        original = service_file.read_text()
        updated = self._build_updated_service_content(original, node_bin)

        if updated == original:
            logger.debug(
                f"PATH already contains {node_bin} in {service_file}, skipping",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        if not self._write_service_file_via_sudo(service_file, updated):
            return False

        if not self._reload_systemd_daemon():
            return False

        logger.info(
            f"Updated PATH in {service_file} to include {node_bin}",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    # ------------------------------------------------------------------
    # Story #1024: Rust toolchain + xray-cli build helpers
    # ------------------------------------------------------------------

    def _check_rustc_installed(self, env: dict) -> bool:
        """Return True if rustc is available on PATH (including ~/.cargo/bin)."""
        try:
            result = subprocess.run(
                ["rustc", "--version"],
                capture_output=True,
                text=True,
                timeout=RUSTC_VERSION_TIMEOUT_SECONDS,
                env=env,
            )
            if result.returncode == 0:
                logger.info(
                    "Rust toolchain already installed: %s",
                    result.stdout.strip(),
                    extra={"correlation_id": get_correlation_id()},
                )
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return False

    def _install_rust_toolchain(self, env: dict) -> bool:
        """Download and run the rustup installer (curl + sh, no shell=True).

        Returns True on success, False on any failure.
        Pin stable is handled separately by the caller (_ensure_rust_toolchain).
        """
        logger.info(
            "rustc not found — installing Rust toolchain via rustup",
            extra={"correlation_id": get_correlation_id()},
        )
        # M5: Use two separate subprocess calls instead of shell=True pipeline.
        # Step 1: Download the installer script via curl (bytes, no text=True).
        try:
            curl_result = subprocess.run(
                RUSTUP_CURL_ARGS,
                capture_output=True,
                timeout=RUSTUP_INSTALL_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"Rust toolchain install failed (curl): {exc}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        if curl_result.returncode != 0:
            stderr_text = (
                curl_result.stderr.decode()
                if isinstance(curl_result.stderr, bytes)
                else curl_result.stderr
            )
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"Rust toolchain install failed (curl): "
                    f"{stderr_text[:MAX_ERROR_SNIPPET_LENGTH]}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        # Step 2: Pipe the downloaded script into sh (no shell=True).
        try:
            install_result = subprocess.run(
                RUSTUP_SH_ARGS,
                input=curl_result.stdout,
                capture_output=True,
                timeout=RUSTUP_INSTALL_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"Rust toolchain install failed (sh): {exc}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        if install_result.returncode != 0:
            stderr_text = (
                install_result.stderr.decode()
                if isinstance(install_result.stderr, bytes)
                else install_result.stderr
            )
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"Rust toolchain install failed (sh): "
                    f"{stderr_text[:MAX_ERROR_SNIPPET_LENGTH]}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False

        logger.info(
            "Rust toolchain installed successfully",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _verify_c_compiler(self) -> bool:
        """Return True if gcc, cc, or clang is available (needed by tree-sitter crate).

        Bug #1255/#1296: the deploy itself stays non-fatal on a missing C
        compiler (xray is a non-core/optional feature and pip install +
        restart must still complete), but this is logged at ERROR, not
        WARNING: there is NO Python evaluation fallback for xray query-time
        execution. xray_search/xray_explore call the native xray-cli binary
        unconditionally; without it they return BinaryNotFound for every
        file. A missing C compiler blocks the xray-cli build, so this is a
        real capability loss that must stay loudly visible to operators.
        """
        for cc in ["gcc", "cc", "clang"]:
            if shutil.which(cc) is not None:
                return True
        logger.error(
            format_error_log(
                "DEPLOY-GENERAL-172",
                "xray native search UNAVAILABLE: no C compiler found "
                "(gcc/cc/clang) -- xray-cli native binary cannot be built; "
                "xray_search/xray_explore will return BinaryNotFound until "
                "a C compiler is installed and the binary is built. "
                "(Evaluator pre-flight validation still works; query-time "
                "evaluation does not.)",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    def _build_xray_cli(self, rust_dir: Path, env: dict) -> bool:
        """Run cargo build --release -p xray-cli inside rust_dir.

        Returns True on success, False on nonzero exit or timeout.

        Bug #1255/#1296: the caller (_ensure_rust_toolchain) still treats a
        False return as non-fatal to the overall deploy (pip install +
        restart must still complete), but the failure is logged at ERROR,
        not WARNING: there is NO Python evaluation fallback for xray
        query-time execution. xray_search/xray_explore call the native
        xray-cli binary unconditionally; without it they return
        BinaryNotFound for every file, so a failed build is a real
        capability loss that must stay loudly visible to operators.
        """
        try:
            build_result = subprocess.run(
                ["cargo", "build", "--release", "-p", "xray-cli"],
                cwd=str(rust_dir),
                capture_output=True,
                text=True,
                timeout=CARGO_BUILD_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"xray native search UNAVAILABLE: xray-cli native binary "
                    f"failed to build (cargo build error: {exc}); "
                    f"xray_search/xray_explore will return BinaryNotFound "
                    f"until the binary is built. (Evaluator pre-flight "
                    f"validation still works; query-time evaluation does "
                    f"not.)",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        if build_result.returncode != 0:
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    f"xray native search UNAVAILABLE: xray-cli native binary "
                    f"failed to build (cargo build exited "
                    f"{build_result.returncode}: "
                    f"{build_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}); "
                    f"xray_search/xray_explore will return BinaryNotFound "
                    f"until the binary is built. (Evaluator pre-flight "
                    f"validation still works; query-time evaluation does "
                    f"not.)",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return False
        logger.info(
            "xray-cli binary built successfully",
            extra={"correlation_id": get_correlation_id()},
        )
        return True

    def _is_rust_toolchain_usable(self, env: dict) -> bool:
        """Bug #1255: return True if BOTH rustc and cargo are present and
        runnable directly from RUST_SYSTEM_DIR/bin, using the resolved
        absolute binary paths (not a PATH lookup).

        Immutable host images often ship a pre-provisioned RUST_SYSTEM_DIR
        on a read-only root filesystem: the toolchain files are present and
        world-executable, but `chown` can never succeed there.  Running a
        binary only requires execute permission, not ownership, so this
        probe is sufficient to recognize "already usable" without touching
        ownership at all.
        """
        rust_bin = RUST_SYSTEM_DIR / "bin"
        for binary_name in ("rustc", "cargo"):
            binary_path = rust_bin / binary_name
            try:
                result = subprocess.run(
                    [str(binary_path), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=RUSTC_VERSION_TIMEOUT_SECONDS,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if result.returncode != 0:
                return False
        return True

    def _resolve_rust_build_env(self, env: dict) -> dict:
        """Bug #1255: redirect CARGO_HOME to a writable fallback (~/.cargo)
        for the xray-cli build step when RUST_SYSTEM_DIR exists but is not
        writable (immutable host, read-only root filesystem).

        RUSTUP_HOME is left untouched -- it must keep pointing at
        RUST_SYSTEM_DIR so rustup's proxy binaries resolve the toolchain
        that is actually installed there.  Only CARGO_HOME needs to be
        writable, since `cargo build` may need to write to its registry
        cache (new dependency downloads, index updates); build artifacts
        themselves go to the project-local rust_dir/target, which is
        writable because pip_install()/git_pull() already proved the repo
        checkout itself is writable earlier in execute().

        Returns a new dict; never mutates the input env.
        """
        build_env = dict(env)
        if RUST_SYSTEM_DIR.exists() and not os.access(RUST_SYSTEM_DIR, os.W_OK):
            writable_cargo_home = str(Path.home() / ".cargo")
            build_env["CARGO_HOME"] = writable_cargo_home
            logger.info(
                "%s is not writable; using %s as CARGO_HOME for the xray-cli build",
                RUST_SYSTEM_DIR,
                writable_cargo_home,
                extra={"correlation_id": get_correlation_id()},
            )
        return build_env

    def _ensure_rust_toolchain(self) -> bool:
        """Story #1024 / Bug #1255: Ensure Rust toolchain is installed and
        xray-cli binary is built.

        Installs to RUST_SYSTEM_DIR (/opt/rust) so the toolchain is accessible
        to the cidx-server service user (not just root).

        Bug #1255: on immutable hosts RUST_SYSTEM_DIR may already be
        provisioned (rustc/cargo present, world-executable) on a read-only
        root filesystem.  Provisioning (mkdir/chown/install) is skipped
        entirely when the toolchain already proves usable; if provisioning
        IS attempted and mkdir/chown fails, a usability recheck makes the
        failure non-fatal as long as the toolchain works anyway.

        Idempotent: skips install when rustc is already on PATH.
        Returns True on success, when rust/ dir is absent (non-fatal skip),
        or when the xray-cli build/C-compiler step fails. That failure is
        non-fatal to the OVERALL deploy (pip install + restart must still
        complete -- xray is a non-core/optional feature), but it is NOT a
        graceful degrade to an alternate execution path: there is no Python
        evaluation fallback for xray query-time execution, so
        xray_search/xray_explore become genuinely unavailable
        (BinaryNotFound) until the binary builds successfully. That
        capability loss is logged at ERROR (see _verify_c_compiler /
        _build_xray_cli) precisely because it is real, even though it does
        not block the deploy.
        Returns False ONLY when the toolchain is genuinely missing and
        cannot be installed (FATAL) -- the host truly lacks Rust.
        """
        rust_bin = RUST_SYSTEM_DIR / "bin"
        env = os.environ.copy()
        env["RUSTUP_HOME"] = str(RUST_SYSTEM_DIR)
        env["CARGO_HOME"] = str(RUST_SYSTEM_DIR)
        current_path = env.get("PATH", "")
        if current_path:
            env["PATH"] = f"{rust_bin}:{current_path}"
        else:
            env["PATH"] = str(rust_bin)

        if self._is_rust_toolchain_usable(env):
            logger.info(
                "Rust toolchain already present and usable at %s; "
                "skipping provisioning",
                RUST_SYSTEM_DIR,
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            mkdir_result = subprocess.run(
                ["sudo", "mkdir", "-p", str(RUST_SYSTEM_DIR)],
                capture_output=True,
                text=True,
            )
            if mkdir_result.returncode != 0:
                if self._is_rust_toolchain_usable(env):
                    logger.warning(
                        format_error_log(
                            "DEPLOY-GENERAL-172",
                            f"sudo mkdir -p {RUST_SYSTEM_DIR} failed but the "
                            f"toolchain is already usable -- continuing "
                            f"without provisioning: "
                            f"{mkdir_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                        ),
                        extra={"correlation_id": get_correlation_id()},
                    )
                else:
                    logger.error(
                        format_error_log(
                            "DEPLOY-GENERAL-172",
                            f"sudo mkdir -p {RUST_SYSTEM_DIR} failed: "
                            f"{mkdir_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                        ),
                        extra={"correlation_id": get_correlation_id()},
                    )
                    return False
            else:
                uid_gid = f"{os.getuid()}:{os.getgid()}"
                chown_result = subprocess.run(
                    ["sudo", "chown", "-R", uid_gid, str(RUST_SYSTEM_DIR)],
                    capture_output=True,
                    text=True,
                )
                if chown_result.returncode != 0:
                    if self._is_rust_toolchain_usable(env):
                        logger.warning(
                            format_error_log(
                                "DEPLOY-GENERAL-172",
                                f"sudo chown -R {uid_gid} {RUST_SYSTEM_DIR} "
                                f"failed but the toolchain is already usable "
                                f"-- continuing without ownership change: "
                                f"{chown_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                            ),
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        logger.error(
                            format_error_log(
                                "DEPLOY-GENERAL-172",
                                f"sudo chown -R {uid_gid} {RUST_SYSTEM_DIR} "
                                f"failed: "
                                f"{chown_result.stderr[:MAX_ERROR_SNIPPET_LENGTH]}",
                            ),
                            extra={"correlation_id": get_correlation_id()},
                        )
                        return False

        self._ensure_systemd_rust_path()

        if not self._check_rustc_installed(env):
            if not self._install_rust_toolchain(env):
                return False

        repo_root = Path(__file__).resolve().parents[4]
        rust_dir = repo_root / "rust"
        if not rust_dir.exists():
            logger.warning(
                format_error_log(
                    "DEPLOY-GENERAL-172",
                    "rust/ directory not found — xray native backend unavailable",
                ),
                extra={"correlation_id": get_correlation_id()},
            )
            return True  # Non-fatal: older code versions may not have rust/

        if not self._verify_c_compiler():
            # Bug #1255/#1296: non-fatal to the overall deploy, but NOT a
            # graceful degrade -- there is no Python fallback, so xray
            # native search is genuinely unavailable (logged at ERROR above).
            return True

        build_env = self._resolve_rust_build_env(env)
        if not self._build_xray_cli(rust_dir, build_env):
            # Bug #1255/#1296: non-fatal to the overall deploy, but NOT a
            # graceful degrade -- there is no Python fallback, so xray
            # native search is genuinely unavailable (logged at ERROR above).
            return True
        return True


def read_execstart_flags(service_name: str = "cidx-server") -> dict:
    """Read host/port/workers from the live systemd ExecStart line (Bug #1232).

    Reuses DeploymentExecutor._is_cidx_execstart (detection) and
    DeploymentExecutor._read_flag (bounded-token extraction) so there is
    exactly ONE ExecStart parser in the codebase.

    Returns a dict containing any subset of 'host' (str), 'port' (int),
    'workers' (int) that were found.  Returns an empty dict when:
      - the service file does not exist,
      - it cannot be read (OSError),
      - it contains no cidx ExecStart line.

    Values for 'port' and 'workers' are coerced to int; entries with
    non-integer values are omitted rather than raising.
    """
    service_path = SYSTEMD_UNIT_DIR / f"{service_name}.service"
    if not service_path.exists():
        return {}
    try:
        lines = service_path.read_text().split("\n")
    except OSError:
        return {}

    result: dict = {}
    for line in lines:
        if not DeploymentExecutor._is_cidx_execstart(line):
            continue
        host = DeploymentExecutor._read_flag(line, "--host")
        if host is not None:
            result["host"] = host
        port_str = DeploymentExecutor._read_flag(line, "--port")
        if port_str is not None:
            try:
                result["port"] = int(port_str)
            except ValueError:
                pass
        workers_str = DeploymentExecutor._read_flag(line, "--workers")
        if workers_str is not None:
            try:
                result["workers"] = int(workers_str)
            except ValueError:
                pass
        break  # only the first cidx ExecStart line matters
    return result
