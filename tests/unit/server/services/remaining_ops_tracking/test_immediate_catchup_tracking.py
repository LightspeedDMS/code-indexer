"""
AC9: api_keys.py immediate_catchup job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC9: trigger_catchup_on_api_key_save() registers immediate_catchup operation type
- AC9: Successful catch-up transitions to completed
- AC9: Failed catch-up transitions to failed with error details
- AC9: No tracker (get_job_tracker returns None) doesn't break function
- AC9: Tracker raising exceptions doesn't break the catch-up trigger

Fixture `job_tracker` is provided by conftest.py in this directory.
"""

import threading
from contextlib import contextmanager
from typing import List
from unittest.mock import MagicMock, patch


from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.routers.api_keys import trigger_catchup_on_api_key_save


# Name the production trigger gives its background worker
# (`api_keys.trigger_catchup_on_api_key_save`).
CATCHUP_THREAD_NAME = "ImmediateCatchupProcessor"

# Ceiling on the join below. This is a deadlock guard, NOT a synchronisation
# delay: the join returns the instant the worker exits, so the test costs
# whatever the worker actually costs and never this number.
CATCHUP_JOIN_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def joined_catchup_thread():
    """Join the background worker `trigger_catchup_on_api_key_save()` spawns.

    Bug #1867. The trigger hands its work to a daemon thread and returns
    immediately, so the test must wait for that thread before asserting on what
    it recorded. Waiting a fixed `time.sleep(0.3)` is a race in two separate
    ways, both observed live in chunk1 of the 20260915_035600 server-fast run:

    1. Under concurrent CPU load the worker was not scheduled until 546 ms
       after `start()` returned -- 246 ms AFTER the assertion had already run
       and failed with `assert 0 >= 1`.
    2. Worse, by then the `patch()` scopes the worker depends on had been torn
       down, so its `get_job_tracker()` resolved the REAL process-wide tracker
       instead of the test's. The worker went on to touch the developer's real
       server data directory.

    Entering this context inside the still-active `patch()` scopes and exiting
    it before they unwind closes both windows.

    The worker is captured in `Thread.start()` rather than by diffing
    `threading.enumerate()`: a worker that finishes before the diff is taken is
    already gone from `enumerate()`, which would make the capture itself racy.
    """
    started: List[threading.Thread] = []
    original_start = threading.Thread.start

    def recording_start(self) -> None:
        if self.name == CATCHUP_THREAD_NAME:
            started.append(self)
        original_start(self)

    threading.Thread.start = recording_start  # type: ignore[method-assign]
    try:
        yield
    finally:
        threading.Thread.start = original_start  # type: ignore[method-assign]
        for thread in started:
            thread.join(timeout=CATCHUP_JOIN_TIMEOUT_SECONDS)

    assert started, (
        f"trigger_catchup_on_api_key_save() spawned no {CATCHUP_THREAD_NAME} thread"
    )
    for thread in started:
        assert not thread.is_alive(), (
            f"{thread.name} still running after "
            f"{CATCHUP_JOIN_TIMEOUT_SECONDS}s -- catch-up worker is stuck"
        )


def _make_mock_result(processed=None, error=None):
    """Create a mock process_all_fallbacks result."""
    result = MagicMock()
    result.processed = processed or []
    result.error = error
    return result


# ---------------------------------------------------------------------------
# AC9: immediate_catchup job registered during trigger_catchup_on_api_key_save
# ---------------------------------------------------------------------------


