"""
TDD Tests for Custom Spans (Story #700).

Tests @traced decorator, create_span() context manager, and span helpers.

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
# Custom Spans Import Tests
# =============================================================================


class TestCustomSpansImport:
    """Tests for custom spans module import behavior."""

    def test_traced_decorator_can_be_imported(self):
        """traced decorator can be imported."""
        from src.code_indexer.server.telemetry.spans import traced

        assert traced is not None
        assert callable(traced)

    def test_create_span_can_be_imported(self):
        """create_span context manager can be imported."""
        from src.code_indexer.server.telemetry.spans import create_span

        assert create_span is not None

    def test_get_tracer_can_be_imported(self):
        """get_tracer function can be imported."""
        from src.code_indexer.server.telemetry.spans import get_tracer

        assert callable(get_tracer)


# =============================================================================
# @traced Decorator Tests
# =============================================================================


@pytest.mark.slow
class TestTracedDecorator:
    """Tests for @traced decorator functionality."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_traced_decorator_creates_span_with_function_name(self):
        """
        @traced decorator creates span named after function.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        _telemetry_manager = get_telemetry_manager(config)  # noqa: F841

        @traced()
        def sample_function():
            return "result"

        # Call the decorated function - should not raise
        result = sample_function()
        assert result == "result"

    def test_traced_decorator_with_custom_name(self):
        """
        @traced decorator can use custom span name.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        @traced(name="cidx.custom.operation")
        def another_function():
            return 42

        result = another_function()
        assert result == 42

    def test_traced_decorator_preserves_function_signature(self):
        """
        @traced decorator preserves function name and docstring.
        """
        from src.code_indexer.server.telemetry.spans import traced

        @traced()
        def documented_function(arg1: str, arg2: int = 10) -> str:
            """This is the docstring."""
            return f"{arg1}:{arg2}"

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ is not None
        assert "docstring" in documented_function.__doc__

    def test_traced_decorator_handles_exceptions(self):
        """
        @traced decorator records exception in span and re-raises.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced
        import pytest

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        @traced()
        def failing_function():
            raise ValueError("intentional error")

        with pytest.raises(ValueError, match="intentional error"):
            failing_function()

    def test_traced_decorator_with_attributes(self):
        """
        @traced decorator can add custom attributes to span.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        @traced(attributes={"operation.type": "search", "repository": "test-repo"})
        def search_with_attrs():
            return "found"

        result = search_with_attrs()
        assert result == "found"


# =============================================================================
# create_span() Context Manager Tests
# =============================================================================


@pytest.mark.slow
class TestCreateSpanContextManager:
    """Tests for create_span() context manager."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_create_span_as_context_manager(self):
        """
        create_span() works as context manager.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import create_span

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        with create_span("cidx.test.operation") as span:
            # Span should be created
            assert span is not None

    def test_create_span_with_attributes(self):
        """
        create_span() can set attributes on span.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import create_span

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        with create_span(
            "cidx.search.semantic",
            attributes={"query": "test", "limit": 10, "repository": "my-repo"},
        ) as span:
            assert span is not None

    def test_create_span_records_exception(self):
        """
        create_span() records exceptions in span.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import create_span
        import pytest

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        with pytest.raises(RuntimeError, match="span error"):
            with create_span("cidx.test.failing"):
                raise RuntimeError("span error")

    def test_create_span_adds_correlation_id(self):
        """
        create_span() includes correlation ID when available.
        """
        from src.code_indexer.server.telemetry import (
            get_telemetry_manager,
            set_current_correlation_id,
        )
        from src.code_indexer.server.telemetry.spans import create_span

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        # Set correlation ID
        set_current_correlation_id("test-correlation-123")

        with create_span("cidx.test.correlated") as span:
            # Span should have correlation.id attribute
            assert span is not None

    def test_create_span_adds_correlation_id_to_real_exported_span(self):
        """
        Behavioral regression guard for the `src.`-prefixed import bug
        (Story #1586 code-review round 2, Codex finding): the pre-existing
        test above only asserts `span is not None`, which would still pass
        even if _get_correlation_id() silently returned None on every call
        (e.g. due to the broken import). This test proves the correlation
        id set via set_current_correlation_id() actually reaches a REAL,
        finished, exported OTEL span's `correlation.id` attribute -- not
        just that create_span() doesn't blow up.

        Uses the real otel_test_support.active_span_exporter() helper (a
        genuine TracerProvider + InMemorySpanExporter), already reused by
        other span-wiring tests in this story -- no mocking of create_span,
        the tracer, or the correlation bridge.
        """
        from code_indexer.server.telemetry.correlation_bridge import (
            set_current_correlation_id,
        )
        from code_indexer.server.telemetry.spans import create_span

        from tests.unit.server.telemetry.otel_test_support import (
            active_span_exporter,
        )

        set_current_correlation_id("real-correlation-id-456")

        with active_span_exporter() as exporter:
            with create_span("cidx.test.correlated"):
                pass

        finished_spans = [
            s for s in exporter.get_finished_spans() if s.name == "cidx.test.correlated"
        ]
        assert len(finished_spans) == 1, "cidx.test.correlated span not emitted"
        attributes = finished_spans[0].attributes
        assert attributes is not None
        assert attributes.get("correlation.id") == "real-correlation-id-456"


