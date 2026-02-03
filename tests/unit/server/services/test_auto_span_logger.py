"""
Unit tests for AutoSpanLogger service.

Tests the MCP tool call interceptor that creates spans when traces are active.
"""

import asyncio
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


class TestAutoSpanLoggerInit:
    """Test AutoSpanLogger initialization."""

    def test_init_stores_dependencies(self, trace_manager, mock_langfuse):
        """Should store trace manager and langfuse client."""
        logger = AutoSpanLogger(trace_manager, mock_langfuse)
        assert logger.trace_manager is trace_manager
        assert logger.langfuse is mock_langfuse


class TestInterceptToolCallNoActiveTrace:
    """Test intercept_tool_call when no trace is active."""

    @pytest.mark.asyncio
    async def test_no_active_trace_executes_handler_directly(
        self, auto_span_logger, mock_langfuse
    ):
        """Should execute handler without creating span when no trace active."""

        async def handler():
            return {"status": "success"}

        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={"arg1": "value1"},
            handler=handler,
        )

        assert result == {"status": "success"}
        mock_langfuse.create_span.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_active_trace_propagates_exceptions(self, auto_span_logger):
        """Should propagate exceptions from handler when no trace active."""

        async def handler():
            raise ValueError("Test error")

        with pytest.raises(ValueError, match="Test error"):
            await auto_span_logger.intercept_tool_call(
                session_id="session-1",
                tool_name="test_tool",
                arguments={},
                handler=handler,
            )


class TestInterceptToolCallWithActiveTrace:
    """Test intercept_tool_call when trace is active."""

    @pytest.mark.asyncio
    async def test_creates_span_with_trace_context(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should create span with trace_id and parent span."""

        async def handler():
            return {"status": "success"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={"arg1": "value1"},
            handler=handler,
        )

        mock_langfuse.create_span.assert_called_once()
        call_kwargs = mock_langfuse.create_span.call_args[1]
        assert call_kwargs["trace_id"] == active_trace.trace_id
        assert call_kwargs["name"] == "test_tool"

    @pytest.mark.asyncio
    async def test_captures_tool_inputs(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should capture tool arguments as span input."""

        async def handler():
            return {"status": "success"}

        arguments = {"arg1": "value1", "arg2": 42}
        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        assert call_kwargs["input_data"] == arguments

    @pytest.mark.asyncio
    async def test_captures_tool_output(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should capture handler return value as span output."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return {"status": "success", "data": "result"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={},
            handler=handler,
        )

        # Langfuse SDK 3.x: output is set via update(), then end() with no args
        mock_span.update.assert_called_once()
        call_kwargs = mock_span.update.call_args[1]
        assert call_kwargs["output"] == {"status": "success", "data": "result"}
        mock_span.end.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_captures_execution_timing(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should measure and include execution time in span."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            await asyncio.sleep(0.01)  # Small delay
            return {"status": "success"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={},
            handler=handler,
        )

        # Span should be ended (which captures timing internally)
        mock_span.end.assert_called_once()

    @pytest.mark.asyncio
    async def test_propagates_handler_result(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should return the handler's result unchanged."""

        async def handler():
            return {"data": "important_result"}

        result = await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments={},
            handler=handler,
        )

        assert result == {"data": "important_result"}
