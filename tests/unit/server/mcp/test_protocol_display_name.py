"""
Unit tests for MCP protocol serverInfo.name configuration (Story #22).

Tests verify that:
1. AC1: Default display name "Neo" appears in serverInfo.name
2. AC2: Custom display name appears in serverInfo.name
3. AC5: Empty display name falls back to "Neo"

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from code_indexer.server.mcp.protocol import process_jsonrpc_request
from code_indexer.server.auth.user_manager import User, UserRole


class TestMCPServerInfoDisplayName:
    """Test suite for MCP protocol serverInfo.name configuration (Story #22)."""

    @pytest.fixture
    def test_user(self):
        """Create a test user for MCP protocol tests."""
        return User(
            username="test",
            password_hash="hashed_password",
            role=UserRole.POWER_USER,
            created_at=datetime.now(),
        )

    @pytest.fixture
    def initialize_request(self):
        """Create a standard MCP initialize request."""
        return {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "TestClient", "version": "1.0.0"},
            },
            "id": "init-1",
        }

    @pytest.mark.asyncio
    async def test_initialize_returns_default_display_name(
        self, test_user, initialize_request
    ):
        """AC1: Fresh installation should return 'Neo' as serverInfo.name."""
        mock_config = MagicMock()
        mock_config.service_display_name = "Neo"

        with patch(
            "code_indexer.server.mcp.protocol.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            response = await process_jsonrpc_request(initialize_request, test_user)

        assert response["jsonrpc"] == "2.0"
        assert "result" in response

        result = response["result"]
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "Neo"

    @pytest.mark.asyncio
    async def test_initialize_returns_custom_display_name(
        self, test_user, initialize_request
    ):
        """AC2: Custom display name should appear in serverInfo.name."""
        mock_config = MagicMock()
        mock_config.service_display_name = "MyBrand"

        with patch(
            "code_indexer.server.mcp.protocol.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            response = await process_jsonrpc_request(initialize_request, test_user)

        result = response["result"]
        assert result["serverInfo"]["name"] == "MyBrand"

    @pytest.mark.asyncio
    async def test_initialize_empty_display_name_fallback(
        self, test_user, initialize_request
    ):
        """AC5: Empty display name should fall back to 'Neo'."""
        mock_config = MagicMock()
        mock_config.service_display_name = ""

        with patch(
            "code_indexer.server.mcp.protocol.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            response = await process_jsonrpc_request(initialize_request, test_user)

        result = response["result"]
        assert result["serverInfo"]["name"] == "Neo"

    @pytest.mark.asyncio
    async def test_initialize_preserves_version_field(
        self, test_user, initialize_request
    ):
        """serverInfo.version should still contain the CIDX version."""
        from code_indexer import __version__

        mock_config = MagicMock()
        mock_config.service_display_name = "CustomName"

        with patch(
            "code_indexer.server.mcp.protocol.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            response = await process_jsonrpc_request(initialize_request, test_user)

        result = response["result"]
        assert result["serverInfo"]["version"] == __version__
