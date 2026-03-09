"""
Git Credential Helper for PAT-based push operations.

Story #387: PAT-Authenticated Git Push with User Attribution & Security Hardening
"""

import logging
import re
import stat
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GitCredentialHelper:
    """Manages GIT_ASKPASS scripts and URL conversion for PAT-based git push."""

    def __init__(self, tmp_dir: Optional[Path] = None):
        self.tmp_dir = tmp_dir or Path.home() / ".tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def create_askpass_script(self, token: str) -> Path:
        """Create a temporary GIT_ASKPASS script that echoes the token.

        The script is created with 0700 permissions (owner-only read/write/execute).
        Caller MUST call cleanup_askpass_script() in a finally block.

        Args:
            token: The PAT to echo

        Returns:
            Path to the created script

        Raises:
            OSError: If unable to create the script
        """
        script_name = f"git_askpass_{uuid.uuid4().hex[:12]}.sh"
        script_path = self.tmp_dir / script_name
        # Escape single quotes for safe shell embedding: ' -> '\''
        escaped = token.replace("'", "'\\''")
        script_path.write_text(
            f"#!/bin/sh\n"
            f"printf '%s\\n' '{escaped}'\n"
        )
        script_path.chmod(stat.S_IRWXU)  # 0700 - owner only
        return script_path

    def cleanup_askpass_script(self, script_path: Path) -> None:
        """Remove a temporary GIT_ASKPASS script.

        Safe to call even if file doesn't exist.
        """
        try:
            if script_path.exists():
                script_path.unlink()
        except OSError as e:
            logger.warning(f"Failed to cleanup askpass script {script_path}: {e}")

    @staticmethod
    def convert_ssh_to_https(remote_url: str) -> str:
        """Convert SSH remote URL to HTTPS format for PAT-based auth.

        Converts: git@github.com:owner/repo.git -> https://github.com/owner/repo.git
        Passes through URLs that are already HTTPS or other formats.

        Args:
            remote_url: Git remote URL (SSH or HTTPS)

        Returns:
            HTTPS URL suitable for PAT authentication
        """
        url = remote_url.strip()

        # Pattern: git@host:path (standard SSH format)
        match = re.match(r"^git@([^:]+):(.+)$", url)
        if match:
            host = match.group(1)
            path = match.group(2)
            return f"https://{host}/{path}"

        # Pattern: ssh://git@host/path or ssh://git@host:port/path
        match = re.match(r"^ssh://git@([^:/]+)(?::\d+)?/(.+)$", url)
        if match:
            host = match.group(1)
            path = match.group(2)
            return f"https://{host}/{path}"

        # Already HTTPS or unrecognized - return as-is
        return url

    @staticmethod
    def extract_host_from_remote_url(remote_url: str) -> Optional[str]:
        """Extract the hostname from a git remote URL.

        Handles both SSH and HTTPS formats:
        - git@github.com:owner/repo.git -> github.com
        - https://github.com/owner/repo.git -> github.com
        - ssh://git@github.com/owner/repo.git -> github.com

        Returns:
            The hostname or None if unable to parse
        """
        if not remote_url:
            return None

        url = remote_url.strip()

        # SSH: git@host:path
        match = re.match(r"^git@([^:]+):", url)
        if match:
            return match.group(1)

        # HTTPS/HTTP: https://host/path or http://host/path
        match = re.match(r"^https?://([^/]+)", url)
        if match:
            return match.group(1)

        # SSH: ssh://git@host(:port)?/path
        match = re.match(r"^ssh://git@([^:/]+)", url)
        if match:
            return match.group(1)

        return None
