"""Standalone subprocess harness for Story #1676 AC5 requirement 3: the
OTLP exporters' own outbound traffic must produce ZERO httpx spans,
because neither the gRPC nor the HTTP OTLP span exporter uses httpx --
verified dynamically against the ACTUALLY-INSTALLED
opentelemetry-exporter-otlp package (gRPC exporter uses a raw grpc
channel; HTTP exporter uses requests.Session), not assumed from source
reading alone.

NOT a pytest file (leading underscore). Runs in a FRESH Python process
for the same only-once-per-process global TracerProvider reason as
_repro_1676_ac5_httpx_span_subprocess.py and _repro_1679_subprocess.py.

Method: instrument httpx globally, attach a real InMemorySpanExporter to
a real global TracerProvider, create one real span (so the exporters have
something to attempt exporting), then invoke BOTH OTLP span exporters'
real .export() against an address nothing listens on (a real outbound
attempt is made and fails fast/times out -- no live collector required).
If httpx instrumentation were (incorrectly) intercepting either
exporter's transport, the exporter's own outbound attempt would show up
as an ADDITIONAL span in the same InMemorySpanExporter; this harness
asserts (both here and via the reported JSON, for the launcher's own
independent check) that the span count is unchanged after both export
attempts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _run() -> dict:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcOTLPSpanExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpOTLPSpanExporter,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    from code_indexer.server.telemetry.instrumentation import instrument_httpx

    instrument_result = instrument_httpx()

    tracer = trace.get_tracer("repro_1676_ac5")
    with tracer.start_as_current_span("dummy-request"):
        pass

    span_count_before_export_attempts = len(span_exporter.get_finished_spans())

    # Port 1 on loopback: nothing listens there, so both exporters fail
    # fast/timeout quickly instead of hanging on a real network wait.
    # Overridable via env vars for a slower/differently-networked CI box;
    # the launcher (test_httpx_span_creation_1676_ac5.py) may set these.
    dead_endpoint_grpc = os.environ.get(
        "REPRO_1676_AC5_OTLP_GRPC_ENDPOINT", "127.0.0.1:1"
    )
    dead_endpoint_http = os.environ.get(
        "REPRO_1676_AC5_OTLP_HTTP_ENDPOINT", "http://127.0.0.1:1/v1/traces"
    )
    export_timeout_seconds = int(
        os.environ.get("REPRO_1676_AC5_OTLP_EXPORT_TIMEOUT_SECONDS", "2")
    )

    grpc_exporter = GrpcOTLPSpanExporter(
        endpoint=dead_endpoint_grpc, insecure=True, timeout=export_timeout_seconds
    )
    try:
        grpc_result = str(grpc_exporter.export(span_exporter.get_finished_spans()))
    finally:
        grpc_exporter.shutdown()

    http_exporter = HttpOTLPSpanExporter(
        endpoint=dead_endpoint_http, timeout=export_timeout_seconds
    )
    try:
        http_result = str(http_exporter.export(span_exporter.get_finished_spans()))
    finally:
        http_exporter.shutdown()

    spans_after = span_exporter.get_finished_spans()

    # Requirement 3's core assertion, made INSIDE the harness itself (not
    # just reported for the launcher to check): neither OTLP exporter's
    # own outbound attempt above may have produced a NEW httpx span in
    # the same InMemorySpanExporter that is still watching the global
    # TracerProvider.
    assert len(spans_after) == span_count_before_export_attempts, (
        "OTLP exporter traffic produced unexpected NEW spans -- expected "
        f"{span_count_before_export_attempts}, got {len(spans_after)}: "
        f"{[s.name for s in spans_after]}"
    )

    return {
        "instrument_httpx_result": instrument_result,
        "span_count_before_export_attempts": span_count_before_export_attempts,
        "span_count_after_export_attempts": len(spans_after),
        "span_names_after": [s.name for s in spans_after],
        "grpc_export_result": grpc_result,
        "http_export_result": http_result,
    }


def _resolve_output_path() -> Path:
    raw = os.environ.get("REPRO_1676_AC5_OTLP_OUTPUT_FILE")
    if not raw:
        raise RuntimeError(
            "REPRO_1676_AC5_OTLP_OUTPUT_FILE environment variable is "
            "required but was not set. This harness must be launched by "
            "test_httpx_span_creation_1676_ac5.py."
        )
    output_path = Path(raw)
    if not output_path.parent.is_dir():
        raise RuntimeError(
            f"REPRO_1676_AC5_OTLP_OUTPUT_FILE parent directory does not "
            f"exist: {output_path.parent}"
        )
    return output_path


if __name__ == "__main__":
    result = _run()
    _resolve_output_path().write_text(json.dumps(result))
