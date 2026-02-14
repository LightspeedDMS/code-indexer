"""
Unit tests for trigger_dependency_analysis MCP tool handler (Story #195).

Tests cover all 8 acceptance criteria incrementally:
AC1: MCP Tool Registration
AC2: Full Analysis Mode
AC3: Delta Analysis Mode
AC4: Default Mode
"""

import json
import pytest
import time
from unittest.mock import Mock, patch
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole

# Background thread wait time for async operations
BACKGROUND_THREAD_WAIT_SECONDS = 0.5


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user for testing."""
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_dependency_map_service():
    """Create a mock DependencyMapService."""
    service = Mock()
    service.is_available = Mock(return_value=True)
    service.run_full_analysis = Mock(return_value={"status": "completed", "domains_count": 5})
    service.run_delta_analysis = Mock(return_value={"status": "completed", "domains_count": 5})
    return service


class TestAC1_ToolRegistration:
    """AC1: MCP Tool Registration."""

    def test_tool_registered_in_registry(self):
        """Verify trigger_dependency_analysis is registered in TOOL_REGISTRY."""
        assert "trigger_dependency_analysis" in TOOL_REGISTRY, \
            "trigger_dependency_analysis tool not found in TOOL_REGISTRY"

    def test_tool_has_proper_schema(self):
        """Verify tool has proper inputSchema with mode parameter."""
        tool = TOOL_REGISTRY["trigger_dependency_analysis"]

        # Check required fields
        assert "inputSchema" in tool
        assert "required_permission" in tool
        assert "description" in tool

        # Check schema structure
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "mode" in schema["properties"]

        # Check mode parameter
        mode_prop = schema["properties"]["mode"]
        assert mode_prop["type"] == "string"
        assert "enum" in mode_prop
        assert set(mode_prop["enum"]) == {"full", "delta"}
        assert mode_prop.get("default") == "delta"

    def test_tool_requires_admin_permission(self):
        """Verify tool requires manage_repos permission."""
        tool = TOOL_REGISTRY["trigger_dependency_analysis"]
        assert tool["required_permission"] == "manage_repos"

    def test_handler_registered(self):
        """Verify handler function is registered."""
        assert "trigger_dependency_analysis" in HANDLER_REGISTRY, \
            "trigger_dependency_analysis handler not found in HANDLER_REGISTRY"


class TestAC2_FullAnalysisMode:
    """AC2: Full Analysis Mode."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_full_mode_returns_job_id(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test full mode returns job_id immediately."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "full"}, mock_admin_user)

        # Verify response structure
        assert "content" in response
        assert len(response["content"]) == 1
        assert response["content"][0]["type"] == "text"

        # Parse response data
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is True
        assert "job_id" in data
        assert data["job_id"] is not None
        assert "mode" in data
        assert data["mode"] == "full"
        assert "status" in data

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_full_mode_calls_run_full_analysis(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that background job calls run_full_analysis."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        handler({"mode": "full"}, mock_admin_user)

        # Give background thread time to execute
        time.sleep(BACKGROUND_THREAD_WAIT_SECONDS)

        # Verify run_full_analysis was called
        mock_dependency_map_service.run_full_analysis.assert_called_once()


class TestAC3_DeltaAnalysisMode:
    """AC3: Delta Analysis Mode."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_delta_mode_returns_job_id(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test delta mode returns job_id immediately."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "delta"}, mock_admin_user)

        # Verify response structure
        assert "content" in response
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is True
        assert data["job_id"] is not None
        assert data["mode"] == "delta"

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_delta_mode_calls_run_delta_analysis(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that background job calls run_delta_analysis."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        handler({"mode": "delta"}, mock_admin_user)

        # Give background thread time to execute
        time.sleep(BACKGROUND_THREAD_WAIT_SECONDS)

        # Verify run_delta_analysis was called
        mock_dependency_map_service.run_delta_analysis.assert_called_once()


class TestAC4_DefaultMode:
    """AC4: Default Mode."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_no_mode_defaults_to_delta(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that omitting mode parameter defaults to delta."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler without mode
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({}, mock_admin_user)

        # Verify delta mode is used
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is True
        assert data["mode"] == "delta"

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_empty_mode_defaults_to_delta(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that empty mode parameter defaults to delta."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler with empty mode
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": ""}, mock_admin_user)

        # Verify delta mode is used
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is True
        assert data["mode"] == "delta"


class TestAC5_ConcurrentRunRejection:
    """AC5: Concurrent Run Rejection."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_concurrent_run_rejected_immediately(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that concurrent run is rejected when lock is held."""
        # Setup mocks - service reports unavailable (lock held)
        mock_dependency_map_service.is_available = Mock(return_value=False)
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "full"}, mock_admin_user)

        # Verify error response
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is False
        assert "error" in data
        assert "already in progress" in data["error"].lower()

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_no_job_created_when_concurrent(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that no job is created when analysis already running."""
        # Setup mocks - service unavailable
        mock_dependency_map_service.is_available = Mock(return_value=False)
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "full"}, mock_admin_user)

        # Verify no job_id returned
        data = json.loads(response["content"][0]["text"])
        assert data.get("job_id") is None


class TestAC6_DisabledFeatureRejection:
    """AC6: Disabled Feature Rejection."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_disabled_feature_rejected(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that tool returns error when dependency_map_enabled is False."""
        # Setup mocks - feature disabled
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = False
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "full"}, mock_admin_user)

        # Verify error response
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is False
        assert "error" in data
        assert "disabled" in data["error"].lower()

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_no_job_when_disabled(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that no job is created when feature is disabled."""
        # Setup mocks - feature disabled
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = False
        mock_app.config = mock_config

        # Call handler
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "full"}, mock_admin_user)

        # Verify no job_id
        data = json.loads(response["content"][0]["text"])
        assert data.get("job_id") is None


class TestAC8_InvalidModeHandling:
    """AC8: Invalid Mode Handling."""

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_invalid_mode_rejected(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that invalid mode parameter is rejected."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler with invalid mode
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "invalid"}, mock_admin_user)

        # Verify error response
        data = json.loads(response["content"][0]["text"])
        assert data.get("success") is False
        assert "error" in data
        assert "invalid" in data["error"].lower() or "must be" in data["error"].lower()

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_invalid_mode_error_message_specifies_options(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that error message specifies valid mode options."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler with invalid mode
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "bad_mode"}, mock_admin_user)

        # Verify error mentions valid options
        data = json.loads(response["content"][0]["text"])
        error_msg = data["error"].lower()
        assert "full" in error_msg or "delta" in error_msg

    @patch("code_indexer.server.mcp.handlers.app_module")
    def test_no_job_created_for_invalid_mode(self, mock_app, mock_admin_user, mock_dependency_map_service):
        """Test that no job is created for invalid mode."""
        # Setup mocks
        mock_app.dependency_map_service = mock_dependency_map_service
        mock_config = Mock()
        mock_config.dependency_map_enabled = True
        mock_app.config = mock_config

        # Call handler with invalid mode
        handler = HANDLER_REGISTRY["trigger_dependency_analysis"]
        response = handler({"mode": "wrong"}, mock_admin_user)

        # Verify no job_id
        data = json.loads(response["content"][0]["text"])
        assert data.get("job_id") is None
