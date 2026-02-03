"""
Unit tests for AutoSpanLogger error handling.

Tests graceful degradation when Langfuse operations fail or tool handlers raise exceptions.
"""

from unittest.mock import MagicMock

import pytest

from src.code_indexer.server.services.auto_span_logger import AutoSpanLogger
from src.code_indexer.server.services.langfuse_client import LangfuseClient
from src.code_indexer.server.services.trace_state_manager import TraceStateManager


@pytest.fixture
def mock_langfuse():
    """Mock LangfuseClient."""
    client = MagicMock(spec=LangfuseClient)
    client.is_enabled.return_value = True
    client.create_span.return_value = "span-123"
    return client


@pytest.fixture
def trace_manager(mock_langfuse):
    """TraceStateManager instance."""
    return TraceStateManager(mock_langfuse)


@pytest.fixture
def auto_span_logger(trace_manager, mock_langfuse):
    """AutoSpanLogger instance."""
    return AutoSpanLogger(trace_manager, mock_langfuse)


@pytest.fixture
def active_trace(trace_manager):
    """Start a trace and return context."""
    return trace_manager.start_trace(
        session_id="session-1",
        topic="Test trace",
    )


class TestErrorHandling:
    """Test error handling in span creation and tool execution."""

    @pytest.mark.asyncio
    async def test_captures_handler_exception_in_span(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should capture exception details in span when handler fails."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            raise ValueError("Tool execution failed")

        with pytest.raises(ValueError, match="Tool execution failed"):
            await auto_span_logger.intercept_tool_call(
                session_id="session-1",
                tool_name="failing_tool",
                arguments={},
                handler=handler,
            )

        # Span should be ended with error status
        mock_span.end.assert_called_once()
        call_kwargs = mock_span.end.call_args[1]
        assert "level" in call_kwargs
        assert call_kwargs["level"] == "ERROR"
        assert "output" in call_kwargs
        assert "error" in call_kwargs["output"]
        assert "Tool execution failed" in call_kwargs["output"]["error"]

    @pytest.mark.asyncio
    async def test_captures_error_type_in_span(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should include exception type in error span."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            raise TypeError("Invalid type")

        with pytest.raises(TypeError):
            await auto_span_logger.intercept_tool_call(
                session_id="session-1",
                tool_name="failing_tool",
                arguments={},
                handler=handler,
            )

        call_kwargs = mock_span.end.call_args[1]
        assert call_kwargs["output"]["error_type"] == "TypeError"

    @pytest.mark.asyncio
    async def test_continues_on_span_creation_failure(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should continue tool execution even if span creation fails."""
        mock_langfuse.create_span.side_effect = Exception("Langfuse unavailable")

        async def handler():
            return {"status": "success"}

        # Should not raise exception, should return result
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={},
            handler=handler,
        )

        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_continues_on_span_end_failure(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should continue even if span.end() fails."""
        mock_span = MagicMock()
        mock_span.end.side_effect = Exception("Span end failed")
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return {"status": "success"}

        # Should not raise exception, should return result
        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={},
            handler=handler,
        )

        assert result == {"status": "success"}

    @pytest.mark.asyncio
    async def test_continues_on_span_end_error_handling_failure(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should continue even if span.end() fails during error handling."""
        mock_span = MagicMock()
        mock_span.end.side_effect = Exception("Span end crashed")
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            raise ValueError("Handler failed")

        # Should propagate handler exception, not span.end() exception
        with pytest.raises(ValueError, match="Handler failed"):
            await auto_span_logger.intercept_tool_call(
                session_id="session-1",
                tool_name="test_tool",
                arguments={},
                handler=handler,
            )

    @pytest.mark.asyncio
    async def test_propagates_handler_exception_after_span_capture(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should propagate handler exception after capturing in span."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            raise RuntimeError("Critical failure")

        with pytest.raises(RuntimeError, match="Critical failure"):
            await auto_span_logger.intercept_tool_call(
                session_id="session-1",
                tool_name="test_tool",
                arguments={},
                handler=handler,
            )

        # Verify span was ended with error
        assert mock_span.end.called
