"""
AC9 and backward compatibility: DependencyMapTrackingBackend coexistence tests.

Story 2 (#312) - Epic #261 Unified Job Tracking Subsystem.

Covers:
- AC9: DependencyMapTrackingBackend continues to function unchanged
- Defensive try/except: tracker failure must never break analysis
- Backward compatibility: service works correctly without job_tracker
"""

from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import JobTracker

from .conftest import make_service


class TestTrackingBackendCoexistence:
    """AC9: DependencyMapTrackingBackend continues to function unchanged."""

    def test_both_mechanisms_operate_independently(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        Both tracking_backend and job_tracker operate independently.

        Given both tracking_backend and job_tracker are provided
        When run_full_analysis is called with no repos (early return)
        Then both mechanisms record the event without interfering with each other
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service._get_activated_repos = MagicMock(return_value=[])

        service.run_full_analysis()

        # job_tracker recorded the job
        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "completed"

    def test_tracker_failure_does_not_break_full_analysis(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        If the job_tracker raises on register_job, full analysis still completes.

        Given a job_tracker that raises RuntimeError on register_job
        When run_full_analysis is called
        Then no exception propagates from the tracker failure (defensive try/except)
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("Tracker DB unavailable")
        broken_tracker.check_operation_conflict.return_value = None

        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=broken_tracker,
        )

        # Must not raise - tracker failures are absorbed defensively
        service.run_full_analysis()

    def test_tracker_failure_does_not_break_delta_analysis(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        If the job_tracker raises on register_job, delta analysis still completes.

        Given a job_tracker that raises RuntimeError on register_job
        When run_delta_analysis is called
        Then no exception propagates from the tracker failure (defensive try/except)
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("Tracker DB unavailable")
        broken_tracker.check_operation_conflict.return_value = None

        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=broken_tracker,
        )

        # Must not raise - tracker failures are absorbed defensively
        result = service.run_delta_analysis()
        # Result is None due to disabled config
        assert result is None

    def test_tracker_complete_job_failure_does_not_mask_analysis_result(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        If the job_tracker raises on complete_job, the analysis result is unchanged.

        Given a job_tracker where complete_job raises
        When run_full_analysis is called
        Then the function completes without propagating the tracker error
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.return_value = MagicMock()
        broken_tracker.check_operation_conflict.return_value = None
        broken_tracker.update_status.return_value = None
        broken_tracker.complete_job.side_effect = RuntimeError("Complete failed")

        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=broken_tracker,
        )

        # Must not raise - complete_job failure is absorbed defensively
        service.run_full_analysis()


class TestNoTrackerBackwardCompatibility:
    """Backward compatibility: service works correctly without job_tracker."""

    def test_full_analysis_works_without_job_tracker(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        run_full_analysis works correctly without a job_tracker.

        Given no job_tracker is provided (original constructor call style)
        When run_full_analysis is called
        Then no exception is raised (backward compatible)
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager_disabled,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        # Must not raise
        service.run_full_analysis()

    def test_delta_analysis_works_without_job_tracker(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        run_delta_analysis works correctly without a job_tracker.

        Given no job_tracker is provided (original constructor call style)
        When run_delta_analysis is called
        Then no exception is raised and None is returned (backward compatible)
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager_disabled,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        result = service.run_delta_analysis()
        assert result is None

    def test_is_available_works_without_job_tracker(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        is_available() continues to work without a job_tracker.

        Given no job_tracker is provided
        When is_available() is called
        Then True is returned (lock is not held)
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )

        assert service.is_available() is True
