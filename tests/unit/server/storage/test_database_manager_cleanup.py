"""
Unit tests for DatabaseConnectionManager stale connection cleanup.

Story #369: Fix SQLite Thread-Local Connection Leak

Tests written FIRST following TDD methodology (red phase).
Uses real SQLite connections and real threads - zero mocking.
"""

import threading
import time
from pathlib import Path

import pytest


class TestDatabaseConnectionManagerCleanup:
    """Tests for stale connection cleanup in DatabaseConnectionManager."""

    def test_stale_connections_cleaned_after_thread_death(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 1: Stale connections cleaned up after thread death.

        Given N threads each get a connection then terminate
        When cleanup is triggered (via get_connection from main thread)
        Then stale connections are removed from _connections dict
        And only alive threads' connections remain.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.0  # Force cleanup every time

        num_threads = 5
        # Use barrier to ensure all threads are alive when we check connection count
        barrier = threading.Barrier(num_threads + 1)  # +1 for main thread

        def get_conn_and_wait() -> None:
            manager.get_connection()
            barrier.wait()  # Signal main thread we have a connection
            barrier.wait()  # Wait for main to check count, then die

        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=get_conn_and_wait)
            t.start()
            threads.append(t)

        # Wait until all threads have connections
        barrier.wait()

        # All 5 threads have unique connections
        assert len(manager._connections) == 5

        # Now let all threads die
        barrier.wait()
        for t in threads:
            t.join()

        # Force cleanup interval to have elapsed
        manager._last_cleanup = 0.0

        # Trigger cleanup via get_connection from main thread
        manager.get_connection()

        # Only main thread's connection should remain
        assert len(manager._connections) == 1

    def test_cleanup_throttled_by_interval(self, tmp_path: Path) -> None:
        """
        Scenario 2: Cleanup is throttled to avoid excessive scanning.

        Given cleanup was recently performed
        When get_connection is called again
        Then _cleanup_stale_connections is NOT called again immediately.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 60.0  # Normal interval

        # Create a thread that gets a connection and dies
        t = threading.Thread(target=manager.get_connection)
        t.start()
        t.join()

        # There is 1 stale connection
        assert len(manager._connections) == 1

        # Set _last_cleanup to now (simulating recent cleanup)
        manager._last_cleanup = time.time()

        # Trigger get_connection from main thread
        manager.get_connection()

        # Stale connection should NOT be cleaned (throttled)
        # Main thread connection added, so total is 2 (main + dead thread)
        assert len(manager._connections) == 2

    def test_active_thread_connections_never_closed_by_cleanup(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 3: Active thread connections never closed by cleanup.

        Given multiple threads are alive and have connections
        When cleanup runs
        Then NO active thread connections are removed.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.0  # Force cleanup every time

        barrier = threading.Barrier(3)  # 2 worker threads + main
        connections_obtained = []
        connection_lock = threading.Lock()

        def get_and_hold_connection() -> None:
            conn = manager.get_connection()
            with connection_lock:
                connections_obtained.append(conn)
            # Wait until main thread has triggered cleanup
            barrier.wait()
            # Keep thread alive a bit longer
            barrier.wait()

        # Start 2 threads that get connections and stay alive
        t1 = threading.Thread(target=get_and_hold_connection)
        t2 = threading.Thread(target=get_and_hold_connection)
        t1.start()
        t2.start()

        # Wait until both workers have connections
        barrier.wait()

        # Now trigger cleanup from main thread
        manager._last_cleanup = 0.0
        manager.get_connection()

        # All 3 connections (2 workers + main) should still be present
        assert len(manager._connections) == 3

        # Let worker threads finish
        barrier.wait()
        t1.join()
        t2.join()

    def test_close_all_closes_all_tracked_connections(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario 5: close_all() still works for shutdown.

        Given multiple threads have connections
        When close_all() is called
        Then all connections are closed and _connections dict is cleared.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        # Prevent cleanup from firing at all
        manager.CLEANUP_INTERVAL = 9999.0

        # Main thread gets a connection
        conn = manager.get_connection()
        assert conn is not None

        num_workers = 3
        # Use barrier so all workers are alive simultaneously → unique thread IDs
        barrier = threading.Barrier(num_workers + 1)  # workers + main

        def get_conn_and_wait() -> None:
            manager.get_connection()
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
        assert len(manager._connections) == 4

        # Release workers to die
        barrier.wait()
        for t in threads:
            t.join()

        # close_all() should clear everything
        manager.close_all()

        assert len(manager._connections) == 0

    def test_cleanup_stale_connections_method_exists(
        self, tmp_path: Path
    ) -> None:
        """
        Verify _cleanup_stale_connections method exists and is callable.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        assert hasattr(manager, "_cleanup_stale_connections")
        assert callable(manager._cleanup_stale_connections)

    def test_last_cleanup_attribute_initialized(self, tmp_path: Path) -> None:
        """
        Verify _last_cleanup and CLEANUP_INTERVAL are initialized in __init__.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        assert hasattr(manager, "_last_cleanup")
        assert manager._last_cleanup == 0.0
        assert hasattr(manager, "CLEANUP_INTERVAL")
        assert manager.CLEANUP_INTERVAL > 0.0

    def test_cleanup_logs_stale_connection_removal(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Verify cleanup logs info message when stale connections are removed.
        """
        import logging

        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.0

        # Create a thread that gets a connection and dies
        t = threading.Thread(target=manager.get_connection)
        t.start()
        t.join()

        manager._last_cleanup = 0.0

        with caplog.at_level(logging.INFO):
            manager.get_connection()

        assert any(
            "stale" in record.message.lower() for record in caplog.records
        ), f"Expected stale cleanup log message, got: {[r.message for r in caplog.records]}"
