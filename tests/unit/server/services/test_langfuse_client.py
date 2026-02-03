"""
Unit tests for LangfuseClient service.

Tests cover:
- Lazy initialization behavior
- Graceful disabled mode
- Trace and span creation (SDK 3.x API)
- Scoring and flush operations
- Error handling and fallback behavior
- New end_trace functionality (Bug #135 fix)
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

# Import will fail initially - TDD red phase
from code_indexer.server.services.langfuse_client import LangfuseClient
from code_indexer.server.utils.config_manager import LangfuseConfig


class TestLangfuseClientDisabled:
    """Tests for LangfuseClient when Langfuse is disabled."""

    def test_disabled_no_initialization(self):
        """When disabled, client should not initialize Langfuse SDK."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        # Should not have initialized the SDK
        assert client._langfuse is None
        assert not client.is_enabled()

    def test_disabled_create_trace_returns_none(self):
        """When disabled, create_trace returns None without error."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        result = client.create_trace(
            name="test-trace",
            session_id="session-123",
            metadata={"topic": "authentication"},
        )

        assert result is None

    def test_disabled_create_span_returns_none(self):
        """When disabled, create_span returns None without error."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        result = client.create_span(
            trace_id="trace-123", name="tool-call", metadata={"tool": "search_code"}
        )

        assert result is None

    def test_disabled_score_does_nothing(self):
        """When disabled, score method does nothing and returns None."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        result = client.score(trace_id="trace-123", name="user-feedback", value=0.8)

        assert result is None

    def test_disabled_flush_does_nothing(self):
        """When disabled, flush does nothing."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        # Should not raise
        client.flush()

    def test_disabled_end_trace_returns_false(self):
        """When disabled, end_trace returns False."""
        config = LangfuseConfig(enabled=False)
        client = LangfuseClient(config)

        result = client.end_trace(None)
        assert result is False


class TestLangfuseClientLazyInit:
    """Tests for lazy initialization behavior."""

    @patch("langfuse.Langfuse")
    def test_lazy_init_on_first_create_trace(self, mock_langfuse_class):
        """Langfuse SDK should initialize on first create_trace call."""
        config = LangfuseConfig(
            enabled=True,
            public_key="pk-test",
            secret_key="sk-test",
            host="https://cloud.langfuse.com",
        )
        client = LangfuseClient(config)

        # Not initialized yet
        assert client._langfuse is None

        # Set up mock for SDK 3.x API
        mock_span = Mock()
        mock_span.trace_id = "test-trace-id"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        result = client.create_trace(name="test", session_id="s1")

        # Should have initialized
        mock_langfuse_class.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://cloud.langfuse.com",
        )
        assert client._langfuse is not None
        assert result is not None
        assert result.trace_id == "test-trace-id"

    @patch("langfuse.Langfuse")
    def test_lazy_init_on_first_create_span(self, mock_langfuse_class):
        """Langfuse SDK should initialize on first create_span call."""
        config = LangfuseConfig(
            enabled=True, public_key="pk-test", secret_key="sk-test"
        )
        client = LangfuseClient(config)

        # Not initialized yet
        assert client._langfuse is None

        # Set up mock for SDK 3.x API
        mock_span = Mock()
        mock_langfuse_class.return_value.start_span.return_value = mock_span

        result = client.create_span(trace_id="t1", name="span1")

        # Should have initialized
        assert client._langfuse is not None
        assert result == mock_span

    @patch("langfuse.Langfuse")
    def test_init_only_once(self, mock_langfuse_class):
        """Langfuse SDK should initialize only once, not on every call."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mocks
        mock_span = Mock()
        mock_span.trace_id = "test-trace-id"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        # Multiple calls
        client.create_trace(name="t1", session_id="s1")
        client.create_trace(name="t2", session_id="s1")
        client.create_span(trace_id="t1", name="span1")

        # Should only init once
        assert mock_langfuse_class.call_count == 1


class TestLangfuseClientTraceOperations:
    """Tests for trace creation and management (SDK 3.x API)."""

    @patch("langfuse.Langfuse")
    def test_create_trace_basic(self, mock_langfuse_class):
        """Basic trace creation with required parameters."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mock for SDK 3.x API
        mock_span = Mock()
        mock_span.trace_id = "trace-123"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        result = client.create_trace(name="research-session", session_id="session-123")

        mock_langfuse_class.return_value.start_as_current_span.assert_called_once_with(
            name="research-session",
            metadata=None,
            end_on_exit=False,
        )
        mock_langfuse_class.return_value.update_current_trace.assert_called_once_with(
            session_id="session-123",
            user_id=None,
        )
        assert result is not None
        assert result.trace_id == "trace-123"
        assert result.span == mock_span  # Bug #135 fix: span stored for end_trace

    @patch("langfuse.Langfuse")
    def test_create_trace_with_metadata(self, mock_langfuse_class):
        """Trace creation with metadata."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mock
        mock_span = Mock()
        mock_span.trace_id = "trace-456"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        metadata = {"topic": "authentication", "strategy": "deep-dive"}
        result = client.create_trace(
            name="research", session_id="s1", metadata=metadata, user_id="user-123"
        )

        mock_langfuse_class.return_value.start_as_current_span.assert_called_once_with(
            name="research", metadata=metadata, end_on_exit=False
        )
        mock_langfuse_class.return_value.update_current_trace.assert_called_once_with(
            session_id="s1", user_id="user-123"
        )

    @patch("langfuse.Langfuse")
    def test_create_trace_error_returns_none(self, mock_langfuse_class):
        """When trace creation fails, return None and log error."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Simulate error
        mock_langfuse_class.return_value.start_as_current_span.side_effect = Exception(
            "API error"
        )

        result = client.create_trace(name="test", session_id="s1")

        # Should return None, not raise
        assert result is None


