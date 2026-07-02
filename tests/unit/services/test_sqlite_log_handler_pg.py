"""
Tests for Story #526: SQLiteLogHandler PG-mode cleanup.

When a logs_backend is provided at construction time, SQLiteLogHandler should
skip local SQLite initialization entirely (AC2), and emit() should delegate to
the backend from the start (AC3). Without a backend, existing behavior is
unchanged (AC4). Calling set_logs_backend() after construction warns about
deprecation (AC6).
"""

import logging
import pytest
from pathlib import Path


class FakeBackend:
    """Minimal LogsBackend stub for testing delegation."""

    def __init__(self):
        self.calls = []

    def insert_log(self, **kwargs):
        self.calls.append(kwargs)

    def query_logs(self, **kwargs):
        return [], 0

    def cleanup_old_logs(self, days_to_keep):
        return 0

    def close(self):
        pass


class TestSQLiteLogHandlerPGMode:
    """Tests for Story #526: SQLiteLogHandler PG-mode cleanup."""

    def test_constructor_with_backend_skips_sqlite(self, tmp_path):
        """AC2: When logs_backend provided, skip _init_database()."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        backend = FakeBackend()
        db_path = str(tmp_path / "should_not_exist.db")
        SQLiteLogHandler(db_path=db_path, logs_backend=backend)

        # AC7: No SQLite file created
        assert not Path(db_path).exists(), "SQLite DB should not be created in PG mode"

    def test_emit_delegates_to_backend(self, tmp_path):
        """AC3: emit() delegates to backend from the start."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        backend = FakeBackend()
        handler = SQLiteLogHandler(db_path=str(tmp_path / "x.db"), logs_backend=backend)

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "Test message", (), None
        )
        handler.emit(record)

        assert len(backend.calls) >= 1, "emit() should delegate to backend"

    def test_constructor_without_backend_uses_sqlite(self, tmp_path):
        """AC4: Without backend, behavior is unchanged (SQLite mode)."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = str(tmp_path / "logs.db")
        SQLiteLogHandler(db_path=db_path)
        assert Path(db_path).exists(), "SQLite DB should be created in file mode"

    def test_set_logs_backend_warns_deprecation(self, tmp_path):
        """AC6: set_logs_backend() emits a DeprecationWarning."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        handler = SQLiteLogHandler(db_path=str(tmp_path / "logs.db"))
        with pytest.warns(DeprecationWarning, match="set_logs_backend"):
            handler.set_logs_backend(FakeBackend())
