"""
Forge client implementations for identity validation and PR/MR creation.

Story #386: Git Credential Management with Identity Discovery
Story #390: Pull/Merge Request Creation via MCP

Provides ForgeClient interface with GitHub and GitLab implementations
to validate personal access tokens, discover user identity, and create
pull/merge requests.
"""

import logging
import urllib.parse
from typing import Dict, Any, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


def detect_forge_type(remote_url: str) -> Optional[str]:
    """Auto-detect forge type from remote URL hostname.

    Returns 'github', 'gitlab', or None if unknown.
    """
    from code_indexer.server.services.git_credential_helper import GitCredentialHelper
    host = GitCredentialHelper.extract_host_from_remote_url(remote_url)
    if not host:
        return None
    host_lower = host.lower()
    if "github" in host_lower:
        return "github"
    if "gitlab" in host_lower:
        return "gitlab"
    return None


def extract_owner_repo(remote_url: str) -> Tuple[str, str]:
    """Extract (owner, repo) from a git remote URL.

    Handles:
      git@github.com:owner/repo.git -> (owner, repo)
      https://github.com/owner/repo.git -> (owner, repo)
      git@gitlab.com:group/subgroup/repo.git -> (group/subgroup, repo)

    Raises:
        ValueError: If the URL cannot be parsed.
    """
    url = remote_url.strip()
    # Remove .git suffix
    if url.endswith(".git"):
        url = url[:-4]

    # SSH format: git@host:path  (has @ and : but no ://)
    if "@" in url and ":" in url and "://" not in url:
        path = url.split(":", 1)[1]
    # HTTPS format
    elif "://" in url:
        # Remove protocol and host — path starts after the 3rd slash segment
        parts = url.split("/")
        if len(parts) < 4:
            raise ValueError(f"Cannot extract owner/repo from: {remote_url}")
        path = "/".join(parts[3:])
    else:
        raise ValueError(f"Cannot parse remote URL: {remote_url}")

    segments = path.split("/")
    if len(segments) < 2:
        raise ValueError(f"Cannot extract owner/repo from: {remote_url}")

    repo = segments[-1]
    owner = "/".join(segments[:-1])
    return (owner, repo)


class ForgeAuthenticationError(Exception):
    """Raised when a forge token is invalid, expired, or lacks required permissions."""

    pass


class ForgeClient:
    """Abstract base for forge identity validation clients."""

    async def validate_and_discover(
        self, token: str, host: str
    ) -> Dict[str, Any]:
        """
        Validate a personal access token against the forge API and discover user identity.

        Args:
            token: Personal access token to validate
            host: Forge hostname (e.g. 'github.com', 'gitlab.com', 'github.corp.com')

        Returns:
            Dict with keys: git_user_name, git_user_email, forge_username

        Raises:
            ForgeAuthenticationError: If token is invalid, expired, or not authorized
        """
        raise NotImplementedError


class GitHubForgeClient(ForgeClient):
    """
    Validates GitHub personal access tokens and discovers user identity.

    Uses the GitHub REST API /user endpoint.
    For github.com uses api.github.com; for GitHub Enterprise Server uses {host}/api/v3.
    """

    async def validate_and_discover(
        self, token: str, host: str
    ) -> Dict[str, Any]:
        """
        Validate a GitHub PAT and return user identity.

        Args:
            token: GitHub personal access token
            host: GitHub hostname (e.g. 'github.com' or enterprise host)

        Returns:
            Dict with git_user_name, git_user_email, forge_username

        Raises:
            ForgeAuthenticationError: If token is invalid or request fails
        """
        if host == "github.com":
            url = "https://api.github.com/user"
        else:
            # GitHub Enterprise Server uses path-based API prefix
            url = f"https://{host}/api/v3/user"

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise ForgeAuthenticationError(
                f"Unable to reach {host} for token validation: {e}"
            )

        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitHub token. HTTP 401 from {host}."
            )

        if response.status_code != 200:
            raise ForgeAuthenticationError(
                f"GitHub API returned {response.status_code} for host {host}."
            )

        data = response.json()
        email = data.get("email") or None

        return {
            "forge_username": data.get("login", ""),
            "git_user_name": data.get("name", "") or data.get("login", ""),
            "git_user_email": email,
        }

    def create_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> Dict[str, Any]:
        """Create a GitHub pull request via REST API (sync).

        Args:
            token: GitHub personal access token
            host: GitHub hostname ('github.com' or enterprise host)
            owner: Repository owner (user or org)
            repo: Repository name
            title: Pull request title
            body: Pull request description
            head: Source branch name
            base: Target branch name

        Returns:
            Dict with 'url' (html_url) and 'number' keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401)
            ValueError: If API returns a validation error (HTTP 422) or other non-success
            httpx.RequestError: On network failures
        """
        if host == "github.com":
            api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        else:
            api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/pulls"

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }

        response = httpx.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitHub token. HTTP 401 from {host}."
            )

        if response.status_code == 403:
            raise ForgeAuthenticationError(
                f"GitHub token lacks required permissions (HTTP 403). "
                f"Ensure the token has 'repo' scope for {host}."
            )

        if response.status_code == 422:
            raise ValueError(
                f"GitHub API validation error (422): {response.text}"
            )

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitHub API returned {response.status_code}: {response.text}"
            )

        data = response.json()
        return {
            "url": data["html_url"],
            "number": data["number"],
        }


