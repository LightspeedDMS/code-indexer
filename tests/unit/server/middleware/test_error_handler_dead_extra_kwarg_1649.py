"""
Bug #1649: GlobalErrorHandler._log_error() passes ``extra={"correlation_id":
...}`` to ``format_error_log()`` -- a plain string-formatting helper that
accepts ``**context`` -- instead of to ``logger.error()``/``logger.warning()``
itself, which is the only place Python's ``logging`` module actually
understands the ``extra=`` keyword.

This is not a silent no-op: ``format_error_log(code, message, **context)``
treats the keyword argument name ``extra`` as just another context key, so
the dict value gets stringified straight into the log message text as a
confusing, redundant ``extra={'correlation_id': '...'}`` suffix -- on top of
the ``[ID: ...]`` text the message already carries. Confirmed directly:

    >>> format_error_log("X", "msg", extra={"correlation_id": "abc"})
    "[X] msg extra={'correlation_id': 'abc'}"

Bug #1641's fix (``IdentityQueueHandler.prepare()`` ->
``logging_utils.inject_correlation_id()``) already populates
``record.correlation_id`` centrally from the ambient request context,
independent of any per-call-site ``extra=`` usage -- so removing this dead
kwarg has zero effect on the log store's correlation_id column.

These tests drive a REAL FastAPI app through a REAL ASGI stack
(TestClient), mirroring the established Bug #1648 pattern
(test_error_handler_correlation_id_body_mismatch_1648.py), and assert the
captured log message text contains no ``extra=`` artifact.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_indexer.server.middleware.error_handler import GlobalErrorHandler
from code_indexer.server.telemetry.correlation_bridge import (
    CorrelationBridgeMiddleware,
)

_LOGGER_NAME = "code_indexer.server.middleware.error_handler"
_KNOWN_CORRELATION_ID = "6b6e6e6f-1111-4c4f-a47a-bc48fd1680c8"


def _drive_failing_route_and_capture_log_message(
    caplog,
    level: int,
    route_handler: Callable[[], None],
) -> str:
    """Wire GlobalErrorHandler + CorrelationBridgeMiddleware around a
    single failing GET route, hit it through a real TestClient with a
    known X-Correlation-ID, and return the sole captured log message text
    at the given level."""
    app = FastAPI()
    app.add_middleware(GlobalErrorHandler)
    app.add_middleware(CorrelationBridgeMiddleware)
    app.get("/boom")(route_handler)

    with caplog.at_level(level, logger=_LOGGER_NAME):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/boom", headers={"X-Correlation-ID": _KNOWN_CORRELATION_ID}
            )

    assert response.status_code == 500

    matching_records: List[logging.LogRecord] = [
        r for r in caplog.records if r.levelno == level
    ]
    assert len(matching_records) == 1
    return matching_records[0].getMessage()


class TestFormatErrorLogCallsNoDeadExtraKwarg:
    """The internal log message text produced by _log_error() must never
    contain the ``extra=`` artifact caused by passing a dead ``extra=``
    kwarg into format_error_log() (a plain **context-accepting formatter,
    not logger.error()/logger.warning() itself)."""

    def test_error_log_message_has_no_dead_extra_kwarg_artifact(self, caplog):
        def boom():
            raise RuntimeError("genuinely unexpected failure")

        log_message = _drive_failing_route_and_capture_log_message(
            caplog, logging.ERROR, boom
        )

        assert "extra=" not in log_message, (
            "the internal error-log message must not contain the dead "
            "extra={'correlation_id': ...} artifact produced by passing "
            "extra= into format_error_log() instead of logger.error(); "
            f"got log message: {log_message!r}"
        )

    def test_warning_log_message_has_no_dead_extra_kwarg_artifact(self, caplog):
        def boom():
            raise HTTPException(status_code=500, detail="internal error")

        log_message = _drive_failing_route_and_capture_log_message(
            caplog, logging.WARNING, boom
        )

        assert "extra=" not in log_message, (
            "the response-side 5xx WARNING log message must not contain "
            "the dead extra={'correlation_id': ...} artifact produced by "
            "passing extra= into format_error_log() instead of "
            f"logger.warning(); got log message: {log_message!r}"
        )
