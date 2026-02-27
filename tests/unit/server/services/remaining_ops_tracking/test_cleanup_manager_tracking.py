"""
AC10: CleanupManager job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC10: CleanupManager accepts Optional[JobTracker] parameter
- AC10: _process_cleanup_queue() registers index_cleanup operation type
- AC10: Successful cleanup transitions to completed
- AC10: Failed cleanup transitions to failed with error
- AC10: Tracker=None doesn't break cleanup operations
- AC10: Tracker raising exceptions doesn't break cleanup

Fixture `job_tracker` is provided by conftest.py in this directory.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cleanup_manager(job_tracker=None):
    """Create a CleanupManager with a real QueryTracker and optional job_tracker."""
    query_tracker = QueryTracker()
    return CleanupManager(
        query_tracker=query_tracker,
        check_interval=0.05,  # Fast interval for tests
        job_tracker=job_tracker,
    )


# ---------------------------------------------------------------------------
# AC10: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestCleanupManagerConstructor:
    """AC10: CleanupManager accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self):
        """
        CleanupManager can be constructed without a job_tracker.

        Given no job_tracker is provided
        When CleanupManager is instantiated
        Then no exception is raised and _job_tracker is None
        """
        manager = _make_cleanup_manager(job_tracker=None)
        assert manager is not None
        assert manager._job_tracker is None

    def test_accepts_job_tracker_instance(self, job_tracker):
        """
        CleanupManager stores the job_tracker.

        Given a real JobTracker instance
        When CleanupManager is instantiated with it
        Then _job_tracker is set
        """
        manager = _make_cleanup_manager(job_tracker=job_tracker)
        assert manager._job_tracker is job_tracker

    def test_backward_compatible_without_job_tracker(self):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When CleanupManager is instantiated
        Then no TypeError is raised
        """
        query_tracker = QueryTracker()
        manager = CleanupManager(query_tracker=query_tracker)
        assert manager is not None


# ---------------------------------------------------------------------------
# AC10: index_cleanup job registered during _process_cleanup_queue
# ---------------------------------------------------------------------------


class TestCleanupManagerJobRegistration:
    """AC10: index_cleanup operation type is registered during cleanup."""

    def test_registers_index_cleanup_job_when_path_deleted(self, tmp_path, job_tracker):
        """
        _process_cleanup_queue() registers an index_cleanup job when deleting a path.

        Given a CleanupManager with job_tracker
        And an old index path scheduled for cleanup with zero ref count
        When _process_cleanup_queue() runs
        Then an index_cleanup job exists in the tracker
        """
        manager = _make_cleanup_manager(job_tracker=job_tracker)

        # Create a real directory to delete
        old_index = tmp_path / "old-index-v1"
        old_index.mkdir()

        # Schedule for cleanup - ref count is 0 (no active queries)
        manager.schedule_cleanup(str(old_index))

        # Run cleanup directly
        manager._process_cleanup_queue()

        jobs = job_tracker.query_jobs(operation_type="index_cleanup")
        assert len(jobs) >= 1

    def test_index_cleanup_job_completes_on_successful_delete(self, tmp_path, job_tracker):
        """
        index_cleanup job transitions to completed when directory is deleted.

        Given a CleanupManager with job_tracker
        And an old index path scheduled for cleanup
        When _process_cleanup_queue() successfully deletes the path
        Then the index_cleanup job has completed status
        """
        manager = _make_cleanup_manager(job_tracker=job_tracker)

        old_index = tmp_path / "old-index-v2"
        old_index.mkdir()

        manager.schedule_cleanup(str(old_index))
        manager._process_cleanup_queue()

        jobs = job_tracker.query_jobs(operation_type="index_cleanup", status="completed")
        assert len(jobs) >= 1

    def test_index_cleanup_job_fails_when_delete_raises(self, tmp_path, job_tracker):
        """
        index_cleanup job transitions to failed when deletion raises an exception.

        Given a CleanupManager with job_tracker
        And a path scheduled for cleanup that fails to delete
        When _process_cleanup_queue() runs and _delete_index raises
        Then an index_cleanup job exists with failed status
        """
        manager = _make_cleanup_manager(job_tracker=job_tracker)

        old_index = tmp_path / "fail-to-delete"
        old_index.mkdir()

        manager.schedule_cleanup(str(old_index))

        # Patch _delete_index to raise so we can test failure tracking
        with patch.object(manager, "_delete_index", side_effect=OSError("Permission denied")):
            manager._process_cleanup_queue()

        failed_jobs = job_tracker.query_jobs(operation_type="index_cleanup", status="failed")
        assert len(failed_jobs) >= 1

    def test_no_job_tracker_does_not_break_cleanup(self, tmp_path):
        """
        When job_tracker is None, _process_cleanup_queue proceeds normally.

        Given a CleanupManager WITHOUT job_tracker
        When _process_cleanup_queue() is called
        Then no exception is raised and cleanup still occurs
        """
        manager = _make_cleanup_manager(job_tracker=None)

        old_index = tmp_path / "old-index-v3"
        old_index.mkdir()

        manager.schedule_cleanup(str(old_index))
        manager._process_cleanup_queue()  # Must not raise

        # Path should have been deleted (normal cleanup behavior)
        assert not old_index.exists()

    def test_tracker_exception_does_not_break_cleanup(self, tmp_path):
        """
        When job_tracker raises on register_job, cleanup still proceeds.

        Given a job_tracker that raises RuntimeError on register_job
        When _process_cleanup_queue() is called
        Then no exception propagates and cleanup still occurs
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")
        manager = _make_cleanup_manager(job_tracker=broken_tracker)

        old_index = tmp_path / "old-index-v4"
        old_index.mkdir()

        manager.schedule_cleanup(str(old_index))
        manager._process_cleanup_queue()  # Must not raise

        # Despite tracker failure, cleanup must still run
        assert not old_index.exists()

    def test_no_job_registered_when_nothing_to_cleanup(self, job_tracker):
        """
        No index_cleanup job registered when cleanup queue is empty.

        Given a CleanupManager with job_tracker
        When _process_cleanup_queue() is called with empty queue
        Then no index_cleanup job is registered
        """
        manager = _make_cleanup_manager(job_tracker=job_tracker)

        # No paths scheduled - queue is empty
        manager._process_cleanup_queue()

        jobs = job_tracker.query_jobs(operation_type="index_cleanup")
        assert len(jobs) == 0

    def test_no_job_registered_when_path_has_active_queries(self, tmp_path, job_tracker):
        """
        No index_cleanup job registered when path has active queries (ref count > 0).

        Given a CleanupManager with job_tracker
        And a path with non-zero ref count (active query)
        When _process_cleanup_queue() is called
        Then no index_cleanup job is registered (path not deleted)
        """
        query_tracker = QueryTracker()
        manager = CleanupManager(
            query_tracker=query_tracker,
            check_interval=0.05,
            job_tracker=job_tracker,
        )

        old_index = tmp_path / "active-index"
        old_index.mkdir()

        # Increment ref count - simulates active query (ref count is now 1)
        query_tracker.increment_ref(str(old_index))

        manager.schedule_cleanup(str(old_index))
        manager._process_cleanup_queue()

        # Path should NOT be deleted (ref count > 0)
        assert old_index.exists()

        # No cleanup job should be registered (no cleanup occurred)
        jobs = job_tracker.query_jobs(operation_type="index_cleanup")
        assert len(jobs) == 0

        # Decrement the reference
        query_tracker.decrement_ref(str(old_index))
