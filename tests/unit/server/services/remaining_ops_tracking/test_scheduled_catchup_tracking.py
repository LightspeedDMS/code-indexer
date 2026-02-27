"""
AC5, AC6: ScheduledCatchupService job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC5: ScheduledCatchupService accepts Optional[JobTracker] parameter
- AC5: _process_catchup() registers scheduled_catchup operation type
- AC5: Successful catch-up transitions to completed
- AC5: Failed catch-up transitions to failed with error details
- AC5: Tracker=None doesn't break _process_catchup
- AC5: Tracker raising exceptions doesn't break _process_catchup
- AC6: _process_catchup passes skip_tracking=True to process_all_fallbacks()
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.scheduled_catchup_service import ScheduledCatchupService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(job_tracker=None):
    """Create a ScheduledCatchupService with optional job_tracker."""
    return ScheduledCatchupService(
        enabled=True,
        interval_minutes=60,
        job_tracker=job_tracker,
    )


def _make_mock_result(processed=None, error=None):
    """Create a mock process_all_fallbacks result."""
    result = MagicMock()
    result.processed = processed or []
    result.error = error
    return result


# ---------------------------------------------------------------------------
# AC5: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestScheduledCatchupServiceConstructor:
    """AC5: ScheduledCatchupService accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self):
        """
        ScheduledCatchupService can be constructed without a job_tracker.

        Given no job_tracker is provided
        When ScheduledCatchupService is instantiated
        Then no exception is raised and _job_tracker is None
        """
        service = ScheduledCatchupService(enabled=True)
        assert service is not None
        assert service._job_tracker is None

    def test_accepts_job_tracker_instance(self, job_tracker):
        """
        ScheduledCatchupService stores the job_tracker.

        Given a real JobTracker instance
        When ScheduledCatchupService is instantiated with it
        Then _job_tracker is set
        """
        service = _make_service(job_tracker=job_tracker)
        assert service._job_tracker is job_tracker

    def test_backward_compatible_without_job_tracker_parameter(self):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When ScheduledCatchupService is instantiated
        Then no TypeError is raised
        """
        service = ScheduledCatchupService(enabled=False, interval_minutes=30)
        assert service is not None


# ---------------------------------------------------------------------------
# AC5: scheduled_catchup job registered during _process_catchup
# ---------------------------------------------------------------------------


class TestScheduledCatchupJobRegistration:
    """AC5: scheduled_catchup operation type is registered during _process_catchup."""

    def test_registers_scheduled_catchup_job(self, job_tracker):
        """
        _process_catchup() registers a scheduled_catchup job.

        Given a ScheduledCatchupService with job_tracker
        When _process_catchup() is called (with mocked manager)
        Then a scheduled_catchup job exists in the tracker
        """
        service = _make_service(job_tracker=job_tracker)
        mock_result = _make_mock_result(processed=["repo1"])

        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()

        jobs = job_tracker.query_jobs(operation_type="scheduled_catchup")
        assert len(jobs) >= 1

    def test_scheduled_catchup_job_completes_on_success(self, job_tracker):
        """
        scheduled_catchup job transitions to completed on success.

        Given a ScheduledCatchupService with job_tracker
        When _process_catchup() succeeds
        Then the scheduled_catchup job has completed status
        """
        service = _make_service(job_tracker=job_tracker)
        mock_result = _make_mock_result(processed=["repo1"])

        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()

        jobs = job_tracker.query_jobs(operation_type="scheduled_catchup", status="completed")
        assert len(jobs) >= 1

    def test_scheduled_catchup_job_fails_when_exception_raised(self, job_tracker):
        """
        scheduled_catchup job transitions to failed when exception occurs.

        Given a ScheduledCatchupService with job_tracker
        When process_all_fallbacks() raises an exception
        Then a scheduled_catchup job exists with failed status
        """
        service = _make_service(job_tracker=job_tracker)

        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.side_effect = RuntimeError("Claude unavailable")

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()

        jobs = job_tracker.query_jobs(operation_type="scheduled_catchup")
        assert len(jobs) >= 1
        failed = [j for j in jobs if j["status"] == "failed"]
        assert len(failed) >= 1

    def test_no_job_tracker_does_not_break_process_catchup(self):
        """
        When job_tracker is None, _process_catchup proceeds normally.

        Given a ScheduledCatchupService WITHOUT job_tracker
        When _process_catchup() is called
        Then no exception is raised
        """
        service = _make_service(job_tracker=None)
        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()  # Should not raise

    def test_tracker_exception_does_not_break_process_catchup(self):
        """
        When job_tracker raises on register_job, _process_catchup proceeds.

        Given a job_tracker that raises RuntimeError on register_job
        When _process_catchup() is called
        Then no exception propagates
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")
        service = _make_service(job_tracker=broken_tracker)

        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()  # Must not raise

    def test_no_job_when_manager_not_initialized(self, job_tracker):
        """
        No job registered when ClaudeCliManager is None.

        Given a ScheduledCatchupService with job_tracker
        When get_claude_cli_manager() returns None
        Then no scheduled_catchup job is registered
        """
        service = _make_service(job_tracker=job_tracker)

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=None,
        ):
            service._process_catchup()

        jobs = job_tracker.query_jobs(operation_type="scheduled_catchup")
        assert len(jobs) == 0


# ---------------------------------------------------------------------------
# AC6: skip_tracking=True passed to process_all_fallbacks
# ---------------------------------------------------------------------------


class TestSkipTrackingFlag:
    """AC6: _process_catchup passes skip_tracking=True to prevent double-tracking."""

    def test_passes_skip_tracking_true_to_process_all_fallbacks(self, job_tracker):
        """
        AC6: process_all_fallbacks is called with skip_tracking=True.

        Given a ScheduledCatchupService with job_tracker
        When _process_catchup() is called
        Then process_all_fallbacks() is called with skip_tracking=True
        """
        service = _make_service(job_tracker=job_tracker)
        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()

        # Verify skip_tracking=True was passed as keyword argument
        mock_manager.process_all_fallbacks.assert_called_once()
        call_kwargs = mock_manager.process_all_fallbacks.call_args
        assert call_kwargs.kwargs.get("skip_tracking") is True

    def test_passes_skip_tracking_true_even_without_job_tracker(self):
        """
        AC6: skip_tracking=True is passed even when job_tracker is None.

        The skip_tracking flag prevents double-tracking with Story 3's
        catchup_processing operation type.
        """
        service = _make_service(job_tracker=None)
        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with patch(
            "code_indexer.server.services.scheduled_catchup_service.get_claude_cli_manager",
            return_value=mock_manager,
        ):
            service._process_catchup()

        mock_manager.process_all_fallbacks.assert_called_once()
        call_kwargs = mock_manager.process_all_fallbacks.call_args
        assert call_kwargs.kwargs.get("skip_tracking") is True
