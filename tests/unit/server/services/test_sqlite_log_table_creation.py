"""
Regression tests for Bug #732: SQLite 'logs' table creation.

Verifies that the logs table is created correctly in logs.db (NOT cidx_server.db)
by both SQLiteLogHandler and LogsSqliteBackend, that the table name is 'logs'
(NOT 'server_logs' as CLAUDE.md Section 11 incorrectly referenced), and that
the schema columns match what the post-E2E audit query requires.
"""

import logging
import sqlite3
import warnings
from pathlib import Path
from typing import List, Set, Tuple


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _get_table_names(db_path: Path) -> Set[str]:
    """Return set of table names in the given SQLite database file."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _get_logs_column_names(db_path: Path) -> Set[str]:
    """Return column names of the 'logs' table via PRAGMA (no interpolation)."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA table_info(logs)").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


def _count_logs(db_path: Path) -> int:
    """Return number of rows in the logs table."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT COUNT(*) FROM logs").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _fetch_rows_by_level(db_path: Path, level: str) -> List[Tuple[str, str, str]]:
    """Return (level, source, message) rows from logs filtered by level."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT level, source, message FROM logs WHERE level=?", (level,)
        ).fetchall()
    finally:
        conn.close()


def _make_error_record(name: str, msg: str) -> logging.LogRecord:
    """Create an ERROR LogRecord for testing."""
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# SQLiteLogHandler: table existence
# ---------------------------------------------------------------------------


