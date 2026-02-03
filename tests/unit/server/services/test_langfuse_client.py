"""
Unit tests for LangfuseClient service.

Tests cover:
- Lazy initialization behavior
- Graceful disabled mode
- Trace and span creation
- Scoring and flush operations
- Error handling and fallback behavior
"""

import pytest
from unittest.mock import Mock, patch

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

        # Create trace triggers lazy init
        mock_trace = Mock()
        mock_langfuse_class.return_value.trace.return_value = mock_trace

        result = client.create_trace(name="test", session_id="s1")

        # Should have initialized
        mock_langfuse_class.assert_called_once_with(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://cloud.langfuse.com",
        )
        assert client._langfuse is not None
        assert result == mock_trace

    @patch("langfuse.Langfuse")
    def test_lazy_init_on_first_create_span(self, mock_langfuse_class):
        """Langfuse SDK should initialize on first create_span call."""
        config = LangfuseConfig(
            enabled=True, public_key="pk-test", secret_key="sk-test"
        )
        client = LangfuseClient(config)

        # Not initialized yet
        assert client._langfuse is None

        # Create span triggers lazy init
        mock_span = Mock()
        mock_langfuse_class.return_value.span.return_value = mock_span

        result = client.create_span(trace_id="t1", name="span1")

        # Should have initialized
        assert client._langfuse is not None
        assert result == mock_span

    @patch("langfuse.Langfuse")
    def test_init_only_once(self, mock_langfuse_class):
        """Langfuse SDK should initialize only once, not on every call."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Multiple calls
        client.create_trace(name="t1", session_id="s1")
        client.create_trace(name="t2", session_id="s1")
        client.create_span(trace_id="t1", name="span1")

        # Should only init once
        assert mock_langfuse_class.call_count == 1


class TestLangfuseClientTraceOperations:
    """Tests for trace creation and management."""

    @patch("langfuse.Langfuse")
    def test_create_trace_basic(self, mock_langfuse_class):
        """Basic trace creation with required parameters."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_trace = Mock()
        mock_langfuse_class.return_value.trace.return_value = mock_trace

        result = client.create_trace(name="research-session", session_id="session-123")

        mock_langfuse_class.return_value.trace.assert_called_once_with(
            name="research-session",
            session_id="session-123",
            metadata=None,
            user_id=None,
        )
        assert result == mock_trace

    @patch("langfuse.Langfuse")
    def test_create_trace_with_metadata(self, mock_langfuse_class):
        """Trace creation with metadata."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_trace = Mock()
        mock_langfuse_class.return_value.trace.return_value = mock_trace

        metadata = {"topic": "authentication", "strategy": "deep-dive"}
        result = client.create_trace(
            name="research", session_id="s1", metadata=metadata, user_id="user-123"
        )

        mock_langfuse_class.return_value.trace.assert_called_once_with(
            name="research", session_id="s1", metadata=metadata, user_id="user-123"
        )

    @patch("langfuse.Langfuse")
    def test_create_trace_error_returns_none(self, mock_langfuse_class):
        """When trace creation fails, return None and log error."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        # Simulate error
        mock_langfuse_class.return_value.trace.side_effect = Exception("API error")

        result = client.create_trace(name="test", session_id="s1")

        # Should return None, not raise
        assert result is None


class TestLangfuseClientSpanOperations:
    """Tests for span creation."""

    @patch("langfuse.Langfuse")
    def test_create_span_basic(self, mock_langfuse_class):
        """Basic span creation."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_span = Mock()
        mock_langfuse_class.return_value.span.return_value = mock_span

        result = client.create_span(trace_id="trace-123", name="search_code")

        mock_langfuse_class.return_value.span.assert_called_once_with(
            trace_id="trace-123",
            name="search_code",
            metadata=None,
            input=None,
            output=None,
        )
        assert result == mock_span

    @patch("langfuse.Langfuse")
    def test_create_span_with_io(self, mock_langfuse_class):
        """Span creation with input/output."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_span = Mock()
        mock_langfuse_class.return_value.span.return_value = mock_span

        input_data = {"query": "vector store", "limit": 5}
        output_data = {"results": 3, "duration_ms": 245}

        result = client.create_span(
            trace_id="t1",
            name="search_code",
            metadata={"tool": "search_code"},
            input_data=input_data,
            output_data=output_data,
        )

        mock_langfuse_class.return_value.span.assert_called_once_with(
            trace_id="t1",
            name="search_code",
            metadata={"tool": "search_code"},
            input=input_data,
            output=output_data,
        )

    @patch("langfuse.Langfuse")
    def test_create_span_error_returns_none(self, mock_langfuse_class):
        """When span creation fails, return None."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_langfuse_class.return_value.span.side_effect = Exception("Network error")

        result = client.create_span(trace_id="t1", name="span1")

        assert result is None


class TestLangfuseClientScoring:
    """Tests for scoring operations."""

    @patch("langfuse.Langfuse")
    def test_score_basic(self, mock_langfuse_class):
        """Basic scoring operation."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_score = Mock()
        mock_langfuse_class.return_value.score.return_value = mock_score

        result = client.score(trace_id="t1", name="user-feedback", value=0.8)

        mock_langfuse_class.return_value.score.assert_called_once_with(
            trace_id="t1", name="user-feedback", value=0.8, comment=None
        )
        assert result == mock_score

    @patch("langfuse.Langfuse")
    def test_score_with_comment(self, mock_langfuse_class):
        """Scoring with comment."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_score = Mock()
        mock_langfuse_class.return_value.score.return_value = mock_score

        result = client.score(
            trace_id="t1", name="quality", value=0.9, comment="Excellent results"
        )

        mock_langfuse_class.return_value.score.assert_called_once_with(
            trace_id="t1", name="quality", value=0.9, comment="Excellent results"
        )

    @patch("langfuse.Langfuse")
    def test_score_error_returns_none(self, mock_langfuse_class):
        """When scoring fails, return None."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

        mock_langfuse_class.return_value.score.side_effect = Exception("API error")

        result = client.score(trace_id="t1", name="feedback", value=0.5)

        assert result is None


class TestLangfuseClientFlush:
    """Tests for flush operation."""

    @patch("langfuse.Langfuse")
    def test_flush_when_enabled(self, mock_langfuse_class):
        """Flush should call SDK flush when enabled."""
        config = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        client = LangfuseClient(config)

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

        # Multiple operations
        client.create_trace(name="t1", session_id="s1")
        client.create_span(trace_id="t1", name="span1")
        client.score(trace_id="t1", name="feedback", value=0.8)

        # Should use same SDK instance
        assert mock_langfuse_class.call_count == 1
