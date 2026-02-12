"""
Unit tests for TraceStateManager service.

Tests cover:
- Per-session trace stack management
- Start and end trace operations
- Nested trace support (stack-based)
- Active trace retrieval
- Session cleanup
- Thread safety with locks
- Edge cases (ending non-existent traces, empty stacks)
"""

import pytest
from unittest.mock import Mock, MagicMock

# Import will fail initially - TDD red phase
from code_indexer.server.services.trace_state_manager import (
    TraceStateManager,
    TraceContext,
)


class TestTraceContextDataclass:
    """Tests for TraceContext dataclass."""

    def test_trace_context_creation(self):
        """TraceContext should hold trace ID, trace object, and optional parent."""
        mock_trace = Mock()
        mock_trace.id = "trace-123"

        context = TraceContext(trace_id="trace-123", trace=mock_trace)

        assert context.trace_id == "trace-123"
        assert context.trace == mock_trace
        assert context.parent_trace_id is None

    def test_trace_context_with_parent(self):
        """TraceContext can have a parent trace ID for nested traces."""
        mock_trace = Mock()

        context = TraceContext(
            trace_id="trace-child", trace=mock_trace, parent_trace_id="trace-parent"
        )

        assert context.trace_id == "trace-child"
        assert context.parent_trace_id == "trace-parent"


class TestTraceStateManagerBasicOperations:
    """Tests for basic trace state management operations."""

    def test_initialization(self):
        """TraceStateManager should initialize with empty state."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        assert manager._session_trace_stacks == {}
        assert manager._langfuse is mock_client

    def test_start_trace_creates_context(self):
        """start_trace should create trace and push to session stack."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(
            session_id="session-1",
            name="authentication",
            metadata={"strategy": "deep-dive"},
        )

        # Should create trace via client
        mock_client.create_trace.assert_called_once_with(
            name="authentication",
            session_id="session-1",
            metadata={"strategy": "deep-dive"},
            user_id=None,
            input=None,
            tags=None,
        )

        # Should return context
        assert context is not None
        assert context.trace_id == "trace-123"
        assert context.trace == mock_trace
        assert context.parent_trace_id is None

        # Should push to session stack
        assert "session-1" in manager._session_trace_stacks
        assert len(manager._session_trace_stacks["session-1"]) == 1
        assert manager._session_trace_stacks["session-1"][0] == context

    def test_start_trace_with_user_id(self):
        """start_trace should pass user_id to langfuse client."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        manager.start_trace(session_id="session-1", name="auth", username="user-456")

        mock_client.create_trace.assert_called_once_with(
            name="auth",
            session_id="session-1",
            metadata=None,
            user_id="user-456",
            input=None,
            tags=None,
        )

    def test_start_trace_when_client_returns_none(self):
        """When client returns None (disabled), start_trace returns None."""
        mock_client = Mock()
        mock_client.create_trace.return_value = None

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(session_id="session-1", name="test")

        assert context is None
        # Should not create session stack
        assert "session-1" not in manager._session_trace_stacks

    def test_get_active_trace_returns_top_of_stack(self):
        """get_active_trace should return the top trace from session stack."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Manually set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        active = manager.get_active_trace("session-1")

        assert active == context

    def test_get_active_trace_empty_stack_returns_none(self):
        """get_active_trace returns None when no active trace."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        active = manager.get_active_trace("session-1")

        assert active is None

    def test_get_active_trace_unknown_session_returns_none(self):
        """get_active_trace returns None for unknown session."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Create stack for different session
        manager._session_trace_stacks["session-2"] = [Mock()]

        active = manager.get_active_trace("session-1")

        assert active is None

    def test_end_trace_pops_from_stack_and_flushes(self):
        """end_trace should pop trace from stack and flush client."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        result = manager.end_trace(session_id="session-1")

        # Should pop from stack
        assert result == context
        assert manager._session_trace_stacks["session-1"] == []

        # Should flush client
        mock_client.flush.assert_called_once()

    def test_end_trace_with_score(self):
        """end_trace should add score when provided."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(
            session_id="session-1", score=0.9, summary="Excellent results"
        )

        # Should score the trace
        mock_client.score.assert_called_once_with(
            trace_id="trace-123",
            name="user-feedback",
            value=0.9,
            comment="Excellent results",
        )

    def test_end_trace_empty_stack_returns_none(self):
        """end_trace returns None when stack is empty."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        result = manager.end_trace(session_id="session-1")

        assert result is None
        # Should not flush or score
        mock_client.flush.assert_not_called()
        mock_client.score.assert_not_called()

    def test_end_trace_unknown_session_returns_none(self):
        """end_trace returns None for unknown session."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        result = manager.end_trace(session_id="session-1")

        assert result is None


