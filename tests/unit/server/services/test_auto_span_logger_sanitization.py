"""
Unit tests for AutoSpanLogger input sanitization and output summarization.

Tests sensitive data removal and large result summarization.
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


class TestInputSanitization:
    """Test input sanitization to remove sensitive data."""

    @pytest.mark.asyncio
    async def test_sanitizes_password_field(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should remove 'password' field from arguments."""

        async def handler():
            return {"status": "success"}

        arguments = {
            "username": "admin",
            "password": "secret123",
            "other_field": "visible",
        }

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="auth_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        sanitized_input = call_kwargs["input"]
        assert "password" not in sanitized_input
        assert sanitized_input["username"] == "admin"
        assert sanitized_input["other_field"] == "visible"

    @pytest.mark.asyncio
    async def test_sanitizes_token_field(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should remove 'token' field from arguments."""

        async def handler():
            return {"status": "success"}

        arguments = {"token": "Bearer abc123", "action": "verify"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="auth_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        sanitized_input = call_kwargs["input"]
        assert "token" not in sanitized_input
        assert sanitized_input["action"] == "verify"

    @pytest.mark.asyncio
    async def test_sanitizes_secret_field(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should remove 'secret' field from arguments."""

        async def handler():
            return {"status": "success"}

        arguments = {"secret": "shh", "data": "visible"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="config_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        sanitized_input = call_kwargs["input"]
        assert "secret" not in sanitized_input
        assert sanitized_input["data"] == "visible"

    @pytest.mark.asyncio
    async def test_sanitizes_api_key_field(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should remove 'api_key' field from arguments."""

        async def handler():
            return {"status": "success"}

        arguments = {"api_key": "key-12345", "endpoint": "/api/data"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="external_api_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        sanitized_input = call_kwargs["input"]
        assert "api_key" not in sanitized_input
        assert sanitized_input["endpoint"] == "/api/data"

    @pytest.mark.asyncio
    async def test_sanitization_case_insensitive(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should sanitize fields case-insensitively."""

        async def handler():
            return {"status": "success"}

        arguments = {
            "PASSWORD": "secret1",
            "Token": "secret2",
            "API_KEY": "secret3",
            "normal_field": "visible",
        }

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="test_tool",
            arguments=arguments,
            handler=handler,
        )

        call_kwargs = mock_langfuse.create_span.call_args[1]
        sanitized_input = call_kwargs["input"]
        assert "PASSWORD" not in sanitized_input
        assert "Token" not in sanitized_input
        assert "API_KEY" not in sanitized_input
        assert sanitized_input["normal_field"] == "visible"


class TestOutputSummarization:
    """Test output summarization for large results."""

    @pytest.mark.asyncio
    async def test_summarizes_dict_with_results_list(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should summarize output when it's a dict with 'results' list."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return {
                "results": [{"id": 1}, {"id": 2}, {"id": 3}],
                "metadata": "keep this",
            }

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_tool",
            arguments={},
            handler=handler,
        )

        call_kwargs = mock_span.end.call_args[1]
        output = call_kwargs["output"]
        assert output == {
            "result_count": 3,
            "summary": "3 results returned",
            "metadata": "keep this",
        }

    @pytest.mark.asyncio
    async def test_summarizes_empty_results_list(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should handle empty results list."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return {"results": [], "status": "no_matches"}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="search_tool",
            arguments={},
            handler=handler,
        )

        call_kwargs = mock_span.end.call_args[1]
        output = call_kwargs["output"]
        assert output == {
            "result_count": 0,
            "summary": "0 results returned",
            "status": "no_matches",
        }

    @pytest.mark.asyncio
    async def test_does_not_summarize_non_dict_output(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should not summarize output that's not a dict."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return ["item1", "item2", "item3"]

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="list_tool",
            arguments={},
            handler=handler,
        )

        call_kwargs = mock_span.end.call_args[1]
        output = call_kwargs["output"]
        assert output == ["item1", "item2", "item3"]

    @pytest.mark.asyncio
    async def test_does_not_summarize_dict_without_results(
        self, auto_span_logger, active_trace, mock_langfuse
    ):
        """Should not summarize dict without 'results' key."""
        mock_span = MagicMock()
        mock_langfuse.create_span.return_value = mock_span

        async def handler():
            return {"data": [1, 2, 3], "count": 3}

        await auto_span_logger.intercept_tool_call(
            session_id="session-1",
            tool_name="data_tool",
            arguments={},
            handler=handler,
        )

        call_kwargs = mock_span.end.call_args[1]
        output = call_kwargs["output"]
        assert output == {"data": [1, 2, 3], "count": 3}
