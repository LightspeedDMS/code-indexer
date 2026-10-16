"""
Git Credential Helper for PAT-based push operations.

Story #387: PAT-Authenticated Git Push with User Attribution & Security Hardening
"""

import logging
from typing import Optional

from code_indexer.utils.git_remote_url import (
    credential_scope_host,
    parse_git_remote_url,
)

logger = logging.getLogger(__name__)


class GitCredentialHelper:
    """URL conversion for PAT-based git push."""

    @staticmethod
    def convert_ssh_to_https(remote_url: str) -> str:
        """Convert SSH remote URL to HTTPS format for PAT-based auth.

        Converts an SSH remote that scopes a credential (``git@host:path`` or
        ``ssh://git@host[:port]/path``, see ``credential_scope_host``), e.g.
        git@github.com:owner/repo.git -> https://github.com/owner/repo.git.
        Returns every other value (already HTTPS, local paths) unchanged.

        Args:
            remote_url: Git remote URL (SSH or HTTPS)

        Returns:
            HTTPS URL suitable for PAT authentication
        """
        parsed = parse_git_remote_url(remote_url)
        if (
            parsed is not None
            and parsed.scheme == "ssh"
            and parsed.path
            and credential_scope_host(remote_url) is not None
        ):
            return parsed.to_https()
        return remote_url.strip()

    @staticmethod
    def extract_host_from_remote_url(remote_url: str) -> Optional[str]:
<<<<<<< HEAD
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
            # The authority may carry a userinfo component (user:secret@host). Return
            # ONLY the host: the host is the credential-lookup key, so anything else
            # here both fails to match a stored credential and travels into logs and
            # error messages. rsplit keeps the last '@' so a userinfo containing '@'
            # still resolves to the correct host.
            host = match.group(1).rsplit("@", 1)[-1]
            return host or None

        # SSH: ssh://git@host(:port)?/path
        match = re.match(r"^ssh://git@([^:/]+)", url)
        if match:
            return match.group(1)

        return None
=======
        """The host stored credentials are scoped by
        (``credential_scope_host``); None when the URL scopes no
        credential."""
        return credential_scope_host(remote_url)
>>>>>>> upstream/master
