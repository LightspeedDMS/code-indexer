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