class TestLangfuseClientSpanOperations:
    """Tests for span creation (SDK 3.x API)."""

    @patch("langfuse.Langfuse")
    def test_create_span_basic(self, mock_langfuse_class):
        """Basic span creation."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_span = Mock()
        mock_langfuse_class.return_value.start_span.return_value = mock_span

        result = client.create_span(trace_id="trace-123", name="search_code")

        mock_langfuse_class.return_value.start_span.assert_called_once()
        call_kwargs = mock_langfuse_class.return_value.start_span.call_args[1]
        assert call_kwargs["name"] == "search_code"
        assert result == mock_span

    @patch("langfuse.Langfuse")
    def test_create_span_with_io(self, mock_langfuse_class):
        """Span creation with input/output."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_span = Mock()
        mock_langfuse_class.return_value.start_span.return_value = mock_span

        input_data = {"query": "vector store", "limit": 5}
        output_data = {"results": 3, "duration_ms": 245}

        result = client.create_span(
            trace_id="t1",
            name="search_code",
            metadata={"tool": "search_code"},
            input_data=input_data,
            output_data=output_data,
        )

        mock_langfuse_class.return_value.start_span.assert_called_once()
        call_kwargs = mock_langfuse_class.return_value.start_span.call_args[1]
        assert call_kwargs["name"] == "search_code"
        assert call_kwargs["metadata"] == {"tool": "search_code"}
        assert call_kwargs["input"] == input_data
        assert call_kwargs["output"] == output_data

    @patch("langfuse.Langfuse")
    def test_create_span_error_returns_none(self, mock_langfuse_class):
        """When span creation fails, return None."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_langfuse_class.return_value.start_span.side_effect = Exception(
            "Network error"
        )

        result = client.create_span(trace_id="t1", name="span1")

        assert result is None


class TestLangfuseClientScoring:
    """Tests for scoring operations (SDK 3.x API)."""

    @patch("langfuse.Langfuse")
    def test_score_basic(self, mock_langfuse_class):
        """Basic scoring operation."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_score = Mock()
        mock_langfuse_class.return_value.create_score.return_value = mock_score

        result = client.score(trace_id="t1", name="user-feedback", value=0.8)

        mock_langfuse_class.return_value.create_score.assert_called_once_with(
            trace_id="t1", name="user-feedback", value=0.8, comment=None
        )
        assert result == mock_score

    @patch("langfuse.Langfuse")
    def test_score_with_comment(self, mock_langfuse_class):
        """Scoring with comment."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_score = Mock()
        mock_langfuse_class.return_value.create_score.return_value = mock_score

        result = client.score(
            trace_id="t1", name="quality", value=0.9, comment="Excellent results"
        )

        mock_langfuse_class.return_value.create_score.assert_called_once_with(
            trace_id="t1", name="quality", value=0.9, comment="Excellent results"
        )

    @patch("langfuse.Langfuse")
    def test_score_error_returns_none(self, mock_langfuse_class):
        """When scoring fails, return None."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_langfuse_class.return_value.create_score.side_effect = Exception(
            "API error"
        )

        result = client.score(trace_id="t1", name="feedback", value=0.5)

        assert result is None


