"""
Unit tests for LangfuseTraceSyncService stop() enhancements and cooperative shutdown.

TDD: Tests written FIRST before production code changes exist.
Bug #436: Orphaned jobs persist as "running" after server restart.

Fix 2: LangfuseTraceSyncService.stop() should fail the tracked job when thread
       does not finish within the join timeout.
Fix 3: Cooperative shutdown checks in _do_sync_all_projects() loop.
"""

import threading
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def mock_config():
    """Return a mock ServerConfig with Langfuse pull enabled."""
    config = Mock()
    langfuse_cfg = Mock()
    langfuse_cfg.pull_enabled = True
    langfuse_cfg.pull_host = "https://langfuse.example.com"
    langfuse_cfg.pull_projects = []
    langfuse_cfg.pull_trace_age_days = 30
    langfuse_cfg.pull_max_concurrent_observations = 5
    config.langfuse_config = langfuse_cfg
    return config


@pytest.fixture
def mock_job_tracker():
    """Return a Mock job tracker."""
    return Mock()


@pytest.fixture
def service(mock_config, mock_job_tracker):
    """Create a LangfuseTraceSyncService with mock dependencies."""
    from code_indexer.server.services.langfuse_trace_sync_service import (
        LangfuseTraceSyncService,
    )

    config_getter = Mock(return_value=mock_config)
    svc = LangfuseTraceSyncService(
        config_getter=config_getter,
        data_dir="/tmp/test-cidx-langfuse",
        job_tracker=mock_job_tracker,
    )
    return svc


