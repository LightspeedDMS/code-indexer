"""A listening, in-process OTLP/HTTP collector for telemetry tests.

Invariant: no telemetry test exports to a collector that is not listening.
An exporter aimed at a dead endpoint retries for its whole timeout (~10 s)
at shutdown, which outlasts asgi_lifespan's 5 s shutdown limit, cancels the
lifespan teardown and leaks the telemetry singleton to later tests.

OtlpHttpSink answers 200 to every OTLP/HTTP POST (/v1/traces, /v1/metrics,
/v1/logs) on an ephemeral loopback port, so every flush completes at once.
Exposed to tests by the ``otlp_sink`` fixture in this directory's conftest.
"""

from __future__ import annotations

import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

_LOOPBACK = "127.0.0.1"


class OtlpHttpSink:
    """Accepts and counts OTLP/HTTP exports; never inspects their content."""

    def __init__(self) -> None:
        self._counts: Counter = Counter()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer((_LOOPBACK, 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    def _handler_class(self) -> Any:
        sink = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- http.server API
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with sink._lock:
                    sink._counts[self.path] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return None

        return _Handler

    @property
    def endpoint(self) -> str:
        return f"http://{_LOOPBACK}:{self._server.server_port}"

    def start(self) -> "OtlpHttpSink":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def request_count(self, path: str) -> int:
        with self._lock:
            return int(self._counts[path])


def otlp_http_config(endpoint: str) -> Dict[str, str]:
    """telemetry_config entries that export over OTLP/HTTP to `endpoint`."""
    return {"collector_protocol": "http", "collector_endpoint": endpoint}
