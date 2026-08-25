"""
TDD Tests for Request Tracing with Correlation ID Bridge (Story #697).

Tests FastAPI auto-instrumentation and X-Correlation-ID to OTEL span bridging.

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
# Instrumentation Module Import Tests
# =============================================================================


class TestInstrumentationImport:
    """Tests for instrumentation module import behavior."""

    def test_instrument_fastapi_function_can_be_imported(self):
        """instrument_fastapi() function can be imported."""
        from src.code_indexer.server.telemetry.instrumentation import (
            instrument_fastapi,
        )

        assert callable(instrument_fastapi)

    def test_uninstrument_fastapi_function_can_be_imported(self):
        """uninstrument_fastapi() function can be imported."""
        from src.code_indexer.server.telemetry.instrumentation import (
            uninstrument_fastapi,
        )

        assert callable(uninstrument_fastapi)


# =============================================================================
# Correlation Bridge Import Tests
# =============================================================================


class TestCorrelationBridgeImport:
    """Tests for correlation bridge module import behavior."""

    def test_correlation_bridge_middleware_can_be_imported(self):
        """CorrelationBridgeMiddleware can be imported."""
        from src.code_indexer.server.telemetry.correlation_bridge import (
            CorrelationBridgeMiddleware,
        )

        assert CorrelationBridgeMiddleware is not None

    def test_get_current_correlation_id_can_be_imported(self):
        """get_current_correlation_id() function can be imported."""
        from src.code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        assert callable(get_current_correlation_id)


# =============================================================================
# Instrumentation Behavior Tests
# =============================================================================


@pytest.mark.slow
class TestFastAPIInstrumentation:
    """Tests for FastAPI auto-instrumentation."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_instrument_fastapi_structurally_instruments_app(self):
        """
        instrument_fastapi(app) structurally instruments the app.

        Bug #1679: instrument_fastapi() no longer takes a TelemetryManager
        and no longer gates on telemetry config at all -- it must be
        callable (and succeed) at FastAPI-app-construction time, before
        any TelemetryManager exists. Whether spans actually get exported
        is decided entirely by TelemetryManager's own tracer-provider
        wiring (see test_instrument_fastapi_no_export_when_traces_disabled
        below), never by this function.
        """
        from fastapi import FastAPI
        from src.code_indexer.server.telemetry.instrumentation import (
            instrument_fastapi,
            uninstrument_fastapi,
        )

        app = FastAPI()

        # Instrument the app -- no telemetry_manager argument needed
        result = instrument_fastapi(app)

        # Should return True indicating instrumentation was applied
        assert result is True
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True

        # Cleanup -- per-app, not global (Bug #1679 round-2 review Finding 3)
        uninstrument_fastapi(app)

    def test_instrument_fastapi_no_export_when_traces_disabled(self):
        """
        Bug #1679 requirement: when export_traces is False, zero export
        overhead is guaranteed -- but the guarantee lives in
        TelemetryManager's own tracer-provider wiring (no span processor
        is ever attached), not in instrument_fastapi() refusing to patch
        the app. This test asserts the invariant at the level where it is
        actually enforced: TelemetryManager._setup_trace_exporter() is
        only ever called when export_traces is True, so a manager built
        with export_traces=False has a real TracerProvider (so callers
        can still record no-op-cost spans) with NO span processors
        attached -- nothing for a span to be exported to.

        TelemetryConfig is imported at module scope (see top of this
        file) alongside pytest.
        """
        from src.code_indexer.server.telemetry import get_telemetry_manager

        # collector_endpoint intentionally omitted: export_traces=False
        # means TelemetryManager never even constructs an exporter, so no
        # collector address is needed for this test at all -- relying on
        # TelemetryConfig's own default value here would be misleading.
        config = TelemetryConfig(enabled=True, export_traces=False)
        telemetry_manager = get_telemetry_manager(config)

        tracer_provider = telemetry_manager.tracer_provider
        assert tracer_provider is not None

        # No span processor (i.e. no exporter) was ever attached when
        # export_traces is False -- confirms zero export overhead
        # regardless of whether FastAPI instrumentation is structurally
        # applied elsewhere.
        processors = tracer_provider._active_span_processor._span_processors
        assert processors == (), (
            f"Expected zero span processors when export_traces=False, "
            f"got: {processors!r}"
        )


