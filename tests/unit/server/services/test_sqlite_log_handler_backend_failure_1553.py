"""RED-first tests for Bug #1553: SQLiteLogHandler backend-write-failure
observability.

The writer loop's backend-insert branch was wrapped in a bare
`except Exception: pass` with the comment "We can't log here (would
recurse), so swallow silently" -- so a PostgreSQL insert failure discards
log records with ZERO signal. These tests assert the TARGET behaviour (not
yet implemented): a `backend_write_failure_count` counter that increments
on backend `insert_log_batch` failure -- whether raised or reported via a
`False` return -- plus a throttled stderr message, with the writer thread
surviving and no retry/fallback/blocking behaviour added. All backend-like
objects here are REAL (no mocks); they deterministically fail by design to
exercise the writer loop's real error-handling code path.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List


def _make_record(msg: str = "test", level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="bug1553.failure",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class _RaisingBackend:
    """A REAL LogsBackend-shaped object whose insert_log_batch always raises."""

    is_cross_node_backend = True

    def __init__(self) -> None:
        self.call_count = 0

    def insert_log_batch(self, items: List[Any]) -> None:
        self.call_count += 1
        raise RuntimeError("simulated cross-node store unreachable")


class _FalseReturningBackend:
    """A REAL LogsBackend-shaped object whose insert_log_batch reports
    failure via a False return (the new bool contract) rather than raising.
    """

    is_cross_node_backend = True

    def __init__(self) -> None:
        self.call_count = 0

    def insert_log_batch(self, items: List[Any]) -> bool:
        self.call_count += 1
        return False


class TestBackendWriteFailureIsCounted:
    def test_raising_backend_increments_failure_counter(self, tmp_path):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        backend = _RaisingBackend()
        handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=backend)
        try:
            handler.emit(_make_record("boom-1553"))
            handler.flush()

            assert backend.call_count >= 1
            assert handler.backend_write_failure_count >= 1
        finally:
            handler.close()

    def test_false_returning_backend_increments_failure_counter(self, tmp_path):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        backend = _FalseReturningBackend()
        handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=backend)
        try:
            handler.emit(_make_record("boom-false-1553"))
            handler.flush()

            assert backend.call_count >= 1
            assert handler.backend_write_failure_count >= 1
        finally:
            handler.close()

    def test_successful_backend_does_not_increment_failure_counter(self, tmp_path):
        """Control: a healthy backend must never increment the failure
        counter, proving the counter tracks genuine failures only.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        backend = LogsSqliteBackend(db_path=str(tmp_path / "cluster.db"))
        handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=backend)
        try:
            handler.emit(_make_record("healthy-1553"))
            handler.flush()

            assert handler.backend_write_failure_count == 0
        finally:
            handler.close()


class TestBackendWriteFailureSurvivesAndIsSurfaced:
    def test_writer_thread_survives_repeated_backend_failures(self, tmp_path):
        """The writer thread must never die from a failing backend -- proven
        by emitting several records across multiple drain cycles and
        confirming the thread is still alive and still processing afterward.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        backend = _RaisingBackend()
        handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=backend)
        try:
            for i in range(5):
                handler.emit(_make_record(f"boom-{i}-1553"))
                handler.flush()
                time.sleep(0.01)

            assert handler._writer_thread is not None
            assert handler._writer_thread.is_alive()
            assert backend.call_count >= 1
            assert handler.backend_write_failure_count >= 1
        finally:
            handler.close()

    def test_failure_is_written_to_stderr(self, tmp_path, capsys):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        backend = _RaisingBackend()
        handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=backend)
        try:
            handler.emit(_make_record("stderr-marker-1553"))
            handler.flush()
        finally:
            handler.close()

        captured = capsys.readouterr()
        assert "SQLiteLogHandler" in captured.err
        assert "backend" in captured.err.lower()
