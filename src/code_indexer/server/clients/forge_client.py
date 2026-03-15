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

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
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

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0)
            ) as client:
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

    def list_pull_requests(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
        author: Optional[str] = None,
    ) -> list:
        """List GitHub pull requests via REST API (sync).

        Args:
            token: GitHub personal access token
            host: GitHub hostname ('github.com' or enterprise host)
            owner: Repository owner (user or org)
            repo: Repository name
            state: Filter by state: 'open', 'closed', 'merged', 'all'
            limit: Maximum number of results (passed as per_page)
            author: Optional author username filter (GitHub: 'creator')

        Returns:
            List of normalized PR dicts with keys: number, title, state,
            author, source_branch, target_branch, url, created_at, updated_at.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If API returns a non-success response
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

        # GitHub API: 'merged' is not a native state - use 'closed' then post-filter
        api_state = "closed" if state == "merged" else state
        params: Dict[str, Any] = {
            "state": api_state,
            "per_page": limit,
        }
        if author:
            params["creator"] = author

        response = httpx.get(
            api_url,
            headers=headers,
            params=params,
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

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitHub API returned {response.status_code}: {response.text}"
            )

        raw_prs = response.json()
        result = []
        for pr in raw_prs:
            merged_at = pr.get("merged_at")
            # For 'merged' state: only include PRs that have been merged
            if state == "merged" and not merged_at:
                continue
            # Determine normalized state
            if merged_at:
                normalized_state = "merged"
            else:
                normalized_state = pr.get("state", "open")

            result.append(
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": normalized_state,
                    "author": pr["user"]["login"],
                    "source_branch": pr["head"]["ref"],
                    "target_branch": pr["base"]["ref"],
                    "url": pr["html_url"],
                    "created_at": pr["created_at"],
                    "updated_at": pr["updated_at"],
                }
            )
        return result

    def get_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
    ) -> Dict[str, Any]:
        """Get full details of a single GitHub pull request via REST API (sync).

        Args:
            token: GitHub personal access token
            host: GitHub hostname ('github.com' or enterprise host)
            owner: Repository owner (user or org)
            repo: Repository name
            number: Pull request number

        Returns:
            Normalized dict with keys: number, title, description, state, author,
            source_branch, target_branch, url, labels, reviewers, mergeable,
            ci_status, diff_stats, created_at, updated_at.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If PR not found (HTTP 404) or other non-success response
            httpx.RequestError: On network failures
        """
        if host == "github.com":
            api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        else:
            api_url = f"https://{host}/api/v3/repos/{owner}/{repo}/pulls/{number}"

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        response = httpx.get(
            api_url,
            headers=headers,
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

        if response.status_code == 404:
            raise ValueError(f"PR #{number} not found in {owner}/{repo} on {host}.")

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitHub API returned {response.status_code}: {response.text}"
            )

        pr = response.json()
        labels = [lbl["name"] for lbl in pr.get("labels", [])]
        reviewers = [r["login"] for r in pr.get("requested_reviewers", [])]

        return {
            "number": pr["number"],
            "title": pr["title"],
            "description": pr.get("body") or "",
            "state": pr.get("state", "open"),
            "author": pr["user"]["login"],
            "source_branch": pr["head"]["ref"],
            "target_branch": pr["base"]["ref"],
            "url": pr["html_url"],
            "labels": labels,
            "reviewers": reviewers,
            "mergeable": pr.get("mergeable"),
            "ci_status": pr.get("mergeable_state"),
            "diff_stats": {
                "additions": pr.get("additions", 0),
                "deletions": pr.get("deletions", 0),
                "changed_files": pr.get("changed_files", 0),
            },
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
        }

    def merge_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        merge_method: str = "merge",
        commit_message: Optional[str] = None,
        delete_branch: bool = False,
    ) -> Dict[str, Any]:
        """Merge a GitHub pull request via REST API (sync).

        First GETs the PR to retrieve head.sha and head.ref, then PUTs to
        /repos/{owner}/{repo}/pulls/{number}/merge.
        Optionally deletes the source branch after merging.

        Args:
            token: GitHub personal access token
            host: GitHub hostname ('github.com' or enterprise host)
            owner: Repository owner (user or org)
            repo: Repository name
            number: Pull request number
            merge_method: One of 'merge', 'squash', 'rebase' (default 'merge')
            commit_message: Optional custom commit message
            delete_branch: If True, delete source branch after merge

        Returns:
            Dict with 'success', 'merged', 'sha', and 'message' keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If PR is not mergeable (HTTP 405) or has conflict (HTTP 409)
            httpx.RequestError: On network failures
        """
        base = self._github_base_url(host)
        headers = self._github_headers(token)
        timeout = httpx.Timeout(30.0, connect=10.0)

        # GET PR to retrieve head.sha and head.ref
        pr_resp = httpx.get(
            f"{base}/repos/{owner}/{repo}/pulls/{number}",
            headers=headers,
            timeout=timeout,
        )
        self._check_github_response(pr_resp, host)
        pr_data = pr_resp.json()
        head_sha = pr_data["head"]["sha"]
        head_ref = pr_data["head"]["ref"]

        # PUT to merge endpoint
        payload: Dict[str, Any] = {
            "merge_method": merge_method,
            "sha": head_sha,
        }
        if commit_message is not None:
            payload["commit_message"] = commit_message

        merge_resp = httpx.put(
            f"{base}/repos/{owner}/{repo}/pulls/{number}/merge",
            headers=headers,
            json=payload,
            timeout=timeout,
        )

        if merge_resp.status_code == 405:
            raise ValueError(
                f"PR #{number} is not mergeable (HTTP 405): {merge_resp.text}"
            )
        if merge_resp.status_code == 409:
            raise ValueError(
                f"PR #{number} has a merge conflict (HTTP 409): {merge_resp.text}"
            )
        if merge_resp.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitHub token. HTTP 401 from {host}."
            )
        if merge_resp.status_code == 403:
            raise ForgeAuthenticationError(
                f"GitHub token lacks required permissions (HTTP 403). "
                f"Ensure the token has 'repo' scope for {host}."
            )
        if merge_resp.status_code not in (200, 201):
            raise ValueError(
                f"GitHub API returned {merge_resp.status_code}: {merge_resp.text}"
            )

        merge_data = merge_resp.json()

        # Optionally delete source branch
        if delete_branch:
            httpx.delete(
                f"{base}/repos/{owner}/{repo}/git/refs/heads/{head_ref}",
                headers=headers,
                timeout=timeout,
            )

        return {
            "success": True,
            "merged": True,
            "sha": merge_data.get("sha", ""),
            "message": f"PR #{number} merged",
        }

    def close_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
    ) -> Dict[str, Any]:
        """Close a GitHub pull request via REST API (sync).

        PATCH /repos/{owner}/{repo}/pulls/{number} with {"state": "closed"}.

        Args:
            token: GitHub personal access token
            host: GitHub hostname ('github.com' or enterprise host)
            owner: Repository owner (user or org)
            repo: Repository name
            number: Pull request number

        Returns:
            Dict with 'success' and 'message' keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If PR not found (HTTP 404) or other non-success response
            httpx.RequestError: On network failures
        """
        base = self._github_base_url(host)
        headers = self._github_headers(token)
        timeout = httpx.Timeout(30.0, connect=10.0)

        response = httpx.patch(
            f"{base}/repos/{owner}/{repo}/pulls/{number}",
            headers=headers,
            json={"state": "closed"},
            timeout=timeout,
        )
        self._check_github_response(response, host)

        return {
            "success": True,
            "message": f"PR #{number} closed",
        }

    def _github_base_url(self, host: str) -> str:
        """Return base API URL for the given GitHub host."""
        if host == "github.com":
            return "https://api.github.com"
        return f"https://{host}/api/v3"

    def _github_headers(self, token: str) -> Dict[str, str]:
        """Return standard GitHub API request headers."""
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _check_github_response(self, response: httpx.Response, host: str) -> None:
        """Raise appropriate exception for non-success GitHub API responses."""
        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitHub token. HTTP 401 from {host}."
            )
        if response.status_code == 403:
            raise ForgeAuthenticationError(
                f"GitHub token lacks required permissions (HTTP 403). "
                f"Ensure the token has 'repo' scope for {host}."
            )
        if response.status_code == 404:
            raise ValueError(f"Resource not found (HTTP 404) on {host}.")
        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitHub API returned {response.status_code}: {response.text}"
            )

    def comment_on_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        body: str,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Add a comment to a GitHub pull request via REST API (sync).

        General comment (no file_path): POST /issues/{number}/comments.
        Inline comment (file_path+line_number): GET PR for head.sha, then POST
        /pulls/{number}/comments with commit_id, path, line, side=RIGHT.

        Returns:
            Dict with 'comment_id' and 'url' keys.

        Raises:
            ValueError: If file_path provided without line_number, or API error.
            ForgeAuthenticationError: If token is invalid (HTTP 401/403).
        """
        if file_path is not None and line_number is None:
            raise ValueError("line_number is required when file_path is provided")

        base = self._github_base_url(host)
        headers = self._github_headers(token)
        timeout = httpx.Timeout(30.0, connect=10.0)

        if file_path is not None:
            pr_resp = httpx.get(
                f"{base}/repos/{owner}/{repo}/pulls/{number}",
                headers=headers,
                timeout=timeout,
            )
            self._check_github_response(pr_resp, host)
            head_sha = pr_resp.json()["head"]["sha"]
            api_url = f"{base}/repos/{owner}/{repo}/pulls/{number}/comments"
            payload: Dict[str, Any] = {
                "body": body,
                "commit_id": head_sha,
                "path": file_path,
                "line": line_number,
                "side": "RIGHT",
            }
        else:
            api_url = f"{base}/repos/{owner}/{repo}/issues/{number}/comments"
            payload = {"body": body}

        response = httpx.post(api_url, headers=headers, json=payload, timeout=timeout)
        if response.status_code == 422:
            raise ValueError(f"GitHub API validation error (422): {response.text}")
        self._check_github_response(response, host)
        data = response.json()
        return {"comment_id": data["id"], "url": data["html_url"]}

    def _normalize_github_review_comment(self, rc: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a GitHub inline review comment to unified format."""
        line = rc.get("line") or rc.get("original_line")
        return {
            "id": rc["id"],
            "author": rc["user"]["login"],
            "body": rc.get("body", ""),
            "created_at": rc["created_at"],
            "updated_at": rc["updated_at"],
            "file_path": rc.get("path"),
            "line_number": line,
            "is_review_comment": True,
            "resolved": None,
        }

    def _normalize_github_issue_comment(self, ic: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a GitHub general issue/conversation comment to unified format."""
        return {
            "id": ic["id"],
            "author": ic["user"]["login"],
            "body": ic.get("body", ""),
            "created_at": ic["created_at"],
            "updated_at": ic["updated_at"],
            "file_path": None,
            "line_number": None,
            "is_review_comment": False,
            "resolved": None,
        }

    def list_pull_request_comments(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        limit: int = 50,
    ) -> list:
        """List all comments on a GitHub pull request via REST API (sync).

        Makes two API calls and merges results:
          1. GET /repos/{owner}/{repo}/pulls/{number}/comments  (inline review comments)
          2. GET /repos/{owner}/{repo}/issues/{number}/comments  (general conversation)

        Returns list sorted by created_at, capped to limit. Each dict has:
        id, author, body, created_at, updated_at, file_path (None for general),
        line_number (None for general), is_review_comment, resolved (always None).

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If API returns a non-success response
        """
        base = (
            "https://api.github.com"
            if host == "github.com"
            else f"https://{host}/api/v3"
        )
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        timeout = httpx.Timeout(30.0, connect=10.0)

        review_resp = httpx.get(
            f"{base}/repos/{owner}/{repo}/pulls/{number}/comments",
            headers=headers,
            timeout=timeout,
        )
        self._check_github_response(review_resp, host)

        issue_resp = httpx.get(
            f"{base}/repos/{owner}/{repo}/issues/{number}/comments",
            headers=headers,
            timeout=timeout,
        )
        self._check_github_response(issue_resp, host)

        result = [
            self._normalize_github_review_comment(rc) for rc in review_resp.json()
        ] + [self._normalize_github_issue_comment(ic) for ic in issue_resp.json()]
        result.sort(key=lambda c: c["created_at"])
        return result[:limit]

    def _build_github_pr_patch_payload(
        self,
        title: Optional[str],
        description: Optional[str],
        labels: Optional[list],
        assignees: Optional[list],
    ) -> Tuple[Dict[str, Any], list]:
        """Build PATCH payload and updated_fields list for update_pull_request."""
        payload: Dict[str, Any] = {}
        fields: list = []
        if title is not None:
            payload["title"] = title
            fields.append("title")
        if description is not None:
            payload["body"] = description
            fields.append("description")
        if labels is not None:
            payload["labels"] = labels
            fields.append("labels")
        if assignees is not None:
            payload["assignees"] = assignees
            fields.append("assignees")
        return payload, fields

    def update_pull_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[list] = None,
        assignees: Optional[list] = None,
        reviewers: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Update a GitHub pull request via REST API (sync).

        PATCH /pulls/{number} for title/body/labels/assignees.
        POST /pulls/{number}/requested_reviewers for reviewers (separate endpoint).
        At least one field must be provided.

        Returns:
            Dict with 'success', 'url', and 'updated_fields' keys.

        Raises:
            ValueError: If no fields provided or API returns error.
            ForgeAuthenticationError: If token is invalid.
        """
        patch_payload, updated_fields = self._build_github_pr_patch_payload(
            title, description, labels, assignees
        )
        if not patch_payload and reviewers is None:
            raise ValueError(
                "At least one field must be provided to update: "
                "title, description, labels, assignees, or reviewers."
            )

        base = self._github_base_url(host)
        headers = self._github_headers(token)
        timeout = httpx.Timeout(30.0, connect=10.0)
        pr_url = f"{base}/repos/{owner}/{repo}/pulls/{number}"
        pr_html_url = ""

        if patch_payload:
            resp = httpx.patch(
                pr_url, headers=headers, json=patch_payload, timeout=timeout
            )
            self._check_github_response(resp, host)
            pr_html_url = resp.json().get("html_url", "")

        if reviewers is not None:
            rev_resp = httpx.post(
                f"{pr_url}/requested_reviewers",
                headers=headers,
                json={"reviewers": reviewers},
                timeout=timeout,
            )
            self._check_github_response(rev_resp, host)
            updated_fields.append("reviewers")
            if not pr_html_url:
                pr_html_url = rev_resp.json().get("html_url", "")

        return {
            "success": True,
            "url": pr_html_url,
            "updated_fields": sorted(updated_fields),
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
            raise ValueError(f"GitHub API validation error (422): {response.text}")

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

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0)
            ) as client:
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

    def list_merge_requests(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        state: str = "open",
        limit: int = 10,
        author: Optional[str] = None,
    ) -> list:
        """List GitLab merge requests via REST API (sync).

        Args:
            token: GitLab personal access token
            host: GitLab hostname ('gitlab.com' or self-hosted host)
            owner: Repository owner or group path (may include subgroups)
            repo: Repository name
            state: Filter by state: 'open', 'closed', 'merged', 'all'
            limit: Maximum number of results (passed as per_page)
            author: Optional author username filter (GitLab: 'author_username')

        Returns:
            List of normalized MR dicts with keys: number, title, state,
            author, source_branch, target_branch, url, created_at, updated_at.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If API returns a non-success response
            httpx.RequestError: On network failures
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = f"https://{host}/api/v4/projects/{project_path}/merge_requests"

        headers = {
            "PRIVATE-TOKEN": token,
        }

        # GitLab uses 'opened' for open state, others pass through
        api_state = "opened" if state == "open" else state
        params: Dict[str, Any] = {
            "state": api_state,
            "per_page": limit,
        }
        if author:
            params["author_username"] = author

        response = httpx.get(
            api_url,
            headers=headers,
            params=params,
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

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        raw_mrs = response.json()
        result = []
        for mr in raw_mrs:
            # Normalize GitLab 'opened' -> 'open'
            mr_state = mr.get("state", "opened")
            normalized_state = "open" if mr_state == "opened" else mr_state

            result.append(
                {
                    "number": mr["iid"],
                    "title": mr["title"],
                    "state": normalized_state,
                    "author": mr["author"]["username"],
                    "source_branch": mr["source_branch"],
                    "target_branch": mr["target_branch"],
                    "url": mr["web_url"],
                    "created_at": mr["created_at"],
                    "updated_at": mr["updated_at"],
                }
            )
        return result

    def get_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
    ) -> Dict[str, Any]:
        """Get full details of a single GitLab merge request via REST API (sync).

        Args:
            token: GitLab personal access token
            host: GitLab hostname ('gitlab.com' or self-hosted host)
            owner: Repository owner or group path (may include subgroups)
            repo: Repository name
            number: Merge request IID (project-scoped number)

        Returns:
            Normalized dict with keys: number, title, description, state, author,
            source_branch, target_branch, url, labels, reviewers, mergeable,
            ci_status, diff_stats, created_at, updated_at.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If MR not found (HTTP 404) or other non-success response
            httpx.RequestError: On network failures
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = (
            f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}"
        )

        headers = {
            "PRIVATE-TOKEN": token,
        }

        response = httpx.get(
            api_url,
            headers=headers,
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

        if response.status_code == 404:
            raise ValueError(f"MR #{number} not found in {owner}/{repo} on {host}.")

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        mr = response.json()

        # Normalize GitLab 'opened' -> 'open'
        mr_state = mr.get("state", "opened")
        normalized_state = "open" if mr_state == "opened" else mr_state

        # Labels in GitLab are a flat list of strings
        labels = mr.get("labels", [])

        # Reviewers extracted from reviewers[].username
        reviewers = [r["username"] for r in mr.get("reviewers", [])]

        # mergeable: True when merge_status == 'can_be_merged'
        mergeable = mr.get("merge_status") == "can_be_merged"

        # CI status from head_pipeline
        head_pipeline = mr.get("head_pipeline")
        ci_status = head_pipeline["status"] if head_pipeline else None

        # diff_stats from nested diff_stats object
        diff_stats_raw = mr.get("diff_stats", {})
        diff_stats = {
            "additions": diff_stats_raw.get("additions", 0),
            "deletions": diff_stats_raw.get("deletions", 0),
            "changed_files": diff_stats_raw.get("changes", 0),
        }

        return {
            "number": mr["iid"],
            "title": mr["title"],
            "description": mr.get("description") or "",
            "state": normalized_state,
            "author": mr["author"]["username"],
            "source_branch": mr["source_branch"],
            "target_branch": mr["target_branch"],
            "url": mr["web_url"],
            "labels": labels,
            "reviewers": reviewers,
            "mergeable": mergeable,
            "ci_status": ci_status,
            "diff_stats": diff_stats,
            "created_at": mr["created_at"],
            "updated_at": mr["updated_at"],
        }

    def merge_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        merge_method: str = "merge",
        delete_branch: bool = False,
    ) -> Dict[str, Any]:
        """Merge a GitLab merge request via REST API (sync).

        PUT /projects/{path}/merge_requests/{number}/merge

        Args:
            token: GitLab personal access token
            host: GitLab hostname ('gitlab.com' or self-hosted host)
            owner: Repository owner or group path (may include subgroups)
            repo: Repository name
            number: Merge request IID (project-scoped number)
            merge_method: One of 'merge', 'squash', 'rebase' (default 'merge')
            delete_branch: If True, remove source branch after merge

        Returns:
            Dict with 'success', 'merged', 'sha', and 'message' keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If MR cannot be merged (HTTP 405/406) or other error
            httpx.RequestError: On network failures
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}/merge"
        headers = {"PRIVATE-TOKEN": token}
        timeout = httpx.Timeout(30.0, connect=10.0)

        payload: Dict[str, Any] = {}
        if merge_method == "squash":
            payload["squash"] = True
        if delete_branch:
            payload["should_remove_source_branch"] = True

        response = httpx.put(
            api_url,
            headers=headers,
            json=payload,
            timeout=timeout,
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
        if response.status_code == 405:
            raise ValueError(
                f"MR #{number} cannot be merged (HTTP 405 Method Not Allowed): {response.text}"
            )
        if response.status_code == 406:
            raise ValueError(
                f"MR #{number} cannot be merged (HTTP 406): {response.text}"
            )
        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        data = response.json()
        sha = data.get("merge_commit_sha") or data.get("squash_commit_sha") or ""

        return {
            "success": True,
            "merged": True,
            "sha": sha,
            "message": f"MR #{number} merged",
        }

    def close_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
    ) -> Dict[str, Any]:
        """Close a GitLab merge request via REST API (sync).

        PUT /projects/{path}/merge_requests/{number} with {"state_event": "close"}

        Args:
            token: GitLab personal access token
            host: GitLab hostname ('gitlab.com' or self-hosted host)
            owner: Repository owner or group path (may include subgroups)
            repo: Repository name
            number: Merge request IID (project-scoped number)

        Returns:
            Dict with 'success' and 'message' keys.

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If MR not found (HTTP 404) or other non-success response
            httpx.RequestError: On network failures
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = (
            f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}"
        )
        headers = {"PRIVATE-TOKEN": token}
        timeout = httpx.Timeout(30.0, connect=10.0)

        response = httpx.put(
            api_url,
            headers=headers,
            json={"state_event": "close"},
            timeout=timeout,
        )
        self._check_gitlab_response(response, host)

        return {
            "success": True,
            "message": f"MR #{number} closed",
        }

    def _check_gitlab_response(self, response: httpx.Response, host: str) -> None:
        """Raise appropriate exception for non-success GitLab API responses."""
        if response.status_code == 401:
            raise ForgeAuthenticationError(
                f"Invalid or expired GitLab token. HTTP 401 from {host}."
            )
        if response.status_code == 403:
            raise ForgeAuthenticationError(
                f"GitLab token lacks required permissions (HTTP 403). "
                f"Ensure the token has 'api' scope for {host}."
            )
        if response.status_code == 404:
            raise ValueError(f"Resource not found (HTTP 404) on {host}.")
        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

    def comment_on_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        body: str,
        file_path: Optional[str] = None,
        line_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Add a comment to a GitLab merge request via REST API (sync).

        General comment (no file_path): POST .../merge_requests/{number}/notes.
        Inline comment (file_path+line_number): GET MR for diff_refs, then POST
        notes with a position object containing base/head/start shas and file info.

        Returns:
            Dict with 'comment_id' and 'url' keys.

        Raises:
            ValueError: If file_path provided without line_number, or API error.
            ForgeAuthenticationError: If token is invalid (HTTP 401/403).
        """
        if file_path is not None and line_number is None:
            raise ValueError("line_number is required when file_path is provided")

        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        headers = {"PRIVATE-TOKEN": token}
        timeout = httpx.Timeout(30.0, connect=10.0)
        notes_url = f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}/notes"

        if file_path is not None:
            mr_url = (
                f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}"
            )
            mr_resp = httpx.get(mr_url, headers=headers, timeout=timeout)
            self._check_gitlab_response(mr_resp, host)
            diff_refs = mr_resp.json()["diff_refs"]
            payload: Dict[str, Any] = {
                "body": body,
                "position": {
                    "base_sha": diff_refs["base_sha"],
                    "head_sha": diff_refs["head_sha"],
                    "start_sha": diff_refs["start_sha"],
                    "new_path": file_path,
                    "new_line": line_number,
                    "position_type": "text",
                },
            }
        else:
            payload = {"body": body}

        response = httpx.post(notes_url, headers=headers, json=payload, timeout=timeout)
        self._check_gitlab_response(response, host)
        data = response.json()
        return {
            "comment_id": data["id"],
            "url": data.get("web_url", ""),
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
            raise ValueError(f"GitLab API conflict (409): {response.text}")

        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        data = response.json()
        return {
            "url": data["web_url"],
            "number": data["iid"],
        }

    def update_merge_request(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[list] = None,
        assignees: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Update a GitLab merge request via REST API (sync).

        PUT /projects/{path}/merge_requests/{number} with only provided fields.
        Labels are sent as a comma-separated string (GitLab API requirement).
        At least one field must be provided.

        Returns:
            Dict with 'success', 'url', and 'updated_fields' keys.

        Raises:
            ValueError: If no fields provided or API returns error.
            ForgeAuthenticationError: If token is invalid.
        """
        put_payload: Dict[str, Any] = {}
        updated_fields: list = []

        if title is not None:
            put_payload["title"] = title
            updated_fields.append("title")
        if description is not None:
            put_payload["description"] = description
            updated_fields.append("description")
        if labels is not None:
            put_payload["labels"] = ",".join(labels)
            updated_fields.append("labels")
        if assignees is not None:
            put_payload["assignee_ids"] = assignees
            updated_fields.append("assignees")

        if not put_payload:
            raise ValueError(
                "At least one field must be provided to update: "
                "title, description, labels, or assignees."
            )

        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = (
            f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}"
        )
        headers = {"PRIVATE-TOKEN": token}
        timeout = httpx.Timeout(30.0, connect=10.0)

        response = httpx.put(
            api_url, headers=headers, json=put_payload, timeout=timeout
        )
        self._check_gitlab_response(response, host)
        data = response.json()
        return {
            "success": True,
            "url": data.get("web_url", ""),
            "updated_fields": sorted(updated_fields),
        }

    def _normalize_gitlab_note(self, note: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a GitLab MR note to unified comment format."""
        position = note.get("position")
        if position:
            file_path = position.get("new_path")
            line_number = position.get("new_line")
            is_review_comment = True
        else:
            file_path = None
            line_number = None
            is_review_comment = False

        resolvable = note.get("resolvable", False)
        resolved: Optional[bool] = note.get("resolved", False) if resolvable else None

        return {
            "id": note["id"],
            "author": note["author"]["username"],
            "body": note.get("body", ""),
            "created_at": note["created_at"],
            "updated_at": note["updated_at"],
            "file_path": file_path,
            "line_number": line_number,
            "is_review_comment": is_review_comment,
            "resolved": resolved,
        }

    def list_merge_request_notes(
        self,
        token: str,
        host: str,
        owner: str,
        repo: str,
        number: int,
        limit: int = 50,
    ) -> list:
        """List user notes (comments) on a GitLab merge request via REST API (sync).

        GET /projects/{url_encoded_path}/merge_requests/{number}/notes?sort=asc

        System notes (system=True) are excluded. Inline notes have position data.

        Returns list capped to limit. Each dict has:
        id, author, body, created_at, updated_at, file_path (None for general),
        line_number (None for general), is_review_comment, resolved (None if not resolvable).

        Raises:
            ForgeAuthenticationError: If token is invalid (HTTP 401/403)
            ValueError: If API returns a non-success response
        """
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe="")
        api_url = f"https://{host}/api/v4/projects/{project_path}/merge_requests/{number}/notes"
        headers = {"PRIVATE-TOKEN": token}

        response = httpx.get(
            api_url,
            headers=headers,
            params={"sort": "asc"},
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
        if response.status_code not in (200, 201):
            raise ValueError(
                f"GitLab API returned {response.status_code}: {response.text}"
            )

        raw_notes = response.json()
        result = [
            self._normalize_gitlab_note(note)
            for note in raw_notes
            if not note.get("system", False)
        ]
        return result[:limit]


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