class TestFastAPIInstrumentationOrderingMechanism1679:
    """
    Fast (no 'slow' marker), non-subprocess characterization test of the
    THIRD-PARTY Starlette/FastAPIInstrumentor ordering mechanism Bug #1679
    depends on -- it does NOT exercise this project's own production code
    path and is NOT, by itself, a regression guard for the real call site.

    Uses a bare FastAPI() app (built by this test, not create_app()) with
    an EXPLICIT local TracerProvider passed directly to
    FastAPIInstrumentor.instrument_app(app, tracer_provider=...) -- this
    bypasses OTEL's "global tracer provider settable only once per
    process" constraint entirely (no subprocess needed), while still
    exercising the REAL FastAPIInstrumentor/TracerProvider/
    InMemorySpanExporter/TestClient machinery. No mocks.

    Round-3 code review correctly identified that this class never
    imports or calls app_wiring.py's create_fastapi_app(), lifespan.py,
    or even this project's own instrument_fastapi() wrapper -- so it
    would keep passing unchanged even if someone moved the real
    instrument_fastapi(app) call back inside lifespan(), deleted it
    entirely, or reverted the telemetry_config.enabled gate. Its value is
    narrower: it pins that FastAPIInstrumentor+Starlette really do behave
    the way this fix's whole design depends on (instrument-before-build
    succeeds, instrument-after-build is a silent no-op).

    The ACTUAL regression guard for the real production call site (its
    presence, its ordering relative to app.add_middleware(), its
    telemetry_config.enabled gate, and its absence from lifespan.py) is
    tests/unit/server/startup/test_app_wiring_fastapi_instrumentation_ordering_1679.py,
    which inspects the real create_fastapi_app()/lifespan source directly.
    """

    @staticmethod
    def _build_app_with_local_tracer():
        from fastapi import FastAPI
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        app = FastAPI()

        @app.get("/ping")
        def ping():
            return {"ok": True}

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return app, provider, exporter

    def test_instrument_before_stack_build_produces_real_spans(self):
        """Matches the FIXED call site: instrument_fastapi(app) in
        app_wiring.py runs immediately after FastAPI(...), before
        Starlette ever builds the middleware stack."""
        from fastapi.testclient import TestClient
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        app, provider, exporter = self._build_app_with_local_tracer()

        assert app.middleware_stack is None, (
            "Precondition: Starlette must not have built its middleware "
            "stack yet, or this test cannot distinguish before/after."
        )

        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        try:
            with TestClient(app) as client:
                response = client.get("/ping")

            assert response.status_code == 200
            assert len(exporter.get_finished_spans()) > 0, (
                "Expected real spans when instrumented BEFORE the "
                "middleware stack is built (the fixed ordering)."
            )
        finally:
            FastAPIInstrumentor.uninstrument_app(app)

    def test_instrument_after_stack_build_produces_zero_spans(self):
        """Reproduces the Bug #1679 mechanism directly: instrumenting
        AFTER Starlette has already built its middleware stack (matching
        the pre-fix call site inside lifespan()) is a structural no-op."""
        from fastapi.testclient import TestClient
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        app, provider, exporter = self._build_app_with_local_tracer()

        # Simulate what Starlette does automatically on the very first
        # ASGI message it receives (the "lifespan" startup message
        # itself, BEFORE lifespan() body runs) -- freezes the middleware
        # stack using the UNPATCHED build_middleware_stack.
        app.middleware_stack = app.build_middleware_stack()

        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        try:
            with TestClient(app) as client:
                response = client.get("/ping")

            assert response.status_code == 200
            assert len(exporter.get_finished_spans()) == 0, (
                "Expected ZERO spans when instrumented AFTER the "
                "middleware stack is already built -- this reproduces "
                "Bug #1679's exact structural no-op."
            )
        finally:
            FastAPIInstrumentor.uninstrument_app(app)


# =============================================================================
# Correlation Bridge Behavior Tests
# =============================================================================


class TestCorrelationBridgeMiddleware:
    """Tests for CorrelationBridgeMiddleware behavior."""

    def setup_method(self):
        """Reset singletons before each test."""
        reset_all_singletons()

    def teardown_method(self):
        """Reset singletons after each test."""
        reset_all_singletons()

    def test_middleware_extracts_correlation_id_from_header(self):
        """
        Middleware extracts X-Correlation-ID from request headers.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.code_indexer.server.telemetry.correlation_bridge import (
            CorrelationBridgeMiddleware,
            get_current_correlation_id,
        )

        app = FastAPI()
        app.add_middleware(CorrelationBridgeMiddleware)

        captured_correlation_id = None

        @app.get("/test")
        async def test_endpoint():
            nonlocal captured_correlation_id
            captured_correlation_id = get_current_correlation_id()
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get(
            "/test", headers={"X-Correlation-ID": "test-correlation-123"}
        )

        assert response.status_code == 200
        assert captured_correlation_id == "test-correlation-123"

    def test_middleware_generates_correlation_id_when_missing(self):
        """
        Middleware generates correlation ID when header is missing.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.code_indexer.server.telemetry.correlation_bridge import (
            CorrelationBridgeMiddleware,
            get_current_correlation_id,
        )

        app = FastAPI()
        app.add_middleware(CorrelationBridgeMiddleware)

        captured_correlation_id = None

        @app.get("/test")
        async def test_endpoint():
            nonlocal captured_correlation_id
            captured_correlation_id = get_current_correlation_id()
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get("/test")  # No X-Correlation-ID header

        assert response.status_code == 200
        # Should have generated a correlation ID
        assert captured_correlation_id is not None
        assert len(captured_correlation_id) > 0

    def test_middleware_adds_correlation_id_to_response_header(self):
        """
        Middleware adds X-Correlation-ID to response headers.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.code_indexer.server.telemetry.correlation_bridge import (
            CorrelationBridgeMiddleware,
        )

        app = FastAPI()
        app.add_middleware(CorrelationBridgeMiddleware)

        @app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        client = TestClient(app)
        response = client.get(
            "/test", headers={"X-Correlation-ID": "response-test-456"}
        )

        assert response.status_code == 200
        assert "X-Correlation-ID" in response.headers
        assert response.headers["X-Correlation-ID"] == "response-test-456"


# =============================================================================
# Trace Sampling Tests
# =============================================================================


# =============================================================================
# Excluded Endpoints Tests
# =============================================================================


class TestExcludedEndpoints:
    """Tests for excluding health endpoints from tracing."""

    def test_health_endpoints_excluded_by_default(self):
        """
        Health endpoints are excluded from tracing.
        """
        from src.code_indexer.server.telemetry.instrumentation import (
            DEFAULT_EXCLUDED_URLS,
        )

        # Health endpoints should be in exclusion list
        assert "/health" in DEFAULT_EXCLUDED_URLS or "health" in DEFAULT_EXCLUDED_URLS
