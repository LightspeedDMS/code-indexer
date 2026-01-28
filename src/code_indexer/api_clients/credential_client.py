"""
Credential API Client for CIDX Server Integration.

Provides API key and MCP credential management functionality via MCP tools.
Calls MCP credential management tools via the /mcp JSON-RPC endpoint.
Follows anti-mock principles with real API integration.

Story #748: Credential Management from CLI Remote Mode
"""

import json
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from .base_client import (
    CIDXRemoteAPIClient,
    APIClientError,
    AuthenticationError,
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


class CredentialAPIClient(CIDXRemoteAPIClient):
    """Client for credential management operations via MCP tools."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize Credential API client.

        Args:
            server_url: Base URL of the CIDX server
            credentials: Credentials dictionary
            project_root: Project root for persistent token storage
        """
        super().__init__(
            server_url=server_url,
            credentials=credentials,
            project_root=project_root,
        )

    def _parse_mcp_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Parse JSON-RPC response and extract MCP tool result.

        Args:
            result: JSON-RPC response dictionary

        Returns:
            Parsed tool response data

        Raises:
            APIClientError: If MCP tool returns error
            AuthenticationError: If permission denied
        """
        if "error" in result:
            error_msg = result["error"].get("message", "Unknown MCP error")
            if "permission" in error_msg.lower():
                raise AuthenticationError(f"Permission denied: {error_msg}")
            raise APIClientError(f"MCP tool error: {error_msg}")

        mcp_result = result.get("result", {})

        if "content" in mcp_result:
            content = mcp_result["content"]
            if content and len(content) > 0:
                text_content = content[0].get("text", "{}")
                return dict(json.loads(text_content))

        return dict(mcp_result)

    def _call_mcp_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call an MCP tool via the /mcp JSON-RPC endpoint.

        Args:
            tool_name: Name of the MCP tool to call
            arguments: Arguments to pass to the tool

        Returns:
            Dictionary with tool response data

        Raises:
            APIClientError: If tool call fails
            AuthenticationError: If authentication fails or permission denied
        """
        jsonrpc_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
            "id": 1,
        }

        try:
            response = self._authenticated_request("POST", "/mcp", json=jsonrpc_request)

            if response.status_code == 200:
                return self._parse_mcp_response(response.json())
            elif response.status_code == 401:
                raise AuthenticationError("Authentication failed")
            elif response.status_code == 403:
                raise AuthenticationError("Permission denied for MCP tool call")
            else:
                error_detail = f"HTTP {response.status_code}"
                try:
                    error_data = response.json()
                    error_detail = error_data.get("detail", error_detail)
                except (ValueError, json.JSONDecodeError):
                    pass
                raise APIClientError(f"MCP call failed: {error_detail}")

        except (
            APIClientError,
            AuthenticationError,
            NetworkConnectionError,
            NetworkTimeoutError,
            DNSResolutionError,
            SSLCertificateError,
            ServerError,
            RateLimitError,
        ):
            raise
        except Exception as e:
            raise APIClientError(f"Unexpected error calling MCP tool: {e}")

    # =============================================================================
    # API Key Methods (3 methods)
    # =============================================================================

    def list_api_keys(self) -> Dict[str, Any]:
        """List all API keys for the current user.

        Returns:
            Dictionary with api_keys list

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool("list_api_keys", {})

        if not result.get("success", False):
            error = result.get("error", "Unknown error")
            raise APIClientError(f"Failed to list API keys: {error}")

        return {"api_keys": result.get("api_keys", [])}

    def create_api_key(self, description: str = "") -> Dict[str, Any]:
        """Create a new API key for the current user.

        Args:
            description: Description for the API key (optional)

        Returns:
            Dictionary with key_id and secret (secret shown ONE TIME only)

        Raises:
            APIClientError: If creation fails
            AuthenticationError: If authentication fails
        """
        arguments: Dict[str, Any] = {}
        if description:
            arguments["description"] = description

        result = self._call_mcp_tool("create_api_key", arguments)

        if not result.get("success", False):
            raise APIClientError(
                f"Failed to create API key: {result.get('error', 'Unknown error')}"
            )

        return {
            "key_id": result.get("key_id"),
            "secret": result.get("secret"),
            "description": result.get("description", ""),
        }

    def delete_api_key(self, key_id: str) -> Dict[str, Any]:
        """Delete an API key.

        Args:
            key_id: The ID of the API key to delete

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If deletion fails or key not found
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool("delete_api_key", {"key_id": key_id})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"API key not found: {key_id}", 404)
            raise APIClientError(f"Failed to delete API key: {error_msg}")

        return {"success": True}

    # =============================================================================
    # MCP Credential Methods - User Self-Service (3 methods)
    # =============================================================================

    def list_mcp_credentials(self) -> Dict[str, Any]:
        """List all MCP credentials for the current user.

        Returns:
            Dictionary with credentials list

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool("list_mcp_credentials", {})

        if not result.get("success", False):
            error = result.get("error", "Unknown error")
            raise APIClientError(f"Failed to list MCP credentials: {error}")

        return {"credentials": result.get("credentials", [])}

    def create_mcp_credential(self, description: str = "") -> Dict[str, Any]:
        """Create a new MCP credential for the current user.

        Args:
            description: Description for the credential (optional)

        Returns:
            Dictionary with credential_id and secret (secret shown ONE TIME only)

        Raises:
            APIClientError: If creation fails
            AuthenticationError: If authentication fails
        """
        arguments: Dict[str, Any] = {}
        if description:
            arguments["description"] = description

        result = self._call_mcp_tool("create_mcp_credential", arguments)

        if not result.get("success", False):
            raise APIClientError(
                f"Failed to create MCP credential: {result.get('error', 'Unknown error')}"
            )

        return {
            "credential_id": result.get("credential_id"),
            "secret": result.get("secret"),
            "description": result.get("description", ""),
        }

    def delete_mcp_credential(self, credential_id: str) -> Dict[str, Any]:
        """Delete an MCP credential.

        Args:
            credential_id: The ID of the credential to delete

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If deletion fails or credential not found
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool(
            "delete_mcp_credential", {"credential_id": credential_id}
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"MCP credential not found: {credential_id}", 404)
            raise APIClientError(f"Failed to delete MCP credential: {error_msg}")

        return {"success": True}

    # =============================================================================
    # Admin MCP Credential Methods (4 methods)
    # =============================================================================

    def admin_list_user_mcp_credentials(self, username: str) -> Dict[str, Any]:
        """List all MCP credentials for a specific user (Admin only).

        Args:
            username: Username whose credentials to list

        Returns:
            Dictionary with credentials list

        Raises:
            APIClientError: If API request fails or user not found
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "admin_list_user_mcp_credentials", {"username": username}
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"User not found: {username}", 404)
            raise APIClientError(f"Failed to list user MCP credentials: {error_msg}")

        return {"credentials": result.get("credentials", [])}

    def admin_create_user_mcp_credential(
        self, username: str, description: str = ""
    ) -> Dict[str, Any]:
        """Create a new MCP credential for a specific user (Admin only).

        Args:
            username: Username for whom to create the credential
            description: Description for the credential (optional)

        Returns:
            Dictionary with credential_id and secret (secret shown ONE TIME only)

        Raises:
            APIClientError: If creation fails or user not found
            AuthenticationError: If authentication fails or permission denied
        """
        arguments: Dict[str, Any] = {"username": username}
        if description:
            arguments["description"] = description

        result = self._call_mcp_tool("admin_create_user_mcp_credential", arguments)

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"User not found: {username}", 404)
            raise APIClientError(f"Failed to create user MCP credential: {error_msg}")

        return {
            "credential_id": result.get("credential_id"),
            "secret": result.get("secret"),
            "description": result.get("description", ""),
        }

    def admin_delete_user_mcp_credential(
        self, username: str, credential_id: str
    ) -> Dict[str, Any]:
        """Delete an MCP credential for a specific user (Admin only).

        Args:
            username: Username whose credential to delete
            credential_id: The ID of the credential to delete

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If deletion fails, user not found, or credential not found
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "admin_delete_user_mcp_credential",
            {"username": username, "credential_id": credential_id},
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                if "user" in error_msg.lower():
                    raise APIClientError(f"User not found: {username}", 404)
                raise APIClientError(f"MCP credential not found: {credential_id}", 404)
            raise APIClientError(f"Failed to delete user MCP credential: {error_msg}")

        return {"success": True}

    def admin_list_all_mcp_credentials(self) -> Dict[str, Any]:
        """List all MCP credentials across all users (Admin only).

        Returns:
            Dictionary with credentials list grouped by user

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool("admin_list_all_mcp_credentials", {})

        if not result.get("success", False):
            error = result.get("error", "Unknown error")
            raise APIClientError(f"Failed to list all MCP credentials: {error}")

        return {"credentials": result.get("credentials", [])}
