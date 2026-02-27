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

import time
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.routers.api_keys import trigger_catchup_on_api_key_save


# Wait time for background daemon thread to complete work before assertions.
BACKGROUND_THREAD_WAIT_SECONDS = 0.3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=mock_manager,
        ), patch(
            "code_indexer.server.routers.api_keys.get_job_tracker",
            return_value=job_tracker,
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        time.sleep(BACKGROUND_THREAD_WAIT_SECONDS)

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

        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=mock_manager,
        ), patch(
            "code_indexer.server.routers.api_keys.get_job_tracker",
            return_value=job_tracker,
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        time.sleep(BACKGROUND_THREAD_WAIT_SECONDS)

        jobs = job_tracker.query_jobs(operation_type="immediate_catchup", status="completed")
        assert len(jobs) >= 1

    def test_immediate_catchup_job_fails_when_exception_raised(self, job_tracker):
        """
        immediate_catchup job transitions to failed when exception occurs.

        Given a job_tracker accessible in api_keys module
        When process_all_fallbacks() raises an exception
        Then an immediate_catchup job exists with failed status
        """
        mock_manager = MagicMock()
        mock_manager.process_all_fallbacks.side_effect = RuntimeError("Claude unavailable")

        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=mock_manager,
        ), patch(
            "code_indexer.server.routers.api_keys.get_job_tracker",
            return_value=job_tracker,
        ):
            trigger_catchup_on_api_key_save("sk-ant-valid-key")

        time.sleep(BACKGROUND_THREAD_WAIT_SECONDS)

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

        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=mock_manager,
        ), patch(
            "code_indexer.server.routers.api_keys.get_job_tracker",
            return_value=None,
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

        with patch(
            "code_indexer.server.routers.api_keys.get_claude_cli_manager",
            return_value=mock_manager,
        ), patch(
            "code_indexer.server.routers.api_keys.get_job_tracker",
            return_value=broken_tracker,
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
