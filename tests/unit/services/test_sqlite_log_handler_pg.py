"""
Tests for Story #526: SQLiteLogHandler PG-mode cleanup.

When a logs_backend is provided at construction time, SQLiteLogHandler should
skip local SQLite initialization entirely (AC2), and emit() should delegate to
the backend from the start (AC3). Without a backend, existing behavior is
unchanged (AC4). Calling set_logs_backend() after construction warns about
deprecation (AC6).
"""

import logging
import time
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


class FakeBatchBackend(FakeBackend):
    """LogsBackend stub that also implements the batch insert_logs method."""

    def __init__(self):
        super().__init__()
        self.batch_calls = []  # list of record-lists

    def insert_logs(self, records):
        self.batch_calls.append(list(records))


def _log_params(msg):
    """Build the params dict emit() enqueues onto the drainer queue."""
    return {
        "timestamp": "2026-07-08T00:00:00+00:00",
        "level": "INFO",
        "source": "test",
        "message": msg,
        "correlation_id": None,
        "user_id": None,
        "request_path": None,
        "extra_data": None,
        "node_id": None,
        "alias": None,
    }


class TestDrainerBatching:
    """EVO-63910 follow-up: the async drainer coalesces queued records into a
    single backend write, cutting commit amplification on Aurora."""

    def test_drainer_batches_via_insert_logs(self, tmp_path):
        """Records already queued are drained in ONE insert_logs batch."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        backend = FakeBatchBackend()
        handler = SQLiteLogHandler(db_path=str(tmp_path / "x.db"), logs_backend=backend)
        # Pre-load the queue BEFORE the drainer starts so all records are
        # available to coalesce in one drain iteration.
        for i in range(5):
            handler._log_queue.put(_log_params(f"m{i}"))

        handler._ensure_flush_thread()
        handler._log_queue.join()  # blocks until every record is task_done()

        total_batched = sum(len(c) for c in backend.batch_calls)
        assert total_batched == 5, "all records must be inserted via batch path"
        assert backend.calls == [], "batch path must not fall back to insert_log"
        assert len(backend.batch_calls) >= 1, "batch insert_logs must be used"
        handler.close()

    def test_drainer_falls_back_to_insert_log_without_batch(self, tmp_path):
        """A backend without insert_logs still drains via per-record insert_log."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        backend = FakeBackend()  # no insert_logs method
        handler = SQLiteLogHandler(db_path=str(tmp_path / "x.db"), logs_backend=backend)
        for i in range(3):
            handler._log_queue.put(_log_params(f"m{i}"))

        handler._ensure_flush_thread()
        handler._log_queue.join()

        assert len(backend.calls) == 3, "each record inserted via insert_log"
        handler.close()


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

        # emit() offloads the backend write to a background drainer (EVO-63910),
        # so the insert is asynchronous — wait for the drainer to flush before
        # asserting instead of racing it.
        deadline = time.time() + 5.0
        while not backend.calls and time.time() < deadline:
            time.sleep(0.01)

        assert len(backend.calls) >= 1, "emit() should delegate to backend"
        handler.close()

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
