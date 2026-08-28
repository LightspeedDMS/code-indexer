"""Regression tests for GitHub issue #1645.

Defense-in-depth hardening: `_mcp_response()`
(src/code_indexer/server/mcp/handlers/_utils.py) calls `json.dumps(data)`
with no `default=` encoder. Bug #1642 fixed the one live instance of a
non-JSON-native value leaking through (a raw `datetime` from
AuditLogPostgresBackend), but that fix was applied at the data-access layer
(`sanitize_row()`), not at this shared serialization boundary. A future
handler that forwards ANY other non-JSON-native value (a fresh `datetime`,
`date`, `Decimal`, `UUID`, custom object, etc.) directly into an MCP
response would reproduce the exact same class of 100%-failure bug.

This test module proves three things:
(a) Before the fix, a raw datetime/date reaching `_mcp_response()` raises
    an unhandled TypeError from the bare `json.dumps(data)` call.
(b) After the fix, `_json_default()` is wired via `json.dumps(data,
    default=_json_default)` so datetime/date values serialize correctly to
    ISO-8601 strings using the "T" separator (never `str()`'s space
    separator).
(c) A genuinely unsupported type (Decimal, or a custom object) is NOT
    silently stringified -- `_json_default()` must still raise TypeError
    with a clear message, never succeed with a wrong/lossy string
    representation. This is the anti-silent-failure guarantee: a blanket
    `str(obj)` fallback was explicitly considered and rejected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from code_indexer.server.mcp.handlers._utils import _json_default, _mcp_response

REAL_DT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
REAL_DATE = date(2026, 8, 23)


@dataclass
class _UnsupportedCustomObject:
    """A plain custom object with no JSON representation -- must never be
    silently stringified."""

    value: str


class TestMcpResponseRawDatetimeCurrentlyRaises:
    """(a) Reproduces the class of bug this issue guards against: today,
    without a `default=` encoder, a raw datetime/date reaching
    `_mcp_response()` blows up with an unhandled TypeError -- exactly the
    kind of 100%-failure bug Bug #1642 hit in a different code path.

    These tests exercise the UNGUARDED `json.dumps` behavior directly
    (bypassing `_mcp_response`'s own fix) to prove the failure mode this
    issue is about actually exists absent the `default=` wiring.
    """

    def test_bare_json_dumps_raises_typeerror_on_datetime(self) -> None:
        with pytest.raises(TypeError, match="not JSON serializable"):
            json.dumps({"success": True, "timestamp": REAL_DT})

    def test_bare_json_dumps_raises_typeerror_on_date(self) -> None:
        with pytest.raises(TypeError, match="not JSON serializable"):
            json.dumps({"success": True, "day": REAL_DATE})


class TestJsonDefaultSerializesDatetimeAndDate:
    """(b) After the fix, `_json_default` converts datetime/date to
    ISO-8601 strings using `.isoformat()` -- the "T" separator this
    codebase uses everywhere else, never `str()`'s space separator.
    """

    def test_json_default_converts_datetime_to_isoformat(self) -> None:
        result = _json_default(REAL_DT)

        assert result == REAL_DT.isoformat()
        assert "T" in result
        assert " " not in result

    def test_json_default_converts_date_to_isoformat(self) -> None:
        result = _json_default(REAL_DATE)

        assert result == REAL_DATE.isoformat()

    def test_mcp_response_serializes_raw_datetime_without_raising(self) -> None:
        data = {"success": True, "timestamp": REAL_DT}

        response = _mcp_response(data)
        text = response["content"][0]["text"]
        payload = json.loads(text)

        assert payload["success"] is True
        assert payload["timestamp"] == REAL_DT.isoformat()
        assert "T" in payload["timestamp"]

    def test_mcp_response_serializes_raw_date_without_raising(self) -> None:
        data = {"success": True, "day": REAL_DATE}

        response = _mcp_response(data)
        payload = json.loads(response["content"][0]["text"])

        assert payload["day"] == REAL_DATE.isoformat()


class TestJsonDefaultRaisesLoudlyForUnsupportedTypes:
    """(c) Anti-silent-failure guarantee: a genuinely unsupported type must
    NOT be silently stringified by a blanket `str(obj)` fallback -- it must
    still raise TypeError with a clear message identifying the offending
    type, both when calling `_json_default` directly and when it reaches
    `_mcp_response()` end-to-end.
    """

    def test_json_default_raises_typeerror_for_decimal(self) -> None:
        with pytest.raises(TypeError, match="Decimal is not JSON serializable"):
            _json_default(Decimal("3.14"))

    def test_json_default_raises_typeerror_for_custom_object(self) -> None:
        obj = _UnsupportedCustomObject(value="whatever")

        with pytest.raises(
            TypeError, match="_UnsupportedCustomObject is not JSON serializable"
        ):
            _json_default(obj)

    def test_mcp_response_raises_for_decimal_not_silently_stringified(self) -> None:
        data = {"success": True, "amount": Decimal("99.95")}

        with pytest.raises(TypeError, match="Decimal is not JSON serializable"):
            _mcp_response(data)

    def test_mcp_response_raises_for_custom_object_not_silently_stringified(
        self,
    ) -> None:
        data = {"success": True, "thing": _UnsupportedCustomObject(value="x")}

        with pytest.raises(
            TypeError, match="_UnsupportedCustomObject is not JSON serializable"
        ):
            _mcp_response(data)
