"""
Unit tests for JobTracker.check_operation_conflict and DuplicateJobError.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC5: check_operation_conflict
"""

import pytest

from code_indexer.server.services.job_tracker import DuplicateJobError


class TestCheckOperationConflict:
    """Tests for check_operation_conflict (AC5)."""

    def test_conflict_raises_for_duplicate_active(self, tracker):
        """
        check_operation_conflict raises DuplicateJobError when an active job
        with the same (operation_type, repo_alias) exists.

        Given an active pending job for 'dep_map_analysis' on 'repo-a'
        When check_operation_conflict is called for the same pair
        Then DuplicateJobError is raised
        """
        tracker.register_job(
            "job-dup-001", "dep_map_analysis", "admin", repo_alias="repo-a"
        )

        with pytest.raises(DuplicateJobError):
            tracker.check_operation_conflict("dep_map_analysis", repo_alias="repo-a")

    def test_conflict_allows_different_operation_type(self, tracker):
        """
        check_operation_conflict does not raise for a different operation_type.

        Given an active job for 'dep_map_analysis' on 'repo-a'
        When check_operation_conflict is called for 'description_refresh' on 'repo-a'
        Then no exception is raised
        """
        tracker.register_job(
            "job-diffOp-001", "dep_map_analysis", "admin", repo_alias="repo-a"
        )

        # Should not raise
        tracker.check_operation_conflict("description_refresh", repo_alias="repo-a")

    def test_conflict_allows_same_op_different_repo(self, tracker):
        """
        check_operation_conflict does not raise for the same op on a different repo.

        Given an active job for 'dep_map_analysis' on 'repo-a'
        When check_operation_conflict is called for 'dep_map_analysis' on 'repo-b'
        Then no exception is raised
        """
        tracker.register_job(
            "job-diffRepo-001", "dep_map_analysis", "admin", repo_alias="repo-a"
        )

        # Should not raise
        tracker.check_operation_conflict("dep_map_analysis", repo_alias="repo-b")

    def test_conflict_allows_after_completion(self, tracker):
        """
        check_operation_conflict does not raise after the conflicting job completes.

        Given a job that was active but has now been completed
        When check_operation_conflict is called for the same pair
        Then no exception is raised
        """
        tracker.register_job(
            "job-afterComp-001", "dep_map_analysis", "admin", repo_alias="repo-a"
        )
        tracker.complete_job("job-afterComp-001")

        # Should not raise after completion
        tracker.check_operation_conflict("dep_map_analysis", repo_alias="repo-a")

    def test_conflict_carries_job_info(self, tracker):
        """
        DuplicateJobError carries operation_type, repo_alias, and existing_job_id.

        Given an active job
        When check_operation_conflict raises DuplicateJobError
        Then the error fields match the original job
        """
        tracker.register_job(
            "job-errInfo-001", "dep_map_analysis", "admin", repo_alias="repo-x"
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker.check_operation_conflict("dep_map_analysis", repo_alias="repo-x")

        err = exc_info.value
        assert err.operation_type == "dep_map_analysis"
        assert err.repo_alias == "repo-x"
        assert err.existing_job_id == "job-errInfo-001"
