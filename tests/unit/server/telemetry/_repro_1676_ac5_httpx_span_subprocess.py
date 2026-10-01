"""Standalone subprocess harness for Story #1676 AC5 (real span creation +
span/counter coexistence).

This module is NOT a pytest test file (leading underscore -- pytest never
collects it). It is invoked as `python3 _repro_1676_ac5_httpx_span_subprocess.py`
by test_httpx_span_creation_1676_ac5.py, always in a FRESH Python process,
never imported into the shared pytest worker -- because this harness needs
to control BOTH of OpenTelemetry's only-settable-once-per-process globals
(TracerProvider AND MeterProvider) directly, which only fresh-process
isolation makes safe (see
tests/unit/server/telemetry/otel_test_support.py's module docstring and
tests/unit/server/telemetry/_repro_1679_subprocess.py for the identical
constraint/precedent).

Drives instrument_httpx() (Story #1676 AC5) exactly as
startup/lifespan.py's real telemetry-init block does, then makes a REAL
outbound HTTP call -- through the genuine, production VoyageAIClient
(`_make_sync_request`), over a genuine socket (never httpx.MockTransport,
which would bypass HTTPXClientInstrumentor's monkey-patched
httpx.HTTPTransport.handle_request entirely) -- against a LOCAL, HERMETIC
`http.server.HTTPServer` standing in for the VoyageAI API. No live
Voyage/Cohere credentials or billable network calls are involved.

Reports span count/attributes (from a real InMemorySpanExporter attached
to the real global TracerProvider) and the `cidx.embedding.requests`
counter value (from a real InMemoryMetricReader attached to the real
global MeterProvider) as JSON on the path named by the
REPRO_1676_AC5_OUTPUT_FILE environment variable -- proving the span and
the counter both fire for the SAME real HTTP call (requirement 7).

REPRO_1676_AC5_OUTPUT_FILE is always supplied by the trusted test-launcher
(the sibling pytest file, via subprocess.run(..., env=...)) -- this script
is never invoked with attacker-controlled input. It is still validated
explicitly (rather than left to raise a bare KeyError) so a misconfigured
launcher fails with a clear message instead of an opaque traceback.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path


class _EmbeddingsHandler(http.server.BaseHTTPRequestHandler):
    """Hermetic stand-in for VoyageAI's /v1/embeddings endpoint."""

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        body = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # pragma: no cover - quiet server
        pass


def _extract_counter_value(metrics_data, metric_name: str):
    """Sum all data-point values for `metric_name` across the real
    InMemoryMetricReader's collected ResourceMetrics -> ScopeMetrics ->
    Metric -> data_points OTEL SDK data model. Returns None if the
    counter has no recorded data points at all.
    """
    if metrics_data is None:
        return None
    total = None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                for point in metric.data.data_points:
                    total = (total or 0) + point.value
    return total


def _run() -> dict:
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Fresh process: these are the FIRST calls that set OTEL's global
    # TracerProvider/MeterProvider, so both succeed unconditionally
    # (each is only settable once per process).
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # The exact function startup/lifespan.py's telemetry-init block calls
    # (Story #1676 AC5).
    from code_indexer.server.telemetry.instrumentation import instrument_httpx

    instrument_result = instrument_httpx()

    # Real TelemetryManager singleton, same as lifespan.py constructs --
    # its own attempt to set the global tracer/meter providers is a
    # harmless no-op (already set, above, by this harness), so the
    # ACTUAL global providers stay the InMemory ones under test.
    from code_indexer.server.telemetry.manager import get_telemetry_manager
    from code_indexer.server.utils.config_manager import TelemetryConfig

    telemetry_manager = get_telemetry_manager(
        TelemetryConfig(
            enabled=True,
            export_traces=True,
            export_metrics=True,
            collector_endpoint="http://127.0.0.1:1",
        )
    )

    from code_indexer.server.telemetry.metrics_instrumentation import (
        get_application_metrics,
    )

    app_metrics = get_application_metrics(telemetry_manager)

    # Hermetic local HTTP server standing in for the VoyageAI API.
    server = http.server.HTTPServer(("127.0.0.1", 0), _EmbeddingsHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    os.environ["VOYAGE_API_KEY"] = "PLACEHOLDER-TEST-KEY-NOT-REAL"

    import httpx

    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    class _RealSocketSyncClientFactory:
        """Returns a genuine httpx.Client (real HTTPTransport, real
        socket I/O) -- deliberately never httpx.MockTransport, which
        would bypass HTTPXClientInstrumentor's monkey-patched
        httpx.HTTPTransport.handle_request entirely and produce zero
        spans regardless of instrumentation state."""

        def create_sync_client(
            self, *, transport=None, pooled: bool = False, **kwargs
        ) -> httpx.Client:
            return httpx.Client(**kwargs)

    config = VoyageAIConfig(
        model="voyage-code-3",
        api_endpoint=f"http://127.0.0.1:{port}/v1/embeddings",
        max_retries=0,
        timeout=10,
        connect_timeout=5,
    )
    client = VoyageAIClient(config, http_client_factory=_RealSocketSyncClientFactory())

    embedding_result = client._make_sync_request(["hello world"], retry=False)

    server.shutdown()
    server_thread.join(timeout=5)

    spans = span_exporter.get_finished_spans()
    httpx_spans = [s for s in spans if s.name in ("GET", "POST")]
    span_data = [
        {"name": s.name, "attributes": dict(s.attributes or {})} for s in httpx_spans
    ]

    embedding_requests_value = _extract_counter_value(
        metric_reader.get_metrics_data(), "cidx.embedding.requests"
    )

    return {
        "instrument_httpx_result": instrument_result,
        "app_metrics_active": app_metrics.is_active,
        "embedding_result_has_data": "data" in embedding_result,
        "span_count": len(spans),
        "httpx_span_count": len(httpx_spans),
        "httpx_spans": span_data,
        "embedding_requests_metric_value": embedding_requests_value,
    }


def _resolve_output_path() -> Path:
    """Resolve the result-file path from REPRO_1676_AC5_OUTPUT_FILE.

    Validated explicitly (clear error) rather than left to raise a bare
    KeyError -- this harness is always launched by the sibling test file
    with a controlled, trusted environment, but a clear failure mode is
    cheap and helps if the launcher is ever misconfigured.
    """
    raw = os.environ.get("REPRO_1676_AC5_OUTPUT_FILE")
    if not raw:
        raise RuntimeError(
            "REPRO_1676_AC5_OUTPUT_FILE environment variable is required but "
            "was not set. This harness must be launched by "
            "test_httpx_span_creation_1676_ac5.py, which always sets it to "
            "a path inside pytest's own tmp_path."
        )
    output_path = Path(raw)
    if not output_path.parent.is_dir():
        raise RuntimeError(
            f"REPRO_1676_AC5_OUTPUT_FILE parent directory does not exist: "
            f"{output_path.parent}"
        )
    return output_path


if __name__ == "__main__":
    result = _run()
    _resolve_output_path().write_text(json.dumps(result))
