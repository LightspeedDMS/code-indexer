"""Unit tests for Langfuse tracing MCP handlers."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from code_indexer.server.mcp.handlers import (
    handle_start_trace,
    handle_end_trace,
)
from code_indexer.server.auth.user_manager import User, UserRole

# Prevent real Langfuse SDK initialization
import sys

sys.modules["langfuse"] = MagicMock()


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


@pytest.fixture
def mock_session_state():
    """Create a mock session state."""
    session_state = Mock()
    session_state.session_id = "session-123"
    return session_state


@pytest.fixture
def mock_langfuse_service():
    """Create a mock LangfuseService."""
    service = Mock()
    service.is_enabled = Mock(return_value=True)
    service.trace_manager = Mock()
    return service


class TestHandleStartTrace:
    """Tests for handle_start_trace handler."""

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_success(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test successful trace start."""
        mock_get_service.return_value = mock_langfuse_service
        mock_trace_ctx = Mock()
        mock_trace_ctx.trace_id = "trace-456"
        mock_langfuse_service.trace_manager.start_trace.return_value = mock_trace_ctx

        args = {
            "name": "Authentication Research",
            "strategy": "Top-down",
            "metadata": {"priority": "high"},
        }

        result = handle_start_trace(args, mock_user, session_state=mock_session_state)

        # Verify trace_manager.start_trace was called correctly
        mock_langfuse_service.trace_manager.start_trace.assert_called_once_with(
            session_id="session-123",
            name="Authentication Research",
            strategy="Top-down",
            metadata={"priority": "high"},
            username="testuser",
            input=None,
            tags=None,
            intel=None,
        )

        # Verify response
        assert "content" in result
        assert result["content"][0]["type"] == "text"
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "active"
        assert response_data["trace_id"] == "trace-456"

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_disabled(
        self, mock_get_service, mock_user, mock_session_state
    ):
        """Test start_trace when Langfuse is disabled."""
        service = Mock()
        service.is_enabled = Mock(return_value=False)
        mock_get_service.return_value = service

        args = {"name": "Test"}

        result = handle_start_trace(args, mock_user, session_state=mock_session_state)

        # Verify response indicates disabled
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "disabled"
        assert "not enabled" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_no_session(
        self, mock_get_service, mock_user, mock_langfuse_service
    ):
        """Test start_trace without session context."""
        mock_get_service.return_value = mock_langfuse_service

        args = {"name": "Test"}

        result = handle_start_trace(args, mock_user, session_state=None)

        # Verify response indicates error
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "No session context" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_missing_name(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test start_trace without required name parameter."""
        mock_get_service.return_value = mock_langfuse_service

        args = {}  # Missing name

        result = handle_start_trace(args, mock_user, session_state=mock_session_state)

        # Verify response indicates error
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "Missing required parameter: name" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_minimal_args(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test start_trace with only required parameters."""
        mock_get_service.return_value = mock_langfuse_service
        mock_trace_ctx = Mock()
        mock_trace_ctx.trace_id = "trace-789"
        mock_langfuse_service.trace_manager.start_trace.return_value = mock_trace_ctx

        args = {"name": "Simple Research"}

        result = handle_start_trace(args, mock_user, session_state=mock_session_state)

        # Verify trace_manager.start_trace was called with None for optional params
        mock_langfuse_service.trace_manager.start_trace.assert_called_once_with(
            session_id="session-123",
            name="Simple Research",
            strategy=None,
            metadata=None,
            username="testuser",
            input=None,
            tags=None,
            intel=None,
        )

        # Verify response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "active"
        assert response_data["trace_id"] == "trace-789"

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_start_trace_exception_handling(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test start_trace handles exceptions gracefully."""
        mock_get_service.return_value = mock_langfuse_service
        mock_langfuse_service.trace_manager.start_trace.side_effect = Exception(
            "Test error"
        )

        args = {"name": "Test"}

        result = handle_start_trace(args, mock_user, session_state=mock_session_state)

        # Verify error response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "Test error" in response_data["message"]


class TestHandleEndTrace:
    """Tests for handle_end_trace handler."""

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_success(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test successful trace end."""
        mock_get_service.return_value = mock_langfuse_service

        # Mock active trace
        mock_trace_ctx = Mock()
        mock_trace_ctx.trace_id = "trace-456"
        mock_langfuse_service.trace_manager.get_active_trace.return_value = (
            mock_trace_ctx
        )
        mock_langfuse_service.trace_manager.end_trace.return_value = True

        args = {"score": 0.9, "summary": "Good results", "outcome": "success"}

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify trace_manager methods were called
        mock_langfuse_service.trace_manager.get_active_trace.assert_called_once_with(
            "session-123", username="testuser"
        )
        mock_langfuse_service.trace_manager.end_trace.assert_called_once_with(
            session_id="session-123",
            score=0.9,
            summary="Good results",
            outcome="success",
            username="testuser",
            output=None,
            tags=None,
            intel=None,
        )

        # Verify response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "ended"
        assert response_data["trace_id"] == "trace-456"

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_disabled(self, mock_get_service, mock_user, mock_session_state):
        """Test end_trace when Langfuse is disabled."""
        service = Mock()
        service.is_enabled = Mock(return_value=False)
        mock_get_service.return_value = service

        args = {}

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify response indicates disabled
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "disabled"
        assert "not enabled" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_no_session(
        self, mock_get_service, mock_user, mock_langfuse_service
    ):
        """Test end_trace without session context."""
        mock_get_service.return_value = mock_langfuse_service

        args = {}

        result = handle_end_trace(args, mock_user, session_state=None)

        # Verify response indicates error
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "No session context" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_no_active_trace(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test end_trace when no active trace exists."""
        mock_get_service.return_value = mock_langfuse_service
        mock_langfuse_service.trace_manager.get_active_trace.return_value = None

        args = {}

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify response indicates no active trace
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "no_active_trace"
        assert "No active trace to end" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_minimal_args(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test end_trace with no optional parameters."""
        mock_get_service.return_value = mock_langfuse_service

        mock_trace_ctx = Mock()
        mock_trace_ctx.trace_id = "trace-789"
        mock_langfuse_service.trace_manager.get_active_trace.return_value = (
            mock_trace_ctx
        )
        mock_langfuse_service.trace_manager.end_trace.return_value = True

        args = {}  # No optional parameters

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify end_trace called with None for optional params
        mock_langfuse_service.trace_manager.end_trace.assert_called_once_with(
            session_id="session-123",
            score=None,
            summary=None,
            outcome=None,
            username="testuser",
            output=None,
            tags=None,
            intel=None,
        )

        # Verify response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "ended"

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_failure(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test end_trace when end operation fails."""
        mock_get_service.return_value = mock_langfuse_service

        mock_trace_ctx = Mock()
        mock_trace_ctx.trace_id = "trace-fail"
        mock_langfuse_service.trace_manager.get_active_trace.return_value = (
            mock_trace_ctx
        )
        mock_langfuse_service.trace_manager.end_trace.return_value = False

        args = {}

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify error response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "Failed to end trace" in response_data["message"]

    @patch("code_indexer.server.services.langfuse_service.get_langfuse_service")
    def test_end_trace_exception_handling(
        self, mock_get_service, mock_user, mock_session_state, mock_langfuse_service
    ):
        """Test end_trace handles exceptions gracefully."""
        mock_get_service.return_value = mock_langfuse_service
        mock_langfuse_service.trace_manager.get_active_trace.side_effect = Exception(
            "Test error"
        )

        args = {}

        result = handle_end_trace(args, mock_user, session_state=mock_session_state)

        # Verify error response
        import json

        response_data = json.loads(result["content"][0]["text"])
        assert response_data["status"] == "error"
        assert "Test error" in response_data["message"]
