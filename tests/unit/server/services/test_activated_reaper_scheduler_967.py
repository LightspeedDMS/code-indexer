"""
Unit tests for Story #967: ActivatedReaperScheduler.

TDD: Tests written BEFORE implementation. All should fail (red phase) until
ActivatedReaperScheduler is implemented.

Acceptance Criteria covered:
  AC3 - Cycle submitted as 'reap_activated_repos' background job
  AC4 - Cadence re-read from config_service each cycle (no caching)
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_service():
    """Mock ActivatedReaperService."""
    svc = MagicMock()
    svc.run_reap_cycle.return_value = MagicMock(
        scanned=0, reaped=[], skipped=[], errors=[]
    )
    return svc


@pytest.fixture
def mock_background_job_manager():
    mgr = MagicMock()
    mgr.submit_job.return_value = "job-001"
    return mgr


@pytest.fixture
def mock_config_service_large_cadence():
    """Config with large cadence so the loop waits on stop_event rather than sleeping."""
    svc = MagicMock()
    svc.get_config.return_value.activated_reaper_config.cadence_hours = 9999
    return svc


@pytest.fixture
def scheduler_factory(
    mock_service, mock_background_job_manager, mock_config_service_large_cadence
):
    """Factory producing ActivatedReaperScheduler instances (not started)."""
    from code_indexer.server.services.activated_reaper_scheduler import (
        ActivatedReaperScheduler,
    )

    def _build():
        return ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_large_cadence,
        )

    return _build


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestSchedulerLifecycle:
    """Scheduler starts and stops cleanly."""

    def test_stop_exits_within_timeout(self, scheduler_factory):
        """stop() signals the thread and it terminates within 5 seconds."""
        scheduler = scheduler_factory()
        scheduler.start()
        scheduler.stop()

        assert scheduler._thread is None or not scheduler._thread.is_alive()

    def test_start_creates_daemon_thread(self, scheduler_factory):
        """start() creates and starts a daemon thread."""
        scheduler = scheduler_factory()
        scheduler.start()

        assert scheduler._thread is not None
        assert scheduler._thread.is_alive()

        scheduler.stop()

    def test_double_stop_is_safe(self, scheduler_factory):
        """Calling stop() twice should not raise."""
        scheduler = scheduler_factory()
        scheduler.start()
        scheduler.stop()
        scheduler.stop()  # Should not raise


# ---------------------------------------------------------------------------
# trigger_now
# ---------------------------------------------------------------------------


class TestSchedulerTriggerNow:
    """trigger_now submits a reap job immediately without waiting for cadence."""

    def test_trigger_now_submits_job_with_correct_operation_type(
        self,
        mock_service,
        mock_background_job_manager,
        mock_config_service_large_cadence,
    ):
        """trigger_now() submits a 'reap_activated_repos' job."""
        from code_indexer.server.services.activated_reaper_scheduler import (
            ActivatedReaperScheduler,
        )

        scheduler = ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_large_cadence,
        )

        scheduler.trigger_now()

        mock_background_job_manager.submit_job.assert_called_once()
        args = mock_background_job_manager.submit_job.call_args
        assert args[0][0] == "reap_activated_repos"
        assert args[1]["submitter_username"] == "system"
        assert args[1]["is_admin"] is True

    def test_trigger_now_returns_job_id(
        self,
        mock_service,
        mock_background_job_manager,
        mock_config_service_large_cadence,
    ):
        """trigger_now() returns the job_id from submit_job."""
        from code_indexer.server.services.activated_reaper_scheduler import (
            ActivatedReaperScheduler,
        )

        mock_background_job_manager.submit_job.return_value = "job-trigger-xyz"

        scheduler = ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_large_cadence,
        )

        job_id = scheduler.trigger_now()

        assert job_id == "job-trigger-xyz"

    def test_trigger_now_does_not_require_scheduler_to_be_started(
        self,
        mock_service,
        mock_background_job_manager,
        mock_config_service_large_cadence,
    ):
        """trigger_now() works even when the scheduler loop is not running."""
        from code_indexer.server.services.activated_reaper_scheduler import (
            ActivatedReaperScheduler,
        )

        scheduler = ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_large_cadence,
        )

        # Don't call start()
        job_id = scheduler.trigger_now()

        assert job_id is not None
        mock_background_job_manager.submit_job.assert_called_once()


# ---------------------------------------------------------------------------
# AC4: Cadence re-read each cycle
# ---------------------------------------------------------------------------


class TestSchedulerCadenceReread:
    """AC4: Cadence re-read from config_service on each cycle."""

    def test_config_service_queried_for_cadence(
        self, mock_service, mock_background_job_manager
    ):
        """Scheduler calls config_service.get_config() at least once for cadence."""
        from code_indexer.server.services.activated_reaper_scheduler import (
            ActivatedReaperScheduler,
        )

        config_service = MagicMock()
        config_service.get_config.return_value.activated_reaper_config.cadence_hours = (
            9999
        )

        scheduler = ActivatedReaperScheduler(
            service=mock_service,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )
        scheduler.start()
        scheduler.stop()

        assert config_service.get_config.call_count >= 1
