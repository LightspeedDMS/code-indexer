"""
Story #1676 AC3 Requirement 3a: SQLiteLogHandler must exclude the private
OTEL-context-capture attribute (``logging_utils.OTEL_CONTEXT_RECORD_ATTR``)
from its ``extra_data`` JSON serialization.

Background: async_logging.IdentityQueueHandler.prepare() now calls
``inject_otel_context()`` on EVERY log record, attaching a raw
``opentelemetry.context.Context`` object as a private attribute so the
context-aware OTEL log bridge handler can reattach it at export time. That
raw object is not JSON-serializable. SQLiteLogHandler.emit()'s
"copy every unknown LogRecord attribute into extra_data, then json.dumps()"
logic must exclude this attribute BEFORE that serialization step, or the
ENTIRE log record is silently dropped via the handler's exception path
(confirmed live: a real end-to-end run through IdentityQueueHandler ->
DrainableQueueListener -> SQLiteLogHandler raised
``TypeError: Object of type _Span is not JSON serializable`` and the row was
never persisted at all).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from code_indexer.server.logging_utils import OTEL_CONTEXT_RECORD_ATTR
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

# Whitelist of columns this test helper is allowed to SELECT -- avoids
# building a query from an unconstrained string even though every current
# caller passes a literal (defense-in-depth per code review).
_ALLOWED_COLUMNS = {"message", "extra_data"}


def _make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.sqlite_log_handler.otel_context_filter",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class _Unserializable:
    """Stand-in for a raw opentelemetry.context.Context: json.dumps() must
    raise TypeError on it (mirrors the real regression's exact failure
    shape) without needing a live OTEL span/context object in this test."""


def _emit_record_and_fetch_column(
    tmp_path: Path, record: logging.LogRecord, column: str
) -> Optional[Any]:
    """Emit+flush ``record`` through a real SQLiteLogHandler and return the
    named column for the row matching ``record.msg`` (None if no row was
    persisted at all)."""
    if column not in _ALLOWED_COLUMNS:
        raise ValueError(f"column {column!r} is not in the test whitelist")

    db_path = tmp_path / "logs.db"
    handler = SQLiteLogHandler(db_path=db_path)
    try:
        handler.emit(record)
        handler.flush()
    finally:
        handler.close()

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT {column} FROM logs WHERE message = ?", (record.msg,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


class TestSQLiteLogHandlerExcludesCapturedOtelContext:
    def test_record_with_private_context_attribute_is_still_persisted(
        self, tmp_path: Path
    ) -> None:
        record = _make_record("still persisted despite context attr")
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, _Unserializable())

        message = _emit_record_and_fetch_column(tmp_path, record, "message")

        assert message is not None, (
            "log row was dropped -- the unserializable OTEL context "
            "attribute leaked into json.dumps() and raised TypeError"
        )

    def test_captured_context_attribute_never_leaks_into_extra_data_json(
        self, tmp_path: Path
    ) -> None:
        import json

        record = _make_record("extra_data must exclude context attr")
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, _Unserializable())
        # A genuine extra field that SHOULD still appear in extra_data,
        # proving the fix is a targeted exclusion, not a blanket
        # "extra_data always empty" regression.
        record.some_real_extra_field = "keep-me"

        raw_extra_data = _emit_record_and_fetch_column(tmp_path, record, "extra_data")

        assert raw_extra_data is not None, "log row was dropped entirely"
        extra_data = json.loads(raw_extra_data)
        assert OTEL_CONTEXT_RECORD_ATTR not in extra_data
        assert extra_data.get("some_real_extra_field") == "keep-me"
