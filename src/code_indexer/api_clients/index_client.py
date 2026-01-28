"""
Index API Client for CIDX Remote Index Management.

Story #656: Provides CLI remote mode access to index management operations.
Follows the same patterns as other API clients in the codebase.
"""

import logging
from typing import Any, Dict, List, NoReturn, Optional
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


class IndexAPIClient(CIDXRemoteAPIClient):
    """API client for index management operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize Index API client.

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

    def trigger(
        self,
        repository: str,
        clear: bool = False,
        index_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Trigger indexing for a repository.

        Args:
            repository: Repository alias to trigger indexing for
            clear: If True, clear existing indexes before re-indexing
            index_types: List of index types to build (semantic, fts, temporal, scip)
                        If None, builds all configured types

        Returns:
            Dictionary with job information

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {}
        if clear:
            data["clear"] = True
        if index_types:
            data["types"] = index_types

        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/index/{repository}/trigger",
                json=data if data else None,
            )

            if response.status_code in (200, 202):
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository}' not found", 404)
            else:
                self._handle_error_response(response, "trigger indexing")

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
            raise APIClientError(f"Unexpected error triggering indexing: {e}")

    def status(self, repository: str) -> Dict[str, Any]:
        """Get index status for a repository.

        Args:
            repository: Repository alias to get status for

        Returns:
            Dictionary with index status information

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                "GET", f"/api/v1/index/{repository}/status"
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository}' not found", 404)
            else:
                self._handle_error_response(response, "get index status")

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
            raise APIClientError(f"Unexpected error getting index status: {e}")

    def add_type(self, repository: str, index_type: str) -> Dict[str, Any]:
        """Add an index type to a repository.

        Args:
            repository: Repository alias to add index type to
            index_type: Type of index to add (semantic, fts, temporal, scip)

        Returns:
            Dictionary with operation result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {"type": index_type}

        try:
            response = self._authenticated_request(
                "POST", f"/api/v1/index/{repository}/add-type", json=data
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository}' not found", 404)
            elif response.status_code == 400:
                self._handle_error_response(response, "add index type")
            else:
                self._handle_error_response(response, "add index type")

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
            raise APIClientError(f"Unexpected error adding index type: {e}")

    def _handle_error_response(self, response: Any, operation: str) -> NoReturn:
        """Handle error responses from the API.

        Args:
            response: HTTP response object
            operation: Description of the operation for error messages

        Raises:
            APIClientError: With appropriate message based on status code
        """
        try:
            error_data = response.json()
            error_detail = error_data.get("detail", f"HTTP {response.status_code}")
        except (ValueError, KeyError):
            error_detail = f"HTTP {response.status_code}"

        raise APIClientError(
            f"Failed to {operation}: {error_detail}", response.status_code
        )
