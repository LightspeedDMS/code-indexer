"""
Unit tests for Story #275: handle_admin_list_system_mcp_credentials() MCP handler.

Tests are written FIRST following TDD methodology (red phase).
Minimal patching: only user_manager is replaced with a test double.
"""

import json
from datetime import datetime, timezone

import pytest


def _make_admin_user():
    """Create a User with admin role for handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="hash",
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_normal_user():
    """Create a User with normal_user role for handler tests."""
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="alice",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


_FAKE_SYSTEM_CREDS = [
    {
        "credential_id": "sys1",
        "client_id": "cli1",
        "client_id_prefix": "mcp1",
        "name": "cidx-local-auto",
        "created_at": "2024-01-01T00:00:00Z",
        "last_used_at": None,
        "owner": "admin (system)",
        "is_system": True,
    }
]


class TestHandleAdminListSystemMcpCredentials:
    """
    Tests for handle_admin_list_system_mcp_credentials() MCP handler.

    Story #275 AC4: Handler must require admin role, return system credentials
    with is_system=True, and follow existing _mcp_response handler conventions.
    """

    def test_returns_permission_denied_for_non_admin(self) -> None:
        """Non-admin user receives success=False with permission error."""
        from code_indexer.server.mcp.handlers import (
            handle_admin_list_system_mcp_credentials,
        )

        result = handle_admin_list_system_mcp_credentials({}, _make_normal_user())

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False
        error_lower = content["error"].lower()
        assert "permission" in error_lower or "denied" in error_lower

    def test_returns_permission_denied_for_power_user(self) -> None:
        """Power user also receives success=False (not admin)."""
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.mcp.handlers import (
            handle_admin_list_system_mcp_credentials,
        )

        power_user = User(
            username="bob",
            password_hash="hash",
            role=UserRole.POWER_USER,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        result = handle_admin_list_system_mcp_credentials({}, power_user)

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False

    def test_returns_system_credentials_for_admin(self) -> None:
        """Admin receives success=True with system_credentials list."""
        from code_indexer.server.mcp.handlers import (
            handle_admin_list_system_mcp_credentials,
        )
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return _FAKE_SYSTEM_CREDS

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManager()
        try:
            result = handle_admin_list_system_mcp_credentials({}, _make_admin_user())
            content = json.loads(result["content"][0]["text"])

            assert content["success"] is True
            assert "system_credentials" in content
            assert content["count"] == 1
            assert content["system_credentials"][0]["is_system"] is True
            assert content["system_credentials"][0]["name"] == "cidx-local-auto"
        finally:
            dep_module.user_manager = original

    def test_returns_empty_list_when_no_system_credentials(self) -> None:
        """Admin receives success=True with empty list when no system creds exist."""
        from code_indexer.server.mcp.handlers import (
            handle_admin_list_system_mcp_credentials,
        )
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManagerEmpty:
            def get_system_mcp_credentials(self):
                return []

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManagerEmpty()
        try:
            result = handle_admin_list_system_mcp_credentials({}, _make_admin_user())
            content = json.loads(result["content"][0]["text"])

            assert content["success"] is True
            assert content["system_credentials"] == []
            assert content["count"] == 0
        finally:
            dep_module.user_manager = original

    def test_response_is_mcp_compliant_content_array(self) -> None:
        """Response must wrap data in MCP content array (content[0].type='text')."""
        from code_indexer.server.mcp.handlers import (
            handle_admin_list_system_mcp_credentials,
        )
        from code_indexer.server.auth import dependencies as dep_module

        class FakeUserManager:
            def get_system_mcp_credentials(self):
                return []

        original = dep_module.user_manager
        dep_module.user_manager = FakeUserManager()
        try:
            result = handle_admin_list_system_mcp_credentials({}, _make_admin_user())

            assert "content" in result, "Response must have 'content' key"
            assert isinstance(result["content"], list)
            assert len(result["content"]) == 1
            assert result["content"][0]["type"] == "text"
            # text must be valid JSON
            json.loads(result["content"][0]["text"])
        finally:
            dep_module.user_manager = original

    def test_handler_is_registered_in_handler_registry(self) -> None:
        """HANDLER_REGISTRY must contain 'admin_list_system_mcp_credentials'."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_list_system_mcp_credentials" in HANDLER_REGISTRY, (
            "Handler 'admin_list_system_mcp_credentials' not found in HANDLER_REGISTRY"
        )
