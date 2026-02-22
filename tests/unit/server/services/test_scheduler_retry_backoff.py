"""
Unit tests for Bug #249: Scheduler retry storm on persistent failure.

Tests that _scheduler_loop() applies a backoff when run_delta_analysis()
raises an exception, preventing a retry storm when Claude CLI is persistently down.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest


class FakeStopEvent:
    """Simulates threading.Event that fires after N waits."""

    def __init__(self, stop_after_n_waits: int = 1):
        self._stop_after = stop_after_n_waits
        self._wait_count = 0
        self._set = False

    def is_set(self) -> bool:
        # Stop after the loop body has executed stop_after_n_waits times
        return self._set

    def wait(self, timeout=None):
        self._wait_count += 1
        if self._wait_count >= self._stop_after:
            self._set = True

    def set(self):
        self._set = True


def _make_service(stop_event, config_enabled=True, next_run_in_past=True, raise_on_delta=None):
    """
    Build a minimal DependencyMapService with mocked collaborators.

    Uses __new__ to bypass __init__ so only the attributes actually read by
    _scheduler_loop() need to be set: _stop_event, _config_manager,
    _tracking_backend, and run_delta_analysis.  No lock attributes are needed
    because _scheduler_loop() does not acquire any locks directly.

    Returns (service, tracking_backend_mock)
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    config_manager = MagicMock()
    tracking_backend = MagicMock()

    # Config
    if config_enabled:
        cfg = MagicMock()
        cfg.dependency_map_enabled = True
        config_manager.get_claude_integration_config.return_value = cfg
    else:
        config_manager.get_claude_integration_config.return_value = None

    # Tracking
    if next_run_in_past:
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        tracking_backend.get_tracking.return_value = {"next_run": past}
    else:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        tracking_backend.get_tracking.return_value = {"next_run": future}

    # Build service bypassing __init__ — set only the attributes _scheduler_loop() reads
    service = DependencyMapService.__new__(DependencyMapService)
    service._config_manager = config_manager
    service._tracking_backend = tracking_backend
    service._stop_event = stop_event

    # run_delta_analysis stub
    if raise_on_delta is not None:
        service.run_delta_analysis = MagicMock(side_effect=raise_on_delta)
    else:
        service.run_delta_analysis = MagicMock()

    return service, tracking_backend


