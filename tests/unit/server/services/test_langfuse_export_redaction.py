"""Nothing secret leaves through the Langfuse export boundary.

Invariant: every trace, span, trace/span update and score LangfuseClient
hands to the Langfuse SDK passes through the shared redact_secret_fields(),
whatever the caller supplied (trace name, input, output, metadata, intel,
tags, score comment). Captured at the SDK boundary: FakeLangfuseSdk is
injected at LangfuseClient's real ``_langfuse`` seam; LangfuseClient and
TraceStateManager are real.
"""

from __future__ import annotations

from tests.unit.server.services._fake_langfuse_sdk import traced_client

_SESSION = "example-session"
_S = [f"ExampleTraceSecret{i}" for i in range(10)]


def _assert_no_secret(sent: str) -> None:
    for secret in _S:
        assert secret not in sent, secret


def test_root_trace_start_and_end_export_nothing_secret() -> None:
    _client, traces, sdk = traced_client()

    assert (
        traces.start_trace(
            session_id=_SESSION,
            name=f"investigate token={_S[0]}",
            metadata={"api_key": _S[1], "note": "example-note"},
            input=f"prompt with password={_S[2]}",
            tags=[f"token={_S[3]}", "example-tag"],
            intel={"client_secret": _S[4]},
        )
        is not None
    )
    assert (
        traces.end_trace(
            session_id=_SESSION,
            score=0.9,
            summary=f"feedback api_key={_S[5]}",
            output=f"answer Authorization: Bearer {_S[6]}",
            tags=[f"secret={_S[7]}"],
            intel={"cookie": _S[8]},
        )
        is not None
    )

    sent = sdk.sent_text()
    _assert_no_secret(sent)
    assert "example-tag" in sent
    assert "example-note" in sent
    assert "investigate" in sent


def test_span_create_update_and_end_export_nothing_secret() -> None:
    client, _traces, sdk = traced_client()

    span = client.create_span(
        trace_id="example-trace-id",
        name="example_tool",
        metadata={"api_key": _S[0]},
        input_data={"password": _S[1], "repository_alias": "example-repo"},
    )
    client.update_span(
        span,
        output={"content": [{"type": "text", "text": f'{{"token": "{_S[2]}"}}'}]},
        level="ERROR",
    )
    client.end_span(span)

    sent = sdk.sent_text()
    _assert_no_secret(sent)
    assert "example-repo" in sent
    assert "ERROR" in sent
    assert ("span.end", {}) in sdk.sent


def test_trace_update_in_context_exports_nothing_secret() -> None:
    client, _traces, sdk = traced_client()
    trace = client.create_trace(name="example", session_id=_SESSION)
    assert trace is not None

    client.update_current_trace_in_context(
        trace.span,
        output=f"pass={_S[0]}",
        metadata={"private_key": _S[1]},
        tags=[f"apikey={_S[2]}"],
    )

    _assert_no_secret(sdk.sent_text())


def test_score_accepts_only_numeric_values() -> None:
    """A score value is a number; anything else (text, a flag, nothing) is
    rejected with an error naming its type and is never sent."""
    import pytest

    client, _traces, sdk = traced_client()

    for good in (1, 0.5):
        assert client.score(trace_id="t", name="quality", value=good) is not None
    sent_values = [
        kwargs["value"] for call, kwargs in sdk.sent if call == "create_score"
    ]
    assert sent_values == [1, 0.5]

    for bad in ("0.9", True, None, f"token={_S[0]}"):
        with pytest.raises(TypeError, match="score value must be a number"):
            client.score(trace_id="t", name="quality", value=bad)  # type: ignore[arg-type]
    assert len([c for c, _ in sdk.sent if c == "create_score"]) == 2
