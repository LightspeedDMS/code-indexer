"""
Unit tests for TrackedJob dataclass and DuplicateJobError exception.

Story #310: JobTracker Class, TrackedJob Dataclass, and Schema Migration (Epic #261 Story 1A)

Covers AC2 (TrackedJob) and AC4 (DuplicateJobError).
"""

from datetime import datetime, timezone

import pytest


class TestTrackedJob:
    """Tests for TrackedJob dataclass (AC2)."""

    def test_tracked_job_has_required_fields(self):
        """
        TrackedJob dataclass has all required fields with correct defaults.

        Given the TrackedJob dataclass is imported
        When created with minimal required fields
        Then optional fields default to None/0
        """
        from code_indexer.server.services.job_tracker import TrackedJob

        job = TrackedJob(
            job_id="test-001",
            operation_type="dep_map_analysis",
            status="pending",
            username="admin",
        )

        assert job.job_id == "test-001"
        assert job.operation_type == "dep_map_analysis"
        assert job.status == "pending"
        assert job.username == "admin"
        assert job.repo_alias is None
        assert job.progress == 0
        assert job.progress_info is None
        assert job.metadata is None
        assert job.started_at is None
        assert job.completed_at is None
        assert job.error is None
        assert job.result is None

    def test_tracked_job_created_at_auto_set(self):
        """
        TrackedJob.created_at is set automatically if not provided.

        Given created_at is not provided
        When TrackedJob is created
        Then created_at should be a datetime in UTC
        """
        from code_indexer.server.services.job_tracker import TrackedJob

        before = datetime.now(timezone.utc)
        job = TrackedJob(
            job_id="test-002",
            operation_type="test_op",
            status="pending",
            username="admin",
        )
        after = datetime.now(timezone.utc)

        assert job.created_at is not None
        assert isinstance(job.created_at, datetime)
        assert before <= job.created_at <= after

    def test_tracked_job_full_construction(self):
        """
        TrackedJob can be constructed with all fields explicitly.

        Given all fields provided
        When TrackedJob is created
        Then all fields match provided values
        """
        from code_indexer.server.services.job_tracker import TrackedJob

        now = datetime.now(timezone.utc)
        job = TrackedJob(
            job_id="test-003",
            operation_type="description_refresh",
            status="running",
            username="user1",
            repo_alias="my-repo",
            progress=50,
            progress_info="Pass 2/3: Processing files",
            metadata={"key": "value"},
            created_at=now,
            started_at=now,
            completed_at=None,
            error=None,
            result=None,
        )

        assert job.job_id == "test-003"
        assert job.repo_alias == "my-repo"
        assert job.progress == 50
        assert job.progress_info == "Pass 2/3: Processing files"
        assert job.metadata == {"key": "value"}
        assert job.started_at == now


class TestDuplicateJobError:
    """Tests for DuplicateJobError exception (AC4)."""

    def test_duplicate_job_error_has_fields(self):
        """
        DuplicateJobError carries operation_type, repo_alias, and existing_job_id.
        """
        from code_indexer.server.services.job_tracker import DuplicateJobError

        err = DuplicateJobError(
            operation_type="dep_map_analysis",
            repo_alias="my-repo",
            existing_job_id="job-existing",
        )

        assert err.operation_type == "dep_map_analysis"
        assert err.repo_alias == "my-repo"
        assert err.existing_job_id == "job-existing"

    def test_duplicate_job_error_is_exception(self):
        """
        DuplicateJobError is a proper Exception subclass.
        """
        from code_indexer.server.services.job_tracker import DuplicateJobError

        with pytest.raises(DuplicateJobError) as exc_info:
            raise DuplicateJobError("op", "repo", "job-id")

        assert "op" in str(exc_info.value)
        assert "repo" in str(exc_info.value)

    def test_duplicate_job_error_message_contains_info(self):
        """
        DuplicateJobError message contains operation_type, repo_alias, and job_id.
        """
        from code_indexer.server.services.job_tracker import DuplicateJobError

        err = DuplicateJobError(
            operation_type="refresh_golden_repo",
            repo_alias="cidx-repo",
            existing_job_id="abc-123",
        )

        msg = str(err)
        assert "refresh_golden_repo" in msg
        assert "cidx-repo" in msg
        assert "abc-123" in msg
