"""An outgoing httpx request's span never carries a secret query value.

A real request goes over a loopback socket to a local http.server; the
client is instrumented with the exact request hook that instrument_httpx()
registers process-wide, and spans are read from a real in-memory exporter.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator

import httpx
import pytest
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from code_indexer.server.telemetry.instrumentation import (
    instrument_httpx,
    uninstrument_httpx,
)

_SECRET = "ExampleSecretValue"


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- http.server API
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def loopback_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _registered_request_hook() -> Any:
    """The request hook instrument_httpx() wired into the global transport
    wrapper (read from the instrumentation's wrapper arguments)."""
    instrument_httpx()
    try:
        wrapper = httpx.HTTPTransport.handle_request
        return wrapper._self_wrapper.keywords["request_hook"]  # type: ignore[attr-defined]
    finally:
        uninstrument_httpx()


def test_outgoing_request_span_never_carries_a_secret_query_value(
    loopback_url: str,
) -> None:
    hook = _registered_request_hook()
    assert callable(hook)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with httpx.Client() as client:
        HTTPXClientInstrumentor.instrument_client(
            client, tracer_provider=provider, request_hook=hook
        )
        response = client.get(f"{loopback_url}/v1/x?access_token={_SECRET}&keep=1")
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes or {}
    url = str(attributes.get("http.url") or attributes.get("url.full"))
    assert _SECRET not in url
    assert "keep=1" in url


class _FirstWriteFails:
    """A recording span whose first set_attribute (write) raises."""

    def __init__(self) -> None:
        self.attributes = {"http.url": f"http://h/x?access_token={_SECRET}"}
        self._writes = 0

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, name: str, value: Any) -> None:
        self._writes += 1
        if self._writes == 1:
            raise RuntimeError("attribute store unavailable")
        self.attributes[name] = value


def test_span_url_redaction_never_raises() -> None:
    from code_indexer.server.telemetry.instrumentation import (
        _redact_client_request_span,
        _redact_request_span,
    )

    _redact_request_span(None, {})
    _redact_client_request_span(object(), None)

    span = _FirstWriteFails()
    _redact_request_span(span, {})

    assert _SECRET not in str(span.attributes)