class TestLangfuseClientEndTrace:
    """Tests for end_trace operation (Bug #135 fix)."""

    def test_end_trace_with_valid_trace_object(self):
        """end_trace should call span.end() on valid trace object."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Create a mock trace object with span
        mock_span = Mock()
        trace_obj = Mock()
        trace_obj.trace_id = "trace-123"
        trace_obj.span = mock_span

        result = client.end_trace(trace_obj)

        mock_span.end.assert_called_once()
        assert result is True

    def test_end_trace_with_none_returns_false(self):
        """end_trace with None should return False."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        result = client.end_trace(None)

        assert result is False

    def test_end_trace_without_span_returns_false(self):
        """end_trace on trace without span attribute returns False."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Trace object without span
        trace_obj = Mock(spec=["trace_id"])
        trace_obj.trace_id = "trace-123"

        result = client.end_trace(trace_obj)

        assert result is False

    def test_end_trace_with_none_span_returns_false(self):
        """end_trace on trace with span=None returns False."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        trace_obj = Mock()
        trace_obj.trace_id = "trace-123"
        trace_obj.span = None

        result = client.end_trace(trace_obj)

        assert result is False

    def test_end_trace_handles_exception(self):
        """end_trace should handle exceptions gracefully."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_span = Mock()
        mock_span.end.side_effect = Exception("Network error")
        trace_obj = Mock()
        trace_obj.trace_id = "trace-123"
        trace_obj.span = mock_span

        result = client.end_trace(trace_obj)

        assert result is False


class TestLangfuseClientFlush:
    """Tests for flush operation."""

    @patch("langfuse.Langfuse")
    def test_flush_when_enabled(self, mock_langfuse_class):
        """Flush should call SDK flush when enabled."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mock
        mock_span = Mock()
        mock_span.trace_id = "test-trace-id"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        # Trigger init
        client.create_trace(name="t1", session_id="s1")

        # Flush
        client.flush()

        mock_langfuse_class.return_value.flush.assert_called_once()

    @patch("langfuse.Langfuse")
    def test_flush_error_does_not_raise(self, mock_langfuse_class):
        """Flush errors should be caught and logged."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mock
        mock_span = Mock()
        mock_span.trace_id = "test-trace-id"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        # Trigger init
        client.create_trace(name="t1", session_id="s1")

        # Simulate flush error
        mock_langfuse_class.return_value.flush.side_effect = Exception(
            "Network timeout"
        )

        # Should not raise
        client.flush()


class TestLangfuseClientThreadSafety:
    """Tests for thread safety (singleton pattern)."""

    @patch("langfuse.Langfuse")
    def test_singleton_behavior(self, mock_langfuse_class):
        """Client should use singleton pattern for SDK instance."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Set up mocks
        mock_span = Mock()
        mock_span.trace_id = "test-trace-id"
        mock_span_cm = MagicMock()
        mock_span_cm.__enter__ = Mock(return_value=mock_span)
        mock_span_cm.__exit__ = Mock(return_value=False)
        mock_langfuse_class.return_value.start_as_current_span.return_value = (
            mock_span_cm
        )

        # Multiple operations
        client.create_trace(name="t1", session_id="s1")
        client.create_span(trace_id="t1", name="span1")
        client.score(trace_id="t1", name="feedback", value=0.8)

        # Should use same SDK instance
        assert mock_langfuse_class.call_count == 1
