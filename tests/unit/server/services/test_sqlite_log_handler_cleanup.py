"""
Unit tests for SQLiteLogHandler stale connection cleanup.

Story #369: Fix SQLite Thread-Local Connection Leak

Tests written FIRST following TDD methodology (red phase).
Uses real SQLite connections and real threads - zero mocking.
"""

import logging
import threading
import time
from pathlib import Path


class TestSQLiteLogHandlerCleanup:
    """Tests for stale connection cleanup in SQLiteLogHandler."""

    def test_handler_tracks_connections_in_dict(self, tmp_path: Path) -> None:
        """
        Scenario 4: SQLiteLogHandler tracks connections in _connections dict.

        Given a SQLiteLogHandler is initialized
        When _get_connection() is called from a thread
        Then the connection is tracked in _connections dict.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        assert hasattr(handler, "_connections")
        assert isinstance(handler._connections, dict)

        # Get connection from main thread
        conn = handler._get_connection()
        assert conn is not None

        thread_id = threading.get_ident()
        assert thread_id in handler._connections
        handler.close()

    def test_stale_log_handler_connections_cleaned(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 4 (continued): Stale connections are cleaned up.

        Given threads get connections from handler then die
        When cleanup is triggered
        Then stale connections are removed from _connections dict.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        # Prevent cleanup from firing at all during setup
        handler.CLEANUP_INTERVAL = 9999.0

        num_workers = 4
        barrier = threading.Barrier(num_workers + 1)  # workers + main

        def get_conn_and_wait() -> None:
            handler._get_connection()
            barrier.wait()   # signal: connection obtained
            barrier.wait()   # wait: main has checked count, now die

        threads = []
        for _ in range(num_workers):
            t = threading.Thread(target=get_conn_and_wait)
            t.start()
            threads.append(t)

        # Wait until all workers have connections
        barrier.wait()

        # 4 stale connections exist (workers are still alive here)
        assert len(handler._connections) == 4

        # Release workers to die
        barrier.wait()
        for t in threads:
            t.join()

        # Now enable cleanup and force it to run
        handler.CLEANUP_INTERVAL = 0.0
        handler._last_cleanup = 0.0

        # Trigger cleanup via _get_connection from main thread
        handler._get_connection()

        # Only main thread's connection should remain
        assert len(handler._connections) == 1
        handler.close()

    def test_enhanced_close_closes_all_connections(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 4: Enhanced close() closes ALL tracked connections.

        Given multiple threads have connections tracked in _connections
        When close() is called
        Then all connections are closed and _connections is cleared.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        # Prevent cleanup from firing at all during setup
        handler.CLEANUP_INTERVAL = 9999.0

        # Main thread gets connection
        main_conn = handler._get_connection()
        assert main_conn is not None

        num_workers = 3
        barrier = threading.Barrier(num_workers + 1)  # workers + main

        def get_conn_and_wait() -> None:
            handler._get_connection()
            barrier.wait()   # signal: connection obtained
            barrier.wait()   # wait: main has checked count, now die

        threads = []
        for _ in range(num_workers):
            t = threading.Thread(target=get_conn_and_wait)
            t.start()
            threads.append(t)

        # Wait until all workers have connections
        barrier.wait()

        # 4 connections total (1 main + 3 live workers)
        assert len(handler._connections) == 4

        # Release workers to die
        barrier.wait()
        for t in threads:
            t.join()

        # close() should close all
        handler.close()

        assert len(handler._connections) == 0

    def test_log_handler_cleanup_throttled_by_interval(
        self, tmp_path: Path
    ) -> None:
        """
        Verify cleanup is throttled - not called when interval hasn't elapsed.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        handler.CLEANUP_INTERVAL = 60.0  # Long interval - won't clean

        # Create a thread that gets a connection and dies
        t = threading.Thread(target=handler._get_connection)
        t.start()
        t.join()

        # 1 stale connection exists
        assert len(handler._connections) == 1

        # Set _last_cleanup to now (simulating recent cleanup)
        handler._last_cleanup = time.time()

        # Trigger _get_connection from main thread
        handler._get_connection()

        # Stale connection should NOT be cleaned (throttled)
        # Total: 2 (dead thread + main thread)
        assert len(handler._connections) == 2
        handler.close()

    def test_log_handler_has_cleanup_interval_attrs(
        self, tmp_path: Path
    ) -> None:
        """
        Verify _last_cleanup and CLEANUP_INTERVAL are initialized.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        assert hasattr(handler, "_last_cleanup")
        assert handler._last_cleanup == 0.0
        assert hasattr(handler, "CLEANUP_INTERVAL")
        assert handler.CLEANUP_INTERVAL > 0.0
        handler.close()

    def test_log_handler_cleanup_method_exists(self, tmp_path: Path) -> None:
        """
        Verify _cleanup_stale_connections method exists and is callable.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        assert hasattr(handler, "_cleanup_stale_connections")
        assert callable(handler._cleanup_stale_connections)
        handler.close()

    def test_emit_still_works_after_adding_tracking(
        self, tmp_path: Path
    ) -> None:
        """
        Verify emit() still works correctly after adding connection tracking.
        """
        import sqlite3

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
