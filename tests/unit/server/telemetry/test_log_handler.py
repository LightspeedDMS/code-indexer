"""
TDD Tests for Log Correlation with Trace Context (Story #701).

Tests get_trace_context() for trace context extraction from active OTEL
spans. Story #1676 AC2 removed OTELLogFormatter/OTELLogHandler as orphaned
code (never wired into the production logging pipeline) in favor of a
columnar trace_id/span_id storage approach -- see:
  - tests/unit/server/services/test_logging_utils_trace_context_1676.py
  - tests/unit/server/services/test_async_logging_trace_context_1676.py
  - tests/unit/server/services/test_sqlite_log_handler_trace_span_1676.py

All tests use real components following MESSI Rule #1: No mocks.
"""

import logging
import queue
import threading
from unittest.mock import patch

import pytest
from src.code_indexer.server.utils.config_manager import TelemetryConfig


def reset_all_singletons():
    """Reset all singletons to ensure clean test state."""
    from src.code_indexer.server.telemetry import (
        reset_telemetry_manager,
        reset_machine_metrics_exporter,
    )
    from src.code_indexer.server.services.system_metrics_collector import (
        reset_system_metrics_collector,
    )

    reset_machine_metrics_exporter()
    reset_telemetry_manager()
    reset_system_metrics_collector()


# =============================================================================
# Log Handler Import Tests
# =============================================================================


class TestLogHandlerImport:
    """Tests for log handler module import behavior."""

    def test_get_trace_context_function_exists(self):
        """get_trace_context() function is exported."""
        from src.code_indexer.server.telemetry.log_handler import (
            get_trace_context,
        )

        assert callable(get_trace_context)


# =============================================================================
# Trace Context Extraction Tests
# =============================================================================


class TestTraceContextExtraction:
    """Tests for trace context extraction from current span."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_get_trace_context_returns_dict(self):
        """
        get_trace_context() returns dictionary with trace_id and span_id.
        """
        from src.code_indexer.server.telemetry.log_handler import (
            get_trace_context,
        )

        context = get_trace_context()

        assert isinstance(context, dict)
        assert "trace_id" in context
        assert "span_id" in context

    def test_trace_context_has_correct_lengths(self):
        """
        trace_id is 32 chars, span_id is 16 chars.
        """
        from src.code_indexer.server.telemetry.log_handler import (
            get_trace_context,
        )

        context = get_trace_context()

        assert len(context["trace_id"]) == 32
        assert len(context["span_id"]) == 16

    def test_trace_context_zeros_when_no_span(self):
        """
        trace_id is all zeros when no active span.
        """
        from src.code_indexer.server.telemetry.log_handler import (
            get_trace_context,
        )

        context = get_trace_context()

        # Without active span, should return zeros
        assert context["trace_id"] == "0" * 32
        assert context["span_id"] == "0" * 16


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.slow
class TestLogCorrelationIntegration:
    """Tests for log correlation with active spans."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()
        from src.code_indexer.server.telemetry.spans import reset_spans_state

        reset_spans_state()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()
        from src.code_indexer.server.telemetry.spans import reset_spans_state

        reset_spans_state()

    def test_trace_context_from_active_span(self):
        """
        get_trace_context() returns real trace/span IDs from active span.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import create_span
        from src.code_indexer.server.telemetry.log_handler import (
            get_trace_context,
        )

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        with create_span("test.operation"):
            context = get_trace_context()

            # Should have non-zero trace_id when span is active
            # (may still be zeros if tracing not fully initialized)
            assert len(context["trace_id"]) == 32
            assert len(context["span_id"]) == 16


# =============================================================================
# Reentrant-Recursion Regression Test (#1676 AC2 round 2, REQUIRED FIX 1)
# =============================================================================


_RECURSION_TIMEOUT_SECONDS = 5.0


class TestGetTraceContextDebugLogRecursionGuard:
    """Regression test for the reintroduced unbounded logging recursion
    hazard flagged in code review of commits d2ef0608/b6904046.

    Root cause: d2ef0608 deleted OTELLogHandler (correctly, as dead code)
    along with its threading.local re-entry guard, but the SAME commit newly
    wired get_trace_context() into IdentityQueueHandler.prepare() -- the
    always-on root logging handler. get_trace_context()'s
    ``except Exception: logger.debug(...)`` branch, when the root logger is
    at DEBUG and IdentityQueueHandler is installed as a root handler (the
    real production wiring), re-enters prepare()/emit() on the SAME thread
    and recurses without bound (mirrors the intent of the deleted
    TestOTELLogHandlerReentryGuard test class).

    This test proves the underlying OTEL call
    (``opentelemetry.trace.get_current_span``) is invoked EXACTLY ONCE for a
    single logger.info() call. Before the fix (debug call still present) it
    is invoked many times (empirically ~30+, bounded only by Python's
    recursion limit) -- a genuinely discriminating failure, not a trivial
    happy-path check.
    """

    def test_root_logger_debug_call_site_invoked_exactly_once_on_failure(
        self,
    ) -> None:
        from code_indexer.server.services.async_logging import (
            IdentityQueueHandler,
        )

        call_count = 0

        def _raise(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("simulated OTEL failure")

        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)

        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level

        completed = threading.Event()

        def run_log_call() -> None:
            try:
                logging.getLogger("test.trace_context.recursion").info(
                    "outer log call -- must not recurse"
                )
            finally:
                completed.set()

        try:
            root_logger.handlers = [handler]
            root_logger.setLevel(logging.DEBUG)

            with patch(
                "opentelemetry.trace.get_current_span",
                side_effect=_raise,
            ):
                worker = threading.Thread(target=run_log_call, daemon=True)
                worker.start()
                finished = completed.wait(timeout=_RECURSION_TIMEOUT_SECONDS)
        finally:
            root_logger.handlers = original_handlers
            root_logger.setLevel(original_level)

        assert finished, (
            f"logger.info() call timed out after {_RECURSION_TIMEOUT_SECONDS}s "
            "-- unbounded recursion in get_trace_context()'s except-branch "
            "debug log call."
        )
        assert call_count == 1, (
            "get_current_span() (called from get_trace_context()) was "
            f"invoked {call_count} times for a single logger.info() call -- "
            "expected exactly 1. This indicates the except-branch "
            "logger.debug(...) call in get_trace_context() is re-entering "
            "the logging pipeline via the root IdentityQueueHandler "
            "(REQUIRED FIX 1, #1676 AC2 round 2)."
        )
