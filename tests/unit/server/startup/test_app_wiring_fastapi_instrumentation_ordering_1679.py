"""
Bug #1679 regression guard: FastAPI OTEL instrumentation call-site ordering.

Round-3 code review of the Bug #1679 fix found that
tests/unit/server/telemetry/test_request_tracing.py's
TestFastAPIInstrumentationOrderingMechanism1679 -- while a legitimate
characterization test of the third-party OTEL+Starlette ordering
mechanism -- never touches the actual production code path
(app_wiring.py / create_fastapi_app() / lifespan.py / instrument_fastapi()
itself). It builds its own bare FastAPI() and calls
FastAPIInstrumentor.instrument_app() directly, so it would still pass
unchanged if someone:

  1. Moved instrument_fastapi(app) back inside lifespan() (the ORIGINAL
     Bug #1679 mistake), or
  2. Deleted the instrument_fastapi(app) call entirely, or
  3. Reverted/inverted the Finding-1 telemetry_config.enabled gate so it
     never instruments (or always does, regardless of config).

This module follows the same structural-inspection pattern already used
in this codebase for wiring verification (see
test_app_wiring_consumer_rate_limiter_pool_1332.py and
test_lifespan_clone_backend_wiring_bug1044.py) -- it reads the real
production source and asserts the actual call-site ordering, rather than
reconstructing create_fastapi_app()'s ~20 unpacked service dependencies
just to observe one call.
"""

from __future__ import annotations

import inspect

from code_indexer.server.startup import app_wiring, lifespan


class TestAppWiringInstrumentsFastAPIBeforeMiddleware:
    """Pins the Bug #1679 fix: instrument_fastapi(app) must run at
    construction time, before any app.add_middleware() call builds up the
    middleware list Starlette will freeze into its stack."""

    def test_instrument_fastapi_call_present_before_first_add_middleware(self):
        source = inspect.getsource(app_wiring.create_fastapi_app)

        assert "instrument_fastapi(app)" in source, (
            "Bug #1679: create_fastapi_app() no longer calls "
            "instrument_fastapi(app) -- FastAPI OTEL tracing would never "
            "be applied at all."
        )

        instrument_idx = source.index("instrument_fastapi(app)")
        add_middleware_idx = source.index("app.add_middleware(")

        assert instrument_idx < add_middleware_idx, (
            "Bug #1679: instrument_fastapi(app) must be called BEFORE the "
            "first app.add_middleware(...) call in create_fastapi_app() -- "
            "Starlette builds its middleware stack lazily on the first "
            "ASGI message it receives, so instrumenting too late (e.g. "
            "moved back inside lifespan()) is a silent structural no-op "
            "that produces zero HTTP request spans on every deployment."
        )

    def test_instrument_fastapi_call_is_gated_by_telemetry_enabled(self):
        """Pins the round-2 Finding-1 fix: the call must be gated on
        telemetry_config.enabled, not applied unconditionally (which adds
        real per-request OpenTelemetryMiddleware overhead even when
        telemetry is fully disabled)."""
        source = inspect.getsource(app_wiring.create_fastapi_app)

        assert "_telemetry_cfg.enabled" in source, (
            "Bug #1679 Finding 1: create_fastapi_app() no longer gates "
            "FastAPI instrumentation on telemetry_config.enabled -- this "
            "would reintroduce real per-request overhead on every "
            "deployment with telemetry disabled."
        )

        gate_idx = source.index("_telemetry_cfg.enabled")
        instrument_idx = source.index("instrument_fastapi(app)")

        assert gate_idx < instrument_idx, (
            "Bug #1679 Finding 1: the telemetry_config.enabled gate must "
            "precede (guard) the instrument_fastapi(app) call, not follow "
            "it -- otherwise instrumentation would run unconditionally "
            "regardless of the gate's presence in the source."
        )


class TestLifespanNeverCallsInstrumentFastapiWithApp:
    """Pins that instrument_fastapi() never moves back into lifespan()."""

    def test_lifespan_source_has_no_instrument_fastapi_call_with_app_arg(self):
        """lifespan.py legitimately still mentions the bare function name
        `instrument_fastapi()` in an explanatory comment (documenting WHY
        it is no longer called there) -- match on the actual call SHAPE
        (with an `app` argument) rather than the bare name, to avoid a
        false positive on that comment."""
        source = inspect.getsource(lifespan)

        assert "instrument_fastapi(app" not in source, (
            "Bug #1679: instrument_fastapi() is being called with an app "
            "argument inside lifespan.py again. Starlette has already "
            "built its ASGI middleware stack by the time the lifespan() "
            "body executes, so calling instrument_fastapi(app) here is a "
            "structural no-op -- it must only be called from "
            "app_wiring.py's create_fastapi_app(), immediately after "
            "FastAPI(...) is constructed."
        )
