"""The HTTP access log never records a sensitive query value
(confirmation_token, source).

Proven against a REAL uvicorn server on an ephemeral loopback port (only
the ASGI app behind it is a trivial stand-in), plus unit cases for the
redaction itself.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable, Iterator, List

import httpx
import pytest
import uvicorn

from code_indexer.server.utils.access_log_redaction import (
    ACCESS_LOGGER_NAME,
    REDACTED,
    SensitiveQueryRedactionFilter,
    install_access_log_redaction,
    redact_sensitive_query_values,
)

_VALUE = "TokenValueExample"
_START_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.05


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            f"/x?confirmation_token={_VALUE}&keep=1",
            f"/x?confirmation_token={REDACTED}&keep=1",
        ),
        (
            f"/x?keep=1&confirmation_token={_VALUE}",
            f"/x?keep=1&confirmation_token={REDACTED}",
        ),
        (f"confirmation_token={_VALUE}", f"confirmation_token={REDACTED}"),
        (
            f"/x?CONFIRMATION%5FTOKEN={_VALUE}",
            f"/x?CONFIRMATION%5FTOKEN={REDACTED}",
        ),
        (
            f"/x?confirmation_token={_VALUE}&confirmation_token={_VALUE}",
            f"/x?confirmation_token={REDACTED}&confirmation_token={REDACTED}",
        ),
        ("/x?keep=1", "/x?keep=1"),
    ],
)
def test_redaction_replaces_only_confirmation_token_values(
    raw: str, expected: str
) -> None:
    assert redact_sensitive_query_values(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "%63onfirmation_token",
        "confirmation%5ftoken",
        "%43ONFIRMATION%5FTOKEN",
        "Confirmation_Token",
        "sour%63e",
        "%73ource",
        "SOURCE",
        "So%55rce",
    ],
)
def test_percent_encoded_parameter_names_are_redacted(name: str) -> None:
    redacted = redact_sensitive_query_values(f"/x?keep=1&{name}={_VALUE}")

    assert redacted == f"/x?keep=1&{name}={REDACTED}"


@pytest.mark.parametrize("name", ["sourcer", "%73ourced", "keep", "page"])
def test_other_parameter_names_keep_their_values(name: str) -> None:
    raw = f"/x?{name}={_VALUE}"

    assert redact_sensitive_query_values(raw) == raw


def test_secret_names_are_classified_by_the_shared_rule() -> None:
    raw = f"/x?pass%77ord={_VALUE}&%70assword={_VALUE}&access%5Ftoken={_VALUE}&keep=1"

    assert redact_sensitive_query_values(raw) == (
        f"/x?pass%77ord={REDACTED}&%70assword={REDACTED}"
        f"&access%5Ftoken={REDACTED}&keep=1"
    )


@pytest.mark.parametrize(
    "name", ["api_key_flag", "secret_scope", "password_enabled", "token_scopes"]
)
def test_flag_suffixed_secret_names_are_redacted(name: str) -> None:
    """A query value is always a string, so a flag-like suffix never exempts
    a secret-looking parameter name."""
    raw = f"GET /api/example?{name}={_VALUE}&keep=1 HTTP/1.1"

    assert redact_sensitive_query_values(raw) == (
        f"GET /api/example?{name}={REDACTED}&keep=1 HTTP/1.1"
    )


@pytest.mark.parametrize("name", ["confirmation_token", "source"])
@pytest.mark.parametrize(
    "value",
    [
        "pre'TAIL",
        "pre;TAIL",
        "pre,TAIL",
        'pre"TAIL',
        "https://u:pre'TAIL@example.com/r",
    ],
)
def test_values_containing_quotes_and_separators_are_masked_whole(
    name: str, value: str
) -> None:
    query = redact_sensitive_query_values(f"/x?{name}={value}&keep=1")
    line = redact_sensitive_query_values(f'"GET /x?{name}={value} HTTP/1.1" 200')

    assert query == f"/x?{name}={REDACTED}&keep=1"
    assert "TAIL" not in line
    assert line.endswith(' HTTP/1.1" 200')


def test_install_is_idempotent() -> None:
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    install_access_log_redaction()
    install_access_log_redaction()

    installed = [
        f for f in access_logger.filters if isinstance(f, SensitiveQueryRedactionFilter)
    ]
    assert len(installed) == 1


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def _ok_app(scope: Any, receive: Callable[..., Any], send: Any) -> None:
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def access_lines() -> Iterator[List[str]]:
    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    capture = _Capture()
    previous_level = access_logger.level
    access_logger.addHandler(capture)
    access_logger.setLevel(logging.INFO)
    try:
        yield capture.lines
    finally:
        access_logger.removeHandler(capture)
        access_logger.setLevel(previous_level)


def test_real_server_access_log_never_records_the_token(
    access_lines: List[str],
) -> None:
    install_access_log_redaction()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            _ok_app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=True,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(_POLL_SECONDS)

        response = httpx.get(
            f"http://127.0.0.1:{port}/repos/example-repo/branches/feature",
            params={"confirmation_token": _VALUE, "keep": "1"},
        )
    finally:
        server.should_exit = True
        thread.join(timeout=_START_TIMEOUT_SECONDS)

    assert response.status_code == 204
    request_lines = [line for line in access_lines if "/branches/feature" in line]
    assert len(request_lines) == 1, access_lines
    assert _VALUE not in request_lines[0]
    assert "keep=1" in request_lines[0]
