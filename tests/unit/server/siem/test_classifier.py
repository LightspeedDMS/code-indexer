"""Chronicle response classification (spec section 8)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.siem_delivery.classifier import (
    DUPLICATE_POLICY,
    DuplicatePolicy,
    HttpOutcome,
    classify,
    local_signature,
    sanitize_path,
)

MARKER = "VENDOR-TEXT-MARKER-91"


def _err(
    code: int, status: str, fields: Optional[List[str]] = None, message: str = MARKER
) -> bytes:
    error: Dict[str, Any] = {"code": code, "message": message, "status": status}
    if fields:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {"field": f, "description": MARKER} for f in fields
                ],
            }
        ]
    return json.dumps({"error": error}).encode()


def _http(status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None):  # type: ignore[no-untyped-def]
    return HttpOutcome(status=status, body=body, headers=headers or {})


def test_duplicate_policy_ships_as_halt() -> None:
    assert DUPLICATE_POLICY is DuplicatePolicy.HALT
    assert [m.name for m in DuplicatePolicy] == ["HALT", "PROVEN_CONTRACT"]


@pytest.mark.parametrize("body", [b"", b"{}", b"  {}\n"])
def test_empty_success_is_accepted(body: bytes) -> None:
    c = classify(_http(200, body), event_count=4)
    assert c.cls == "accepted" and not c.unexpected_success_body


def test_success_with_body_is_accepted_and_flagged() -> None:
    c = classify(_http(200, b'{"x":1}'), event_count=4)
    assert c.cls == "accepted" and c.unexpected_success_body


def test_indexed_row_rejection() -> None:
    body = _err(
        400, "INVALID_ARGUMENT", ["inline_source.events[3].udm.metadata.eventType"]
    )
    c = classify(_http(400, body), event_count=8)
    assert c.cls == "row_rejection" and c.indices == [3]
    assert c.not_ingested
    assert MARKER not in c.signature
    assert "inline_source.events[*].udm.metadata.eventType" in c.signature


def test_camel_case_index_path_is_recognised() -> None:
    body = _err(400, "INVALID_ARGUMENT", ["inlineSource.events[0].udm"])
    assert classify(_http(400, body), event_count=1).indices == [0]


@pytest.mark.parametrize(
    "body",
    [
        _err(400, "INVALID_ARGUMENT"),
        b"<html>" + MARKER.encode() + b"</html>",
        b'{"unexpected":"shape"}',
        b'{"error":{"code":400,"message":"trunc',
    ],
)
def test_unindexed_rejection(body: bytes) -> None:
    c = classify(_http(400, body), event_count=4)
    assert c.cls == "row_rejection" and c.indices is None
    assert MARKER not in c.signature and c.signature.endswith("unindexed")


@pytest.mark.parametrize(
    "fields,expected",
    [
        (["parent"], "request_rejection"),
        (["inline_source.events[1].udm", "parent"], "request_rejection"),
        (["inline_source.events[9].udm"], "unclassified"),
    ],
)
def test_request_level_and_out_of_range(fields: List[str], expected: str) -> None:
    c = classify(_http(400, _err(400, "INVALID_ARGUMENT", fields)), event_count=4)
    assert c.cls == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        (404, "request_rejection"),
        (413, "request_rejection"),
        (415, "request_rejection"),
        (501, "request_rejection"),
        (401, "credential"),
        (403, "credential"),
        (409, "duplicate_response"),
        (429, "throttled"),
        (500, "transient"),
        (502, "transient"),
        (503, "transient"),
        (504, "transient"),
        (307, "unclassified"),
        (418, "unclassified"),
    ],
)
def test_status_table(status: int, expected: str) -> None:
    assert classify(_http(status, _err(status, "X")), event_count=4).cls == expected


def test_retry_after_seconds() -> None:
    c = classify(_http(429, b"", {"Retry-After": "120"}), event_count=1)
    assert c.retry_after_seconds == 120.0
    soon = classify(_http(429, b"", {"Retry-After": "soon"}), event_count=1)
    assert soon.retry_after_seconds is None


def test_retry_after_http_date() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=300)
    header = {"Retry-After": format_datetime(future, usegmt=True)}
    seconds = classify(_http(429, b"", header), event_count=1).retry_after_seconds
    assert seconds is not None and 290 <= seconds <= 300
    past = datetime.now(timezone.utc) - timedelta(seconds=300)
    header = {"Retry-After": format_datetime(past, usegmt=True)}
    assert classify(_http(429, b"", header), event_count=1).retry_after_seconds == 0.0


def test_transport_failures_are_transient_with_known_or_unknown_outcome() -> None:
    refused = classify(None, event_count=2, transport_error="connect")
    assert refused.cls == "transient" and not refused.outcome_unknown
    dropped = classify(None, event_count=2, transport_error="reset")
    assert dropped.cls == "transient" and dropped.outcome_unknown
    timeout = classify(None, event_count=2, transport_error="timeout")
    assert timeout.outcome_unknown


def test_responses_are_known_outcomes() -> None:
    assert not classify(_http(503), event_count=1).outcome_unknown


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("inline_source.events[12].udm.x", "inline_source.events[*].udm.x"),
        ("a b", "<unrecognised>"),
        ("x" * 201, "<unrecognised>"),
        ("udm.metadata/../", "<unrecognised>"),
    ],
)
def test_sanitize_path(raw: str, expected: str) -> None:
    assert sanitize_path(raw) == expected


def test_unknown_google_status_is_not_stored_verbatim() -> None:
    body = _err(400, MARKER, ["parent"])
    assert MARKER not in classify(_http(400, body), event_count=1).signature


def test_local_signature_shape() -> None:
    assert local_signature("oversize", "") == "local|oversize|"
    unrecognised = local_signature("unknown_field", "x y")
    assert unrecognised == "local|unknown_field|<unrecognised>"
