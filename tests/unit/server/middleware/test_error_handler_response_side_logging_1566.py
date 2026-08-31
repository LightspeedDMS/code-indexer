"""
Bug #1566: GlobalErrorHandler's ``except HTTPException`` branch is dead code
under real Starlette/FastAPI middleware ordering, so HTTP error responses
were never WARNING-logged.

Mechanism: Starlette installs its own ExceptionMiddleware INSIDE every
user-added BaseHTTPMiddleware layer (GlobalErrorHandler included). When a
route raises HTTPException, ExceptionMiddleware converts it to a Response
before it can ever propagate out to GlobalErrorHandler.dispatch()'s own
``except HTTPException`` clause -- that clause is therefore dead code for
ordinary request handling.

These tests drive a REAL FastAPI app through a REAL ASGI stack (TestClient),
with GlobalErrorHandler installed exactly as app_wiring.py installs it
(``app.add_middleware(GlobalErrorHandler)``), instead of calling
``dispatch()``/``handle_*`` methods directly. A direct-call unit test would
PASS today and would not have caught this bug -- that is precisely why it
went unnoticed (see the issue body's own review note).
"""

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_indexer.server.middleware.error_handler import GlobalErrorHandler

_LOGGER_NAME = "code_indexer.server.middleware.error_handler"


def _build_app_with_route(status_code: int, detail: str) -> FastAPI:
    """Real FastAPI app with GlobalErrorHandler wired the same way app_wiring.py wires it."""
    app = FastAPI()
    app.add_middleware(GlobalErrorHandler)

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=status_code, detail=detail)

    return app


def _get_warning_records_for_status(caplog, status_code: int, detail: str):
    """Drive /boom through a real ASGI stack and return the WARNING records produced."""
    app = _build_app_with_route(status_code, detail)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")

    assert response.status_code == status_code
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class TestServerErrorResponsesAreWarningLogged:
    """5xx HTTPException responses must be WARNING-logged (Bug #1566)."""

    @pytest.mark.parametrize(
        "status_code,detail",
        [
            (500, "internal error"),
            (503, "database unavailable"),
        ],
    )
    def test_5xx_http_exception_produces_exactly_one_warning_log(
        self, caplog, status_code, detail
    ):
        warning_records = _get_warning_records_for_status(caplog, status_code, detail)

        assert len(warning_records) == 1, (
            f"Expected exactly one WARNING log record for a {status_code} "
            f"HTTPException response, got {len(warning_records)}: "
            f"{[r.getMessage() for r in warning_records]}"
        )
        assert str(status_code) in warning_records[0].getMessage()


class TestRoutineClientErrorsDoNotFloodWarning:
    """
    401/403/404/422/429 are the routine, expected outcome of normal traffic
    (unauthenticated probes, expired tokens, missing resources, admission
    shedding) -- they must NOT be WARNING-logged. Blindly restoring
    "log every HTTP error at WARNING" would repeat the exact by-design-noise
    mistake Bug #1565 already had to walk back.
    """

    @pytest.mark.parametrize(
        "status_code,detail",
        [
            (401, "Unauthorized"),
            (403, "Forbidden"),
            (404, "Not Found"),
            (422, "Unprocessable"),
            (429, "Too Many Requests"),
        ],
    )
    def test_routine_4xx_does_not_produce_warning_log(
        self, caplog, status_code, detail
    ):
        warning_records = _get_warning_records_for_status(caplog, status_code, detail)

        assert warning_records == [], (
            f"Routine {status_code} response must not produce a WARNING log, "
            f"got: {[r.getMessage() for r in warning_records]}"
        )
