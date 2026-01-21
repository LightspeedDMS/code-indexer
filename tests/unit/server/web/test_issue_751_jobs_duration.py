"""
Unit tests for Issue #751: Jobs Duration column always empty.

Bug: The Duration column shows "-" for all jobs, even when both started_at
and completed_at timestamps are available. The _get_all_jobs() function
does not calculate or include duration_seconds in the job_dict.

Tests are written FIRST following TDD methodology.
"""

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


class TestJobsDurationCalculation:
    """Tests for Bug #751: Duration column should show calculated duration."""

    def test_duration_seconds_calculated_when_both_timestamps_available(self):
        """
        Bug #751: duration_seconds should be calculated when start and end exist.

        Given a completed job with started_at and completed_at
        When _get_all_jobs is called
        Then job_dict includes duration_seconds as integer
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Job with both timestamps: ran for 2 minutes 35 seconds (155 seconds)
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="completed")
        mock_job.progress = 100
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.started_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.completed_at = datetime(2026, 1, 12, 10, 2, 35)  # 2m 35s later
        mock_job.error = None
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert "duration_seconds" in jobs[0], "job_dict should include duration_seconds"
        assert jobs[0]["duration_seconds"] == 155, "Duration should be 155 seconds (2m 35s)"

    def test_duration_seconds_none_when_job_not_completed(self):
        """
        Bug #751: duration_seconds should be None for running jobs.

        Given a running job with started_at but no completed_at
        When _get_all_jobs is called
        Then job_dict has duration_seconds = None
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Running job: no completed_at
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="running")
        mock_job.progress = 50
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.started_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.completed_at = None  # Still running
        mock_job.error = None
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert "duration_seconds" in jobs[0], "job_dict should include duration_seconds"
        assert jobs[0]["duration_seconds"] is None, "Running job should have duration_seconds=None"

    def test_duration_seconds_none_when_not_started(self):
        """
        Bug #751: duration_seconds should be None for queued jobs.

        Given a queued job with no started_at
        When _get_all_jobs is called
        Then job_dict has duration_seconds = None
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Queued job: no started_at
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="queued")
        mock_job.progress = 0
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.started_at = None  # Not started yet
        mock_job.completed_at = None
        mock_job.error = None
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert "duration_seconds" in jobs[0], "job_dict should include duration_seconds"
        assert jobs[0]["duration_seconds"] is None, "Queued job should have duration_seconds=None"

    def test_duration_seconds_handles_long_duration(self):
        """
        Bug #751: duration_seconds should handle long-running jobs correctly.

        Given a completed job that ran for 1 hour 30 minutes
        When _get_all_jobs is called
        Then duration_seconds is 5400 (90 minutes)
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Job that ran for 1h 30m = 5400 seconds
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="completed")
        mock_job.progress = 100
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.started_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.completed_at = datetime(2026, 1, 12, 11, 30, 0)  # 1h 30m later
        mock_job.error = None
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert jobs[0]["duration_seconds"] == 5400, "Duration should be 5400 seconds (1h 30m)"

    def test_duration_seconds_handles_failed_job_with_timestamps(self):
        """
        Bug #751: duration_seconds should be calculated for failed jobs too.

        Given a failed job with both started_at and completed_at
        When _get_all_jobs is called
        Then duration_seconds is calculated correctly
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Failed job with timestamps: ran for 45 seconds before failing
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="failed")
        mock_job.progress = 30
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.started_at = datetime(2026, 1, 12, 10, 0, 0)
        mock_job.completed_at = datetime(2026, 1, 12, 10, 0, 45)  # 45 seconds later
        mock_job.error = "Network error"
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert jobs[0]["duration_seconds"] == 45, "Failed job duration should be 45 seconds"

    def test_duration_seconds_handles_timezone_mismatch(self):
        """
        Bug #751 fix: Handle timezone-aware vs timezone-naive datetime mismatch.

        Given a job where started_at is timezone-aware and completed_at is naive
        When _get_all_jobs is called
        Then duration_seconds is calculated without raising TypeError
        """
        from src.code_indexer.server.web.routes import _get_all_jobs

        # Job with mixed timezone awareness (simulates database load vs in-memory creation)
        mock_job = MagicMock()
        mock_job.job_id = "job1"
        mock_job.operation_type = "add_golden_repo"
        mock_job.status = MagicMock(value="completed")
        mock_job.progress = 100
        mock_job.created_at = datetime(2026, 1, 12, 10, 0, 0, tzinfo=timezone.utc)
        # started_at is timezone-aware (from database load with UTC timezone)
        mock_job.started_at = datetime(2026, 1, 12, 10, 0, 0, tzinfo=timezone.utc)
        # completed_at is timezone-naive (simulating a potential mismatch)
        mock_job.completed_at = datetime(2026, 1, 12, 10, 2, 35)  # Naive datetime
        mock_job.error = None
        mock_job.username = "user1"
        mock_job.result = None

        mock_job_manager = MagicMock()
        mock_job_manager.jobs = {"job1": mock_job}
        mock_job_manager._lock = MagicMock()
        mock_job_manager._lock.__enter__ = MagicMock(return_value=None)
        mock_job_manager._lock.__exit__ = MagicMock(return_value=None)

        with patch(
            "src.code_indexer.server.web.routes._get_background_job_manager",
            return_value=mock_job_manager,
        ):
            # This should NOT raise TypeError: can't subtract offset-naive and offset-aware datetimes
            jobs, total, pages = _get_all_jobs()

        assert len(jobs) == 1
        assert "duration_seconds" in jobs[0], "job_dict should include duration_seconds"
        # Duration should be calculated correctly (155 seconds = 2m 35s)
        assert jobs[0]["duration_seconds"] == 155, "Duration should be 155 seconds (2m 35s)"
