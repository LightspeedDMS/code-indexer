"""
Unit tests for AutoSpanLogger integration with MCP protocol.

Tests verify that tool execution via handle_tools_call() creates spans
when traces are active, following the flow:
  tools/call → handle_tools_call() → AutoSpanLogger.intercept_tool_call() → handler
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def admin_user():
    """Create admin user for tests."""
    return User(
        username="admin",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
        groups=set(),
    )


@pytest.fixture
def mock_langfuse_service():
    """Mock LangfuseService with span_logger."""
    service = MagicMock()
    service.is_enabled.return_value = True
    service.span_logger = MagicMock()
    service.span_logger.intercept_tool_call = AsyncMock()
    return service


class TestBasicSpanIntegration:
    """Test basic integration between protocol and AutoSpanLogger."""

    @pytest.mark.asyncio
    async def test_intercept_tool_call_invoked_for_tool_execution(
        self, admin_user, mock_langfuse_service
    ):
        """Should invoke AutoSpanLogger.intercept_tool_call during tool execution."""
        # Setup: Mock handler result
        mock_handler_result = {"content": [{"type": "text", "text": "Result"}]}

        # Setup: Mock span_logger to execute handler and return result
        async def mock_intercept(session_id, tool_name, arguments, handler, username=None):
            # Execute the handler wrapper that was passed
            return await handler()

        mock_langfuse_service.span_logger.intercept_tool_call.side_effect = mock_intercept

        # Setup: Patch the tool registry and handler
        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": AsyncMock(return_value=mock_handler_result)},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute
            params = {"name": "list_repositories", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: intercept_tool_call was called
            mock_langfuse_service.span_logger.intercept_tool_call.assert_called_once()

            # Verify: Result returned correctly
            assert result == mock_handler_result

    @pytest.mark.asyncio
    async def test_passes_correct_parameters_to_intercept(
        self, admin_user, mock_langfuse_service
    ):
        """Should pass session_id, tool_name, arguments, and username to intercept_tool_call."""
        # Setup: Mock span_logger
        async def mock_intercept(session_id, tool_name, arguments, handler, username=None):
            return await handler()

        mock_langfuse_service.span_logger.intercept_tool_call.side_effect = mock_intercept

        # Setup: Patch registry
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Success"}]}
        )
        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "search_code": {
                    "name": "search_code",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"search_code": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute with specific arguments
            params = {
                "name": "search_code",
                "arguments": {"query_text": "authentication", "limit": 5},
            }
            await handle_tools_call(params, admin_user, session_id="session-123")

            # Verify: Called with correct parameters
            call_args = mock_langfuse_service.span_logger.intercept_tool_call.call_args
            assert call_args[1]["session_id"] == "session-123"
            assert call_args[1]["tool_name"] == "search_code"
            assert call_args[1]["arguments"] == {
                "query_text": "authentication",
                "limit": 5,
            }
            assert call_args[1]["username"] == "admin"


class TestErrorHandling:
    """Test graceful error handling in span integration."""

    @pytest.mark.asyncio
    async def test_langfuse_service_unavailable_continues_execution(self, admin_user):
        """Should execute tool successfully even if LangfuseService unavailable."""
        # Setup: Mock get_langfuse_service to return None (simulating unavailable service)
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Success"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,  # Service unavailable
        ):
            # Execute
            params = {"name": "list_repositories", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: Tool executed successfully despite Langfuse unavailable
            assert result == {"content": [{"type": "text", "text": "Success"}]}
            mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_id_none_handled_gracefully(self, admin_user):
        """Should handle session_id=None gracefully without creating session state."""
        # Setup: Mock handler
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Success"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ):
            # Execute with session_id=None
            params = {"name": "list_repositories", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id=None)

            # Verify: Tool executed successfully
            assert result == {"content": [{"type": "text", "text": "Success"}]}
            mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_handler_execution(self, admin_user, mock_langfuse_service):
        """Should handle synchronous (non-async) handlers correctly."""
        # Setup: Synchronous handler using MagicMock (not AsyncMock)
        mock_handler = MagicMock(
            return_value={"content": [{"type": "text", "text": "Sync Success"}]}
        )

        # Setup: Mock span_logger to execute handler and return result
        async def mock_intercept(session_id, tool_name, arguments, handler, username=None):
            # Execute the handler wrapper that was passed
            return await handler()

        mock_langfuse_service.span_logger.intercept_tool_call.side_effect = mock_intercept

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute
            params = {"name": "list_repositories", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: Synchronous handler was called and result returned
            assert result == {"content": [{"type": "text", "text": "Sync Success"}]}
            mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_langfuse_service_exception(self, admin_user):
        """Should handle get_langfuse_service() exception gracefully."""
        # Setup: Mock get_langfuse_service to raise exception
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Success"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            side_effect=Exception("Service initialization failed"),
        ):
            # Execute - should not fail
            params = {"name": "list_repositories", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: Tool executed successfully despite service exception
            assert result == {"content": [{"type": "text", "text": "Success"}]}
            mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_span_logger_exception_does_not_fail_tool(self, admin_user):
        """Should execute tool successfully even if span_logger raises exception."""
        # Setup: Mock span_logger that raises exception
        mock_langfuse_service = MagicMock()
        mock_langfuse_service.span_logger.intercept_tool_call = AsyncMock(
            side_effect=Exception("Langfuse error")
        )

        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Success"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "list_repositories": {
                    "name": "list_repositories",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"list_repositories": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute
            params = {"name": "list_repositories", "arguments": {}}

            # Should NOT raise - should catch Langfuse error and execute handler anyway
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: Tool executed successfully
            assert result == {"content": [{"type": "text", "text": "Success"}]}
            mock_handler.assert_called_once()


class TestExcludedTools:
    """Test that tracing tools are excluded from span interception."""

    @pytest.mark.asyncio
    async def test_start_trace_not_intercepted(self, admin_user, mock_langfuse_service):
        """Should NOT intercept start_trace tool to avoid recursion."""
        # Setup: Mock handler
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Trace started"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "start_trace": {
                    "name": "start_trace",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"start_trace": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute
            params = {"name": "start_trace", "arguments": {"topic": "Test"}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: span_logger was NOT called
            mock_langfuse_service.span_logger.intercept_tool_call.assert_not_called()

            # Verify: Handler executed directly
            assert result == {"content": [{"type": "text", "text": "Trace started"}]}
            mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_trace_not_intercepted(self, admin_user, mock_langfuse_service):
        """Should NOT intercept end_trace tool to avoid recursion."""
        # Setup: Mock handler
        mock_handler = AsyncMock(
            return_value={"content": [{"type": "text", "text": "Trace ended"}]}
        )

        with patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "end_trace": {
                    "name": "end_trace",
                    "required_permission": "query_repos",
                }
            },
        ), patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"end_trace": mock_handler},
        ), patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=mock_langfuse_service,
        ):
            # Execute
            params = {"name": "end_trace", "arguments": {}}
            result = await handle_tools_call(params, admin_user, session_id="session-1")

            # Verify: span_logger was NOT called
            mock_langfuse_service.span_logger.intercept_tool_call.assert_not_called()

            # Verify: Handler executed directly
            assert result == {"content": [{"type": "text", "text": "Trace ended"}]}
            mock_handler.assert_called_once()
