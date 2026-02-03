"""
Unit tests for quick_reference handler a.k.a. line (Story #22 AC3).

Tests verify that:
1. AC3: Quick reference shows "This server is CIDX (a.k.a. {name})." line
2. The a.k.a. line contains the configured display name
3. Empty display name falls back to "Neo" in a.k.a. line

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from code_indexer.server.mcp.handlers import quick_reference
from code_indexer.server.auth.user_manager import User, UserRole


def _extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON data from MCP-compliant content array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


class TestQuickReferenceDisplayName:
    """Test suite for quick_reference a.k.a. line (Story #22 AC3)."""

    @pytest.fixture
    def test_user(self):
        """Create a test user with query permissions."""
        return User(
            username="test",
            password_hash="hashed_password",
            role=UserRole.POWER_USER,
            created_at=datetime.now(),
        )

    def test_quick_reference_includes_aka_line(self, test_user):
        """AC3: Quick reference should include a.k.a. line with display name."""
        mock_config = MagicMock()
        mock_config.service_display_name = "Neo"

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            mcp_response = quick_reference({}, test_user)

        result = _extract_mcp_data(mcp_response)
        assert result["success"] is True
        assert "server_identity" in result
        assert result["server_identity"] == "This server is CIDX (a.k.a. Neo)."

    def test_quick_reference_aka_with_custom_name(self, test_user):
        """AC3: a.k.a. line should use configured custom display name."""
        mock_config = MagicMock()
        mock_config.service_display_name = "ProductionServer"

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            mcp_response = quick_reference({}, test_user)

        result = _extract_mcp_data(mcp_response)
        assert (
            result["server_identity"]
            == "This server is CIDX (a.k.a. ProductionServer)."
        )

    def test_quick_reference_aka_empty_name_fallback(self, test_user):
        """AC3/AC5: Empty display name should fall back to 'Neo' in a.k.a. line."""
        mock_config = MagicMock()
        mock_config.service_display_name = ""

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            mcp_response = quick_reference({}, test_user)

        result = _extract_mcp_data(mcp_response)
        assert result["server_identity"] == "This server is CIDX (a.k.a. Neo)."