class TestCurrentTrackedJobIdLifecycle:
    """Tests for _current_tracked_job_id being set and cleared correctly."""

    def test_tracked_job_id_starts_as_none(self, service) -> None:
        """_current_tracked_job_id must be None before any sync runs."""
        assert service._current_tracked_job_id is None

    def test_tracked_job_id_cleared_after_successful_sync(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """After _do_sync_all_projects() completes normally, _current_tracked_job_id is None."""
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.complete_job.return_value = None

        # No projects configured — sync completes immediately
        mock_config.langfuse_config.pull_projects = []

        service._do_sync_all_projects()

        assert service._current_tracked_job_id is None

    def test_tracked_job_id_cleared_after_sync_error(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """After _do_sync_all_projects() encounters errors, _current_tracked_job_id is None."""
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.fail_job.return_value = None

        # One project that raises an error
        bad_project = Mock()
        mock_config.langfuse_config.pull_projects = [bad_project]

        with patch.object(service, "sync_project", side_effect=Exception("API down")):
            service._do_sync_all_projects()

        assert service._current_tracked_job_id is None


class TestStopFailsTrackedJobOnTimeout:
    """Tests for stop() failing the tracked job when thread doesn't stop in time."""

    def test_stop_fails_tracked_job_when_thread_alive_after_join(
        self, service, mock_job_tracker
    ) -> None:
        """When thread remains alive after join timeout, stop() must fail the tracked job."""
        # Simulate a thread that never stops
        fake_thread = Mock(spec=threading.Thread)
        fake_thread.is_alive.return_value = True  # still alive after join

        service._thread = fake_thread
        service._current_tracked_job_id = "langfuse-sync-abc12345"

        service.stop()

        mock_job_tracker.fail_job.assert_called_once()
        call_args = mock_job_tracker.fail_job.call_args
        assert (
            call_args[0][0] == "langfuse-sync-abc12345"
            or call_args[1].get("job_id") == "langfuse-sync-abc12345"
            or "langfuse-sync-abc12345" in str(call_args)
        )

    def test_stop_does_not_fail_job_when_thread_finishes_normally(
        self, service, mock_job_tracker
    ) -> None:
        """When thread finishes within join timeout, stop() must NOT call fail_job."""
        fake_thread = Mock(spec=threading.Thread)
        fake_thread.is_alive.return_value = False  # finished cleanly

        service._thread = fake_thread
        service._current_tracked_job_id = "langfuse-sync-done"

        service.stop()

        mock_job_tracker.fail_job.assert_not_called()

    def test_stop_does_not_fail_job_when_no_tracked_job(
        self, service, mock_job_tracker
    ) -> None:
        """When _current_tracked_job_id is None, stop() does not call fail_job."""
        fake_thread = Mock(spec=threading.Thread)
        fake_thread.is_alive.return_value = True  # still alive

        service._thread = fake_thread
        service._current_tracked_job_id = None  # no job registered

        service.stop()

        mock_job_tracker.fail_job.assert_not_called()

    def test_stop_does_not_fail_job_when_no_job_tracker(self, mock_config) -> None:
        """When job_tracker is None, stop() must not raise even if thread is alive."""
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        svc = LangfuseTraceSyncService(
            config_getter=Mock(return_value=mock_config),
            data_dir="/tmp/test-cidx-langfuse-no-tracker",
            job_tracker=None,
        )
        fake_thread = Mock(spec=threading.Thread)
        fake_thread.is_alive.return_value = True

        svc._thread = fake_thread
        svc._current_tracked_job_id = "langfuse-sync-orphan"

        # Must not raise
        svc.stop()

    def test_stop_fail_job_error_does_not_propagate(
        self, service, mock_job_tracker
    ) -> None:
        """If fail_job raises, stop() must still complete without propagating the error."""
        fake_thread = Mock(spec=threading.Thread)
        fake_thread.is_alive.return_value = True

        service._thread = fake_thread
        service._current_tracked_job_id = "langfuse-sync-errjob"
        mock_job_tracker.fail_job.side_effect = Exception("tracker DB error")

        # Must not raise
        service.stop()


class TestCooperativeShutdownBetweenProjects:
    """Tests for cooperative shutdown check in _do_sync_all_projects() project loop."""

    def test_stop_event_breaks_project_loop(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """When _stop_event is set before the loop, no projects are synced."""
        project_a = Mock()
        project_b = Mock()
        mock_config.langfuse_config.pull_projects = [project_a, project_b]
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.fail_job.return_value = None

        service._stop_event.set()

        with patch.object(service, "sync_project") as mock_sync:
            service._do_sync_all_projects()
            mock_sync.assert_not_called()

    def test_stop_event_set_mid_loop_stops_after_current_project(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """When _stop_event is set during sync, subsequent projects are skipped."""
        project_a = Mock()
        project_b = Mock()
        mock_config.langfuse_config.pull_projects = [project_a, project_b]
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.fail_job.return_value = None

        call_count = [0]

        def sync_and_stop(host, creds, age_days, max_concurrent):
            call_count[0] += 1
            service._stop_event.set()  # Set stop after first project

        with patch.object(service, "sync_project", side_effect=sync_and_stop):
            service._do_sync_all_projects()

        # Only first project was synced; second was skipped due to stop signal
        assert call_count[0] == 1

    def test_on_sync_complete_not_called_when_stop_event_set(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """Post-sync callback must not be called when stop event is set."""
        mock_config.langfuse_config.pull_projects = []
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.complete_job.return_value = None

        on_complete = Mock()
        service._on_sync_complete = on_complete
        service._stop_event.set()

        service._do_sync_all_projects()

        on_complete.assert_not_called()

    def test_on_sync_complete_called_when_stop_event_not_set(
        self, service, mock_config, mock_job_tracker
    ) -> None:
        """Post-sync callback IS called when stop event is not set."""
        mock_config.langfuse_config.pull_projects = []
        mock_job_tracker.register_job.return_value = None
        mock_job_tracker.update_status.return_value = None
        mock_job_tracker.complete_job.return_value = None

        on_complete = Mock()
        service._on_sync_complete = on_complete
        # stop event NOT set

        service._do_sync_all_projects()

        on_complete.assert_called_once()
