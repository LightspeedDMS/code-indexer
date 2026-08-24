"""Bug #1642: query_audit_logs (and any MCP tool) must serialize datetime.

Root cause: cluster (PostgreSQL) backends return ``TIMESTAMPTZ`` columns as
native ``datetime`` objects. ``audit_logs.timestamp`` is ``TIMESTAMPTZ`` in the
PG schema (`002_groups_access_schema.sql`) but ``TEXT`` in the SQLite/solo
schema (`audit_log_service.py`), so ``handle_query_audit_logs`` forwarded a raw
``datetime`` into its response ONLY in cluster mode -- and ``_mcp_response``
called ``json.dumps(data)`` with no ``default=``, crashing 100% of the time
with ``Object of type datetime is not JSON serializable``.

These tests pin the fix at the single serialization choke point (`_mcp_response`)
and assert the ISO-8601 "T" convention used everywhere else in the codebase.
"""

import json
from datetime import date, datetime, timezone

from code_indexer.server.mcp.handlers._utils import _json_default, _mcp_response


def test_mcp_response_serializes_top_level_datetime() -> None:
    """A raw datetime placed directly in a response value must serialize,
    reproducing the exact audit-log entry shape from the PG path."""
    ts = datetime(2026, 8, 24, 14, 30, 21, tzinfo=timezone.utc)
    data = {
        "success": True,
        "entries": [{"timestamp": ts, "action": "user.create"}],
        "total": 1,
    }

    response = _mcp_response(data)
    text = response["content"][0]["text"]

    parsed = json.loads(text)
    assert parsed["entries"][0]["timestamp"] == ts.isoformat()
    assert "T" in parsed["entries"][0]["timestamp"]  # ISO "T", never str()'s space


def test_mcp_response_serializes_nested_datetime_in_details() -> None:
    """The PR/cleanup-log entries embed the whole source dict under ``details``;
    a datetime nested there must serialize too, not just the top-level field."""
    ts = datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc)
    data = {
        "success": True,
        "entries": [{"timestamp": ts, "details": {"created_at": ts}}],
    }

    parsed = json.loads(_mcp_response(data)["content"][0]["text"])
    assert parsed["entries"][0]["details"]["created_at"] == ts.isoformat()


def test_json_default_matches_isoformat_convention() -> None:
    """datetime/date render via .isoformat(); anything else degrades to str
    rather than raising, so a serialization edge case never takes a tool down."""
    dt = datetime(2026, 1, 2, 3, 4, 5)
    assert _json_default(dt) == dt.isoformat()
    assert _json_default(date(2026, 1, 2)) == "2026-01-02"

    class _Weird:
        def __str__(self) -> str:
            return "weird-repr"

    assert _json_default(_Weird()) == "weird-repr"


def test_mcp_response_still_compact_with_default_encoder() -> None:
    """The Story #1491 AC7 invariant holds: adding default= must not
    reintroduce indentation nor force the pure-Python encoder."""
    ts = datetime(2026, 8, 24, 14, 30, 21, tzinfo=timezone.utc)
    text = _mcp_response({"entries": [{"timestamp": ts}]})["content"][0]["text"]
    assert "\n" not in text
