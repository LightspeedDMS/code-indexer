"""
Integration tests for SQLite connection leak prevention.

Story #369: Fix SQLite Thread-Local Connection Leak

Simulates thread pool churn and verifies connection counts stay bounded.
Uses real SQLite connections and real threads - zero mocking.
"""

import os
import threading
from pathlib import Path

import pytest


def _count_open_fds() -> int:
    """Count open file descriptors for the current process."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    try:
        return len(os.listdir(fd_dir))
    except OSError:
        return -1


class TestConnectionLeakIntegration:
    """Integration tests for thread pool churn scenarios."""

    def test_fd_count_stays_bounded_after_cleanup(
        self, tmp_path: Path
    ) -> None:
        """
        Integration test: FD count stays bounded with thread pool churn.

        Given a DatabaseConnectionManager with short cleanup interval
        When many threads get connections and die in waves
        Then the FD count does not grow unboundedly.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.1  # 100ms cleanup interval

        baseline_fds = _count_open_fds()

        # Wave 1: Create 20 threads, each gets a connection, then dies
        threads = []
        for _ in range(20):
            t = threading.Thread(target=manager.get_connection)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # Force cleanup to run
        manager._last_cleanup = 0.0
        manager.get_connection()

        # Wave 2: Another 20 threads
        threads = []
        for _ in range(20):
            t = threading.Thread(target=manager.get_connection)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # Force cleanup again
        manager._last_cleanup = 0.0
        manager.get_connection()

        fds_after_second_cleanup = _count_open_fds()

        # After cleanup, connection dict should be small (just main thread)
        assert len(manager._connections) == 1, (
            f"Expected 1 connection after cleanup, got {len(manager._connections)}"
        )

        # FD count should not have grown significantly beyond baseline
        # Allow generous margin for other system activity (50 FDs extra max)
        assert fds_after_second_cleanup <= baseline_fds + 50, (
            f"FD count grew from {baseline_fds} baseline to "
            f"{fds_after_second_cleanup} after cleanup - possible leak"
        )

        manager.close_all()

    def test_connection_count_bounded_with_churn(self, tmp_path: Path) -> None:
        """
        Verify _connections dict size stays bounded with cleanup enabled.

        Given threads keep getting connections and dying
        When cleanup runs between waves
        Then _connections dict never accumulates unboundedly.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.0  # Cleanup on every get_connection call

        max_seen_connections = 0

        for wave in range(5):
            # Each wave: 10 threads get connections and die
            threads = []
            for _ in range(10):
                t = threading.Thread(target=manager.get_connection)
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

            # Force cleanup elapsed
            manager._last_cleanup = 0.0

            # Main thread triggers cleanup
            manager.get_connection()

            current_count = len(manager._connections)
            max_seen_connections = max(max_seen_connections, current_count)

        # After each wave's cleanup, only main thread connection remains
        # So max should be 1 (just main thread)
        assert max_seen_connections <= 1, (
            f"Connection dict grew to {max_seen_connections} after cleanup - "
            f"expected at most 1 (main thread only)"
        )

        manager.close_all()

    def test_both_managers_cleanup_independently(
        self, tmp_path: Path
    ) -> None:
        """
        Verify DatabaseConnectionManager and SQLiteLogHandler
        clean up independently without interference.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_manager = DatabaseConnectionManager(str(tmp_path / "main.db"))
        # Prevent cleanup from firing at all during setup
        db_manager.CLEANUP_INTERVAL = 9999.0

        log_handler = SQLiteLogHandler(tmp_path / "logs.db")
        log_handler.CLEANUP_INTERVAL = 9999.0

        num_workers = 5
        barrier = threading.Barrier(num_workers + 1)  # workers + main

        def worker() -> None:
            db_manager.get_connection()
            log_handler._get_connection()
            barrier.wait()   # signal: connections obtained
            barrier.wait()   # wait: main has checked counts, now die

        threads = []
        for _ in range(num_workers):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        # Wait until all workers have connections
        barrier.wait()

        # Both should have 5 stale connections (workers still alive)
        assert len(db_manager._connections) == 5
        assert len(log_handler._connections) == 5

        # Release workers to die
        barrier.wait()
        for t in threads:
            t.join()

        # Now enable cleanup
        db_manager.CLEANUP_INTERVAL = 0.0
        log_handler.CLEANUP_INTERVAL = 0.0

        # Force cleanup for both
        db_manager._last_cleanup = 0.0
        log_handler._last_cleanup = 0.0

        db_manager.get_connection()
        log_handler._get_connection()

        # Each should only have main thread connection now
        assert len(db_manager._connections) == 1
        assert len(log_handler._connections) == 1

        db_manager.close_all()
        log_handler.close()

    def test_no_connection_errors_during_churn(self, tmp_path: Path) -> None:
        """
        Verify no exceptions occur during concurrent cleanup and connection creation.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager(str(tmp_path / "test.db"))
        manager.CLEANUP_INTERVAL = 0.05  # Very short interval

        errors = []
        error_lock = threading.Lock()

        def worker() -> None:
            try:
                conn = manager.get_connection()
                # Do a simple query to verify connection works
                conn.execute("SELECT 1").fetchone()
            except Exception as e:
                with error_lock:
                    errors.append(str(e))

        # Run many concurrent threads
        threads = []
        for _ in range(30):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Got errors during concurrent access: {errors}"
        manager.close_all()
