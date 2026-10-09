"""With telemetry and Langfuse both enabled in one process, no secret reaches
either exporter, and HTTP request spans never reach Langfuse.

Invariants:
  - Langfuse exports only its own spans, on a tracer provider of its own,
    never the global telemetry provider's request and httpx spans;
  - request spans keep their URL attributes with every secret query value
    and URL credential masked;
  - Langfuse span input and metadata are masked by the shared redactor.

The real Langfuse SDK exports over HTTP to a loopback sink; telemetry uses
an in-memory exporter. Runs in a child process (see the child's docstring).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_CHILD = Path(__file__).with_name("_repro_span_export_redaction_subprocess.py")
_SRC = Path(__file__).resolve().parents[4] / "src"
_TIMEOUT_SECONDS = 120
_TOKEN = "ExampleConfirmTokenValue1"
_URL_PASSWORD = "ExampleUrlPasswordValue2"
_INPUT_SECRET = "ExampleInputSecretValue3"


def _run_child() -> Dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    done = subprocess.run(
        [sys.executable, str(_CHILD), _TOKEN, _URL_PASSWORD, _INPUT_SECRET],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        env=env,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_secrets_reach_neither_exporter_and_request_spans_stay_out_of_langfuse() -> (
    None
):
    result = _run_child()
    otel_text = json.dumps(result["otel"])
    langfuse_text = result["langfuse"]

    assert result["langfuse_posts"] > 0
    for secret in (_TOKEN, _URL_PASSWORD, _INPUT_SECRET):
        assert secret not in otel_text
        assert secret not in langfuse_text
    request_urls = [
        str(span.get("http.url", "")) + str(span.get("http.target", ""))
        for span in result["otel"]
    ]
    assert any("keep=1" in url for url in request_urls), result["otel"]
    for attribute in ("http.target", "http.url", "url.query", "/example"):
        assert attribute not in langfuse_text
    assert "example-trace" in langfuse_text
