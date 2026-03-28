"""
Hardware profile capture for the CIDX performance report.

Story #335: Performance Report with Hardware Profile
AC1: SSH-based hardware profile capture using subprocess (no paramiko dependency).

Captures: CPU info, RAM, disk type, OS release, Python version.
Returns None gracefully when SSH is not configured or connection fails.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Commands to run on the remote host for hardware profiling
_HARDWARE_COMMANDS: dict[str, str] = {
    "cpu": "lscpu | grep -E 'Model name|CPU\\(s\\)|Thread' | head -5",
    "cpu_cores": "nproc",
    "ram": "free -h | head -2",
    "disk": "lsblk -d -o NAME,TYPE,SIZE | grep disk | head -5",
    "os": "grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'",
    "python_version": "python3 --version",
}

# SSH connection timeout in seconds
_SSH_TIMEOUT_SECONDS = 10


def capture_hardware_profile(
    ssh_host: Optional[str],
    ssh_user: Optional[str],
    ssh_password: Optional[str],
) -> Optional[dict[str, str]]:
    """
    Capture hardware profile from a remote host via SSH using subprocess.

    No paramiko dependency - uses the system ssh binary with sshpass for
    password authentication. Returns None if SSH is not configured or fails.

    Args:
        ssh_host: Remote hostname or IP. None disables capture.
        ssh_user: SSH username. None disables capture.
        ssh_password: SSH password. None disables capture.

    Returns:
        Dict with hardware info keys, or None if capture is unavailable.
    """
    if not ssh_host or not ssh_user:
        return None

    results: dict[str, str] = {}
    for key, command in _HARDWARE_COMMANDS.items():
        output = _run_ssh_command(
            host=ssh_host,
            user=ssh_user,
            password=ssh_password,
            command=command,
        )
        if output is None:
            # SSH connection failed entirely - abort early
            return None
        results[key] = output.strip()

    return results if results else None


def _run_ssh_command(
    host: str,
    user: str,
    password: Optional[str],
    command: str,
) -> Optional[str]:
    """
    Run a single command on a remote host via SSH.

    Uses sshpass if password is provided, otherwise falls back to key-based auth.
    Returns None on any connection or execution failure.

    Args:
        host: Remote hostname or IP address.
        user: SSH username.
        password: SSH password (uses sshpass). None = key-based auth.
        command: Shell command to execute on remote host.

    Returns:
        Command stdout as string, or None on failure.
    """
    # Build SSH options for non-interactive use
    ssh_opts = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"ConnectTimeout={_SSH_TIMEOUT_SECONDS}",
        "-o",
        "BatchMode=yes" if not password else "BatchMode=no",
        "-o",
        "PasswordAuthentication=yes" if password else "PasswordAuthentication=no",
        "-o",
        "PubkeyAuthentication=no" if password else "PubkeyAuthentication=yes",
    ]

    if password:
        # Use sshpass for password-based SSH
        cmd = (
            ["sshpass", "-p", password, "ssh"] + ssh_opts + [f"{user}@{host}", command]
        )
    else:
        cmd = ["ssh"] + ssh_opts + [f"{user}@{host}", command]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.debug(
                "SSH command returned non-zero for %s@%s (rc=%d)",
                user,
                host,
                result.returncode,
            )
            return None
        return result.stdout
    except subprocess.TimeoutExpired as e:
        logger.debug("SSH command timed out for %s@%s: %s", user, host, e)
        return None
    except FileNotFoundError as e:
        logger.debug("SSH binary not found (%s): %s", cmd[0], e)
        return None
    except OSError as e:
        logger.debug("SSH command OS error for %s@%s: %s", user, host, e)
        return None
