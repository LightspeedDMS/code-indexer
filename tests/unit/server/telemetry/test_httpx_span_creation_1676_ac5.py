"""
Real span-creation tests for Story #1676 AC5 (outbound HTTP span
instrumentation, httpx, global).

All three scenarios below run their real work in a FRESH subprocess
(never inside the shared pytest worker process) because OpenTelemetry's
global TracerProvider/MeterProvider can each be set successfully only
ONCE per process (see
tests/unit/server/telemetry/otel_test_support.py's module docstring, and
tests/unit/server/telemetry/_repro_1679_subprocess.py /
test_fastapi_instrumentation_ordering_1679.py for the identical
constraint's established precedent in this codebase) -- running any of
this in-process would make the outcome depend on which other telemetry
test file happened to run first in the same pytest session and already
claimed the global registries.

No mocks anywhere in the harnesses these tests launch: real
HTTPXClientInstrumentor, real TracerProvider/MeterProvider,
InMemorySpanExporter/InMemoryMetricReader, real VoyageAIClient, a real
local hermetic HTTP server (http.server), and the real,
actually-installed opentelemetry-exporter-otlp gRPC/HTTP exporters.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_TELEMETRY_DIR = Path(__file__).parent
_PROJECT_ROOT = Path(__file__).resolve().parents[4]

_SPAN_HARNESS = _TELEMETRY_DIR / "_repro_1676_ac5_httpx_span_subprocess.py"
_OTLP_HARNESS = _TELEMETRY_DIR / "_repro_1676_ac5_otlp_no_httpx_subprocess.py"
_WIRING_HARNESS = _TELEMETRY_DIR / "_repro_1676_ac5_lifespan_wiring_subprocess.py"

# Ceiling for a fresh-interpreter subprocess run. Overridable for a
# slower CI/dev machine; generous multiple of locally-observed run time,
# guarding only against a genuine hang.
_DEFAULT_HARNESS_TIMEOUT_SECONDS = 90


def _positive_int_env(var_name: str, default: int) -> int:
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


_HARNESS_TIMEOUT_SECONDS = _positive_int_env(
    "CIDX_TEST_HARNESS_TIMEOUT_SECONDS", _DEFAULT_HARNESS_TIMEOUT_SECONDS
)


def _run_harness(
    harness_path: Path, output_env_var: str, tmp_path: Path, extra_env: Dict[str, str]
) -> Dict[str, Any]:
    output_file = tmp_path / f"{harness_path.stem}_result.json"
    env = dict(os.environ)
    env[output_env_var] = str(output_file)
    env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, str(harness_path)],
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=_HARNESS_TIMEOUT_SECONDS,
    )

    assert proc.returncode == 0, (
        f"Subprocess harness {harness_path.name} failed (exit "
        f"{proc.returncode}).\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    result: Dict[str, Any] = json.loads(output_file.read_text())
    return result


@pytest.mark.slow
class TestRealHttpxSpanCreationForHermeticServerCall:
    """Story #1676 AC5 requirements 6 and 7: a real outbound HTTP call
    through the production VoyageAIClient, over a genuine socket to a
    local hermetic server, must produce a real span AND increment the
    existing cidx.embedding.requests counter for the SAME call.
    """

    def test_real_call_produces_span_with_expected_attributes(self, tmp_path: Path):
        result = _run_harness(_SPAN_HARNESS, "REPRO_1676_AC5_OUTPUT_FILE", tmp_path, {})

        assert result["instrument_httpx_result"] is True, result
        assert result["embedding_result_has_data"] is True, result

        assert result["httpx_span_count"] == 1, (
            "Expected exactly one httpx span for the one real POST to the "
            f"hermetic server -- got {result['httpx_span_count']}. Without "
            "instrument_httpx() wired up, this would be 0. "
            f"Full result: {result}"
        )

        span = result["httpx_spans"][0]
        assert span["name"] == "POST", span
        attrs = span["attributes"]
        assert attrs["http.method"] == "POST", attrs
        assert attrs["http.status_code"] == 200, attrs
        assert "/v1/embeddings" in attrs["http.url"], attrs
        assert "127.0.0.1" in attrs["http.url"], attrs

    def test_real_call_increments_embedding_requests_counter_too(self, tmp_path: Path):
        """Requirement 7: the span above and the pre-existing
        cidx.embedding.requests counter (Story #1586 AC2) must BOTH fire
        for the same real call -- neither replaces nor double-counts the
        other.
        """
        result = _run_harness(_SPAN_HARNESS, "REPRO_1676_AC5_OUTPUT_FILE", tmp_path, {})

        assert result["app_metrics_active"] is True, result
        assert result["httpx_span_count"] == 1, result
        assert result["embedding_requests_metric_value"] == 1, (
            "Expected the cidx.embedding.requests counter to have recorded "
            f"exactly one event for the one real embedding call -- got "
            f"{result['embedding_requests_metric_value']}. Full result: "
            f"{result}"
        )


@pytest.mark.slow
class TestOtlpExporterTrafficProducesZeroHttpxSpans:
    """Story #1676 AC5 requirement 3: neither OTLP exporter implementation
    (gRPC: a raw grpc channel; HTTP: requests.Session) uses httpx, so
    their own outbound export attempts must never show up as additional
    httpx spans -- verified dynamically against the actually-installed
    opentelemetry-exporter-otlp package, not assumed from source reading.
    """

    def test_otlp_grpc_and_http_exporters_produce_no_new_spans(self, tmp_path: Path):
        result = _run_harness(
            _OTLP_HARNESS, "REPRO_1676_AC5_OTLP_OUTPUT_FILE", tmp_path, {}
        )

        assert result["instrument_httpx_result"] is True, result
        assert result["span_count_before_export_attempts"] == 1, result
        assert result["span_count_after_export_attempts"] == 1, (
            "OTLP exporter traffic produced unexpected NEW spans -- this "
            "would mean the gRPC or HTTP OTLP exporter's own outbound "
            f"call was (incorrectly) intercepted by httpx instrumentation. "
            f"Full result: {result}"
        )
        assert result["span_names_after"] == ["dummy-request"], result


@pytest.mark.slow
class TestLifespanWiresHttpxInstrumentation:
    """Story #1676 AC5 requirement 2: httpx instrumentation must actually
    fire as a structural consequence of real server startup
    (startup/lifespan.py), gated on telemetry_config.enabled -- not just
    exist as a correctly-behaving standalone function nobody calls.
    """

    def _run_wiring_harness(
        self, tmp_path: Path, *, telemetry_enabled: bool
    ) -> Dict[str, Any]:
        config_dir = tmp_path / ".cidx-server"
        (config_dir / "data" / "golden-repos").mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "telemetry_config": {
                        "enabled": telemetry_enabled,
                        "export_traces": True,
                        "collector_endpoint": "http://localhost:4317",
                    }
                }
            )
        )

        extra_env = {"CIDX_SERVER_DATA_DIR": str(config_dir)}
        return _run_harness(
            _WIRING_HARNESS, "REPRO_1676_AC5_WIRING_OUTPUT_FILE", tmp_path, extra_env
        )

    def test_httpx_instrumented_when_telemetry_enabled(self, tmp_path: Path):
        result = self._run_wiring_harness(tmp_path, telemetry_enabled=True)

        assert result["telemetry_manager_present"] is True, result
        assert result["httpx_instrumented_during_lifespan"] is True, (
            "Expected startup/lifespan.py's telemetry-init block to have "
            "called instrument_httpx() during real server startup -- got "
            f"False. Full result: {result}"
        )

    def test_httpx_not_instrumented_when_telemetry_disabled(self, tmp_path: Path):
        result = self._run_wiring_harness(tmp_path, telemetry_enabled=False)

        assert result["telemetry_manager_present"] is False, result
        assert result["httpx_instrumented_during_lifespan"] is False, result
