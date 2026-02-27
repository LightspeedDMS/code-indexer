"""
AC6: Conflict detection tests.

Story 2 (#312) - Epic #261 Unified Job Tracking Subsystem.

Covers:
- AC6: Conflict detection prevents concurrent dependency map runs
  via check_operation_conflict()
"""

import pytest

from code_indexer.server.services.job_tracker import DuplicateJobError

from .conftest import make_service


class TestConflictDetection:
    """AC6: Conflict detection prevents concurrent dependency map runs."""

    def test_job_tracker_raises_on_full_analysis_conflict(
        self,
        job_tracker,
    ):
        """
        check_operation_conflict raises DuplicateJobError when full analysis is running.

        Given a 'dependency_map_full' job is active in the tracker (status=running)
        When check_operation_conflict('dependency_map_full') is called
        Then DuplicateJobError is raised with the conflicting job_id
        """
        job_tracker.register_job("existing-full-job", "dependency_map_full", "system")
        job_tracker.update_status("existing-full-job", status="running")

        with pytest.raises(DuplicateJobError) as exc_info:
            job_tracker.check_operation_conflict("dependency_map_full")

        assert exc_info.value.existing_job_id == "existing-full-job"

    def test_job_tracker_raises_on_delta_analysis_conflict(
        self,
        job_tracker,
    ):
        """
        check_operation_conflict raises DuplicateJobError when delta analysis is running.

        Given a 'dependency_map_delta' job is active in the tracker (status=running)
        When check_operation_conflict('dependency_map_delta') is called
        Then DuplicateJobError is raised with the conflicting job_id
        """
        job_tracker.register_job(
            "existing-delta-job", "dependency_map_delta", "system"
        )
        job_tracker.update_status("existing-delta-job", status="running")

        with pytest.raises(DuplicateJobError) as exc_info:
            job_tracker.check_operation_conflict("dependency_map_delta")

        assert exc_info.value.existing_job_id == "existing-delta-job"

    def test_job_tracker_raises_on_pending_full_analysis_conflict(
        self,
        job_tracker,
    ):
        """
        check_operation_conflict raises DuplicateJobError when full analysis is pending.

        Given a 'dependency_map_full' job is in 'pending' state
        When check_operation_conflict('dependency_map_full') is called
        Then DuplicateJobError is raised (pending counts as conflicting)
        """
        job_tracker.register_job("pending-full-job", "dependency_map_full", "system")
        # Status is 'pending' by default after register_job

        with pytest.raises(DuplicateJobError):
            job_tracker.check_operation_conflict("dependency_map_full")

    def test_no_conflict_when_no_active_jobs(
        self,
        job_tracker,
    ):
        """
        check_operation_conflict does not raise when no active jobs exist.

        Given no active or pending dependency_map_full jobs
        When check_operation_conflict('dependency_map_full') is called
        Then no exception is raised
        """
        job_tracker.check_operation_conflict("dependency_map_full")  # No exception

    def test_no_conflict_after_job_completed(
        self,
        job_tracker,
    ):
        """
        check_operation_conflict does not raise when prior job is completed.

        Given a completed 'dependency_map_full' job
        When check_operation_conflict('dependency_map_full') is called
        Then no exception is raised (completed jobs do not block new runs)
        """
        job_tracker.register_job("old-full-job", "dependency_map_full", "system")
        job_tracker.update_status("old-full-job", status="running")
        job_tracker.complete_job("old-full-job")

        job_tracker.check_operation_conflict("dependency_map_full")  # No exception

    def test_full_analysis_raises_duplicate_job_error_on_conflict(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis raises DuplicateJobError when a concurrent full job is active.

        Given a 'dependency_map_full' job is already running in the tracker
        When run_full_analysis is called again
        Then DuplicateJobError is raised before analysis starts
        """
        job_tracker.register_job("active-full-001", "dependency_map_full", "system")
        job_tracker.update_status("active-full-001", status="running")

        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        with pytest.raises(DuplicateJobError):
            service.run_full_analysis()

    def test_delta_analysis_raises_duplicate_job_error_on_conflict(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis raises DuplicateJobError when a concurrent delta job is active.

        Given a 'dependency_map_delta' job is already running in the tracker
        When run_delta_analysis is called again
        Then DuplicateJobError is raised before analysis starts
        """
        job_tracker.register_job("active-delta-001", "dependency_map_delta", "system")
        job_tracker.update_status("active-delta-001", status="running")

        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        with pytest.raises(DuplicateJobError):
            service.run_delta_analysis()

    def test_full_conflict_does_not_interfere_with_delta_at_tracker_level(
        self,
        job_tracker,
    ):
        """
        An active 'dependency_map_full' job does not block delta conflict check.

        Given a running 'dependency_map_full' job
        When check_operation_conflict('dependency_map_delta') is called
        Then no exception is raised (different operation types do not conflict)
        """
        job_tracker.register_job("active-full-002", "dependency_map_full", "system")
        job_tracker.update_status("active-full-002", status="running")

        # Delta operation type has no conflict with full operation type
        job_tracker.check_operation_conflict("dependency_map_delta")  # No exception