class TestTraceStateManagerNestedTraces:
    """Tests for nested trace support (stack-based)."""

    def test_nested_traces_push_to_stack(self):
        """Starting multiple traces for same session creates stack."""
        mock_client = Mock()
        mock_trace1 = Mock()
        mock_trace1.id = "trace-1"
        mock_trace2 = Mock()
        mock_trace2.id = "trace-2"
        mock_client.create_trace.side_effect = [mock_trace1, mock_trace2]

        manager = TraceStateManager(mock_client)

        # Start first trace
        context1 = manager.start_trace(session_id="session-1", name="auth")

        # Start second trace (nested)
        context2 = manager.start_trace(session_id="session-1", name="search")

        # Stack should have both
        assert len(manager._session_trace_stacks["session-1"]) == 2
        assert manager._session_trace_stacks["session-1"][0] == context1
        assert manager._session_trace_stacks["session-1"][1] == context2

        # Active trace should be most recent (top of stack)
        active = manager.get_active_trace("session-1")
        assert active == context2

    def test_nested_trace_has_parent_id(self):
        """Nested traces should record parent trace ID."""
        mock_client = Mock()
        mock_trace1 = Mock()
        mock_trace1.id = "trace-parent"
        mock_trace2 = Mock()
        mock_trace2.id = "trace-child"
        mock_client.create_trace.side_effect = [mock_trace1, mock_trace2]

        manager = TraceStateManager(mock_client)

        context1 = manager.start_trace(session_id="session-1", name="parent")
        context2 = manager.start_trace(session_id="session-1", name="child")

        # Child should reference parent
        assert context2.parent_trace_id == "trace-parent"

    def test_end_trace_pops_most_recent(self):
        """end_trace pops the most recent trace (LIFO)."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack with two traces
        context1 = TraceContext(trace_id="trace-1", trace=Mock())
        context2 = TraceContext(trace_id="trace-2", trace=Mock())
        manager._session_trace_stacks["session-1"] = [context1, context2]

        # End trace
        popped = manager.end_trace(session_id="session-1")

        # Should pop trace-2 (most recent)
        assert popped == context2
        assert manager._session_trace_stacks["session-1"] == [context1]

        # Active should now be trace-1
        active = manager.get_active_trace("session-1")
        assert active == context1


class TestTraceStateManagerSessionCleanup:
    """Tests for session cleanup operations."""

    def test_cleanup_session_removes_all_traces(self):
        """cleanup_session should remove all traces for session."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up traces for multiple sessions
        manager._session_trace_stacks["session-1"] = [Mock(), Mock()]
        manager._session_trace_stacks["session-2"] = [Mock()]

        manager.cleanup_session("session-1")

        # session-1 should be removed
        assert "session-1" not in manager._session_trace_stacks
        # session-2 should remain
        assert "session-2" in manager._session_trace_stacks

    def test_cleanup_session_flushes_client(self):
        """cleanup_session should flush pending data."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        manager._session_trace_stacks["session-1"] = [Mock()]

        manager.cleanup_session("session-1")

        mock_client.flush.assert_called_once()

    def test_cleanup_session_unknown_session_no_error(self):
        """cleanup_session should handle unknown session gracefully."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Should not raise
        manager.cleanup_session("session-unknown")

        # Should still flush
        mock_client.flush.assert_called_once()


class TestTraceStateManagerThreadSafety:
    """Tests for thread safety with locks."""

    def test_manager_has_lock(self):
        """TraceStateManager should have a threading lock."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        assert hasattr(manager, "_lock")
        import threading

        # Lock is a factory function, so check type name instead
        assert type(manager._lock).__name__ == "lock"

    def test_start_trace_uses_lock(self):
        """start_trace should acquire lock before modifying state."""
        # This test verifies lock exists; actual thread safety testing
        # would require concurrent test execution which is complex.
        # The implementation should use `with self._lock:` pattern.
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        # Verify lock is released after operation
        manager.start_trace(session_id="session-1", name="test")

        # Lock should be released (not locked)
        assert not manager._lock.locked()

    def test_end_trace_uses_lock(self):
        """end_trace should acquire lock before modifying state."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        context = TraceContext(trace_id="trace-123", trace=Mock())
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(session_id="session-1")

        # Lock should be released after operation
        assert not manager._lock.locked()

    def test_get_active_trace_uses_lock(self):
        """get_active_trace should acquire lock before reading state."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        manager.get_active_trace("session-1")

        # Lock should be released
        assert not manager._lock.locked()

    def test_cleanup_session_uses_lock(self):
        """cleanup_session should acquire lock before modifying state."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        manager._session_trace_stacks["session-1"] = [Mock()]

        manager.cleanup_session("session-1")

        # Lock should be released
        assert not manager._lock.locked()


