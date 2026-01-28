"""
File API Client for CIDX Remote File Operations.

Story #738: Provides CLI remote mode access to file CRUD operations.
Follows the same patterns as other API clients in the codebase.
"""

import logging
import urllib.parse
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


class FileAPIClient(CIDXRemoteAPIClient):
    """API client for File CRUD operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize File API client.

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

    def create_file(
        self,
        repository_alias: str,
        file_path: str,
        content: str,
    ) -> Dict[str, Any]:
        """Create a new file in the repository.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file within repository
            content: File content

        Returns:
            Dictionary with file metadata (success, file_path, content_hash, etc.)

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = self._authenticated_request(
                "POST",
                f"/api/v1/repos/{repository_alias}/files",
                json={"file_path": file_path, "content": content},
            )

            if response.status_code == 201:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"Repository '{repository_alias}' not found", 404)
            elif response.status_code == 409:
                error_detail = self._extract_error_detail(response)
                raise APIClientError(f"File already exists: {error_detail}", 409)
            else:
                self._handle_error_response(response, "create file")

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
            raise APIClientError(f"Unexpected error creating file: {e}")

    def edit_file(
        self,
        repository_alias: str,
        file_path: str,
        old_string: str,
        new_string: str,
        content_hash: Optional[str] = None,
        replace_all: bool = False,
    ) -> Dict[str, Any]:
        """Edit a file using string replacement with optimistic locking.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file within repository
            old_string: String to search for in file
            new_string: String to replace with
            content_hash: Expected content hash for verification (optimistic locking)
            replace_all: Replace all occurrences (default: first only)

        Returns:
            Dictionary with updated file metadata

        Raises:
            APIClientError: If API request fails (including hash mismatch 409)
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        # URL-encode the file path for the URL
        encoded_path = urllib.parse.quote(file_path, safe="")

        # Build request data
        data: Dict[str, Any] = {
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
        if content_hash is not None:
            data["content_hash"] = content_hash

        try:
            response = self._authenticated_request(
                "PATCH",
                f"/api/v1/repos/{repository_alias}/files/{encoded_path}",
                json=data,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(
                    f"Repository or file not found: {repository_alias}/{file_path}",
                    404,
                )
            elif response.status_code == 409:
                error_detail = self._extract_error_detail(response)
                raise APIClientError(
                    f"Hash mismatch - file was modified: {error_detail}", 409
                )
            else:
                self._handle_error_response(response, "edit file")

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
            raise APIClientError(f"Unexpected error editing file: {e}")

    def delete_file(
        self,
        repository_alias: str,
        file_path: str,
        content_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a file from the repository.

        Args:
            repository_alias: Repository alias identifier
            file_path: Path to the file within repository
            content_hash: Expected content hash for verification (optimistic locking)

        Returns:
            Dictionary with deletion confirmation

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        # URL-encode the file path for the URL
        encoded_path = urllib.parse.quote(file_path, safe="")

        # Build query parameters
        params: Optional[Dict[str, str]] = None
        if content_hash is not None:
            params = {"content_hash": content_hash}

        try:
            response = self._authenticated_request(
                "DELETE",
                f"/api/v1/repos/{repository_alias}/files/{encoded_path}",
                params=params,
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(
                    f"Repository or file not found: {repository_alias}/{file_path}",
                    404,
                )
            else:
                self._handle_error_response(response, "delete file")

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
            raise APIClientError(f"Unexpected error deleting file: {e}")

    def _extract_error_detail(self, response: Any) -> str:
        """Extract error detail from response JSON."""
        try:
            error_data = response.json()
            return str(error_data.get("detail", f"HTTP {response.status_code}"))
        except (ValueError, KeyError):
            return f"HTTP {response.status_code}"

    def _handle_error_response(self, response: Any, operation: str) -> NoReturn:
        """Handle error responses from the API.

        Args:
            response: HTTP response object
            operation: Description of the operation for error messages

        Raises:
            APIClientError: With appropriate message based on status code
        """
        error_detail = self._extract_error_detail(response)
        raise APIClientError(
            f"Failed to {operation}: {error_detail}", response.status_code
        )
