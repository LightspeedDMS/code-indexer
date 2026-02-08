"""DeploymentExecutor - deployment command execution for auto-update service."""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.utils.ripgrep_installer import RipgrepInstaller
from pathlib import Path
from typing import Optional
import subprocess
import logging
import time
import sys
import pwd

import requests
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Issue #154: Pending redeploy marker for self-healing Python environment
PENDING_REDEPLOY_MARKER = Path("/tmp/cidx-pending-redeploy")
AUTO_UPDATE_SERVICE_NAME = "cidx-auto-update"


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

    def _enter_maintenance_mode(self) -> bool:
        """Enter maintenance mode via server API.

        Returns:
            True if successful, False on error (e.g., connection refused)
        """
        try:
            url = f"{self.server_url}/api/admin/maintenance/enter"
            response = requests.post(url, timeout=10)

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
            url = f"{self.server_url}/api/admin/maintenance/drain-timeout"
            response = requests.get(url, timeout=10)

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

        Returns:
            True if drained, False if timeout
        """
        # Get dynamic timeout from server
        drain_timeout = self._get_drain_timeout()

        start_time = time.time()

        while time.time() - start_time < drain_timeout:
            try:
                url = f"{self.server_url}/api/admin/maintenance/drain-status"
                response = requests.get(url, timeout=10)

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
                logger.warning(
                    format_error_log(
                        "DEPLOY-GENERAL-004",
                        "Could not connect to server for drain status",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
            except Exception as e:
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
            url = f"{self.server_url}/api/admin/maintenance/exit"
            response = requests.post(url, timeout=10)

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
            url = f"{self.server_url}/api/admin/maintenance/drain-status"
            response = requests.get(url, timeout=10)

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

    def git_submodule_update(self) -> bool:
        """Initialize and update git submodules.

        Required for custom hnswlib build from third_party/hnswlib submodule.
        The custom build includes check_integrity() method for HNSW index validation.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.error(
                    format_error_log(
                        "DEPLOY-GENERAL-040",
                        f"Git submodule update failed: {result.stderr}",
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
                ["cat", str(service_path)],
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
                ["cat", str(service_path)],
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

    def _write_service_file_and_reload(
        self, service_path: Path, content: str
    ) -> bool:
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
            if not self._write_service_file_and_reload(auto_update_service, new_content):
                return False

            # Create pending-redeploy marker
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

    def pip_install(self) -> bool:
        """Execute pip install to update dependencies.

        Issue #154: Uses _get_server_python() to install into the correct environment
        (e.g., pipx venv, not system Python).

        Returns:
            True if successful, False otherwise
        """
        try:
            python_path = self._get_server_python()
            result = subprocess.run(
                [
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

    def execute(self) -> bool:
        """Execute complete deployment: git pull + pip install.

        Returns:
            True if all steps successful, False otherwise
        """
        logger.info(
            "Starting deployment execution",
            extra={"correlation_id": get_correlation_id()},
        )

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

        # Step 1.5: Git submodule update (for custom hnswlib build)
        if not self.git_submodule_update():
            logger.error(
                format_error_log(
                    "DEPLOY-GENERAL-041",
                    "Deployment failed at git submodule update step",
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

        logger.info(
            "Deployment execution completed successfully",
            extra={"correlation_id": get_correlation_id()},
        )
        return True
