"""
Unit tests for AutoSpanLogger auto-trace functionality (Story #136 follow-up).

Tests automatic trace creation on first tool call when:
- auto_trace_enabled is True
- No active trace exists for the session
"""

import pytest
from unittest.mock import MagicMock, Mock

from src.code_indexer.server.services.auto_span_logger import AutoSpanLogger
from src.code_indexer.server.services.langfuse_client import LangfuseClient
from src.code_indexer.server.services.trace_state_manager import TraceStateManager
from src.code_indexer.server.utils.config_manager import LangfuseConfig


@pytest.fixture
def langfuse_config_auto_enabled():
    """LangfuseConfig with auto_trace_enabled=True."""
    return LangfuseConfig(
        enabled=True,
        public_key="pk-test",
        secret_key="sk-test",
        auto_trace_enabled=True,
    )


@pytest.fixture
def langfuse_config_auto_disabled():
    """LangfuseConfig with auto_trace_enabled=False."""
    return LangfuseConfig(
        enabled=True,
        public_key="pk-test",
        secret_key="sk-test",
        auto_trace_enabled=False,
    )


@pytest.fixture
def mock_langfuse_client():
    """Mock LangfuseClient."""
    client = MagicMock(spec=LangfuseClient)
    client.is_enabled.return_value = True
    return client


@pytest.fixture
def trace_manager(mock_langfuse_client):
    """TraceStateManager instance."""
    return TraceStateManager(mock_langfuse_client)


class TestAutoTraceCreation:
    """Tests for automatic trace creation when auto_trace_enabled=True."""

    @pytest.mark.asyncio
    async def test_creates_trace_when_auto_enabled_and_no_trace(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
    ):
        """Should auto-create trace on first tool call when enabled and no trace exists."""
        # Setup: mock config access (will be passed via AutoSpanLogger constructor later)
        # For now, we'll inject the behavior directly
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
        )

        # Verify no trace exists
        assert trace_manager.get_active_trace("session-1") is None

        # Mock trace creation to return a trace
        mock_trace = Mock()
        mock_trace.id = "auto-trace-123"
        mock_langfuse_client.create_trace.return_value = mock_trace

        # Mock span creation
        mock_span = MagicMock()
        mock_langfuse_client.create_span.return_value = mock_span

        async def handler():
            return {"status": "success"}

        # Execute tool call - should auto-create trace
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_code",
            arguments={"query": "authentication"},
            handler=handler,
            username="test-user",
        )

        # Verify trace was created
        mock_langfuse_client.create_trace.assert_called_once()
        call_kwargs = mock_langfuse_client.create_trace.call_args[1]
        assert call_kwargs["name"] == "research-session"
        assert call_kwargs["session_id"] == "session-1"
        assert "Auto-trace: search_code" in call_kwargs["metadata"]["topic"]
        assert call_kwargs["metadata"]["strategy"] == "auto"
        assert call_kwargs["user_id"] == "test-user"

        # Verify span was created under the auto-created trace
        mock_langfuse_client.create_span.assert_called_once()

        # Verify handler executed successfully
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_does_not_create_trace_when_auto_disabled(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_disabled
    ):
        """Should NOT auto-create trace when auto_trace_enabled=False."""
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_disabled
        )

        # Verify no trace exists
        assert trace_manager.get_active_trace("session-1") is None

        async def handler():
            return {"status": "success"}

        # Execute tool call - should NOT auto-create trace
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_code",
            arguments={"query": "authentication"},
            handler=handler,
            username="test-user",
        )

        # Verify trace was NOT created
        mock_langfuse_client.create_trace.assert_not_called()

        # Verify span was NOT created (no active trace)
        mock_langfuse_client.create_span.assert_not_called()

        # Verify handler executed successfully (without trace/span overhead)
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_does_not_create_duplicate_trace_when_trace_exists(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
    ):
        """Should NOT auto-create trace when trace already exists for session."""
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
        )

        # Setup: create existing trace manually
        existing_trace = Mock()
        existing_trace.id = "existing-trace-123"
        mock_langfuse_client.create_trace.return_value = existing_trace

        trace_manager.start_trace(
            session_id="session-1", topic="manual-research", username="test-user"
        )

        # Verify trace exists
        assert trace_manager.get_active_trace("session-1") is not None

        # Reset mock to verify auto-trace does not create another
        mock_langfuse_client.create_trace.reset_mock()

        # Mock span creation
        mock_span = MagicMock()
        mock_langfuse_client.create_span.return_value = mock_span

        async def handler():
            return {"status": "success"}

        # Execute tool call - should use existing trace, NOT create new one
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_code",
            arguments={"query": "authentication"},
            handler=handler,
            username="test-user",
        )

        # Verify NO additional trace was created
        mock_langfuse_client.create_trace.assert_not_called()

        # Verify span WAS created under existing trace
        mock_langfuse_client.create_span.assert_called_once()
        span_call_kwargs = mock_langfuse_client.create_span.call_args[1]
        assert span_call_kwargs["trace_id"] == "existing-trace-123"

        # Verify handler executed successfully
        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_continues_on_auto_trace_creation_failure(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
    ):
        """Should continue tool execution even if auto-trace creation fails."""
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
        )

        # Mock trace creation to fail
        mock_langfuse_client.create_trace.side_effect = Exception(
            "Langfuse API unavailable"
        )

        async def handler():
            return {"status": "success"}

        # Execute tool call - should NOT raise, should return result
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_code",
            arguments={"query": "authentication"},
            handler=handler,
            username="test-user",
        )

        # Verify handler executed successfully despite trace creation failure
        assert result == {"status": "success"}

        # Verify span was NOT created (no trace available)
        mock_langfuse_client.create_span.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_trace_uses_tool_name_in_topic(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
    ):
        """Auto-trace topic should include the tool name for context."""
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
        )

        mock_trace = Mock()
        mock_trace.id = "auto-trace-123"
        mock_langfuse_client.create_trace.return_value = mock_trace

        async def handler():
            return {"status": "success"}

        # Execute with specific tool name
        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="browse_repository_tree",
            arguments={},
            handler=handler,
            username="test-user",
        )

        # Verify topic includes tool name
        call_kwargs = mock_langfuse_client.create_trace.call_args[1]
        assert "browse_repository_tree" in call_kwargs["metadata"]["topic"]

    @pytest.mark.asyncio
    async def test_auto_trace_sets_strategy_to_auto(
        self, trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
    ):
        """Auto-trace should set strategy='auto' for identification."""
        auto_span_logger = AutoSpanLogger(
            trace_manager, mock_langfuse_client, langfuse_config_auto_enabled
        )

        mock_trace = Mock()
        mock_trace.id = "auto-trace-123"
        mock_langfuse_client.create_trace.return_value = mock_trace

        async def handler():
            return {"status": "success"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_code",
            arguments={},
            handler=handler,
            username="test-user",
        )

        # Verify strategy is set to 'auto'
        call_kwargs = mock_langfuse_client.create_trace.call_args[1]
        assert call_kwargs["metadata"]["strategy"] == "auto"
