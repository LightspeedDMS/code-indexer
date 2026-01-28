"""
Git API Client for CIDX Remote Git Operations.

Story #737: Provides CLI remote mode access to git workflow operations.
Follows the same patterns as other API clients in the codebase.
"""

import logging
from typing import Any, Dict, NoReturn, Optional
from pathlib import Path

from .base_client import (
    CIDXRemoteAPIClient,
    APIClientError,
    AuthenticationError,
    NetworkError,
)
from .network_error_handler import (
    NetworkConnectionError,
    NetworkTimeoutError,
    DNSResolutionError,
    SSLCertificateError,
    ServerError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


class ConfirmationRequiredError(APIClientError):
    """Exception raised when a destructive operation requires confirmation."""

    def __init__(self, message: str, token: str):
        super().__init__(message, status_code=403)
        self.token = token


class GitAPIClient(CIDXRemoteAPIClient):
    """API client for Git workflow operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize Git API client.

        Args:
            server_url: Base URL of the CIDX server
            credentials: Encrypted credentials dictionary
            project_root: Project root for persistent token storage
        """
        super().__init__(
            server_url=server_url,
            credentials=credentials,
            project_root=project_root,
        )

    # Status/Inspection Methods

    def status(self, repository_alias: str) -> Dict[str, Any]:
        """Get working tree status.

        Args:
            repository_alias: Repository alias identifier

        Returns:
            Dictionary with status information (staged, unstaged, untracked files)

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                "GET", f"/api/v1/repos/{repository_alias}/git/status"
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get status")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting status: {e}")

    def diff(
        self,
        repository_alias: str,
        path: Optional[str] = None,
        staged: bool = False,
        commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get diff of changes.

        Args:
            repository_alias: Repository alias identifier
            path: Limit diff to specific path
            staged: Show staged changes only
            commit: Show diff for specific commit

        Returns:
            Dictionary with diff information

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        params: Dict[str, Any] = {}
        if path:
            params["path"] = path
        if staged:
            params["staged"] = "true"
        if commit:
            params["commit"] = commit

        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/diff",
                params=params if params else None,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get diff")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting diff: {e}")

    def log(
        self,
        repository_alias: str,
        limit: int = 20,
        author: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get commit history.

        Args:
            repository_alias: Repository alias identifier
            limit: Maximum number of commits to return
            author: Filter commits by author
            path: Filter commits affecting this path

        Returns:
            Dictionary with commit history

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        params: Dict[str, Any] = {"limit": limit}
        if author:
            params["author"] = author
        if path:
            params["path"] = path

        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/log",
                params=params,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get log")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting log: {e}")

    def show_commit(
        self,
        repository_alias: str,
        commit_hash: str,
        include_diff: bool = False,
        include_stats: bool = True,
    ) -> Dict[str, Any]:
        """Show details of a specific commit.

        Args:
            repository_alias: Repository alias identifier
            commit_hash: Commit SHA hash
            include_diff: Include diff in response
            include_stats: Include file statistics

        Returns:
            Dictionary with commit details

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        params: Dict[str, Any] = {
            "include_diff": str(include_diff).lower(),
            "include_stats": str(include_stats).lower(),
        }

        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/show/{commit_hash}",
                params=params,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(
                    f"Commit '{commit_hash}' not found in repository '{repository_alias}'",
                    404,
                )
            else:
                self._handle_error_response(response, "show commit")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error showing commit: {e}")

    # Staging/Commit Methods

    def stage(
        self,
        repository_alias: str,
        files: list[str],
    ) -> Dict[str, Any]:
        """Stage files for commit.

        Args:
            repository_alias: Repository alias identifier
            files: List of file paths to stage

        Returns:
            Dictionary with staging result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/stage",
                json={"file_paths": files},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "stage files")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error staging files: {e}")

    def unstage(
        self,
        repository_alias: str,
        files: list[str],
    ) -> Dict[str, Any]:
        """Unstage files.

        Args:
            repository_alias: Repository alias identifier
            files: List of file paths to unstage

        Returns:
            Dictionary with unstaging result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/unstage",
                json={"file_paths": files},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "unstage files")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error unstaging files: {e}")

    def commit(
        self,
        repository_alias: str,
        message: str,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a commit.

        Args:
            repository_alias: Repository alias identifier
            message: Commit message
            author_name: Optional author name
            author_email: Optional author email

        Returns:
            Dictionary with commit result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {"message": message}
        if author_name:
            data["author_name"] = author_name
        if author_email:
            data["author_email"] = author_email

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/commit",
                json=data,
            )

            if response.status_code in (200, 201):
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "create commit")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error creating commit: {e}")

    # Remote Operations Methods

    def push(
        self,
        repository_alias: str,
        remote: str = "origin",
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Push commits to remote.

        Args:
            repository_alias: Repository alias identifier
            remote: Remote name (default: origin)
            branch: Branch to push (default: current branch)

        Returns:
            Dictionary with push result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {"remote": remote}
        if branch:
            data["branch"] = branch

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/push",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "push")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error pushing: {e}")

    def pull(
        self,
        repository_alias: str,
        remote: str = "origin",
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pull changes from remote.

        Args:
            repository_alias: Repository alias identifier
            remote: Remote name (default: origin)
            branch: Branch to pull (default: current branch)

        Returns:
            Dictionary with pull result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {"remote": remote}
        if branch:
            data["branch"] = branch

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/pull",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "pull")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error pulling: {e}")

    def fetch(
        self,
        repository_alias: str,
        remote: str = "origin",
    ) -> Dict[str, Any]:
        """Fetch changes from remote.

        Args:
            repository_alias: Repository alias identifier
            remote: Remote name (default: origin)

        Returns:
            Dictionary with fetch result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {"remote": remote}

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/fetch",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "fetch")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error fetching: {e}")

    # Recovery Methods

    def reset(
        self,
        repository_alias: str,
        mode: str = "mixed",
        commit: Optional[str] = None,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reset working tree.

        Args:
            repository_alias: Repository alias identifier
            mode: Reset mode (soft, mixed, hard)
            commit: Target commit (default: HEAD)
            confirmation_token: Required for hard reset

        Returns:
            Dictionary with reset result

        Raises:
            APIClientError: If API request fails
            ConfirmationRequiredError: If hard reset requires confirmation
        """
        data: Dict[str, Any] = {"mode": mode}
        if commit:
            data["commit_hash"] = commit
        if confirmation_token:
            data["confirmation_token"] = confirmation_token

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/reset",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "reset")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
            ConfirmationRequiredError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error resetting: {e}")

    def clean(
        self,
        repository_alias: str,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove untracked files.

        Args:
            repository_alias: Repository alias identifier
            confirmation_token: Required for clean operation

        Returns:
            Dictionary with clean result

        Raises:
            APIClientError: If API request fails
            ConfirmationRequiredError: If confirmation is required
        """
        data: Dict[str, Any] = {}
        if confirmation_token:
            data["confirmation_token"] = confirmation_token

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/clean",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "clean")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
            ConfirmationRequiredError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error cleaning: {e}")

    def merge_abort(self, repository_alias: str) -> Dict[str, Any]:
        """Abort a merge in progress.

        Args:
            repository_alias: Repository alias identifier

        Returns:
            Dictionary with merge abort result

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/merge-abort",
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "abort merge")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error aborting merge: {e}")

    def checkout_file(
        self,
        repository_alias: str,
        file_path: str,
    ) -> Dict[str, Any]:
        """Checkout a file from HEAD, discarding local changes.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to file to checkout

        Returns:
            Dictionary with checkout result

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/checkout-file",
                json={"file_path": file_path},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "checkout file")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error checking out file: {e}")

    def branches(self, repository_alias: str) -> Dict[str, Any]:
        """List all branches.

        Args:
            repository_alias: Repository alias identifier

        Returns:
            Dictionary with branch list and current branch

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/branches",
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "list branches")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error listing branches: {e}")

    def branch_create(
        self,
        repository_alias: str,
        branch_name: str,
        start_point: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new branch.

        Args:
            repository_alias: Repository alias identifier
            branch_name: Name of the new branch
            start_point: Starting ref (default: HEAD)

        Returns:
            Dictionary with branch creation result

        Raises:
            APIClientError: If API request fails
        """
        data: Dict[str, Any] = {"branch_name": branch_name}
        if start_point:
            data["start_point"] = start_point

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/branch-create",
                json=data,
            )

            if response.status_code in (200, 201):
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "create branch")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error creating branch: {e}")

    def branch_switch(
        self,
        repository_alias: str,
        branch_name: str,
    ) -> Dict[str, Any]:
        """Switch to a branch.

        Args:
            repository_alias: Repository alias identifier
            branch_name: Name of the branch to switch to

        Returns:
            Dictionary with switch result

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/branch-switch",
                json={"branch_name": branch_name},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "switch branch")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error switching branch: {e}")

    def branch_delete(
        self,
        repository_alias: str,
        branch_name: str,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a branch.

        Args:
            repository_alias: Repository alias identifier
            branch_name: Name of the branch to delete
            confirmation_token: Required for deletion

        Returns:
            Dictionary with deletion result

        Raises:
            APIClientError: If API request fails
            ConfirmationRequiredError: If confirmation is required
        """
        data: Dict[str, Any] = {"branch_name": branch_name}
        if confirmation_token:
            data["confirmation_token"] = confirmation_token

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/git/branch-delete",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "delete branch")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
            ConfirmationRequiredError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error deleting branch: {e}")

    def blame(
        self,
        repository_alias: str,
        file_path: str,
    ) -> Dict[str, Any]:
        """Get blame information for a file.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file

        Returns:
            Dictionary with blame information per line

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/blame",
                params={"file_path": file_path},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get blame")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting blame: {e}")

    def file_history(
        self,
        repository_alias: str,
        file_path: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Get commit history for a specific file.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file
            limit: Maximum number of commits to return

        Returns:
            Dictionary with commit history for the file

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/file-history",
                params={"file_path": file_path, "limit": limit},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get file history")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting file history: {e}")

    def search_commits(
        self,
        repository_alias: str,
        query: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search commits by message content.

        Args:
            repository_alias: Repository alias identifier
            query: Search query for commit messages
            limit: Maximum number of results

        Returns:
            Dictionary with matching commits

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/search-commits",
                params={"query": query, "limit": limit},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "search commits")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error searching commits: {e}")

    def search_diffs(
        self,
        repository_alias: str,
        pattern: str,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search for a pattern in diff content.

        Args:
            repository_alias: Repository alias identifier
            pattern: Search pattern for diff content
            limit: Maximum number of results

        Returns:
            Dictionary with matching diffs

        Raises:
            APIClientError: If API request fails
        """
        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/search-diffs",
                params={"pattern": pattern, "limit": limit},
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "search diffs")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error searching diffs: {e}")

    def cat_file(
        self,
        repository_alias: str,
        file_path: str,
        revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get file content at a specific revision.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file
            revision: Git revision (default: HEAD)

        Returns:
            Dictionary with file content

        Raises:
            APIClientError: If API request fails
        """
        params: Dict[str, Any] = {"file_path": file_path}
        if revision:
            params["revision"] = revision

        try:
            response = self._authenticated_request(
                "GET",
                f"/api/v1/repos/{repository_alias}/git/cat",
                params=params,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            else:
                self._handle_error_response(response, "get file content")

        except (
            APIClientError,
            AuthenticationError,
            NetworkError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error getting file content: {e}")

    def _handle_error_response(self, response: Any, operation: str) -> NoReturn:
        """Handle error responses from the API.

        Args:
            response: HTTP response object
            operation: Description of the operation for error messages

        Raises:
            APIClientError: With appropriate message based on status code
            ConfirmationRequiredError: If confirmation is required
        """
        try:
            error_data = response.json()
            error_detail = error_data.get("detail", f"HTTP {response.status_code}")

            if response.status_code == 403 and error_data.get("requires_confirmation"):
                token = error_data.get("token", "")
                raise ConfirmationRequiredError(
                    f"Confirmation required to {operation}", token
                )

        except (ValueError, KeyError):
            error_detail = f"HTTP {response.status_code}"

        raise APIClientError(
            f"Failed to {operation}: {error_detail}", response.status_code
        )
