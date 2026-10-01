"""Standalone subprocess harness for Story #1676 AC5 requirement 2: httpx
instrumentation must be wired into the REAL server startup path
(startup/lifespan.py), not just exist as a standalone, correctly-behaving
function nobody calls.

NOT a pytest test file (leading underscore -- pytest never collects it).
Invoked as `python3 _repro_1676_ac5_lifespan_wiring_subprocess.py` by
test_httpx_span_creation_1676_ac5.py, always in a FRESH Python process --
mirrors tests/unit/server/telemetry/_repro_1679_subprocess.py's
create_app() + asgi_lifespan.LifespanManager pattern (the established,
real-startup-path harness for exactly this class of wiring bug), since
OpenTelemetry's global registries are only settable once per process.

Drives the REAL `create_app()` code path (reading a
telemetry-{enabled,disabled} config.json from CIDX_SERVER_DATA_DIR)
through a REAL ASGI lifespan, then reports whether
HTTPXClientInstrumentor().is_instrumented_by_opentelemetry became True as
a structural consequence of that real startup -- no mocks anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


def _run() -> dict:
    from asgi_lifespan import LifespanManager
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    from code_indexer.server.app import create_app

    app = create_app()

    async def _drive() -> dict:
        async with LifespanManager(app):
            telemetry_manager = getattr(app.state, "telemetry_manager", None)
            return {
                "telemetry_manager_present": telemetry_manager is not None,
                "httpx_instrumented_during_lifespan": bool(
                    HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
                ),
            }

    return asyncio.run(_drive())


def _resolve_output_path() -> Path:
    raw = os.environ.get("REPRO_1676_AC5_WIRING_OUTPUT_FILE")
    if not raw:
        raise RuntimeError(
            "REPRO_1676_AC5_WIRING_OUTPUT_FILE environment variable is "
            "required but was not set. This harness must be launched by "
            "test_httpx_span_creation_1676_ac5.py."
        )
    output_path = Path(raw)
    if not output_path.parent.is_dir():
        raise RuntimeError(
            f"REPRO_1676_AC5_WIRING_OUTPUT_FILE parent directory does not "
            f"exist: {output_path.parent}"
        )
    return output_path


if __name__ == "__main__":
    result = _run()
    _resolve_output_path().write_text(json.dumps(result))