class TestHandlerCreatesLogsTableOnInit:
    """SQLiteLogHandler creates logs.db and the logs table on construction."""

    def test_logs_db_file_is_created(self, tmp_path: Path) -> None:
        """logs.db file must be created on SQLiteLogHandler init."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        log_db_path = tmp_path / "logs.db"
        assert not log_db_path.exists()
        handler = SQLiteLogHandler(log_db_path)
        handler.close()
        assert log_db_path.exists()

    def test_logs_table_exists_after_handler_init(self, tmp_path: Path) -> None:
        """'logs' table must exist in logs.db after SQLiteLogHandler init."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        log_db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(log_db_path)
        handler.close()
        assert "logs" in _get_table_names(log_db_path)

    def test_table_name_is_logs_not_server_logs(self, tmp_path: Path) -> None:
        """Table must be named 'logs', not 'server_logs' (Bug #732 doc correction)."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        log_db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(log_db_path)
        handler.close()
        tables = _get_table_names(log_db_path)
        assert "server_logs" not in tables
        assert "logs" in tables


# ---------------------------------------------------------------------------
# SQLiteLogHandler: emit correctness
# ---------------------------------------------------------------------------


class TestHandlerEmitWritesToLogsTable:
    """emit() must write to the logs table immediately after handler init."""

    def test_emit_succeeds_on_fresh_db(self, tmp_path: Path) -> None:
        """emit() must not raise 'no such table: logs' on a fresh DB."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        log_db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(log_db_path)
        handler.emit(_make_error_record("test.bug732", "Bug #732 emit test"))
        handler.close()
        assert _count_logs(log_db_path) == 1

    def test_emit_persists_message_content(self, tmp_path: Path) -> None:
        """emit() must persist the message text to the logs table."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        log_db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(log_db_path)
        handler.emit(_make_error_record("test.content", "Bug #732 content check"))
        handler.close()
        rows = _fetch_rows_by_level(log_db_path, "ERROR")
        assert len(rows) == 1
        assert "Bug #732" in rows[0][2]


# ---------------------------------------------------------------------------
# SQLiteLogHandler: correct DB path
# ---------------------------------------------------------------------------


class TestHandlerWritesToLogsDbNotMainDb:
    """logs must go to logs.db, NOT cidx_server.db (Bug #732 path correction)."""

    def test_logs_table_not_in_cidx_server_db(self, tmp_path: Path) -> None:
        """'logs' table must NOT appear in cidx_server.db after handler init."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        server_dir = tmp_path / ".cidx-server"
        data_dir = server_dir / "data"
        data_dir.mkdir(parents=True)
        log_db_path = server_dir / "logs.db"
        main_db_path = data_dir / "cidx_server.db"

        conn = sqlite3.connect(str(main_db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        handler = SQLiteLogHandler(log_db_path)
        handler.close()

        assert "logs" not in _get_table_names(main_db_path)

    def test_logs_table_is_in_logs_db(self, tmp_path: Path) -> None:
        """'logs' table must be in logs.db (not cidx_server.db)."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        server_dir = tmp_path / ".cidx-server"
        (server_dir / "data").mkdir(parents=True)
        log_db_path = server_dir / "logs.db"

        handler = SQLiteLogHandler(log_db_path)
        handler.close()

        assert "logs" in _get_table_names(log_db_path)


# ---------------------------------------------------------------------------
# LogsSqliteBackend: table creation
# ---------------------------------------------------------------------------


class TestBackendCreatesLogsTable:
    """LogsSqliteBackend creates the logs table and supports idempotent re-init."""

    def test_logs_table_exists_after_backend_init(self, tmp_path: Path) -> None:
        """'logs' table must exist after LogsSqliteBackend init."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        LogsSqliteBackend(db_path=str(db_path))
        assert "logs" in _get_table_names(db_path)

    def test_insert_log_works_immediately_after_backend_init(
        self, tmp_path: Path
    ) -> None:
        """insert_log() must succeed on a fresh DB without 'no such table'."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        backend = LogsSqliteBackend(db_path=str(db_path))
        backend.insert_log(
            timestamp="2026-04-16T12:00:00+00:00",
            level="ERROR",
            source="test.bug732.backend",
            message="Bug #732 backend insert_log regression",
        )
        rows = _fetch_rows_by_level(db_path, "ERROR")
        assert len(rows) == 1
        assert "Bug #732" in rows[0][2]

    def test_ensure_schema_is_idempotent(self, tmp_path: Path) -> None:
        """Second LogsSqliteBackend init on same DB must not wipe existing rows."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        backend1 = LogsSqliteBackend(db_path=str(db_path))
        backend1.insert_log(
            timestamp="2026-04-16T12:00:00+00:00",
            level="INFO",
            source="test",
            message="First",
        )
        backend2 = LogsSqliteBackend(db_path=str(db_path))
        backend2.insert_log(
            timestamp="2026-04-16T12:00:01+00:00",
            level="INFO",
            source="test",
            message="Second",
        )
        assert _count_logs(db_path) == 2


# ---------------------------------------------------------------------------
# Schema regression: column names
# ---------------------------------------------------------------------------


class TestLogsTableSchemaColumns:
    """Verify the logs table schema has the columns the audit query requires."""

    def test_handler_creates_required_columns(self, tmp_path: Path) -> None:
        """SQLiteLogHandler must create logs table with required audit columns."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(db_path)
        handler.close()
        columns = _get_logs_column_names(db_path)
        assert {"timestamp", "level", "source", "message"}.issubset(columns), (
            f"logs table missing required columns; found: {columns}"
        )

    def test_backend_creates_required_columns(self, tmp_path: Path) -> None:
        """LogsSqliteBackend must create logs table with required audit columns."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        LogsSqliteBackend(db_path=str(db_path))
        columns = _get_logs_column_names(db_path)
        assert {"timestamp", "level", "source", "message", "node_id"}.issubset(
            columns
        ), f"logs table missing required columns; found: {columns}"

    def test_column_named_source_not_logger(self, tmp_path: Path) -> None:
        """Column must be 'source', not 'logger' (Bug #732 doc correction)."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        LogsSqliteBackend(db_path=str(db_path))
        columns = _get_logs_column_names(db_path)
        assert "logger" not in columns
        assert "source" in columns


# ---------------------------------------------------------------------------
# Integration: constructor injection (Story #526)
# ---------------------------------------------------------------------------


class TestHandlerConstructorInjection:
    """SQLiteLogHandler with logs_backend at construction (Story #526 path)."""

    def test_constructor_injection_has_logs_table(self, tmp_path: Path) -> None:
        """Handler with logs_backend at construction must have logs table."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        backend = LogsSqliteBackend(db_path=str(db_path))
        handler = SQLiteLogHandler(db_path, logs_backend=backend)
        handler.close()
        assert "logs" in _get_table_names(db_path)

    def test_constructor_injection_emit_writes_row(self, tmp_path: Path) -> None:
        """emit() via constructor-injected backend must write to logs table."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        backend = LogsSqliteBackend(db_path=str(db_path))
        handler = SQLiteLogHandler(db_path, logs_backend=backend)
        handler.emit(_make_error_record("test.constructor", "Constructor path emit"))
        handler.close()
        assert _count_logs(db_path) == 1


# ---------------------------------------------------------------------------
# Integration: set_logs_backend injection (lifespan.py pattern)
# ---------------------------------------------------------------------------


class TestHandlerSetLogsBackendInjection:
    """SQLiteLogHandler + set_logs_backend() injection (lifespan.py pattern)."""

    def test_set_logs_backend_emit_writes_to_logs_table(self, tmp_path: Path) -> None:
        """emit() via set_logs_backend() must write to logs table."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        handler = SQLiteLogHandler(db_path)
        backend = LogsSqliteBackend(db_path=str(db_path))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            handler.set_logs_backend(backend)
        handler.emit(
            _make_error_record("test.set_backend", "Bug #732 set_logs_backend")
        )
        handler.close()
        rows = _fetch_rows_by_level(db_path, "ERROR")
        assert len(rows) == 1
        assert "Bug #732" in rows[0][2]

    def test_post_e2e_audit_query_runs_on_logs_db(self, tmp_path: Path) -> None:
        """Corrected CLAUDE.md Section 11 audit query must succeed on logs.db."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        db_path = tmp_path / "logs.db"
        backend = LogsSqliteBackend(db_path=str(db_path))
        backend.insert_log(
            timestamp="2026-04-16T12:00:00+00:00",
            level="ERROR",
            source="test.audit",
            message="Audit ERROR",
        )
        backend.insert_log(
            timestamp="2026-04-16T12:00:01+00:00",
            level="WARNING",
            source="test.audit",
            message="Audit WARNING",
        )
        backend.insert_log(
            timestamp="2026-04-16T12:00:02+00:00",
            level="INFO",
            source="test.audit",
            message="Audit INFO (excluded)",
        )

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT timestamp, level, source, message FROM logs"
                " WHERE level IN ('ERROR','WARNING')"
                " ORDER BY timestamp DESC LIMIT 100"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        assert {r[1] for r in rows} == {"ERROR", "WARNING"}