# =============================================================================
# Noop When Disabled Tests
# =============================================================================


class TestNoopWhenDisabled:
    """Tests for no-op behavior when telemetry disabled."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_traced_decorator_noop_when_disabled(self):
        """
        @traced decorator is no-op when telemetry disabled.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced

        config = TelemetryConfig(
            enabled=False,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        @traced()
        def disabled_function():
            return "still works"

        result = disabled_function()
        assert result == "still works"

    def test_create_span_noop_when_disabled(self):
        """
        create_span() is no-op when telemetry disabled.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import create_span

        config = TelemetryConfig(
            enabled=False,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        with create_span("cidx.test.disabled") as _span:  # noqa: F841
            # Should still work even with dummy span
            pass


# =============================================================================
# Async Support Tests
# =============================================================================


@pytest.mark.slow
class TestAsyncSupport:
    """Tests for async function support."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_traced_decorator_supports_async(self):
        """
        @traced decorator works with async functions.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager
        from src.code_indexer.server.telemetry.spans import traced
        import asyncio

        config = TelemetryConfig(
            enabled=True,
            export_traces=True,
            collector_endpoint="http://localhost:4317",
        )
        get_telemetry_manager(config)

        @traced()
        async def async_operation():
            await asyncio.sleep(0.01)
            return "async result"

        result = asyncio.run(async_operation())
        assert result == "async result"


class TestCorrelationIdImportRobustness:
    """Bug (Story #1586 code-review Finding 9): spans.py's
    _get_correlation_id() imports correlation_bridge via the 'src.'-prefixed
    module path, which does not exist in a non-editable production install
    (setuptools ships only the real 'code_indexer' package, never a
    top-level 'src' namespace package). The broken import raises
    ModuleNotFoundError there, silently swallowed by a bare
    'except ImportError: return None', so every span in a real deployment
    silently carries no correlation.id attribute.

    Verified via real source inspection (inspect.getsource) of the actual
    function under test -- no mocking of the import system or of the
    function itself.
    """

    def test_get_correlation_id_does_not_use_broken_src_prefixed_import(self):
        import inspect

        from code_indexer.server.telemetry import spans

        source = inspect.getsource(spans._get_correlation_id)

        assert "from src.code_indexer" not in source
        assert "from code_indexer.server.telemetry.correlation_bridge import" in source


class TestSingletonHelperLifoRestore:
    """Story #1586 code-review round 2, Codex finding: the test-support
    singleton helpers (active_job_metrics_singleton() /
    active_application_metrics_singleton()) unconditionally cleared the
    global TelemetryManager singleton to None on exit instead of restoring
    whatever manager was active before the context was entered -- a real
    latent bug in shared test infrastructure. A nested use already exists
    in-tree (tests/e2e/server/test_20_telemetry_metrics_wiring_1586.py nests
    active_job_metrics_singleton() inside
    active_application_metrics_singleton()): entering the inner context
    used to shut down the OUTER's manager and replace the global, and the
    outer's own exit then cleared to None instead of restoring what was
    there before the outer context started.

    Real OTEL SDK objects throughout (via the real helpers under test) --
    MESSI Rule #1: no mocks of the code under test.
    """

    def test_nested_job_metrics_singleton_restores_outer_application_metrics_manager_without_shutdown(
        self,
    ):
        from code_indexer.server.telemetry.manager import peek_telemetry_manager

        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics_singleton,
            active_job_metrics_singleton,
        )

        with active_application_metrics_singleton() as (_app_metrics, _areader):
            outer_manager = peek_telemetry_manager()
            assert outer_manager is not None

            with active_job_metrics_singleton() as (_job_metrics, _jreader):
                # While the inner context is active, the global singleton
                # points at the inner's own manager, not the outer's.
                assert peek_telemetry_manager() is not outer_manager
                # The outer's manager must survive, untouched, for the
                # duration of the (still-active) inner context.
                assert outer_manager.is_initialized is True

            # LIFO restore: after the inner context exits, the OUTER's
            # manager must be back in place as the global singleton --
            # never cleared to None, and never shut down along the way.
            assert peek_telemetry_manager() is outer_manager
            assert outer_manager.is_initialized is True
