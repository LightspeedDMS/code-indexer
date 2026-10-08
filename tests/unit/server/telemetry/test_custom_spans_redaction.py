"""OTEL custom spans never export a secret value.

Invariant: create_span() passes span attributes, event attributes and
exception text through the shared redact_secret_fields() before they reach
the OpenTelemetry span, whether supplied up front or set on the yielded span.
Captured with a real TracerProvider and InMemorySpanExporter installed at the
spans module's tracer seam (active_span_exporter).
"""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest

from code_indexer.server.telemetry.spans import create_span
from tests.unit.server.telemetry.otel_test_support import active_span_exporter

_S = [f"ExampleOtelSecret{i}" for i in range(6)]


def _exported_text(spans: Sequence[Any]) -> str:
    """Everything an exporter would send for these spans."""
    records = []
    for span in spans:
        records.append(
            {
                "attributes": dict(span.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in span.events
                ],
                "status": span.status.description,
            }
        )
    return json.dumps(records, default=str)


def test_initial_attributes_are_redacted() -> None:
    with active_span_exporter() as exporter:
        with create_span(
            "cidx.example",
            attributes={
                "api_key": _S[0],
                "repo_url": f"https://example-user:{_S[1]}@example.com/r.git",
                "detail": f"token={_S[2]}",
                "alias": "example-repo",
            },
        ):
            pass
        exported = _exported_text(exporter.get_finished_spans())

    for secret in _S[:3]:
        assert secret not in exported
    assert "example-repo" in exported


def test_exception_text_is_redacted() -> None:
    with active_span_exporter() as exporter:
        with pytest.raises(RuntimeError):
            with create_span("cidx.example.failing"):
                raise RuntimeError(
                    f"clone failed password={_S[3]} for "
                    f"https://example-user:{_S[4]}@example.com/r.git"
                )
        exported = _exported_text(exporter.get_finished_spans())

    assert _S[3] not in exported
    assert _S[4] not in exported
    assert "RuntimeError" in exported


def test_attributes_set_on_the_yielded_span_are_redacted() -> None:
    with active_span_exporter() as exporter:
        with create_span("cidx.example.later") as span:
            span.set_attribute("client_secret", _S[5])
            span.add_event("step", attributes={"password": _S[5], "n": 3})
        exported = _exported_text(exporter.get_finished_spans())

    assert _S[5] not in exported
    assert '"n": 3' in exported
