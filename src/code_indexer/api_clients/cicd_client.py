"""
CI/CD API Client for CIDX Remote CI/CD Monitoring.

Story #746: Provides CLI remote mode access to CI/CD monitoring operations.
Follows the same patterns as other API clients in the codebase.
"""

import logging
from typing import Any, Dict, Optional
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


class CICDAPIClient(CIDXRemoteAPIClient):
    """API client for CI/CD monitoring operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize CI/CD API client.

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

    def _make_cicd_request(
        self,
        method: str,
        endpoint: str,
        not_found_msg: str,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a CI/CD API request with standard error handling.

        Args:
            method: HTTP method (GET, POST)
            endpoint: API endpoint path
            not_found_msg: Error message for 404 responses
            operation: Operation description for error messages
            params: Query parameters (optional)

        Returns:
            Dictionary with response data

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                method, endpoint, params=params if params else None
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(not_found_msg, 404)
            else:
                try:
                    error_data = response.json()
                    error_detail = error_data.get(
                        "detail", f"HTTP {response.status_code}"
                    )
                except (ValueError, KeyError):
                    error_detail = f"HTTP {response.status_code}"
                raise APIClientError(
                    f"Failed to {operation}: {error_detail}", response.status_code
                )

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
            raise APIClientError(f"Unexpected error in {operation}: {e}")

    # GitHub Actions Methods

    def github_list_runs(
        self,
        owner: str,
        repo: str,
        status: Optional[str] = None,
        branch: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List GitHub Actions workflow runs for a repository."""
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        if limit:
            params["limit"] = limit

        return self._make_cicd_request(
            "GET",
            f"/api/cicd/github/{owner}/{repo}/runs",
            f"Repository '{owner}/{repo}' not found",
            "list GitHub runs",
            params,
        )

    def github_get_run(self, owner: str, repo: str, run_id: int) -> Dict[str, Any]:
        """Get details of a specific GitHub Actions workflow run."""
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/github/{owner}/{repo}/runs/{run_id}",
            f"Run '{run_id}' not found in repository '{owner}/{repo}'",
            "get GitHub run",
        )

    def github_search_logs(
        self,
        owner: str,
        repo: str,
        run_id: int,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search logs for a GitHub Actions workflow run."""
        params = {"query": query} if query else None
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/github/{owner}/{repo}/runs/{run_id}/logs",
            f"Run '{run_id}' not found in repository '{owner}/{repo}'",
            "search GitHub logs",
            params,
        )

    def github_get_job_logs(self, owner: str, repo: str, job_id: int) -> Dict[str, Any]:
        """Get complete logs for a specific GitHub Actions job."""
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/github/{owner}/{repo}/jobs/{job_id}/logs",
            f"Job '{job_id}' not found in repository '{owner}/{repo}'",
            "get GitHub job logs",
        )

    def github_retry_run(self, owner: str, repo: str, run_id: int) -> Dict[str, Any]:
        """Retry a failed GitHub Actions workflow run."""
        return self._make_cicd_request(
            "POST",
            f"/api/cicd/github/{owner}/{repo}/runs/{run_id}/retry",
            f"Run '{run_id}' not found in repository '{owner}/{repo}'",
            "retry GitHub run",
        )

    def github_cancel_run(self, owner: str, repo: str, run_id: int) -> Dict[str, Any]:
        """Cancel a running or queued GitHub Actions workflow run."""
        return self._make_cicd_request(
            "POST",
            f"/api/cicd/github/{owner}/{repo}/runs/{run_id}/cancel",
            f"Run '{run_id}' not found in repository '{owner}/{repo}'",
            "cancel GitHub run",
        )

    # GitLab CI Methods

    def gitlab_list_pipelines(
        self,
        project_id: str,
        status: Optional[str] = None,
        ref: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List GitLab CI pipelines for a project."""
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status
        if ref:
            params["ref"] = ref
        if limit:
            params["limit"] = limit

        return self._make_cicd_request(
            "GET",
            f"/api/cicd/gitlab/{project_id}/pipelines",
            f"Project '{project_id}' not found",
            "list GitLab pipelines",
            params,
        )

    def gitlab_get_pipeline(self, project_id: str, pipeline_id: int) -> Dict[str, Any]:
        """Get details of a specific GitLab CI pipeline."""
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}",
            f"Pipeline '{pipeline_id}' not found in project '{project_id}'",
            "get GitLab pipeline",
        )

    def gitlab_search_logs(
        self,
        project_id: str,
        pipeline_id: int,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search logs for a GitLab CI pipeline."""
        params = {"query": query} if query else None
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/logs",
            f"Pipeline '{pipeline_id}' not found in project '{project_id}'",
            "search GitLab logs",
            params,
        )

    def gitlab_get_job_logs(self, project_id: str, job_id: int) -> Dict[str, Any]:
        """Get complete logs for a specific GitLab CI job."""
        return self._make_cicd_request(
            "GET",
            f"/api/cicd/gitlab/{project_id}/jobs/{job_id}/logs",
            f"Job '{job_id}' not found in project '{project_id}'",
            "get GitLab job logs",
        )

    def gitlab_retry_pipeline(
        self, project_id: str, pipeline_id: int
    ) -> Dict[str, Any]:
        """Retry a failed GitLab CI pipeline."""
        return self._make_cicd_request(
            "POST",
            f"/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/retry",
            f"Pipeline '{pipeline_id}' not found in project '{project_id}'",
            "retry GitLab pipeline",
        )

    def gitlab_cancel_pipeline(
        self, project_id: str, pipeline_id: int
    ) -> Dict[str, Any]:
        """Cancel a running or pending GitLab CI pipeline."""
        return self._make_cicd_request(
            "POST",
            f"/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/cancel",
            f"Pipeline '{pipeline_id}' not found in project '{project_id}'",
            "cancel GitLab pipeline",
        )
