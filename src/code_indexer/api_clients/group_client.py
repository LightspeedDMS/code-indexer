"""
Group API Client for CIDX Server Integration.

Provides group and access management functionality via MCP tools.
Calls MCP group management tools via the /mcp JSON-RPC endpoint.
Follows anti-mock principles with real API integration.

Story #747: Group & Access Management from CLI Remote Mode
"""

import json
import logging
from typing import Dict, Any, Optional, List
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


class GroupAPIClient(CIDXRemoteAPIClient):
    """Client for group management operations via MCP tools."""

    def __init__(
        self,
        server_url: str,
        credentials: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """Initialize Group API client.

        Args:
            server_url: Base URL of the CIDX server
            credentials: Credentials dictionary (must have admin role for write ops)
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

    def list_groups(self) -> Dict[str, Any]:
        """List all groups with member counts and repository access.

        Returns:
            Dictionary with groups list

        Raises:
            APIClientError: If API request fails
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool("list_groups", {})

        if not result.get("success", False):
            error = result.get("error", "Unknown error")
            raise APIClientError(f"Failed to list groups: {error}")

        return {"groups": result.get("groups", [])}

    def create_group(self, name: str, description: str = "") -> Dict[str, Any]:
        """Create a new custom group.

        Args:
            name: Group name (required)
            description: Group description (optional)

        Returns:
            Dictionary with group_id and name

        Raises:
            APIClientError: If creation fails
            AuthenticationError: If authentication fails or permission denied
        """
        if not name:
            raise ValueError("Group name is required")

        result = self._call_mcp_tool(
            "create_group", {"name": name, "description": description}
        )

        if not result.get("success", False):
            raise APIClientError(
                f"Failed to create group: {result.get('error', 'Unknown error')}"
            )

        return {"group_id": result.get("group_id"), "name": result.get("name")}

    def get_group(self, group_id: int) -> Dict[str, Any]:
        """Get detailed information about a specific group.

        Args:
            group_id: The unique identifier of the group

        Returns:
            Dictionary with group details (id, name, description, members, repos)

        Raises:
            APIClientError: If group not found or request fails
            AuthenticationError: If authentication fails
        """
        result = self._call_mcp_tool("get_group", {"group_id": str(group_id)})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            raise APIClientError(f"Failed to get group: {error_msg}")

        return {
            "id": result.get("id"),
            "name": result.get("name"),
            "description": result.get("description"),
            "members": result.get("members", []),
            "repos": result.get("repos", []),
        }

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a custom group's name and/or description.

        Args:
            group_id: The unique identifier of the group
            name: New group name (optional)
            description: New group description (optional)

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If group not found or request fails
            AuthenticationError: If authentication fails or permission denied
        """
        arguments: Dict[str, Any] = {"group_id": str(group_id)}
        if name is not None:
            arguments["name"] = name
        if description is not None:
            arguments["description"] = description

        result = self._call_mcp_tool("update_group", arguments)

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            raise APIClientError(f"Failed to update group: {error_msg}")

        return {"success": True}

    def delete_group(self, group_id: int) -> Dict[str, Any]:
        """Delete a custom group.

        Args:
            group_id: The unique identifier of the group

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If group not found, is system group, or has users
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool("delete_group", {"group_id": str(group_id)})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            if (
                "cannot be deleted" in error_msg.lower()
                or "default" in error_msg.lower()
            ):
                raise APIClientError(f"System group cannot be deleted: {error_msg}")
            raise APIClientError(f"Failed to delete group: {error_msg}")

        return {"success": True}

    def add_member(self, group_id: int, user_id: str) -> Dict[str, Any]:
        """Assign a user to a group.

        Args:
            group_id: The unique identifier of the group
            user_id: Username of the user to add

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If group not found or request fails
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "add_member_to_group", {"group_id": str(group_id), "user_id": user_id}
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            raise APIClientError(f"Failed to add member: {error_msg}")

        return {"success": True}

    def add_repos(self, group_id: int, repo_names: List[str]) -> Dict[str, Any]:
        """Grant a group access to one or more repositories.

        Args:
            group_id: The unique identifier of the group
            repo_names: List of repository names to add

        Returns:
            Dictionary with success status and added_count

        Raises:
            APIClientError: If group not found or request fails
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "add_repos_to_group", {"group_id": str(group_id), "repo_names": repo_names}
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            raise APIClientError(f"Failed to add repos: {error_msg}")

        return {"success": True, "added_count": result.get("added_count", 0)}

    def remove_repo(self, group_id: int, repo_name: str) -> Dict[str, Any]:
        """Revoke a group's access to a single repository.

        Args:
            group_id: The unique identifier of the group
            repo_name: Repository name to remove

        Returns:
            Dictionary with success status

        Raises:
            APIClientError: If group/repo not found or cidx-meta removal attempted
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "remove_repo_from_group",
            {"group_id": str(group_id), "repo_name": repo_name},
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            if "cidx-meta" in error_msg.lower():
                raise APIClientError(f"cidx-meta access cannot be revoked: {error_msg}")
            raise APIClientError(f"Failed to remove repo: {error_msg}")

        return {"success": True}

    def remove_repos(self, group_id: int, repo_names: List[str]) -> Dict[str, Any]:
        """Revoke a group's access to multiple repositories.

        Args:
            group_id: The unique identifier of the group
            repo_names: List of repository names to remove

        Returns:
            Dictionary with success status and removed_count

        Raises:
            APIClientError: If group not found or request fails
            AuthenticationError: If authentication fails or permission denied
        """
        result = self._call_mcp_tool(
            "bulk_remove_repos_from_group",
            {"group_id": str(group_id), "repo_names": repo_names},
        )

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            if "not found" in error_msg.lower():
                raise APIClientError(f"Group not found: {group_id}", 404)
            raise APIClientError(f"Failed to remove repos: {error_msg}")

        return {"success": True, "removed_count": result.get("removed_count", 0)}
