"""
Bug #1648: GlobalErrorHandler's unhandled-exception path generates a NEW,
unrelated correlation id (via generate_correlation_id()) for the error
response body's ``correlation_id`` field and the internal log message's
``[ID: ...]`` text -- instead of reusing the ambient request's correlation
id (the one already resolved by get_correlation_id(), returned in the
``X-Correlation-ID`` response header by the REGISTERED
CorrelationBridgeMiddleware, and correctly persisted to the log store's
``correlation_id`` column per Bug #1641's fix).

Evidence from the original report: a request with
``X-Correlation-ID: 7a225a32-...`` produced a log row with
``correlation_id = '7a225a32-...'`` (correct, via #1641's
inject_correlation_id() wiring) but a log message body containing
``[ID: 67f66c00-...]`` -- a completely different, unrelated id generated
fresh inside handle_unhandled_exception(). The same fresh id also leaks
into the client-facing JSON error response's ``correlation_id`` field,
since both come from the same local variable.

These tests drive a REAL FastAPI app through a REAL ASGI stack
(TestClient), with GlobalErrorHandler and CorrelationBridgeMiddleware wired
in the EXACT same order app_wiring.py uses (GlobalErrorHandler added
first, CorrelationBridgeMiddleware added second -- making the bridge the
OUTERMOST layer, matching production), so the ambient contextvar is
genuinely populated before GlobalErrorHandler's exception-handling code
runs -- exactly like Bug #1566's test file, which found a real defect a
direct dispatch()/handle_*() unit test would have missed.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from code_indexer.server.middleware.error_handler import GlobalErrorHandler
from code_indexer.server.telemetry.correlation_bridge import (
    CorrelationBridgeMiddleware,
    _correlation_id_var,
    set_current_correlation_id,
)

_LOGGER_NAME = "code_indexer.server.middleware.error_handler"
_KNOWN_CORRELATION_ID = "7a225a32-2767-4c4f-a47a-bc48fd1680c8"


@pytest.fixture(autouse=True)
def _reset_correlation_contextvar():
    """Prevent correlation-id leakage between tests via the shared ContextVar."""
    token = _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


def _build_app_with_unhandled_exception_route() -> FastAPI:
    """Real FastAPI app wired exactly as app_wiring.py wires the two
    relevant middlewares (GlobalErrorHandler added first, then
    CorrelationBridgeMiddleware -- making it the outermost layer)."""
    app = FastAPI()
    app.add_middleware(GlobalErrorHandler)
    app.add_middleware(CorrelationBridgeMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError("genuinely unexpected failure")

    return app


class TestUnhandledExceptionBodyMatchesAmbientCorrelationId:
    """The client-facing JSON error body's correlation_id must equal the
    ambient request's correlation id (the same value returned in the
    X-Correlation-ID response header) -- never a freshly generated,
    unrelated one."""

    def test_error_response_body_correlation_id_matches_header_and_request_id(self):
        app = _build_app_with_unhandled_exception_route()

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/boom", headers={"X-Correlation-ID": _KNOWN_CORRELATION_ID}
            )

        assert response.status_code == 500
        header_correlation_id = response.headers["x-correlation-id"]
        assert header_correlation_id == _KNOWN_CORRELATION_ID, (
            "sanity check: CorrelationBridgeMiddleware must echo back the "
            "request's own X-Correlation-ID header"
        )

        body = response.json()
        assert body["correlation_id"] == _KNOWN_CORRELATION_ID, (
            "error response body's correlation_id must match the ambient "
            f"request correlation id ({_KNOWN_CORRELATION_ID!r}) and the "
            f"x-correlation-id response header ({header_correlation_id!r}), "
            f"but got a different, unrelated id: {body['correlation_id']!r}"
        )

    def test_error_log_message_id_matches_ambient_correlation_id(self, caplog):
        app = _build_app_with_unhandled_exception_route()

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/boom", headers={"X-Correlation-ID": _KNOWN_CORRELATION_ID}
                )

        assert response.status_code == 500

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        log_message = error_records[0].getMessage()

        assert f"[ID: {_KNOWN_CORRELATION_ID}]" in log_message, (
            "the internal error-log message's embedded [ID: ...] text must "
            "reuse the ambient request correlation id, matching the "
            "correlation_id column the log store persists for this same "
            f"log row (Bug #1641); got log message: {log_message!r}"
        )


class TestHandleUnhandledExceptionDirectUnit:
    """Direct unit coverage of handle_unhandled_exception()'s correlation id
    resolution, independent of the full ASGI stack."""

    @pytest.fixture
    def error_handler(self) -> GlobalErrorHandler:
        return GlobalErrorHandler()

    @pytest.fixture
    def real_request(self) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/whatever",
                "headers": [(b"host", b"testserver")],
                "query_string": b"",
                "root_path": "",
            }
        )

    def test_reuses_ambient_correlation_id_when_present(
        self, error_handler: GlobalErrorHandler, real_request: Request
    ):
        set_current_correlation_id("ambient-request-id-1648")

        response_data = error_handler.handle_unhandled_exception(
            RuntimeError("boom"), real_request
        )

        assert response_data["correlation_id"] == "ambient-request-id-1648"

    def test_falls_back_to_a_fresh_id_when_no_ambient_context(
        self, error_handler: GlobalErrorHandler, real_request: Request
    ):
        # No set_current_correlation_id() call -- ambient context is empty
        # (the _reset_correlation_contextvar autouse fixture ensures this).
        response_data = error_handler.handle_unhandled_exception(
            RuntimeError("boom"), real_request
        )

        correlation_id = response_data["correlation_id"]
        assert correlation_id, "must still generate SOME id as a fallback"
        assert isinstance(correlation_id, str)
