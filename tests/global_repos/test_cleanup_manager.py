"""
Tests for CleanupManager - background cleanup of old index versions.

Tests AC3 Technical Requirements:
- Cleanup thread monitors ref counts
- Delete old index when ref count = 0
- Keep max 2 versions (current + previous)
"""

import time
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


class TestCleanupManager:
    """Test suite for CleanupManager component."""

    def test_cleanup_manager_starts_and_stops(self, tmp_path):
        """
        Test that cleanup manager can be started and stopped cleanly.

        Basic lifecycle management
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        cleanup_mgr.start()
        assert cleanup_mgr.is_running()

        cleanup_mgr.stop()
        assert not cleanup_mgr.is_running()

    def test_schedule_cleanup_adds_path_to_queue(self, tmp_path):
        """
        Test that schedule_cleanup() adds path to cleanup queue.

        AC3: Old index path marked for cleanup
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        index_path = str(tmp_path / "v_1234")

        cleanup_mgr.schedule_cleanup(index_path)

        # Verify path is in queue (internal inspection for testing)
        assert index_path in cleanup_mgr._cleanup_queue

    def test_cleanup_deletes_when_ref_count_zero(self, tmp_path):
        """
        Test that cleanup deletes directory when ref count reaches zero.

        AC3: Delete triggered when ref count reaches zero
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        # Create index directory
        index_path = tmp_path / "v_1234"
        index_path.mkdir()
        (index_path / "test.txt").write_text("test")

        # Schedule cleanup (ref count is 0)
        cleanup_mgr.schedule_cleanup(str(index_path))

        # Start cleanup manager
        cleanup_mgr.start()

        # Wait for cleanup to occur
        time.sleep(0.3)

        cleanup_mgr.stop()

        # Verify directory was deleted
        assert not index_path.exists()

    def test_cleanup_waits_for_active_queries(self, tmp_path):
        """
        Test that cleanup waits while queries are active.

        AC3: Cleanup occurs after query completion
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        # Create index directory
        index_path = tmp_path / "v_1234"
        index_path.mkdir()

        # Simulate active query (increment ref count)
        tracker.increment_ref(str(index_path))

        # Schedule cleanup
        cleanup_mgr.schedule_cleanup(str(index_path))

        # Start cleanup manager
        cleanup_mgr.start()

        # Wait (cleanup should NOT happen yet)
        time.sleep(0.3)

        # Verify directory still exists (query active)
        assert index_path.exists()

        # Complete query (decrement ref count)
        tracker.decrement_ref(str(index_path))

        # Wait for cleanup
        time.sleep(0.3)

        cleanup_mgr.stop()

        # Verify directory was deleted
        assert not index_path.exists()

    def test_cleanup_handles_nonexistent_directory(self, tmp_path):
        """
        Test that cleanup handles case where directory doesn't exist.

        Error handling: Graceful handling of already-deleted paths
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        # Schedule cleanup for non-existent path
        nonexistent_path = str(tmp_path / "v_9999")
        cleanup_mgr.schedule_cleanup(nonexistent_path)

        # Start cleanup (should not crash)
        cleanup_mgr.start()
        time.sleep(0.2)
        cleanup_mgr.stop()

        # No exception raised = success

    def test_cleanup_logs_deletion(self, tmp_path, caplog):
        """
        Test that cleanup logs deletion for audit trail.

        AC3: Cleanup is logged for audit
        """
        import logging

        caplog.set_level(logging.INFO)

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        # Create index directory
        index_path = tmp_path / "v_1234"
        index_path.mkdir()

        cleanup_mgr.schedule_cleanup(str(index_path))
        cleanup_mgr.start()
        time.sleep(0.3)
        cleanup_mgr.stop()

        # Verify log contains deletion message
        assert "Deleted old index" in caplog.text
        assert str(index_path) in caplog.text

    def test_multiple_paths_cleaned_independently(self, tmp_path):
        """
        Test that multiple paths are cleaned up independently.

        Scenario: Multiple old versions scheduled for cleanup
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        # Create multiple index directories
        path1 = tmp_path / "v_1234"
        path2 = tmp_path / "v_5678"
        path3 = tmp_path / "v_9999"
        path1.mkdir()
        path2.mkdir()
        path3.mkdir()

        # Path1: no active queries (should be deleted)
        cleanup_mgr.schedule_cleanup(str(path1))

        # Path2: active query (should wait)
        tracker.increment_ref(str(path2))
        cleanup_mgr.schedule_cleanup(str(path2))

        # Path3: no active queries (should be deleted)
        cleanup_mgr.schedule_cleanup(str(path3))

        cleanup_mgr.start()
        time.sleep(0.3)

        # Verify path1 and path3 deleted, path2 still exists
        assert not path1.exists()
        assert path2.exists()
        assert not path3.exists()

        # Complete path2 query
        tracker.decrement_ref(str(path2))
        time.sleep(0.3)

        cleanup_mgr.stop()

        # Verify path2 deleted
        assert not path2.exists()

    def test_cleanup_thread_stops_gracefully(self, tmp_path):
        """
        Test that cleanup thread stops within reasonable time.

        Thread management: Clean shutdown
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        cleanup_mgr.start()
        time.sleep(0.1)

        # Stop and measure shutdown time
        start = time.time()
        cleanup_mgr.stop()
        shutdown_time = time.time() - start

        # Should stop within 1 second (generous timeout)
        assert shutdown_time < 1.0

    def test_cleanup_not_started_schedule_still_queues(self, tmp_path):
        """
        Test that schedule_cleanup() works even when manager not started.

        Pattern: Queue operations before starting background thread
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        index_path = str(tmp_path / "v_1234")

        # Schedule before starting
        cleanup_mgr.schedule_cleanup(index_path)

        # Verify queued
        assert index_path in cleanup_mgr._cleanup_queue

    def test_get_pending_cleanups_returns_queue(self, tmp_path):
        """
        Test that get_pending_cleanups() returns queued paths.

        Observability: Inspect pending cleanups
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        path1 = str(tmp_path / "v_1234")
        path2 = str(tmp_path / "v_5678")

        cleanup_mgr.schedule_cleanup(path1)
        cleanup_mgr.schedule_cleanup(path2)

        pending = cleanup_mgr.get_pending_cleanups()

        assert set(pending) == {path1, path2}

    def test_cleanup_manager_double_start_is_safe(self, tmp_path):
        """
        Test that calling start() twice is safe (no duplicate threads).

        Error handling: Idempotent start
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        cleanup_mgr.start()
        cleanup_mgr.start()  # Should be no-op

        assert cleanup_mgr.is_running()

        cleanup_mgr.stop()

    def test_cleanup_manager_double_stop_is_safe(self, tmp_path):
        """
        Test that calling stop() twice is safe.

        Error handling: Idempotent stop
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        cleanup_mgr.start()
        cleanup_mgr.stop()
        cleanup_mgr.stop()  # Should be no-op

        assert not cleanup_mgr.is_running()

    def test_cleanup_check_interval_controls_frequency(self, tmp_path):
        """
        Test that check_interval parameter is accepted and stored.

        Performance: Configurable polling rate
        """
        tracker = QueryTracker()
        # Longer interval for this test
        cleanup_mgr = CleanupManager(tracker, check_interval=1.0)

        # Verify interval is stored
        assert cleanup_mgr._check_interval == 1.0

        # Test with different interval
        cleanup_mgr2 = CleanupManager(tracker, check_interval=0.5)
        assert cleanup_mgr2._check_interval == 0.5

    def test_exponential_backoff_on_delete_failure(self, tmp_path):
        """
        Test that after a deletion failure, the path gets a next_retry_time
        that grows with each failure according to exponential backoff.

        Issue #297: Retry storm caused exponential FD exhaustion.
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        index_path = str(tmp_path / "v_backoff")

        # Record first failure at time 0 (simulated)
        cleanup_mgr._record_failure(index_path)
        delay_after_1 = cleanup_mgr._get_backoff_delay(index_path)

        # Record second failure
        cleanup_mgr._record_failure(index_path)
        delay_after_2 = cleanup_mgr._get_backoff_delay(index_path)

        # Record third failure
        cleanup_mgr._record_failure(index_path)
        delay_after_3 = cleanup_mgr._get_backoff_delay(index_path)

        # Each delay should be larger than the previous (exponential backoff)
        assert delay_after_1 > 0
        assert delay_after_2 > delay_after_1
        assert delay_after_3 > delay_after_2

    def test_circuit_breaker_removes_path_after_max_failures(self, tmp_path, caplog):
        """
        Test that after MAX_FAILURES consecutive failures, path is removed
        from queue and a CRITICAL message is logged.

        Issue #297: Prevent infinite retry loops for permanently stuck paths.
        """
        import logging

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=10.0)

        # Create index directory
        index_path = tmp_path / "v_circuit"
        index_path.mkdir()

        cleanup_mgr.schedule_cleanup(str(index_path))

        caplog.set_level(logging.CRITICAL)

        # Simulate MAX_FAILURES consecutive failures
        for _ in range(CleanupManager.MAX_FAILURES):
            cleanup_mgr._record_failure(str(index_path))

        # Process cleanup: circuit breaker should trigger
        cleanup_mgr._process_cleanup_queue()

        # Path should have been removed from queue (circuit breaker)
        assert str(index_path) not in cleanup_mgr._cleanup_queue

        # CRITICAL log should have been emitted
        assert any(
            r.levelname == "CRITICAL" for r in caplog.records
        ), "Expected CRITICAL log when circuit breaker trips"

    def test_circuit_breaker_resets_on_success(self, tmp_path):
        """
        Test that failure counts are per-path and do not affect other paths.

        Issue #297: Ensure counter is per-path, not global.
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=10.0)

        path_a = str(tmp_path / "v_path_a")
        path_b = str(tmp_path / "v_path_b")

        # Record failures for path_a
        for _ in range(3):
            cleanup_mgr._record_failure(path_a)

        # path_b should have zero failures
        assert cleanup_mgr._get_failure_count(path_b) == 0

        # path_a should have 3 failures
        assert cleanup_mgr._get_failure_count(path_a) == 3

        # After successful deletion of path_a, its failure count should reset
        cleanup_mgr._reset_failure_count(path_a)
        assert cleanup_mgr._get_failure_count(path_a) == 0

    def test_fd_check_skips_cleanup_when_fd_usage_high(self, tmp_path, caplog):
        """
        Test that cleanup cycle is skipped when FD usage exceeds 80% threshold.

        Issue #297: Prevent making FD exhaustion worse during cleanup.
        """
        import logging
        from unittest.mock import patch

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=10.0)

        # Create index directory and schedule cleanup
        index_path = tmp_path / "v_fd_test"
        index_path.mkdir()
        cleanup_mgr.schedule_cleanup(str(index_path))

        caplog.set_level(logging.WARNING)

        # Mock FD check to report high usage (above 80%)
        with patch.object(cleanup_mgr, "_is_fd_usage_high", return_value=True):
            cleanup_mgr._process_cleanup_queue()

        # Directory should still exist (cleanup was skipped)
        assert index_path.exists()

        # Warning should have been logged
        assert any("fd" in r.message.lower() or "file descriptor" in r.message.lower()
                   for r in caplog.records if r.levelno >= logging.WARNING)

    def test_robust_deletion_handles_oserror_gracefully(self, tmp_path):
        """
        Test that OSError during deletion does not crash the cleanup loop
        and triggers backoff tracking.

        Issue #297: Deletion errors should be handled without crashing.
        Note: OSError("Too many open files") does NOT set errno=EMFILE (errno is None),
        so this test exercises general OSError handling / backoff, NOT the bottom-up
        fallback path.
        """
        from unittest.mock import patch

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=10.0)

        # Create index directory
        index_path = tmp_path / "v_oserror"
        index_path.mkdir()
        cleanup_mgr.schedule_cleanup(str(index_path))

        # Make rmtree raise OSError (errno=None, not EMFILE)
        with patch("code_indexer.global_repos.cleanup_manager.shutil.rmtree",
                   side_effect=OSError("Too many open files")):
            # Should not raise
            cleanup_mgr._process_cleanup_queue()

        # Path should still be in queue (for retry)
        assert str(index_path) in cleanup_mgr._cleanup_queue

        # Failure count should have been incremented
        assert cleanup_mgr._get_failure_count(str(index_path)) >= 1

    def test_robust_deletion_emfile_triggers_bottom_up_fallback(self, tmp_path):
        """
        Test that when shutil.rmtree raises OSError with errno=EMFILE, the
        bottom-up os.walk fallback is triggered and successfully deletes the directory.

        Issue #297: EMFILE-specific fallback path must be exercised.
        """
        import errno
        from unittest.mock import patch

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=10.0)

        # Create a real directory with files so bottom-up deletion has something to do
        index_path = tmp_path / "v_emfile_fallback"
        index_path.mkdir()
        (index_path / "vectors.json").write_text("data")
        (index_path / "metadata.json").write_text("meta")
        sub_dir = index_path / "subdir"
        sub_dir.mkdir()
        (sub_dir / "chunk.json").write_text("chunk")

        cleanup_mgr.schedule_cleanup(str(index_path))

        # Make shutil.rmtree raise OSError with errno=EMFILE to trigger bottom-up fallback
        with patch("code_indexer.global_repos.cleanup_manager.shutil.rmtree",
                   side_effect=OSError(errno.EMFILE, "Too many open files")):
            # Should not raise - bottom-up fallback handles the actual deletion
            cleanup_mgr._process_cleanup_queue()

        # Directory should be gone (bottom-up fallback actually deleted it)
        assert not index_path.exists()

        # Path should be removed from cleanup queue after successful deletion
        assert str(index_path) not in cleanup_mgr._cleanup_queue

    def test_backoff_caps_at_max_delay(self, tmp_path):
        """
        Test that exponential backoff caps at MAX_BACKOFF_DELAY (60 seconds).

        Issue #297: Prevent excessively long delays.
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        index_path = str(tmp_path / "v_maxbackoff")

        # Simulate many failures to exceed the cap
        for _ in range(20):
            cleanup_mgr._record_failure(index_path)

        delay = cleanup_mgr._get_backoff_delay(index_path)

        # Delay should never exceed MAX_BACKOFF_DELAY
        assert delay <= CleanupManager.MAX_BACKOFF_DELAY
        assert delay == CleanupManager.MAX_BACKOFF_DELAY

    def test_cleanup_stats_tracking(self, tmp_path):
        """
        Test that failure counts are tracked per-path correctly.

        Issue #297: Per-path failure tracking is required for backoff and
        circuit breaker.
        """
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker, check_interval=0.1)

        path_x = str(tmp_path / "v_stats_x")
        path_y = str(tmp_path / "v_stats_y")

        # Initially no failures
        assert cleanup_mgr._get_failure_count(path_x) == 0
        assert cleanup_mgr._get_failure_count(path_y) == 0

        # Record failures independently
        cleanup_mgr._record_failure(path_x)
        cleanup_mgr._record_failure(path_x)
        cleanup_mgr._record_failure(path_y)

        assert cleanup_mgr._get_failure_count(path_x) == 2
        assert cleanup_mgr._get_failure_count(path_y) == 1

        # Reset path_x
        cleanup_mgr._reset_failure_count(path_x)
        assert cleanup_mgr._get_failure_count(path_x) == 0
        # path_y unchanged
        assert cleanup_mgr._get_failure_count(path_y) == 1
