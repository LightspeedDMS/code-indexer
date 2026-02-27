"""
AC1: DependencyMapService accepts Optional[JobTracker] parameter.

Story 2 (#312) - Epic #261 Unified Job Tracking Subsystem.
"""

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService

from .conftest import make_service


class TestDependencyMapServiceConstructor:
    """AC1: DependencyMapService accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        DependencyMapService can be constructed without a job_tracker.

        Given no job_tracker is provided
        When DependencyMapService is instantiated
        Then no exception is raised
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )
        assert service is not None

    def test_accepts_job_tracker_instance(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        DependencyMapService stores the job_tracker when provided.

        Given a real JobTracker instance
        When DependencyMapService is instantiated with job_tracker=tracker
        Then _job_tracker attribute is set to the provided instance
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
            job_tracker=job_tracker,
        )
        assert service._job_tracker is job_tracker

    def test_job_tracker_defaults_to_none(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        _job_tracker is None when not provided.

        Given no job_tracker keyword argument
        When DependencyMapService is instantiated
        Then _job_tracker is None
        """
        service = DependencyMapService(
            golden_repos_manager=mock_golden_repos_manager,
            config_manager=mock_config_manager,
            tracking_backend=mock_tracking_backend,
            analyzer=mock_analyzer,
        )
        assert service._job_tracker is None

    def test_existing_parameters_still_work(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        Existing constructor parameters continue to function alongside job_tracker.

        Given all constructor parameters are provided
        When DependencyMapService is instantiated
        Then all attributes are set correctly
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        assert service._golden_repos_manager is mock_golden_repos_manager
        assert service._config_manager is mock_config_manager
        assert service._tracking_backend is mock_tracking_backend
        assert service._analyzer is mock_analyzer
        assert service._job_tracker is job_tracker
