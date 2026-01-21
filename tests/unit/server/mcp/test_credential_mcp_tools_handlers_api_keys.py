"""
Unit tests for Credential Management MCP Tools - API Key Handler Tests.

Story #743: Implement 10 new MCP tools for API key and MCP credential management.

TDD Approach: These tests are written FIRST before implementation.

Tests verify API key handler behavior with mocked dependencies.
"""

import pytest
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def normal_user():
    """Create a normal user for testing."""
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


# =============================================================================
# Handler Functional Tests - list_api_keys
# =============================================================================


class TestListAPIKeysHandler:
    """Tests for list_api_keys handler functionality."""

    @pytest.fixture
    def mock_user_manager(self):
        """Create mock user manager for testing handlers."""
        manager = MagicMock()
        manager.get_api_keys.return_value = [
            {
                "key_id": "key-123",
                "key_prefix": "cidx_sk_abc1",
                "name": "Test Key",
                "created_at": "2024-01-01T00:00:00Z",
                "last_used_at": None,
            }
        ]
        return manager

    @pytest.mark.asyncio
    async def test_list_api_keys_returns_success_true(
        self, normal_user, mock_user_manager
    ):
        """list_api_keys handler returns success=True on valid call."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.user_manager = mock_user_manager

            handler = HANDLER_REGISTRY["list_api_keys"]
            result = await handler({}, normal_user)

            # Parse MCP response format
            assert "content" in result
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    @pytest.mark.asyncio
    async def test_list_api_keys_returns_keys_array(
        self, normal_user, mock_user_manager
    ):
        """list_api_keys handler returns keys array."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.user_manager = mock_user_manager

            handler = HANDLER_REGISTRY["list_api_keys"]
            result = await handler({}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert "keys" in content
            assert isinstance(content["keys"], list)


# =============================================================================
# Handler Functional Tests - create_api_key
# =============================================================================


class TestCreateAPIKeyHandler:
    """Tests for create_api_key handler functionality."""

    @pytest.fixture
    def mock_api_key_manager(self):
        """Create mock API key manager for testing."""
        manager = MagicMock()
        manager.generate_key.return_value = ("cidx_sk_full_key_value", "key-uuid-123")
        return manager

    @pytest.mark.asyncio
    async def test_create_api_key_returns_success(
        self, normal_user, mock_api_key_manager
    ):
        """create_api_key handler returns success on valid creation."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.api_key_manager = mock_api_key_manager

            handler = HANDLER_REGISTRY["create_api_key"]
            result = await handler({"description": "Test key"}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    @pytest.mark.asyncio
    async def test_create_api_key_returns_key_id(
        self, normal_user, mock_api_key_manager
    ):
        """create_api_key handler returns key_id."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.api_key_manager = mock_api_key_manager

            handler = HANDLER_REGISTRY["create_api_key"]
            result = await handler({}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert "key_id" in content

    @pytest.mark.asyncio
    async def test_create_api_key_returns_full_api_key(
        self, normal_user, mock_api_key_manager
    ):
        """create_api_key handler returns full api_key (one-time display)."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.api_key_manager = mock_api_key_manager

            handler = HANDLER_REGISTRY["create_api_key"]
            result = await handler({}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert "api_key" in content


# =============================================================================
# Handler Functional Tests - delete_api_key
# =============================================================================


class TestDeleteAPIKeyHandler:
    """Tests for delete_api_key handler functionality."""

    @pytest.fixture
    def mock_user_manager(self):
        """Create mock user manager for testing handlers."""
        manager = MagicMock()
        manager.delete_api_key.return_value = True
        return manager

    @pytest.mark.asyncio
    async def test_delete_api_key_returns_success(
        self, normal_user, mock_user_manager
    ):
        """delete_api_key handler returns success=True on valid deletion."""
        with patch(
            "code_indexer.server.mcp.handlers.app_module"
        ) as mock_app:
            mock_app.user_manager = mock_user_manager

            handler = HANDLER_REGISTRY["delete_api_key"]
            result = await handler({"key_id": "key-123"}, normal_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    @pytest.mark.asyncio
    async def test_delete_api_key_missing_key_id_fails(self, normal_user):
        """delete_api_key handler fails when key_id is missing."""
        handler = HANDLER_REGISTRY["delete_api_key"]
        result = await handler({}, normal_user)

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False
        assert "error" in content
