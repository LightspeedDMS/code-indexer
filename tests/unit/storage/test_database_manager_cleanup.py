"""Tests for DatabaseConnectionManager global cleanup (Bug #517).

Verifies that when any DatabaseConnectionManager instance triggers the periodic
cleanup, ALL instances in the singleton registry have their stale connections
removed -- not just the instance that triggered the cleanup.
"""

import threading
import time


class TestGlobalCleanup:
    """Verify cleanup propagates to ALL instances when any instance triggers it."""

    def test_cleanup_propagates_to_all_instances(self, tmp_path):
        """When instance A triggers cleanup, instance B's stale connections are also cleaned."""
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        # Reset class state for clean test
        DatabaseConnectionManager._instances.clear()
        DatabaseConnectionManager._last_global_cleanup = 0.0

        db_a = str(tmp_path / "a.db")
        db_b = str(tmp_path / "b.db")

        inst_a = DatabaseConnectionManager.get_instance(db_a)
        inst_b = DatabaseConnectionManager.get_instance(db_b)

        # Create a connection on inst_b from a thread that will die
        stale_thread_id = None

        def create_stale_connection():
            nonlocal stale_thread_id
            stale_thread_id = threading.get_ident()
            inst_b.get_connection()

        t = threading.Thread(target=create_stale_connection)
        t.start()
        t.join()

        # Verify inst_b has a connection for the dead thread
        assert stale_thread_id in inst_b._connections

        # Force cleanup interval to have passed
        DatabaseConnectionManager._last_global_cleanup = 0.0

        # Trigger cleanup from inst_a (NOT inst_b)
        inst_a.get_connection()

        # inst_b's stale connection should have been cleaned
        assert stale_thread_id not in inst_b._connections

        # Cleanup
        inst_a.close_all()
        inst_b.close_all()
        DatabaseConnectionManager._instances.clear()

    def test_cleanup_preserves_live_thread_connections(self, tmp_path):
        """Cleanup must NOT remove connections for threads that are still alive."""
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        DatabaseConnectionManager._instances.clear()
        DatabaseConnectionManager._last_global_cleanup = 0.0

        db = str(tmp_path / "live.db")
        inst = DatabaseConnectionManager.get_instance(db)

        # Get connection on current (live) thread
        inst.get_connection()
        current_tid = threading.get_ident()

        # Force cleanup
        DatabaseConnectionManager._last_global_cleanup = 0.0
        inst.get_connection()

        # Current thread's connection should still be there
        assert current_tid in inst._connections

        inst.close_all()
        DatabaseConnectionManager._instances.clear()

    def test_cleanup_respects_throttle_interval(self, tmp_path):
        """Cleanup should not run more often than the throttle interval."""
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        DatabaseConnectionManager._instances.clear()

        db = str(tmp_path / "throttle.db")
        inst = DatabaseConnectionManager.get_instance(db)

        # First call - sets cleanup timestamp (force it to run by zeroing first)
        DatabaseConnectionManager._last_global_cleanup = 0.0
        inst.get_connection()
        first_cleanup_time = DatabaseConnectionManager._last_global_cleanup

        # Immediate second call - should NOT reset timestamp (throttled)
        time.sleep(0.01)
        inst.get_connection()
        second_cleanup_time = DatabaseConnectionManager._last_global_cleanup

        # Timestamp should be the same (throttled, no new cleanup)
        assert second_cleanup_time == first_cleanup_time

        inst.close_all()
        DatabaseConnectionManager._instances.clear()
