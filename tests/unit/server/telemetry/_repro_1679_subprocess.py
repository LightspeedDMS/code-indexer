"""Standalone subprocess harness for Bug #1679's regression test.

This module is NOT a pytest test file (leading underscore -- pytest never
collects it). It is invoked as `python3 _repro_1679_subprocess.py` by
test_fastapi_instrumentation_ordering_1679.py, always in a FRESH Python
process, never imported/executed inside the shared pytest worker --
because OpenTelemetry's global TracerProvider can be set successfully
only ONCE per process (see
tests/unit/server/telemetry/otel_test_support.py's module docstring for
the identical constraint on MeterProvider). Running this logic in-process
would make the test's outcome depend on which other telemetry test file
happened to run first in the same pytest session and already claimed the
global registry.

Builds a real FastAPI app via the production `create_app()` code path
(reading a telemetry-enabled config.json from CIDX_SERVER_DATA_DIR),
drives the REAL ASGI lifespan protocol via `asgi_lifespan.LifespanManager`
(the same mechanism
tests/unit/server/telemetry/test_telemetry_app_integration.py uses),
attaches a real `InMemorySpanExporter` directly to the real
`TelemetryManager.tracer_provider`, issues one real HTTP GET through a
real ASGI transport (`httpx.ASGITransport`), then reports how many spans
were captured for that request as JSON on the path named by the
REPRO_1679_OUTPUT_FILE environment variable.

REPRO_1679_OUTPUT_FILE is always supplied by the trusted test-launcher
(the sibling pytest file, via subprocess.run(..., env=...)) -- this
script is never invoked with attacker-controlled input. It is still
validated explicitly (rather than left to raise a bare KeyError) so a
misconfigured launcher fails with a clear message instead of an opaque
traceback.

No mocks anywhere: every object here (FastAPI app via create_app(),
FastAPIInstrumentor, TracerProvider, InMemorySpanExporter, ASGI
transport) is the genuine production/OTEL-SDK implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


def _run() -> dict:
    import httpx
    from asgi_lifespan import LifespanManager
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from code_indexer.server.app import create_app

    app = create_app()

    async def _drive() -> dict:
        async with LifespanManager(app):
            telemetry_manager = getattr(app.state, "telemetry_manager", None)
            tracer_provider = (
                telemetry_manager.tracer_provider
                if telemetry_manager is not None
                else None
            )

            exporter = None
            if tracer_provider is not None:
                exporter = InMemorySpanExporter()
                tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/openapi.json")

            span_count = 0
            span_names: list = []
            if exporter is not None and tracer_provider is not None:
                # No force_flush() needed: exporter is wired via a
                # SimpleSpanProcessor, which exports synchronously inside
                # span.end() (called when the request's server span
                # finishes, before the HTTP response above returns) --
                # get_finished_spans() already reflects everything.
                # force_flush() on the shared TracerProvider would ALSO
                # wait on the real OTLP/gRPC BatchSpanProcessor pointed at
                # a dead fake collector endpoint, whose retry/backoff
                # could blow this subprocess's own timeout for no benefit.
                spans = exporter.get_finished_spans()
                span_count = len(spans)
                span_names = [s.name for s in spans]

            return {
                "status_code": response.status_code,
                "telemetry_manager_present": telemetry_manager is not None,
                "span_count": span_count,
                "span_names": span_names,
            }

    return asyncio.run(_drive())


def _resolve_output_path() -> Path:
    """Resolve the result-file path from REPRO_1679_OUTPUT_FILE.

    Validated explicitly (clear error) rather than left to raise a bare
    KeyError -- this harness is always launched by the sibling test file
    with a controlled, trusted environment, but a clear failure mode is
    cheap and helps if the launcher is ever misconfigured.
    """
    raw = os.environ.get("REPRO_1679_OUTPUT_FILE")
    if not raw:
        raise RuntimeError(
            "REPRO_1679_OUTPUT_FILE environment variable is required but "
            "was not set. This harness must be launched by "
            "test_fastapi_instrumentation_ordering_1679.py, which always "
            "sets it to a path inside pytest's own tmp_path."
        )
    output_path = Path(raw)
    if not output_path.parent.is_dir():
        raise RuntimeError(
            f"REPRO_1679_OUTPUT_FILE parent directory does not exist: "
            f"{output_path.parent}"
        )
    return output_path


if __name__ == "__main__":
    result = _run()
    _resolve_output_path().write_text(json.dumps(result))
