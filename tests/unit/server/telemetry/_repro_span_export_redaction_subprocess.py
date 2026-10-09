"""Child process: telemetry and Langfuse both enabled in one process.

Not collected by pytest (leading underscore). Run by
test_span_export_redaction.py with the secret values as argv. A fresh process
is needed because OTEL's global tracer provider can be set only once.

  - telemetry: a global OTEL SDK tracer provider with an in-memory exporter,
    and a FastAPI app instrumented by the server's own instrument_fastapi();
  - Langfuse: the real SDK, exporting over HTTP to a loopback sink that
    records every request body (what would leave the host).

Prints one JSON object: the in-memory span attributes and the sink text.
"""

import gzip
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, List

_RECEIVED: List[bytes] = []
_RECEIVED_LOCK = threading.Lock()


class _Sink(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 -- http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        with _RECEIVED_LOCK:
            _RECEIVED.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        return None


def _install_global_exporter() -> Any:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


def _instrumented_app() -> Any:
    from fastapi import FastAPI

    from code_indexer.server.telemetry.instrumentation import instrument_fastapi

    app = FastAPI()

    @app.get("/example")
    def example() -> dict:
        return {"ok": True}

    assert instrument_fastapi(app)
    return app


def _exercise(
    langfuse: Any, app: Any, token: str, url_password: str, input_secret: str
) -> None:
    from fastapi.testclient import TestClient

    root = langfuse.create_trace(
        name="example-trace",
        session_id="example-session",
        input=f"password={input_secret}",
        metadata={"api_key": input_secret},
    )
    assert root is not None
    with TestClient(app) as http:
        response = http.get(
            "/example",
            params={
                "confirmation_token": token,
                "source": f"https://example-user:{url_password}@example.com/r",
                "keep": "1",
            },
        )
        assert response.status_code == 200
    span = langfuse.create_span(
        trace_id=root.id, name="example-tool", input_data={"token": input_secret}
    )
    langfuse.end_span(span)
    langfuse.end_trace(root)
    langfuse.flush()


def main() -> None:
    token, url_password, input_secret = sys.argv[1:4]
    sink = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    try:
        exporter = _install_global_exporter()

        from code_indexer.server.services.langfuse_client import LangfuseClient
        from code_indexer.server.utils.config_manager import LangfuseConfig

        langfuse = LangfuseClient(
            LangfuseConfig(
                enabled=True,
                public_key="pk-example",
                secret_key="sk-example",
                host=f"http://127.0.0.1:{sink.server_port}",
            )
        )
        _exercise(langfuse, _instrumented_app(), token, url_password, input_secret)
        with _RECEIVED_LOCK:
            received = list(_RECEIVED)
        spans = [
            {"name": s.name, **dict(s.attributes or {})}
            for s in exporter.get_finished_spans()
        ]
        print(
            json.dumps(
                {
                    "otel": spans,
                    "langfuse": b"\n".join(received).decode("latin-1"),
                    "langfuse_posts": len(received),
                },
                default=str,
            )
        )
    finally:
        sink.shutdown()
        sink.server_close()


if __name__ == "__main__":
    main()