class TestImmediateCatchupJobRegistration:
    """AC9: immediate_catchup operation type is registered during trigger."""

    def test_registers_immediate_catchup_job(self, job_tracker):
        """
        trigger_catchup_on_api_key_save() registers an immediate_catchup job.

        Given a job_tracker injected via app module global
        When trigger_catchup_on_api_key_save() is called with a valid key
        Then an immediate_catchup job exists in the tracker
        """
        mock_result = _make_mock_result(processed=["repo1"])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with (
            patch(
                "code_indexer.server.routers.api_keys.get_claude_cli_manager",
                return_value=mock_manager,
            ),
            patch(
                "code_indexer.server.routers.api_keys.get_job_tracker",
                return_value=job_tracker,
            ),
            joined_catchup_thread(),
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        jobs = job_tracker.query_jobs(operation_type="immediate_catchup")
        assert len(jobs) >= 1

    def test_immediate_catchup_job_completes_on_success(self, job_tracker):
        """
        immediate_catchup job transitions to completed on success.

        Given a job_tracker accessible in api_keys module
        When trigger_catchup_on_api_key_save() succeeds
        Then the immediate_catchup job has completed status
        """
        mock_result = _make_mock_result(processed=["repo1"])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with (
            patch(
                "code_indexer.server.routers.api_keys.get_claude_cli_manager",
                return_value=mock_manager,
            ),
            patch(
                "code_indexer.server.routers.api_keys.get_job_tracker",
                return_value=job_tracker,
            ),
            joined_catchup_thread(),
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        jobs = job_tracker.query_jobs(
            operation_type="immediate_catchup", status="completed"
        )
        assert len(jobs) >= 1

    def test_immediate_catchup_job_fails_when_exception_raised(self, job_tracker):
        """
        immediate_catchup job transitions to failed when exception occurs.

        Given a job_tracker accessible in api_keys module
        When process_all_fallbacks() raises an exception
        Then an immediate_catchup job exists with failed status
        """
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.side_effect = RuntimeError(
            "Claude unavailable"
        )

        with (
            patch(
                "code_indexer.server.routers.api_keys.get_claude_cli_manager",
                return_value=mock_manager,
            ),
            patch(
                "code_indexer.server.routers.api_keys.get_job_tracker",
                return_value=job_tracker,
            ),
            joined_catchup_thread(),
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        jobs = job_tracker.query_jobs(operation_type="immediate_catchup")
        assert len(jobs) >= 1
        failed = [j for j in jobs if j["status"] == "failed"]
        assert len(failed) >= 1

    def test_no_job_tracker_does_not_break_trigger(self):
        """
        When get_job_tracker() returns None, trigger_catchup_on_api_key_save proceeds.

        Given no job_tracker available in api_keys module
        When trigger_catchup_on_api_key_save() is called
        Then no exception is raised and True is returned
        """
        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with (
            patch(
                "code_indexer.server.routers.api_keys.get_claude_cli_manager",
                return_value=mock_manager,
            ),
            patch(
                "code_indexer.server.routers.api_keys.get_job_tracker",
                return_value=None,
            ),
            joined_catchup_thread(),
        ):
            result = trigger_catchup_on_api_key_save("sk-ant-valid-key")

        assert result is True

    def test_tracker_exception_does_not_break_trigger(self):
        """
        When job_tracker raises on register_job, trigger proceeds normally.

        Given a job_tracker that raises RuntimeError on register_job
        When trigger_catchup_on_api_key_save() is called
        Then no exception propagates from the trigger function
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")

        mock_result = _make_mock_result(processed=[])
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.return_value = mock_result

        with (
            patch(
                "code_indexer.server.routers.api_keys.get_claude_cli_manager",
                return_value=mock_manager,
            ),
            patch(
                "code_indexer.server.routers.api_keys.get_job_tracker",
                return_value=broken_tracker,
            ),
            joined_catchup_thread(),
        ):
            result = trigger_catchup_on_api_key_save("sk-ant-valid-key")

        assert result is True

    def test_no_manager_returns_false(self):
        """
        When ClaudeCliManager is None, trigger returns False (existing behavior).

        Given get_claude_cli_manager() returns None
        When trigger_catchup_on_api_key_save() is called
        Then it returns False without registering any job
        """
        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=None,
        ):
            result = trigger_catchup_on_api_key_save("sk-ant-valid-key")

        assert result is False

    def test_empty_api_key_returns_false(self):
        """
        When api_key is empty, trigger returns False (existing behavior).

        Given an empty api_key string
        When trigger_catchup_on_api_key_save() is called
        Then it returns False without any job registration
        """
        result = trigger_catchup_on_api_key_save("")
        assert result is False