class GitLabForgeClient(ForgeClient):
    """
    Validates GitLab personal access tokens and discovers user identity.

    Uses the GitLab REST API /api/v4/user endpoint with PRIVATE-TOKEN header.
    Supports both gitlab.com and self-hosted GitLab instances.
    """

    async def validate_and_discover(
        self, token: str, host: str
    ) -> Dict[str, Any]:
        """
        Validate a GitLab PAT and return user identity.

        Args:
            token: GitLab personal access token
            host: GitLab hostname (e.g. 'gitlab.com' or self-hosted host)

        Returns:
            Dict with git_user_name, git_user_email, forge_username

        Raises:
            ForgeAuthenticationError: If token is invalid or request fails
        """
        url = f"https://{host}/api/v4/user"
        headers = {
            "PRIVATE-TOKEN": token,
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise ForgeAuthenticationError(
                f"Unable to reach {host} for token validation: {e}"
            )

        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitLab token. HTTP 401 from {host}."
            )

        if response.status_code != 200:
            raise ForgeAuthenticationError(
                f"GitLab API returned {response.status_code} for host {host}."
            )

        data = response.json()
        email = data.get("email") or None

        return {
            "forge_username": data.get("username", ""),
            "git_user_name": data.get("name", "") or data.get("username", ""),
            "git_user_email": email,
        }

    def create_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
    ) -> Dict[str, Any]:
        """Create a GitLab merge request via REST API (sync).

        Args:
            token: GitLab personal access token
            host: GitLab hostname ('gitlab.com' or self-hosted host)
            owner: Repository owner or group path (may include subgroups)
            repo: Repository name
            title: Merge request title
            body: Merge request description
            source_branch: Source branch name
            target_branch: Target branch name

        Returns:
            Dict with 'url' (web_url) and 'number' (iid) keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401)
            ValueError: If API returns a conflict (HTTP 409) or other non-success
            httpx.RequestError: On network failures
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = f"https://{host}/api/v4/projects/{project_path}/merge_requests"

        headers = {
            "PRIVATE-TOKEN": token,
        }
        payload = {
            "title": title,
            "description": body,
            "source_branch": source_branch,
            "target_branch": target_branch,
        }

        response = httpx.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitLab token. HTTP 401 from {host}."
            )

        if response.status_code == 403:
            raise ForgeAuthenticationError(
                f"GitLab token lacks required permissions (HTTP 403). "
                f"Ensure the token has 'api' scope for {host}."
            )

        if response.status_code == 409:
            raise ValueError(
                f"GitLab API conflict (409): {response.text}"
            )

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        data = response.json()
        return {
            "url": data["web_url"],
            "number": data["iid"],
        }


def get_forge_client(forge_type: str) -> ForgeClient:
    """
    Factory function returning the appropriate ForgeClient for the given forge type.

    Args:
        forge_type: Either 'github' or 'gitlab'

    Returns:
        ForgeClient instance

    Raises:
        ValueError: If forge_type is not supported
    """
    if forge_type == "github":
        return GitHubForgeClient()
    elif forge_type == "gitlab":
        return GitLabForgeClient()
    else:
        raise ValueError(
            f"Unsupported forge type: '{forge_type}'. Supported types: 'github', 'gitlab'"
        )
