"""
TDD regression test for Bug #1679: FastAPI HTTP request spans are NEVER
exported to the OTEL collector in production, on ANY deployment.

Root cause: `instrument_fastapi(app, telemetry_manager)` used to be
called from INSIDE the `lifespan()` async context manager body
(startup/lifespan.py), but Starlette builds its ASGI middleware stack
LAZILY on the very FIRST ASGI message the app receives -- which is the
"lifespan" startup message itself (`Starlette.__call__` checks
`self.middleware_stack is None` and builds it BEFORE dispatching to the
lifespan() context manager). `FastAPIInstrumentor.instrument_app()` works
by monkey-patching `Starlette.build_middleware_stack` -- calling it after
the stack is already built is a structural no-op: no exception is
raised, no warning logged, just zero HTTP request spans forever.

This test drives the REAL `create_app()` code path (not a bare
hand-constructed FastAPI app) through a REAL ASGI lifespan
(`asgi_lifespan.LifespanManager`) and a REAL HTTP request
(`httpx.ASGITransport`), with the REAL `FastAPIInstrumentor` and a REAL
`InMemorySpanExporter` -- exactly the "existing test suite's blind spot"
the issue calls out: prior tests
(tests/unit/server/telemetry/test_request_tracing.py) only ever asserted
`instrument_app()` ran without raising, never that it produced a real
span for a real request.

Both scenarios below run the repro in a FRESH subprocess (never inside
the shared pytest worker process) because OpenTelemetry's global
TracerProvider can be set successfully only ONCE per process (see
tests/unit/server/telemetry/otel_test_support.py's module docstring for
the identical constraint on MeterProvider) -- running in-process would
make this test's outcome depend on which other telemetry test file
happened to run first in the same pytest session. This applies even to
the "telemetry disabled" scenario, for consistency and to keep both
scenarios sharing one isolated launch mechanism.

All objects involved in the subprocess harness are real (no mocks):
FastAPI app via `create_app()`, `FastAPIInstrumentor`, `TracerProvider`,
`InMemorySpanExporter`, ASGI transport.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import pytest

_HARNESS = Path(__file__).parent / "_repro_1679_subprocess.py"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Ceiling for a full create_app() service-initialization pass (DB setup,
# service wiring) plus one HTTP round trip in a fresh interpreter.
# Overridable via CIDX_TEST_HARNESS_TIMEOUT_SECONDS for a slower CI/dev
# machine; this default is a generous multiple of locally-observed run
# time, guarding only against a genuine hang.
_DEFAULT_HARNESS_TIMEOUT_SECONDS = 90

# Fake collector address that is never expected to have a real listener --
# only the span CREATION path (verified via a separately-attached
# InMemorySpanExporter) is under test here, never real network export.
# Overridable via CIDX_TEST_FAKE_COLLECTOR_ENDPOINT for a local dev
# collector.
_DEFAULT_FAKE_COLLECTOR_ENDPOINT = "http://localhost:4317"


def _positive_int_env(var_name: str, default: int) -> int:
    """Read a positive-int env override, or fall back to `default`.

    Raises a clear ValueError (rather than letting subprocess.run() fail
    with an opaque error) if the override is present but not a positive
    integer.
    """
    raw = os.environ.get(var_name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{var_name}={raw!r} is not a valid integer (expected a "
            "positive number of seconds)."
        ) from None
    if value <= 0:
        raise ValueError(f"{var_name}={raw!r} must be a positive number of seconds.")
    return value


def _url_env(var_name: str, default: str) -> str:
    """Read a http(s):// URL env override, or fall back to `default`.

    Raises a clear ValueError if the override is present but is not a
    URL with a http/https scheme AND a non-empty hostname.
    """
    raw = os.environ.get(var_name)
    if raw is None:
        return default
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"{var_name}={raw!r} must be a http:// or https:// URL with "
            "a non-empty hostname."
        )
    return raw


# The harness's TelemetryManager still constructs a real (grpc)
# OTLPSpanExporter against this endpoint, exactly like
# tests/unit/server/telemetry/test_custom_spans.py and
# test_request_tracing.py already do -- the exporter's background
# BatchSpanProcessor thread is a daemon thread that fails to connect
# silently and never blocks the test or process exit.
_FAKE_COLLECTOR_ENDPOINT = _url_env(
    "CIDX_TEST_FAKE_COLLECTOR_ENDPOINT", _DEFAULT_FAKE_COLLECTOR_ENDPOINT
)

_HARNESS_TIMEOUT_SECONDS = _positive_int_env(
    "CIDX_TEST_HARNESS_TIMEOUT_SECONDS", _DEFAULT_HARNESS_TIMEOUT_SECONDS
)


def _run_harness(tmp_path: Path, *, telemetry_enabled: bool) -> Dict[str, Any]:
    """Launch _repro_1679_subprocess.py in a fresh Python process with a
    telemetry-enabled config.json, and return its parsed JSON result.

    Mirrors the config bootstrap pattern established in
    test_telemetry_app_integration.py (minimal config.json +
    data/golden-repos directory is sufficient for create_app()).
    """
    config_dir = tmp_path / ".cidx-server"
    (config_dir / "data" / "golden-repos").mkdir(parents=True)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "telemetry_config": {
                    "enabled": telemetry_enabled,
                    "export_traces": True,
                    "collector_endpoint": _FAKE_COLLECTOR_ENDPOINT,
                }
            }
        )
    )

    output_file = tmp_path / "repro_result.json"

    env = dict(os.environ)
    env["CIDX_SERVER_DATA_DIR"] = str(config_dir)
    env["REPRO_1679_OUTPUT_FILE"] = str(output_file)

    proc = subprocess.run(
        [sys.executable, str(_HARNESS)],
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=_HARNESS_TIMEOUT_SECONDS,
    )

    assert proc.returncode == 0, (
        f"Subprocess harness failed (exit {proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    result: Dict[str, Any] = json.loads(output_file.read_text())
    return result


@pytest.mark.slow
class TestFastAPIRequestSpansBug1679:
    """Reproduces and validates the fix for Bug #1679."""

    def test_real_http_request_produces_real_spans(self, tmp_path: Path) -> None:
        """
        A real HTTP request through create_app()'s real ASGI lifespan
        MUST produce at least one real span once FastAPI instrumentation
        is wired correctly.

        Before the Bug #1679 fix: instrument_fastapi() runs from inside
        lifespan() AFTER Starlette already built its middleware stack --
        this asserts 0, reproducing the bug.

        After the fix: instrumentation happens at app-construction time,
        before the middleware stack is built -- this asserts > 0.
        """
        result = _run_harness(tmp_path, telemetry_enabled=True)

        assert result["telemetry_manager_present"] is True, result
        assert result["status_code"] == 200, result

        assert result["span_count"] > 0, (
            "Expected at least one real span for the HTTP request -- got "
            f"{result['span_count']}. This reproduces Bug #1679: FastAPI "
            "instrumentation applied too late (after Starlette already "
            "built its middleware stack) is a structural no-op. "
            f"Full result: {result}"
        )

    def test_telemetry_disabled_produces_no_telemetry_manager(
        self, tmp_path: Path
    ) -> None:
        """
        Requirement #5: when telemetry is disabled entirely, there is no
        TelemetryManager (and therefore no TracerProvider) at all -- so
        nothing could possibly export, regardless of whether the FastAPI
        app is structurally instrumented. Unaffected by the Bug #1679
        fix; documents the zero-overhead guarantee the fix must preserve.
        """
        result = _run_harness(tmp_path, telemetry_enabled=False)

        assert result["telemetry_manager_present"] is False, result
        assert result["status_code"] == 200, result
        assert result["span_count"] == 0, result
