"""
Unit tests for trigger_dependency_analysis handler implementation.

Tests Bug 2 and Bug 3 fixes:
- Bug 2: Handler should access dependency_map_service from app.state
- Bug 2: Handler should load config via ServerConfigManager, not app_module.config
- Bug 3: Handler should not import non-existent get_golden_repo_manager function
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
from code_indexer.server.mcp.handlers import handle_trigger_dependency_analysis
from code_indexer.server.auth.user_manager import User, UserRole


def unwrap_mcp_response(mcp_response):
    """Unwrap MCP response to get actual data."""
    return json.loads(mcp_response["content"][0]["text"])


@pytest.fixture
def admin_user():
    """Create admin user for testing."""
    return User(
        username="admin",
        password_hash="dummy_hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_app_state():
    """Mock app.state with dependency_map_service."""
    mock_service = Mock()
    mock_service.is_available.return_value = True
    mock_service.run_full_analysis = Mock()
    mock_service.run_delta_analysis = Mock()

    return mock_service


def test_handler_accesses_dependency_map_service_from_app_state(admin_user, mock_app_state):
    """Test that handler gets dependency_map_service from app.state, not module level."""
    # Arrange
    with patch('code_indexer.server.mcp.handlers.app_module') as mock_app_module:
        # Set up app.state (correct pattern)
        mock_app_module.app.state.dependency_map_service = mock_app_state

        # Mock config loading via ServerConfigManager
        with patch('code_indexer.server.utils.config_manager.ServerConfigManager') as mock_scm:
            mock_config = Mock()
            mock_config.claude_integration_config.dependency_map_enabled = True
            mock_scm.return_value.load_config.return_value = mock_config

            # Act
            mcp_result = handle_trigger_dependency_analysis({"mode": "delta"}, admin_user)
            result = unwrap_mcp_response(mcp_result)

    # Assert - service was accessed from app.state
    assert result["success"] is True
    assert "job_id" in result


def test_handler_loads_config_via_server_config_manager(admin_user, mock_app_state):
    """Test that handler loads config via ServerConfigManager, not app_module.config."""
    # Arrange
    with patch('code_indexer.server.mcp.handlers.app_module') as mock_app_module:
        mock_app_module.app.state.dependency_map_service = mock_app_state

        # Mock ServerConfigManager (correct pattern)
        with patch('code_indexer.server.utils.config_manager.ServerConfigManager') as mock_scm:
            mock_config = Mock()
            mock_config.claude_integration_config.dependency_map_enabled = True
            mock_scm_instance = mock_scm.return_value
            mock_scm_instance.load_config.return_value = mock_config

            # Act
            mcp_result = handle_trigger_dependency_analysis({"mode": "delta"}, admin_user)
            result = unwrap_mcp_response(mcp_result)

            # Assert - ServerConfigManager was used to load config
            mock_scm.assert_called_once()
            mock_scm_instance.load_config.assert_called_once()


def test_handler_returns_error_when_feature_disabled(admin_user, mock_app_state):
    """Test that handler checks dependency_map_enabled from config."""
    # Arrange
    with patch('code_indexer.server.mcp.handlers.app_module') as mock_app_module:
        mock_app_module.app.state.dependency_map_service = mock_app_state

        with patch('code_indexer.server.utils.config_manager.ServerConfigManager') as mock_scm:
            mock_config = Mock()
            mock_config.claude_integration_config.dependency_map_enabled = False  # Disabled
            mock_scm.return_value.load_config.return_value = mock_config

            # Act
            mcp_result = handle_trigger_dependency_analysis({"mode": "delta"}, admin_user)
            result = unwrap_mcp_response(mcp_result)

    # Assert
    assert result["success"] is False
    assert "disabled" in result["error"].lower()


def test_handler_returns_error_when_service_unavailable(admin_user):
    """Test that handler handles missing dependency_map_service gracefully."""
    # Arrange
    with patch('code_indexer.server.mcp.handlers.app_module') as mock_app_module:
        # Service not available on app.state
        mock_app_module.app.state.dependency_map_service = None

        with patch('code_indexer.server.utils.config_manager.ServerConfigManager') as mock_scm:
            mock_config = Mock()
            mock_config.claude_integration_config.dependency_map_enabled = True
            mock_scm.return_value.load_config.return_value = mock_config

            # Act
            mcp_result = handle_trigger_dependency_analysis({"mode": "delta"}, admin_user)
            result = unwrap_mcp_response(mcp_result)

    # Assert
    assert result["success"] is False
    assert "not available" in result["error"].lower()
