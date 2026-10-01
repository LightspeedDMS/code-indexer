"""Bug #1632: middleware.correlation.get_correlation_id() must delegate to
the WIRED telemetry.correlation_bridge reader.

Background: Story #1293 and Bug #1631 fixed 12 individual MCP handler
submodules that imported get_correlation_id from the UNWIRED
code_indexer.server.middleware.correlation module (whose
CorrelationContextMiddleware is NEVER registered in
startup/app_wiring.py -- only CorrelationBridgeMiddleware is). That left
~70 OTHER files across nearly every server subsystem still importing the
broken reader. Rather than sweep ~70 individual files, this bug fixes the
shared root: middleware.correlation's own get_correlation_id function now
delegates directly to telemetry.correlation_bridge's canonical
ContextVar, healing every existing call site without touching each file.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.telemetry.correlation_bridge import (
    CorrelationBridgeMiddleware,
    _correlation_id_var,
    set_current_correlation_id,
)


@pytest.fixture(autouse=True)
def _reset_bridge_contextvar():
    """Ensure no correlation ID leaks between tests via the shared ContextVar."""
    token = _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


class TestGetCorrelationIdDelegatesToBridge:
    """RED->GREEN: get_correlation_id() must read the SAME ContextVar the
    real, registered CorrelationBridgeMiddleware populates."""

    def test_get_correlation_id_returns_bridge_value_when_bridge_var_set(self):
        """Proves the fix directly: setting ONLY the bridge's ContextVar
        (as the real registered middleware does) must be visible through
        middleware.correlation.get_correlation_id() -- the reader ~70
        files still import. Before the fix this returns None because
        middleware.correlation reads its own separate, never-populated
        ContextVar instead of the bridge's."""
        from code_indexer.server.middleware.correlation import get_correlation_id

        set_current_correlation_id("bridge-set-value-1632")

        assert get_correlation_id() == "bridge-set-value-1632", (
            "middleware.correlation.get_correlation_id() must delegate to "
            "telemetry.correlation_bridge.get_current_correlation_id() -- "
            "it returned a value that does not match what the real, "
            "registered CorrelationBridgeMiddleware would have set."
        )


class TestRealAppRealMiddlewareStackProvesFix:
    """Component-level proof: build a real FastAPI app with the REAL
    registered CorrelationBridgeMiddleware (matching production's
    startup/app_wiring.py wiring exactly -- see test_request_tracing.py's
    established pattern for constructing this), issue a real HTTP request
    via TestClient, and assert middleware.correlation.get_correlation_id()
    returns a REAL, non-None correlation ID downstream in the route
    handler, matching the response's X-Correlation-ID header."""

    def test_middleware_correlation_get_correlation_id_sees_real_request_id(self):
        from code_indexer.server.middleware.correlation import get_correlation_id

        app = FastAPI()
        app.add_middleware(CorrelationBridgeMiddleware)
        captured: dict = {"value": "UNSET"}

        @app.get("/probe")
        async def probe():
            captured["value"] = get_correlation_id()
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get(
                "/probe", headers={"X-Correlation-ID": "real-request-id-1632"}
            )

        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"] == "real-request-id-1632"
        assert captured["value"] == "real-request-id-1632", (
            "get_correlation_id() did not see the real correlation ID "
            "populated by the ACTUALLY-registered CorrelationBridgeMiddleware "
            "during a real HTTP request."
        )


class TestLoggingUtilsGetLogExtraIncludesRealCorrelationId:
    """Highest-impact call site: logging_utils.get_log_extra() is used
    server-wide to attach correlation_id to structured log `extra=` dicts.
    It silently OMITS the field when get_correlation_id() returns None
    (guards with `if correlation_id:`), so before this fix every log line
    built via get_log_extra() during a real request silently dropped
    correlation_id entirely."""

    def test_get_log_extra_includes_real_correlation_id_within_request_context(self):
        from code_indexer.server.logging_utils import get_log_extra

        app = FastAPI()
        app.add_middleware(CorrelationBridgeMiddleware)
        captured: dict = {}

        @app.get("/probe")
        async def probe():
            captured["extra"] = get_log_extra("TEST-CODE-1632")
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get(
                "/probe", headers={"X-Correlation-ID": "log-extra-real-id-1632"}
            )

        assert response.status_code == 200
        assert captured["extra"]["error_code"] == "TEST-CODE-1632"
        assert captured["extra"].get("correlation_id") == "log-extra-real-id-1632", (
            "get_log_extra() must include the REAL correlation_id set by "
            "the registered CorrelationBridgeMiddleware -- it either "
            "omitted the key entirely (pre-fix behavior, since None is "
            "falsy) or returned the wrong value."
        )
