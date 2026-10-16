"""Spans and log records are redacted at the exporter boundary: whatever a
span or log record carries -- attributes, exception events, status
description, log body -- no credential value is ever handed to a collector.

Real OTEL SDK pipelines with in-memory exporters standing in for the
collector; a real FastAPI app for the request path.
"""

from __future__ import annotations

from typing import Any, List

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from code_indexer.server.telemetry.export_redaction import RedactingSpanExporter

SECRET = "ExampleExporterSecret123"


class _CaptureSink:
    """A loopback OTLP/HTTP collector that keeps every request body."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        bodies: List[bytes] = []
        self.bodies = bodies

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- http.server API
                bodies.append(
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                )
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _manager(sink: _CaptureSink, export_traces: bool, export_logs: bool) -> Any:
    from code_indexer.server.telemetry.manager import TelemetryManager
    from code_indexer.server.utils.config_manager import TelemetryConfig

    config = TelemetryConfig(
        enabled=True,
        collector_endpoint=sink.endpoint,
        collector_protocol="http",
        export_metrics=False,
        export_traces=export_traces,
        export_logs=export_logs,
    )
    return TelemetryManager(config)


def test_manager_trace_export_carries_no_secret_from_an_exception_event() -> None:
    sink = _CaptureSink()
    manager = _manager(sink, export_traces=True, export_logs=False)
    try:
        tracer = manager.tracer_provider.get_tracer("example.exporter")
        with tracer.start_as_current_span("example-operation") as span:
            span.record_exception(RuntimeError(f"rejected token={SECRET}"))
        manager.tracer_provider.force_flush()
    finally:
        manager.shutdown()
        sink.stop()

    exported = b"".join(sink.bodies)
    assert b"example-operation" in exported
    assert b"token=" in exported
    assert (SECRET.encode() in exported) is False


def test_an_item_whose_redaction_fails_is_dropped_with_a_warning(
    caplog: Any,
) -> None:
    import logging

    from code_indexer.server.telemetry.export_redaction import redact_each

    def redact(item: str) -> str:
        if item == "unredactable":
            raise ValueError("cannot redact")
        return item.upper()

    with caplog.at_level(logging.WARNING):
        result = redact_each(["one", "unredactable", "two"], redact, "span")

    assert result == ["ONE", "TWO"]
    assert any("not exported" in r.getMessage() for r in caplog.records)


def _log_exception_through(handler: Any) -> None:
    import logging

    example_logger = logging.getLogger("code_indexer.example.exporter")
    example_logger.addHandler(handler)
    example_logger.propagate = False
    try:
        try:
            raise RuntimeError(f"rejected token={SECRET}")
        except RuntimeError:
            example_logger.exception("Example operation failed")
    finally:
        example_logger.removeHandler(handler)
        example_logger.propagate = True


def test_manager_log_export_carries_no_secret_from_logger_exception() -> None:
    import logging

    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    sink = _CaptureSink()
    manager = _manager(sink, export_traces=False, export_logs=True)
    try:
        handler = LoggingHandler(
            level=logging.NOTSET, logger_provider=manager.logger_provider
        )
        _log_exception_through(handler)
        manager.logger_provider.force_flush()
    finally:
        manager.shutdown()
        sink.stop()

    exported = b"".join(sink.bodies)
    assert b"Example operation failed" in exported
    assert b"token=" in exported
    assert (SECRET.encode() in exported) is False


def _span_texts(spans: List[Any]) -> List[str]:
    texts: List[str] = []
    for span in spans:
        texts.append(repr(dict(span.attributes or {})))
        texts.append(str(span.status.description))
        for event in span.events:
            texts.append(repr(dict(event.attributes or {})))
    return texts


def test_exception_in_a_request_exports_no_secret_in_any_span_field() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    redacting = RedactingSpanExporter(exporter)
    provider.add_span_processor(SimpleSpanProcessor(redacting))
    assert redacting.force_flush() is True
    app = FastAPI()

    @app.get("/example")
    def example() -> dict:
        raise RuntimeError(f"upstream rejected token={SECRET}")

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/example")
        provider.force_flush()
        spans = list(exporter.get_finished_spans())
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()

    assert response.status_code == 500
    events = [event for span in spans for event in span.events]
    assert any(event.name == "exception" for event in events), spans
    texts = _span_texts(spans)
    assert any("token=" in text for text in texts), texts
    for text in texts:
        assert (SECRET in text) is False, text


def test_span_exporter_redacts_names_links_and_resource() -> None:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Link

    memory = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create(
            {"deploy.note": f"token={SECRET}", "api_token": SECRET}
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(memory)))
    tracer = provider.get_tracer("example.exporter")
    with tracer.start_as_current_span("linked") as other:
        other_context = other.get_span_context()
    link = Link(
        other_context, attributes={"note": f"password={SECRET}", "api_token": SECRET}
    )
    with tracer.start_as_current_span(f"op token={SECRET}", links=[link]) as span:
        span.add_event(f"step password={SECRET}")
    provider.shutdown()

    exported = [s for s in memory.get_finished_spans() if s.name != "linked"]
    assert len(exported) == 1
    span_out = exported[0]
    fields = [
        span_out.name,
        *(event.name for event in span_out.events),
        *(str(dict(item.attributes or {})) for item in span_out.links),
        str(dict(span_out.resource.attributes)),
    ]
    assert "token=" in span_out.name, "the readable part of the name stays"
    assert len(span_out.links) == 1
    for field in fields:
        assert SECRET not in field, field
