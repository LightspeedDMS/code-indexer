"""
Unit tests for Bug #1089: DuplicateJobError guard in description and lifecycle
backfill startup sweeps.

On fast server restarts the partial unique index idx_active_job_per_repo can
reject a new job registration with UniqueViolation.  Both backfill workers must
use register_job_if_no_conflict() and catch DuplicateJobError specifically,
logging at INFO level and returning cleanly so the finally-block still clears
the running event.

Test strategy:
- object.__new__(DescriptionRefreshScheduler) + manual attribute injection
  (same pattern as the sibling test file).
- Patch _init_backfill_journal to return a MagicMock so the journal calls
  do not hit the filesystem.

Classes:
  TestDescriptionBackfillDuplicateJobGuard   (2 tests)
  TestLifecycleBackfillDuplicateJobGuard     (2 tests)
  TestBackfillUsesAtomicRegistration         (2 tests)
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch


SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"
LOGGER_NAME = SCHEDULER_MODULE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler_bare() -> Any:
    """
    Construct a DescriptionRefreshScheduler without calling __init__.

    Injects the minimal set of attributes used by
    _run_description_backfill_async() and _run_lifecycle_backfill_async().
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    # Lifecycle collaborators
    sched._lifecycle_invoker = MagicMock()
    sched._golden_repos_dir = MagicMock()
    sched._lifecycle_debouncer = MagicMock()
    sched._refresh_scheduler = MagicMock()
    sched._job_tracker = MagicMock()
    sched._tracking_backend = MagicMock()

    # Threading events used in the finally blocks
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()

    # Backend used in golden-repo list (not exercised here, but avoids AttributeError)
    sched._golden_backend = MagicMock()

    return sched


def _make_duplicate_error(
    operation_type: str, repo_alias: str, existing_job_id: str
) -> Any:
    from code_indexer.server.services.job_tracker import DuplicateJobError

    return DuplicateJobError(
        operation_type=operation_type,
        repo_alias=repo_alias,
        existing_job_id=existing_job_id,
    )


# ---------------------------------------------------------------------------
# Class 1: Description backfill — DuplicateJobError guard
# ---------------------------------------------------------------------------


class TestDescriptionBackfillDuplicateJobGuard:
    """
    When register_job_if_no_conflict raises DuplicateJobError the description
    backfill worker must:
      - NOT log any ERROR
      - clear _description_backfill_running via the finally block
      - NOT invoke LifecycleBatchRunner
    And it must emit an INFO-level message containing "duplicate" or
    "already active" plus the existing job ID.
    """

    def _run_with_duplicate(self, sched: Any, existing_job_id: str, caplog):
        """Helper: configure duplicate-error side-effect and run the worker."""
        dup_err = _make_duplicate_error(
            operation_type="description_backfill",
            repo_alias="server",
            existing_job_id=existing_job_id,
        )
        sched._job_tracker.register_job_if_no_conflict.side_effect = dup_err

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            with patch.object(
                sched, "_init_backfill_journal", return_value=MagicMock()
            ):
                with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
                    sched._run_description_backfill_async(["alias-a"])

        return mock_runner_cls

    def test_description_backfill_skips_silently_when_duplicate_job_active(
        self, caplog
    ):
        sched = _make_scheduler_bare()
        mock_runner_cls = self._run_with_duplicate(sched, "prev-123", caplog)

        # (a) No ERROR log
        error_messages = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert not error_messages, f"Expected no ERROR logs, got: {error_messages}"

        # (b) _description_backfill_running event is cleared by finally
        assert not sched._description_backfill_running.is_set(), (
            "_description_backfill_running must be cleared after duplicate skip"
        )

        # (c) LifecycleBatchRunner never invoked
        mock_runner_cls.assert_not_called()

    def test_description_backfill_logs_info_on_duplicate(self, caplog):
        sched = _make_scheduler_bare()
        self._run_with_duplicate(sched, "prev-123", caplog)

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            ("duplicate" in m.lower() or "already active" in m.lower())
            and "prev-123" in m
            for m in info_messages
        ), (
            f"Expected INFO log with 'duplicate'/'already active' and 'prev-123', "
            f"got INFO messages: {info_messages}"
        )


