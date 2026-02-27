"""
AC2, AC4, AC5, AC7, AC8: Full analysis job tracking tests.

Story 2 (#312) - Epic #261 Unified Job Tracking Subsystem.

Covers:
- AC2: Full analysis registers as 'dependency_map_full' operation type
- AC4: Entry points can pass job_id to run_full_analysis
- AC5: Progress updates (status transitions during analysis)
- AC7: Status transitions pending -> running -> completed/failed
- AC8: Failed analyses report error details via fail_job()
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from .conftest import make_service


class TestFullAnalysisJobRegistration:
    """AC2: Full analysis registers as 'dependency_map_full' operation type."""

    def test_full_analysis_registers_dependency_map_full_operation_type(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis registers a job with operation_type='dependency_map_full'.

        Given a DependencyMapService with job_tracker and disabled config
        When run_full_analysis is called (early return path)
        Then a job of type 'dependency_map_full' is visible in the tracker
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1

    def test_full_analysis_job_uses_system_username(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis registers the job with username='system'.

        Given job_tracker is provided
        When run_full_analysis is called
        Then the registered job has username='system'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert dep_map_jobs[0]["username"] == "system"

    def test_full_analysis_generates_nonempty_job_id(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis generates a non-empty job_id when none is provided.

        Given no job_id argument
        When run_full_analysis is called
        Then the registered job has a non-empty job_id
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["job_id"]  # non-empty string


class TestFullAnalysisStatusTransitions:
    """AC7: Status transitions pending -> running -> completed/failed."""

    def test_full_analysis_completes_on_disabled_config(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis job ends as 'completed' when analysis is disabled.

        Given dependency map is disabled (early return path)
        When run_full_analysis is called
        Then the tracked job reaches terminal state 'completed'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "completed"

    def test_full_analysis_completes_on_empty_repo_list(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis job completes when there are no repos (early return).

        Given config is enabled but no activated repos exist
        When run_full_analysis is called
        Then job reaches 'completed' status
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

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "completed"


class TestFullAnalysisEntryPointJobId:
    """AC4: Entry points can pass job_id to run_full_analysis."""

    def test_run_full_analysis_accepts_custom_job_id(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis accepts an optional job_id parameter.

        Given a caller-provided job_id
        When run_full_analysis(job_id='custom-id') is called
        Then the tracker registers a job with that exact job_id
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        custom_id = "dep-map-full-abc-123"

        service.run_full_analysis(job_id=custom_id)

        job = job_tracker.get_job(custom_id)
        assert job is not None
        assert job.operation_type == "dependency_map_full"

    def test_run_full_analysis_custom_job_id_is_completed(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis with custom job_id ends in 'completed' state.

        Given a caller-provided job_id and disabled config
        When run_full_analysis completes
        Then the job with the custom id reaches 'completed' status
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        custom_id = "dep-map-full-xyz-789"

        service.run_full_analysis(job_id=custom_id)

        job = job_tracker.get_job(custom_id)
        assert job is not None
        assert job.status == "completed"


class TestFullAnalysisFailureReporting:
    """AC8: Failed analyses report error details via fail_job()."""

    def test_full_analysis_marks_job_failed_on_exception(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis marks job as 'failed' when an exception occurs.

        Given _execute_analysis_passes raises an exception
        When run_full_analysis propagates the exception
        Then the tracked job status is 'failed'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service._get_activated_repos = MagicMock(
            return_value=[{"alias": "repo-1", "clone_path": "/fake"}]
        )
        service._enrich_repo_sizes = MagicMock(
            return_value=[
                {
                    "alias": "repo-1",
                    "clone_path": "/fake",
                    "file_count": 1,
                    "total_bytes": 100,
                }
            ]
        )
        service._execute_analysis_passes = MagicMock(
            side_effect=RuntimeError("Synthesis failed")
        )

        with pytest.raises(RuntimeError, match="Synthesis failed"):
            service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "failed"

    def test_full_analysis_stores_error_message_on_failure(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_full_analysis stores the exception message in job error field.

        Given analysis raises RuntimeError('Synthesis failed')
        When run_full_analysis propagates the exception
        Then job error contains the exception message
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service._get_activated_repos = MagicMock(
            return_value=[{"alias": "repo-1", "clone_path": "/fake"}]
        )
        service._enrich_repo_sizes = MagicMock(
            return_value=[
                {
                    "alias": "repo-1",
                    "clone_path": "/fake",
                    "file_count": 1,
                    "total_bytes": 100,
                }
            ]
        )
        service._execute_analysis_passes = MagicMock(
            side_effect=RuntimeError("Synthesis failed")
        )

        with pytest.raises(RuntimeError):
            service.run_full_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_full"]
        assert "Synthesis failed" in dep_map_jobs[0]["error"]

    def test_full_analysis_stores_error_in_sqlite(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
        db_path,
    ):
        """
        run_full_analysis persists 'failed' status and error message to SQLite.

        Given _execute_analysis_passes raises with 'Claude CLI timeout after 300s'
        When run_full_analysis propagates the exception
        Then SQLite row has status='failed' and error contains the message
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service._get_activated_repos = MagicMock(
            return_value=[{"alias": "repo-1", "clone_path": "/fake"}]
        )
        service._enrich_repo_sizes = MagicMock(
            return_value=[
                {
                    "alias": "repo-1",
                    "clone_path": "/fake",
                    "file_count": 1,
                    "total_bytes": 100,
                }
            ]
        )
        error_msg = "Claude CLI timeout after 300s"
        service._execute_analysis_passes = MagicMock(
            side_effect=RuntimeError(error_msg)
        )

        with pytest.raises(RuntimeError):
            service.run_full_analysis()

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs "
            "WHERE operation_type = 'dependency_map_full'"
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert error_msg in row[1]
