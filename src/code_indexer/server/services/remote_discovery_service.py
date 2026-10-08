"""
Remote Discovery Service.

Discovers remote hostnames from activated repositories.
"""

import json
from pathlib import Path
from typing import Optional, Set

from code_indexer.utils.git_remote_url import credential_scope_host


class RemoteDiscoveryService:
    """
    Service for discovering remote hostnames from activated repositories.

    Extracts hostnames from git remote URLs to identify which hosts
    need SSH key authentication.
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize the remote discovery service.

        Args:
            config_path: Path to CIDX server config. Defaults to
                         ~/.code-indexer-server/config.json
        """
        if config_path is None:
            config_path = Path.home() / ".code-indexer-server" / "config.json"
        self.config_path = config_path

    def extract_hostname(self, remote_url: str) -> Optional[str]:
        """
        Extract hostname from a git remote URL.

        The host SSH keys are tried against, so it follows the credential
        scoping rule (``credential_scope_host``): an SSH form yields a host
        only for login user ``git``.

        Args:
            remote_url: Git remote URL

        Returns:
            Hostname or None if the URL scopes no credential
        """
        return credential_scope_host(remote_url)

    def discover_remote_hostnames(self) -> Set[str]:
        """
        Discover unique hostnames from activated repositories.

        Returns:
            Set of unique hostnames that require SSH authentication
        """
        if not self.config_path.exists():
            return set()

        try:
            content = self.config_path.read_text()
            config = json.loads(content)
        except (json.JSONDecodeError, IOError):
            return set()

        activated_repos = config.get("activated_repositories", [])
        hostnames: Set[str] = set()

        for repo in activated_repos:
            remote_url = repo.get("remote_url", "")
            if not remote_url:
                continue

            hostname = self.extract_hostname(remote_url)
            if hostname:
                hostnames.add(hostname)

        return hostnames