# ---------------------------------------------------------------------------
# Class 2: Lifecycle backfill — DuplicateJobError guard
# ---------------------------------------------------------------------------


class TestLifecycleBackfillDuplicateJobGuard:
    """
    When register_job_if_no_conflict raises DuplicateJobError the lifecycle
    backfill worker must:
      - NOT log any ERROR
      - clear _lifecycle_backfill_running via the finally block
      - NOT invoke LifecycleBatchRunner
    And it must emit an INFO-level message containing "duplicate" or
    "already active" plus the existing job ID.
    """

    def _run_with_duplicate(self, sched: Any, existing_job_id: str, caplog):
        """Helper: configure duplicate-error side-effect and run the worker."""
        dup_err = _make_duplicate_error(
            operation_type="lifecycle_backfill",
            repo_alias="server",
            existing_job_id=existing_job_id,
        )
        sched._job_tracker.register_job_if_no_conflict.side_effect = dup_err

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            with patch.object(
                sched, "_init_backfill_journal", return_value=MagicMock()
            ):
                with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
                    sched._run_lifecycle_backfill_async(["alias-x"])

        return mock_runner_cls

    def test_lifecycle_backfill_skips_silently_when_duplicate_job_active(self, caplog):
        sched = _make_scheduler_bare()
        mock_runner_cls = self._run_with_duplicate(sched, "prev-456", caplog)

        # (a) No ERROR log
        error_messages = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert not error_messages, f"Expected no ERROR logs, got: {error_messages}"

        # (b) _lifecycle_backfill_running event is cleared by finally
        assert not sched._lifecycle_backfill_running.is_set(), (
            "_lifecycle_backfill_running must be cleared after duplicate skip"
        )

        # (c) LifecycleBatchRunner never invoked
        mock_runner_cls.assert_not_called()

    def test_lifecycle_backfill_logs_info_on_duplicate(self, caplog):
        sched = _make_scheduler_bare()
        self._run_with_duplicate(sched, "prev-456", caplog)

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            ("duplicate" in m.lower() or "already active" in m.lower())
            and "prev-456" in m
            for m in info_messages
        ), (
            f"Expected INFO log with 'duplicate'/'already active' and 'prev-456', "
            f"got INFO messages: {info_messages}"
        )


# ---------------------------------------------------------------------------
# Class 3: Both backfills use register_job_if_no_conflict (atomic registration)
# ---------------------------------------------------------------------------


class TestBackfillUsesAtomicRegistration:
    """
    On the successful (no-duplicate) path, both workers must call
    register_job_if_no_conflict instead of register_job.
    """

    def test_description_backfill_calls_register_job_if_no_conflict(self):
        sched = _make_scheduler_bare()

        # register_job_if_no_conflict returns a MagicMock TrackedJob
        sched._job_tracker.register_job_if_no_conflict.return_value = MagicMock()

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner
            with patch.object(
                sched, "_init_backfill_journal", return_value=MagicMock()
            ):
                sched._run_description_backfill_async(["alias-a"])

        # register_job_if_no_conflict must have been called
        sched._job_tracker.register_job_if_no_conflict.assert_called_once()
        # register_job must NOT have been called
        sched._job_tracker.register_job.assert_not_called()

    def test_lifecycle_backfill_calls_register_job_if_no_conflict(self):
        sched = _make_scheduler_bare()

        # register_job_if_no_conflict returns a MagicMock TrackedJob
        sched._job_tracker.register_job_if_no_conflict.return_value = MagicMock()

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner
            with patch.object(
                sched, "_init_backfill_journal", return_value=MagicMock()
            ):
                sched._run_lifecycle_backfill_async(["alias-x"])

        # register_job_if_no_conflict must have been called
        sched._job_tracker.register_job_if_no_conflict.assert_called_once()
        # register_job must NOT have been called
        sched._job_tracker.register_job.assert_not_called()
