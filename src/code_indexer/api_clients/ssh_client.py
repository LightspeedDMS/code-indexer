"""
SSH API Client for CIDX Remote SSH Key Management.

Story #656: Provides CLI remote mode access to SSH key management operations.
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


class SSHAPIClient(CIDXRemoteAPIClient):
    """API client for SSH key management operations."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize SSH API client.

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

    async def create_key(
        self,
        name: str,
        email: str,
        key_type: str = "ed25519",
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new SSH key.

        Args:
            name: Unique name for the SSH key
            email: Email address to associate with the key
            key_type: Key type (ed25519 or rsa), default: ed25519
            description: Optional description for the key

        Returns:
            Dictionary with created key information including public key

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {
            "name": name,
            "email": email,
            "key_type": key_type,
        }
        if description:
            data["description"] = description

        try:
            response = await self._authenticated_request(
                "POST", "/api/v1/ssh/keys", json=data
            )

            if response.status_code in (200, 201):
                return dict(response.json())
            else:
                self._handle_error_response(response, "create SSH key")

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
            raise APIClientError(f"Unexpected error creating SSH key: {e}")

    async def list_keys(self) -> Dict[str, Any]:
        """List all SSH keys.

        Returns:
            Dictionary with list of SSH keys

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = await self._authenticated_request("GET", "/api/v1/ssh/keys")

            if response.status_code == 200:
                return dict(response.json())
            else:
                self._handle_error_response(response, "list SSH keys")

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
            raise APIClientError(f"Unexpected error listing SSH keys: {e}")

    async def delete_key(self, name: str) -> Dict[str, Any]:
        """Delete an SSH key.

        Args:
            name: Name of the SSH key to delete

        Returns:
            Dictionary with deletion result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = await self._authenticated_request(
                "DELETE", f"/api/v1/ssh/keys/{name}"
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"SSH key '{name}' not found", 404)
            else:
                self._handle_error_response(response, "delete SSH key")

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
            raise APIClientError(f"Unexpected error deleting SSH key: {e}")

    async def show_public_key(self, name: str) -> Dict[str, Any]:
        """Show the public key for an SSH key.

        Args:
            name: Name of the SSH key

        Returns:
            Dictionary with public key information

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        try:
            response = await self._authenticated_request(
                "GET", f"/api/v1/ssh/keys/{name}"
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"SSH key '{name}' not found", 404)
            else:
                self._handle_error_response(response, "show SSH public key")

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
            raise APIClientError(f"Unexpected error showing SSH public key: {e}")

    async def assign_key(
        self,
        name: str,
        hostname: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Assign an SSH key to a hostname.

        Args:
            name: Name of the SSH key to assign
            hostname: Hostname to assign the key to (e.g., github.com)
            force: If True, replace any existing assignment

        Returns:
            Dictionary with assignment result

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
            NetworkError: If network request fails
        """
        data: Dict[str, Any] = {
            "hostname": hostname,
            "force": force,
        }

        try:
            response = await self._authenticated_request(
                "POST", f"/api/v1/ssh/keys/{name}/assign", json=data
            )

            if response.status_code == 200:
                return dict(response.json())
            elif response.status_code == 404:
                raise APIClientError(f"SSH key '{name}' not found", 404)
            else:
                self._handle_error_response(response, "assign SSH key")

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
            raise APIClientError(f"Unexpected error assigning SSH key: {e}")

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
