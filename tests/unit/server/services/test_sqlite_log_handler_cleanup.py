"""
Unit tests for SQLiteLogHandler connection management.

Story #369: Fix SQLite Thread-Local Connection Leak
Bug #435: Remaining sqlite3.connect() calls migrated to DatabaseConnectionManager

The SQLiteLogHandler now delegates all connection management to
DatabaseConnectionManager. These tests verify:
- _get_connection() returns a real usable connection
- emit() writes correctly across multiple threads
- close() is safe and idempotent
- The handler delegates to DatabaseConnectionManager (not its own pool)

Uses real SQLite connections and real threads - zero mocking.
"""

import logging
import sqlite3
import threading
from pathlib import Path


class TestSQLiteLogHandlerCleanup:
    """Tests for connection management in SQLiteLogHandler."""

    def test_get_connection_returns_real_connection(self, tmp_path: Path) -> None:
        """
        _get_connection() delegates to DatabaseConnectionManager and returns
        a real sqlite3.Connection that can execute SQL.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        conn = handler._get_connection()
        assert conn is not None
        # Connection must be usable
        result = conn.execute("SELECT 1").fetchone()
        assert result == (1,)
        handler.close()

    def test_get_connection_same_connection_within_thread(
        self, tmp_path: Path
    ) -> None:
        """
        _get_connection() returns the same connection object for repeated
        calls from the same thread (thread-local semantics via manager).
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        conn1 = handler._get_connection()
        conn2 = handler._get_connection()
        assert conn1 is conn2
        handler.close()

    def test_different_threads_get_independent_connections(
        self, tmp_path: Path
    ) -> None:
        """
        Threads get independent connections through DatabaseConnectionManager's
        thread-local storage.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        main_conn = handler._get_connection()
        thread_conn_holder: list = []

        def get_conn_in_thread() -> None:
            thread_conn_holder.append(handler._get_connection())

        t = threading.Thread(target=get_conn_in_thread)
        t.start()
        t.join()

        assert len(thread_conn_holder) == 1
        # Thread got a real connection
        assert thread_conn_holder[0] is not None
        # Thread's connection is independent from main thread's
        assert thread_conn_holder[0] is not main_conn
        handler.close()

    def test_concurrent_emit_from_multiple_threads(self, tmp_path: Path) -> None:
        """
        Multiple threads can emit log records concurrently without errors.
        All records must be written to the database.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        num_threads = 4
        records_per_thread = 5
        errors: list = []

        def emit_records(thread_idx: int) -> None:
            logger = logging.getLogger(f"thread_{thread_idx}")
            for i in range(records_per_thread):
                try:
                    record = logger.makeRecord(
                        name=f"thread_{thread_idx}",
                        level=logging.INFO,
                        fn="test_file.py",
                        lno=i,
                        msg=f"Thread {thread_idx} record {i}",
                        args=(),
                        exc_info=None,
                    )
                    handler.emit(record)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=emit_records, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        handler.close()

        assert errors == [], f"Errors during concurrent emit: {errors}"

        # Verify all records were written
        conn = sqlite3.connect(str(tmp_path / "test_logs.db"))
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()

        assert count == num_threads * records_per_thread

    def test_close_is_safe_and_idempotent(self, tmp_path: Path) -> None:
        """
        close() can be called multiple times without raising errors.
        Connection management is owned by DatabaseConnectionManager.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        handler._get_connection()

        # close() must not raise
        handler.close()
        handler.close()  # second call must also be safe

    def test_handler_delegates_to_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """
        SQLiteLogHandler uses DatabaseConnectionManager for connections,
        not its own internal pool. Verify by checking manager has a connection
        for this db after _get_connection() is called.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = tmp_path / "test_logs.db"
        handler = SQLiteLogHandler(db_path)
        conn_from_handler = handler._get_connection()

        # DatabaseConnectionManager should have the same connection for this thread
        conn_from_manager = DatabaseConnectionManager.get_instance(
            str(db_path)
        ).get_connection()
        assert conn_from_handler is conn_from_manager

        handler.close()

    def test_emit_still_works_after_adding_tracking(
        self, tmp_path: Path
    ) -> None:
        """
        Verify emit() still works correctly after connection management
        was delegated to DatabaseConnectionManager.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")

        # Create a log record and emit it
        logger = logging.getLogger("test_emit")
        record = logger.makeRecord(
            name="test_emit",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        # Verify it was written to the DB
        conn = sqlite3.connect(str(tmp_path / "test_logs.db"))
        rows = conn.execute("SELECT message FROM logs").fetchall()
        conn.close()

        assert len(rows) == 1
        assert "Test message" in rows[0][0]
        handler.close()


class TestEmitUsesExecuteAtomic:
    """
    Bug #435 fix: emit() must delegate its INSERT to execute_atomic()
    on DatabaseConnectionManager, not call conn.commit() directly on the
    shared thread-local connection.

    Using raw conn.commit() on the shared connection can commit or roll back
    transactions belonging to other callers on the same thread.
    execute_atomic() provides proper transaction isolation for each INSERT.
    """

    def test_emit_calls_execute_atomic_not_raw_commit(
        self, tmp_path: Path
    ) -> None:
        """
        emit() must call execute_atomic() on DatabaseConnectionManager,
        not raw conn.commit().
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = tmp_path / "test_emit_atomic.db"
        handler = SQLiteLogHandler(db_path)

        atomic_calls: list = []
        real_manager = DatabaseConnectionManager.get_instance(str(db_path))
        real_execute_atomic = real_manager.execute_atomic

        def tracking_execute_atomic(fn):
            atomic_calls.append(fn)
            return real_execute_atomic(fn)

        real_manager.execute_atomic = tracking_execute_atomic

        logger_inst = logging.getLogger("test_atomic")
        record = logger_inst.makeRecord(
            name="test_atomic",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="Atomic test message",
            args=(),
            exc_info=None,
        )

        try:
            handler.emit(record)
        finally:
            real_manager.execute_atomic = real_execute_atomic
            handler.close()

        assert len(atomic_calls) >= 1, (
            "emit() must call execute_atomic() for the INSERT; "
            "raw conn.commit() was used instead"
        )

    def test_emit_does_not_call_conn_commit_directly(
        self, tmp_path: Path
    ) -> None:
        """
        emit() must not call conn.commit() directly on the shared connection.
        Uses MagicMock(wraps=real_conn) to track commit() calls while
        still executing real SQL (Python 3.9: commit is a read-only C descriptor).
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        db_path = tmp_path / "test_emit_no_commit.db"
        handler = SQLiteLogHandler(db_path)

        # Build a wrapped connection that tracks commit() calls
        real_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        wrapped_conn = MagicMock(wraps=real_conn)

        mock_manager = MagicMock()
        mock_manager.get_connection.return_value = wrapped_conn

        logger_inst = logging.getLogger("test_no_commit")
        record = logger_inst.makeRecord(
            name="test_no_commit",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="No-commit test message",
            args=(),
            exc_info=None,
        )

        with patch(
            "code_indexer.server.services.sqlite_log_handler."
            "DatabaseConnectionManager.get_instance",
            return_value=mock_manager,
        ):
            handler.emit(record)

        real_conn.close()
        handler.close()

        commit_count = wrapped_conn.commit.call_count
        assert commit_count == 0, (
            f"emit() called conn.commit() directly {commit_count} time(s); "
            "it must use execute_atomic() instead"
        )
