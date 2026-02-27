"""
AC3, AC4, AC7, AC8: Delta analysis job tracking tests.

Story 2 (#312) - Epic #261 Unified Job Tracking Subsystem.

Covers:
- AC3: Delta analysis registers as 'dependency_map_delta' operation type
- AC4: Entry points can pass job_id to run_delta_analysis
- AC7: Status transitions pending -> running -> completed/failed
- AC8: Failed delta analyses report error details via fail_job()
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from .conftest import make_service


class TestDeltaAnalysisJobRegistration:
    """AC3: Delta analysis registers as 'dependency_map_delta' operation type."""

    def test_delta_analysis_registers_dependency_map_delta_operation_type(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis registers a job with operation_type='dependency_map_delta'.

        Given a DependencyMapService with job_tracker and disabled config
        When run_delta_analysis is called (early return path)
        Then a job of type 'dependency_map_delta' is visible in the tracker
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert len(dep_map_jobs) == 1

    def test_delta_analysis_job_uses_system_username(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis registers the job with username='system'.

        Given job_tracker is provided
        When run_delta_analysis is called
        Then the registered job has username='system'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert dep_map_jobs[0]["username"] == "system"


class TestDeltaAnalysisStatusTransitions:
    """AC7: Status transitions pending -> running -> completed/failed for delta."""

    def test_delta_analysis_completes_when_disabled(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis job ends as 'completed' when analysis is disabled.

        Given dependency map is disabled (early return path)
        When run_delta_analysis is called
        Then the tracked job reaches terminal state 'completed'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )

        service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "completed"

    def test_delta_analysis_completes_when_no_changes_detected(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis job ends as 'completed' when no changes are detected.

        Given no changed/new/removed repos
        When run_delta_analysis is called
        Then the tracked job reaches 'completed' status
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service.detect_changes = MagicMock(return_value=([], [], []))

        service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "completed"

    def test_delta_analysis_without_tracker_returns_none_when_disabled(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
    ):
        """
        run_delta_analysis without tracker returns None when disabled (backward compat).

        Given no job_tracker is provided
        When run_delta_analysis is called with disabled config
        Then None is returned (original behavior preserved)
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=None,
        )

        result = service.run_delta_analysis()
        assert result is None


class TestDeltaAnalysisEntryPointJobId:
    """AC4: Entry points can pass job_id to run_delta_analysis."""

    def test_run_delta_analysis_accepts_custom_job_id(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis accepts an optional job_id parameter.

        Given a caller-provided job_id
        When run_delta_analysis(job_id='custom-id') is called
        Then the tracker registers a job with that exact job_id
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        custom_id = "dep-map-delta-xyz-456"

        service.run_delta_analysis(job_id=custom_id)

        job = job_tracker.get_job(custom_id)
        assert job is not None
        assert job.operation_type == "dependency_map_delta"

    def test_run_delta_analysis_custom_job_id_is_completed(
        self,
        mock_golden_repos_manager,
        mock_config_manager_disabled,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis with custom job_id ends in 'completed' state.

        Given a caller-provided job_id and disabled config
        When run_delta_analysis completes
        Then the job with the custom id reaches 'completed' status
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager_disabled,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        custom_id = "dep-map-delta-abc-111"

        service.run_delta_analysis(job_id=custom_id)

        job = job_tracker.get_job(custom_id)
        assert job is not None
        assert job.status == "completed"


class TestDeltaAnalysisFailureReporting:
    """AC8: Failed delta analyses report error details via fail_job()."""

    def test_delta_analysis_marks_job_failed_on_exception(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis marks job as 'failed' when an exception occurs.

        Given detect_changes raises an exception
        When run_delta_analysis propagates the exception
        Then the tracked job status is 'failed'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service.detect_changes = MagicMock(side_effect=RuntimeError("DB error"))

        with pytest.raises(RuntimeError, match="DB error"):
            service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert len(dep_map_jobs) == 1
        assert dep_map_jobs[0]["status"] == "failed"

    def test_delta_analysis_stores_error_message_on_failure(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
    ):
        """
        run_delta_analysis stores the exception message in job error field.

        Given detect_changes raises with 'DB error'
        When run_delta_analysis propagates the exception
        Then job error contains 'DB error'
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        service.detect_changes = MagicMock(side_effect=RuntimeError("DB error"))

        with pytest.raises(RuntimeError):
            service.run_delta_analysis()

        jobs = job_tracker.get_recent_jobs()
        dep_map_jobs = [j for j in jobs if j["operation_type"] == "dependency_map_delta"]
        assert "DB error" in dep_map_jobs[0]["error"]

    def test_delta_analysis_stores_error_in_sqlite(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_tracking_backend,
        mock_analyzer,
        job_tracker,
        db_path,
    ):
        """
        run_delta_analysis persists 'failed' status and error message to SQLite.

        Given detect_changes raises with 'Hash comparison failed'
        When run_delta_analysis propagates the exception
        Then SQLite row has status='failed' and error contains the message
        """
        service = make_service(
            mock_golden_repos_manager,
            mock_config_manager,
            mock_tracking_backend,
            mock_analyzer,
            job_tracker=job_tracker,
        )
        error_msg = "Hash comparison failed: corrupt metadata"
        service.detect_changes = MagicMock(side_effect=RuntimeError(error_msg))

        with pytest.raises(RuntimeError):
            service.run_delta_analysis()

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs "
            "WHERE operation_type = 'dependency_map_delta'"
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert error_msg in row[1]
