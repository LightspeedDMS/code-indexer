"""
Tests for Story #876 Phase C: SQLiteLogHandler forwards `alias` to the backend.

The lifecycle-runner emits ERROR log records tagged with the repo alias:

    logger.error(
        "write_meta_md failed",
        extra={"alias": "my-repo", ...},
    )

`SQLiteLogHandler.emit()` must extract the alias from the LogRecord's
`__dict__` (where `extra=...` lands) and pass it through as a dedicated
`alias=...` kwarg to `LogsBackend.insert_log()` — so the alias lands in the
new `logs.alias` column rather than the generic `extra_data` JSON blob.

A FakeLogsBackend captures the kwargs; no real SQLite or PG file is touched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional


class _FakeLogsBackend:
    """Minimal LogsBackend stand-in that records every insert_log() call."""

    def __init__(self) -> None:
        self.calls: list[Dict[str, Any]] = []

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: str,
        message: str,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
        alias: Optional[str] = None,
    ) -> None:
        self.calls.append(
            {
                "timestamp": timestamp,
                "level": level,
                "source": source,
                "message": message,
                "correlation_id": correlation_id,
                "user_id": user_id,
                "request_path": request_path,
                "extra_data": extra_data,
                "node_id": node_id,
                "alias": alias,
            }
        )

    # Protocol completeness (not exercised by these tests)
    def query_logs(self, *args: Any, **kwargs: Any):  # pragma: no cover
        return [], 0

    def cleanup_old_logs(self, days_to_keep: int) -> int:  # pragma: no cover
        return 0

    def close(self) -> None:  # pragma: no cover
        return None


def _build_handler_with_backend(backend: _FakeLogsBackend, tmp_path: Path) -> Any:
    """Construct SQLiteLogHandler with the backend injected at __init__.

    Using constructor injection (post Story #526) skips all local SQLite init.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    # db_path is not used when backend is injected, but the constructor still
    # requires a Path — pass a throwaway path under tmp_path for safety.
    return SQLiteLogHandler(db_path=tmp_path / "unused_logs.db", logs_backend=backend)


def test_emit_forwards_alias_from_extra_to_backend(tmp_path: Path) -> None:
    """alias on LogRecord.extra must arrive as the alias kwarg on insert_log."""
    backend = _FakeLogsBackend()
    handler = _build_handler_with_backend(backend, tmp_path)

    record = logging.LogRecord(
        name="lifecycle-runner",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="write_meta_md failed",
        args=(),
        exc_info=None,
    )
    # Simulate `logger.error(..., extra={"alias": "my-repo"})` — the logging
    # framework attaches extra-dict entries as attributes on the record.
    setattr(record, "alias", "my-repo")

    handler.emit(record)

    assert len(backend.calls) == 1
    assert backend.calls[0]["alias"] == "my-repo", (
        "SQLiteLogHandler.emit() must forward extra['alias'] as alias kwarg. "
        f"Captured call: {backend.calls[0]}"
    )


def test_emit_without_alias_forwards_none(tmp_path: Path) -> None:
    """When no alias is attached to the record, insert_log gets alias=None."""
    backend = _FakeLogsBackend()
    handler = _build_handler_with_backend(backend, tmp_path)

    record = logging.LogRecord(
        name="any.module",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="no alias here",
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    assert len(backend.calls) == 1
    assert backend.calls[0]["alias"] is None, (
        "Records without extra['alias'] must produce alias=None — never raise, "
        "never sneak the alias into extra_data."
    )


def test_emit_does_not_include_alias_in_extra_data_json(tmp_path: Path) -> None:
    """Alias must land in its own column, NOT be re-serialised into extra_data."""
    import json

    backend = _FakeLogsBackend()
    handler = _build_handler_with_backend(backend, tmp_path)

    record = logging.LogRecord(
        name="lifecycle-runner",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )
    setattr(record, "alias", "my-repo")
    # Add a legitimate extra field to make sure normal extra handling still works.
    setattr(record, "attempt", 3)

    handler.emit(record)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["alias"] == "my-repo"

    # extra_data should carry `attempt` but NOT `alias` — alias has its own column.
    extra_data_raw = call["extra_data"]
    assert extra_data_raw is not None, (
        "extra_data should carry the `attempt` field as JSON"
    )
    parsed = json.loads(extra_data_raw)
    assert parsed.get("attempt") == 3
    assert "alias" not in parsed, (
        "alias must NOT leak into extra_data JSON — it has its own column now."
    )