class TestStory185ParameterChanges:
    """Tests for Story #185: Refactor start_trace/end_trace for full prompt observability."""

    def test_start_trace_accepts_name_parameter(self):
        """start_trace should accept 'name' parameter (renamed from 'topic')."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(
            session_id="session-1",
            name="Authentication Investigation",
        )

        # Should pass name to create_trace
        mock_client.create_trace.assert_called_once()
        call_args = mock_client.create_trace.call_args
        assert call_args.kwargs["name"] == "Authentication Investigation"
        assert context is not None

    def test_start_trace_accepts_input_parameter(self):
        """start_trace should accept optional 'input' parameter for user prompt."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(
            session_id="session-1",
            name="Test Task",
            input="Please find all authentication code",
        )

        # Should pass input to create_trace
        mock_client.create_trace.assert_called_once()
        call_args = mock_client.create_trace.call_args
        assert call_args.kwargs["input"] == "Please find all authentication code"

    def test_start_trace_accepts_tags_parameter(self):
        """start_trace should accept optional 'tags' parameter."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(
            session_id="session-1",
            name="Test Task",
            tags=["bugfix", "high-priority"],
        )

        # Should pass tags to create_trace
        mock_client.create_trace.assert_called_once()
        call_args = mock_client.create_trace.call_args
        assert call_args.kwargs["tags"] == ["bugfix", "high-priority"]

    def test_start_trace_accepts_intel_parameter(self):
        """start_trace should accept optional 'intel' parameter with prompt metadata."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        intel = {
            "frustration": 0.7,
            "specificity": "surg",
            "task_type": "bug",
            "quality": 0.8,
            "iteration": 2,
        }

        context = manager.start_trace(
            session_id="session-1", name="Bug Investigation", intel=intel
        )

        # Should merge intel into metadata with intel_ prefix
        mock_client.create_trace.assert_called_once()
        call_args = mock_client.create_trace.call_args
        metadata = call_args.kwargs["metadata"]
        assert metadata["intel_frustration"] == 0.7
        assert metadata["intel_specificity"] == "surg"
        assert metadata["intel_task_type"] == "bug"
        assert metadata["intel_quality"] == 0.8
        assert metadata["intel_iteration"] == 2

    def test_start_trace_keeps_strategy_and_metadata_unchanged(self):
        """start_trace should still support strategy and metadata parameters."""
        mock_client = Mock()
        mock_trace = Mock()
        mock_trace.id = "trace-123"
        mock_client.create_trace.return_value = mock_trace

        manager = TraceStateManager(mock_client)

        context = manager.start_trace(
            session_id="session-1",
            name="Test Task",
            strategy="depth-first",
            metadata={"project": "backend", "ticket": "JIRA-123"},
        )

        # Should include strategy and metadata in trace metadata
        mock_client.create_trace.assert_called_once()
        call_args = mock_client.create_trace.call_args
        metadata = call_args.kwargs["metadata"]
        assert metadata["strategy"] == "depth-first"
        assert metadata["project"] == "backend"
        assert metadata["ticket"] == "JIRA-123"

    def test_end_trace_accepts_summary_parameter(self):
        """end_trace should accept 'summary' parameter (renamed from 'feedback')."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(
            session_id="session-1", score=0.9, summary="Found root cause in auth module"
        )

        # Should pass summary as comment to score
        mock_client.score.assert_called_once_with(
            trace_id="trace-123",
            name="user-feedback",
            value=0.9,
            comment="Found root cause in auth module",
        )

    def test_end_trace_accepts_output_parameter(self):
        """end_trace should accept optional 'output' parameter for Claude response."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(
            session_id="session-1",
            output="I found the authentication code in src/auth.py...",
        )

        # Should call update_current_trace_in_context with output
        mock_client.update_current_trace_in_context.assert_called_once()
        call_args = mock_client.update_current_trace_in_context.call_args
        assert call_args.kwargs["span"] == mock_trace
        assert call_args.kwargs["output"] == "I found the authentication code in src/auth.py..."

    def test_end_trace_accepts_intel_parameter(self):
        """end_trace should accept optional 'intel' parameter to update prompt metadata."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        intel = {
            "frustration": 0.3,
            "quality": 0.9,
        }

        manager.end_trace(session_id="session-1", intel=intel)

        # Should call update_current_trace_in_context with intel metadata
        mock_client.update_current_trace_in_context.assert_called_once()
        call_args = mock_client.update_current_trace_in_context.call_args
        metadata = call_args.kwargs["metadata"]
        assert metadata["intel_frustration"] == 0.3
        assert metadata["intel_quality"] == 0.9

    def test_end_trace_accepts_tags_parameter(self):
        """end_trace should accept optional 'tags' parameter to merge with start tags."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(session_id="session-1", tags=["completed", "verified"])

        # Should call update_current_trace_in_context with tags
        mock_client.update_current_trace_in_context.assert_called_once()
        call_args = mock_client.update_current_trace_in_context.call_args
        assert call_args.kwargs["tags"] == ["completed", "verified"]

    def test_end_trace_wires_outcome_to_metadata(self):
        """end_trace should wire 'outcome' parameter to trace metadata (fix dead code)."""
        mock_client = Mock()
        manager = TraceStateManager(mock_client)

        # Set up stack
        mock_trace = Mock()
        context = TraceContext(trace_id="trace-123", trace=mock_trace)
        manager._session_trace_stacks["session-1"] = [context]

        manager.end_trace(session_id="session-1", outcome="bug_found")

        # Should call update_current_trace_in_context with outcome in metadata
        mock_client.update_current_trace_in_context.assert_called_once()
        call_args = mock_client.update_current_trace_in_context.call_args
        metadata = call_args.kwargs["metadata"]
        assert metadata["outcome"] == "bug_found"
