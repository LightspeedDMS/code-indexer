"""
Tests for Story #876 Phase C: direct-SQLite emit path honours alias column.

SQLiteLogHandler has two emit paths:

1. Delegated path (preferred, Story #500): a LogsBackend is injected at
   construction time, emit() forwards to backend.insert_log().  This path is
   covered by test_sqlite_log_handler_alias.py.

2. Direct-SQLite path (backwards-compat fallback): no backend injected,
   emit() writes directly to logs.db via the handler's own connection.

This suite exercises path 2 — we must not regress it when adding the new
`alias` column.  Both paths must produce logs.alias rows identically.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path


def test_init_database_creates_logs_table_with_alias_column(
    tmp_path: Path,
) -> None:
    """SQLiteLogHandler._init_database must provision the alias column."""
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    db_path = tmp_path / "handler_logs.db"
    handler = SQLiteLogHandler(db_path=db_path)  # no backend -> path 2
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()
            }
            assert "alias" in columns, (
                "SQLiteLogHandler._init_database must provision `alias` so the "
                "direct-SQLite fallback path stays consistent with the backend "
                "path (Story #876 Phase C). "
                f"Columns found: {sorted(columns)}"
            )
        finally:
            conn.close()
    finally:
        handler.close()


def test_emit_direct_path_persists_alias_to_own_column(tmp_path: Path) -> None:
    """emit() without a backend must write alias to the logs.alias column."""
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    db_path = tmp_path / "handler_direct.db"
    handler = SQLiteLogHandler(db_path=db_path)
    try:
        record = logging.LogRecord(
            name="lifecycle-runner",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="write_meta_md failed",
            args=(),
            exc_info=None,
        )
        setattr(record, "alias", "my-repo-direct")

        handler.emit(record)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT message, alias FROM logs WHERE source = ?",
                ("lifecycle-runner",),
            ).fetchone()
            assert row is not None, "emit() must have written a row"
            assert row[0] == "write_meta_md failed"
            assert row[1] == "my-repo-direct", (
                f"alias must land in its own column on the direct path; got row={row}"
            )
        finally:
            conn.close()
    finally:
        handler.close()