class TestSchedulerHappyPath:
    """When run_delta_analysis succeeds, no backoff should be applied."""

    def test_no_backoff_on_success(self):
        """Successful delta analysis must NOT call update_tracking for backoff."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, tracking_backend = _make_service(stop_event, raise_on_delta=None)

        service._scheduler_loop()

        # update_tracking should NOT have been called (success path - no backoff)
        tracking_backend.update_tracking.assert_not_called()

    def test_delta_analysis_called_when_next_run_is_past(self):
        """run_delta_analysis is called when next_run is in the past."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, _ = _make_service(stop_event, raise_on_delta=None)

        service._scheduler_loop()

        service.run_delta_analysis.assert_called_once()

    def test_no_action_when_next_run_is_none(self):
        """When next_run is None, neither delta analysis nor backoff should execute."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, tracking_backend = _make_service(
            stop_event, raise_on_delta=None
        )
        tracking_backend.get_tracking.return_value = {"next_run": None}

        service._scheduler_loop()

        service.run_delta_analysis.assert_not_called()
        tracking_backend.update_tracking.assert_not_called()


class TestSchedulerBackoffOnFailure:
    """When run_delta_analysis raises, backoff must be applied."""

    def test_update_tracking_called_with_future_next_run(self):
        """After exception, next_run must be set to now + SCHEDULER_ERROR_BACKOFF_SECONDS."""
        from code_indexer.server.services.dependency_map_service import (
            SCHEDULER_ERROR_BACKOFF_SECONDS,
        )

        stop_event = FakeStopEvent(stop_after_n_waits=1)
        error = RuntimeError("Claude CLI is down")
        service, tracking_backend = _make_service(stop_event, raise_on_delta=error)

        before = datetime.now(timezone.utc)
        service._scheduler_loop()
        after = datetime.now(timezone.utc)

        tracking_backend.update_tracking.assert_called_once()
        kwargs = tracking_backend.update_tracking.call_args[1]
        assert "next_run" in kwargs, "update_tracking must receive next_run kwarg"

        stored_next_run = datetime.fromisoformat(kwargs["next_run"])
        expected_min = before + timedelta(seconds=SCHEDULER_ERROR_BACKOFF_SECONDS)
        expected_max = after + timedelta(seconds=SCHEDULER_ERROR_BACKOFF_SECONDS)

        assert stored_next_run >= expected_min, (
            f"next_run {stored_next_run} should be >= {expected_min}"
        )
        assert stored_next_run <= expected_max, (
            f"next_run {stored_next_run} should be <= {expected_max}"
        )

    def test_error_message_recorded_in_tracking(self):
        """Exception message must be stored via update_tracking(error_message=...)."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        error_text = "Connection refused: Claude CLI not running"
        service, tracking_backend = _make_service(
            stop_event, raise_on_delta=RuntimeError(error_text)
        )

        service._scheduler_loop()

        kwargs = tracking_backend.update_tracking.call_args[1]
        assert "error_message" in kwargs, "update_tracking must receive error_message kwarg"
        assert error_text in kwargs["error_message"]

    def test_backoff_info_logged(self, caplog):
        """An INFO log mentioning 'backoff' must appear after applying the backoff."""
        import logging

        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, _ = _make_service(stop_event, raise_on_delta=RuntimeError("fail"))

        with caplog.at_level(logging.INFO, logger="code_indexer.server.services.dependency_map_service"):
            service._scheduler_loop()

        backoff_logs = [r for r in caplog.records if "backoff" in r.message.lower()]
        assert backoff_logs, "Expected at least one INFO log mentioning 'backoff'"

    def test_scheduler_loop_does_not_crash_on_failure(self):
        """The scheduler loop must complete without raising even when delta raises."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, _ = _make_service(stop_event, raise_on_delta=RuntimeError("persistent error"))

        # Must not raise
        service._scheduler_loop()


class TestSchedulerBackoffUpdateFailure:
    """If update_tracking itself raises, the loop must not crash."""

    def test_loop_survives_update_tracking_failure(self):
        """If update_tracking raises, an error is logged but the loop exits cleanly."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, tracking_backend = _make_service(
            stop_event, raise_on_delta=RuntimeError("delta failed")
        )
        tracking_backend.update_tracking.side_effect = Exception("DB write failed")

        # Must not raise
        service._scheduler_loop()

    def test_update_tracking_failure_logs_error(self, caplog):
        """If update_tracking fails, an ERROR log must appear."""
        import logging

        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, tracking_backend = _make_service(
            stop_event, raise_on_delta=RuntimeError("delta fail")
        )
        tracking_backend.update_tracking.side_effect = Exception("DB write failed")

        with caplog.at_level(logging.ERROR, logger="code_indexer.server.services.dependency_map_service"):
            service._scheduler_loop()

        error_logs = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "backoff" in r.message.lower()
        ]
        assert error_logs, "Expected ERROR log about failed backoff update"


class TestSchedulerConstant:
    """Verify the backoff constant is defined and has a reasonable value."""

    def test_scheduler_error_backoff_constant_exists(self):
        """SCHEDULER_ERROR_BACKOFF_SECONDS must be importable from dependency_map_service."""
        from code_indexer.server.services.dependency_map_service import (
            SCHEDULER_ERROR_BACKOFF_SECONDS,
        )

        assert isinstance(SCHEDULER_ERROR_BACKOFF_SECONDS, int)

    def test_scheduler_error_backoff_is_at_least_one_hour(self):
        """SCHEDULER_ERROR_BACKOFF_SECONDS must be >= 3600 to prevent retry storms."""
        from code_indexer.server.services.dependency_map_service import (
            SCHEDULER_ERROR_BACKOFF_SECONDS,
        )

        assert SCHEDULER_ERROR_BACKOFF_SECONDS >= 3600, (
            f"Backoff must be at least 3600s, got {SCHEDULER_ERROR_BACKOFF_SECONDS}"
        )

    def test_delta_analysis_not_called_when_next_run_is_future(self):
        """run_delta_analysis must NOT be called when next_run is still in the future."""
        stop_event = FakeStopEvent(stop_after_n_waits=1)
        service, tracking_backend = _make_service(
            stop_event, next_run_in_past=False, raise_on_delta=None
        )

        service._scheduler_loop()

        service.run_delta_analysis.assert_not_called()
        tracking_backend.update_tracking.assert_not_called()
