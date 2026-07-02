"""
Unit tests for TrackedOperation context manager.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC7: TrackedOperation context manager
"""

import pytest

from code_indexer.server.services.job_tracker import TrackedOperation


class TestTrackedOperation:
    """Tests for TrackedOperation context manager (AC7)."""

    def test_context_manager_registers_and_starts(self, tracker):
        """
        On entry, TrackedOperation registers the job and transitions it to 'running'.

        Given a TrackedOperation context
        When the with block is entered
        Then the job is in memory with status='running'
        """
        with TrackedOperation(tracker, "op-cm-001", "dep_map_analysis", "admin") as job:
            assert job is not None
            assert job.job_id == "op-cm-001"

            active = tracker.get_active_jobs()
            assert any(j.job_id == "op-cm-001" for j in active)

            in_block_job = tracker.get_job("op-cm-001")
            assert in_block_job is not None
            assert in_block_job.status == "running"

    def test_context_manager_completes_on_success(self, tracker):
        """
        On clean exit, TrackedOperation marks the job as 'completed'.

        Given a TrackedOperation that exits normally
        When the with block ends without exception
        Then the job status is 'completed'
        """
        with TrackedOperation(tracker, "op-cm-002", "dep_map_analysis", "admin"):
            pass  # clean exit

        job = tracker.get_job("op-cm-002")
        assert job is not None
        assert job.status == "completed"

    def test_context_manager_fails_on_exception(self, tracker):
        """
        On exception, TrackedOperation marks the job as 'failed'.

        Given a TrackedOperation whose with block raises
        When the exception occurs
        Then the job status is 'failed'
        """
        with pytest.raises(ValueError):
            with TrackedOperation(tracker, "op-cm-003", "dep_map_analysis", "admin"):
                raise ValueError("test error")

        job = tracker.get_job("op-cm-003")
        assert job is not None
        assert job.status == "failed"

    def test_context_manager_does_not_suppress_exception(self, tracker):
        """
        TrackedOperation does not suppress exceptions raised inside the with block.

        Given a with block that raises RuntimeError
        When the block exits
        Then the RuntimeError propagates to the caller
        """
        with pytest.raises(RuntimeError, match="propagated"):
            with TrackedOperation(tracker, "op-cm-004", "dep_map_analysis", "admin"):
                raise RuntimeError("propagated")

    def test_context_manager_with_metadata(self, tracker):
        """
        TrackedOperation passes metadata to register_job.

        Given metadata is provided to TrackedOperation
        When the job is registered
        Then the job has the correct metadata
        """
        meta = {"repo_count": 5, "trigger": "manual"}
        with TrackedOperation(
            tracker, "op-cm-005", "dep_map_analysis", "admin", metadata=meta
        ) as job:
            assert job.metadata == meta
