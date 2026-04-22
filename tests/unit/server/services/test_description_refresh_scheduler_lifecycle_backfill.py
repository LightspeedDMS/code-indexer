"""
Unit tests for startup lifecycle backfill sweep in DescriptionRefreshScheduler.

reconcile_broken_lifecycle_metadata() is a one-shot sweep that runs at start()
(after reconcile_orphan_tracking(), before the periodic daemon thread) to find
golden repos with missing or broken cidx-meta lifecycle frontmatter and route
them asynchronously through LifecycleBatchRunner for repair.

Closes Story #876 gap: pre-existing aliases with stale v2 or 'confidence: unknown'
metadata are never repaired by any event-driven code path.

Test strategy:
- Use object.__new__(DescriptionRefreshScheduler) + manual attribute injection
  for lightweight direct-method tests (same pattern as the orphan_cleanup sibling).
- No mocking of code under test (Messi Rule #1 — anti-mock).
- All 11 tests are RED on HEAD (methods do not exist yet).
- All 11 tests must be GREEN after implementation.

Classes:
  TestReconcileBrokenLifecycleMetadataWiringGuard  (4 tests) — Messi Rule #2
  TestReconcileBrokenLifecycleMetadataScan         (4 tests) — scan outcomes
  TestReconcileBrokenLifecycleMetadataDispatch     (2 tests) — async dispatch
  TestRunLifecycleBackfillAsync                    (1 test)  — worker registration
"""

from __future__ import annotations

import logging
import uuid as _uuid_module
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"

LOGGER_NAME = SCHEDULER_MODULE


def _make_scheduler_bare() -> Any:
    """
    Construct a DescriptionRefreshScheduler without calling __init__.

    Injects only the minimal attributes needed by the methods under test.
    All lifecycle collaborators are initially None (mirrors production
    state before lifespan wires them).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    # Required by reconcile_broken_lifecycle_metadata() wiring guard
    sched._lifecycle_invoker = None
    sched._golden_repos_dir = None
    sched._lifecycle_debouncer = None
    sched._refresh_scheduler = None

    # Required by _run_lifecycle_backfill_async()
    sched._job_tracker = None
    sched._tracking_backend = MagicMock()

    # Required by reconcile_broken_lifecycle_metadata() scan phase
    sched._golden_backend = MagicMock()

    return sched


def _wire_all(sched: Any) -> None:
    """Set all five lifecycle collaborators to MagicMock instances."""
    sched._lifecycle_invoker = MagicMock()
    sched._golden_repos_dir = MagicMock()
    sched._lifecycle_debouncer = MagicMock()
    sched._refresh_scheduler = MagicMock()
    sched._job_tracker = MagicMock()


# ---------------------------------------------------------------------------
# Class 1: Wiring Guard — Messi Rule #2 (no silent fallback)
# ---------------------------------------------------------------------------


class TestReconcileBrokenLifecycleMetadataWiringGuard:
    """
    One test per required collaborator.  Any None collaborator must:
      - emit a WARNING log containing the collaborator name and "not wired"
      - return 0 immediately (short-circuit before list_repos is called)
    """

    def test_returns_zero_when_lifecycle_invoker_not_wired(self, caplog):
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._lifecycle_invoker = None  # break this one

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "lifecycle_invoker" in m and "not wired" in m for m in warning_messages
        ), (
            f"Expected WARNING about 'lifecycle_invoker not wired', got: {warning_messages}"
        )
        sched._golden_backend.list_repos.assert_not_called()

    def test_returns_zero_when_golden_repos_dir_not_wired(self, caplog):
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_repos_dir = None  # break this one

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "golden_repos_dir" in m and "not wired" in m for m in warning_messages
        ), (
            f"Expected WARNING about 'golden_repos_dir not wired', got: {warning_messages}"
        )
        sched._golden_backend.list_repos.assert_not_called()

    def test_returns_zero_when_lifecycle_debouncer_not_wired(self, caplog):
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._lifecycle_debouncer = None  # break this one

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "lifecycle_debouncer" in m and "not wired" in m for m in warning_messages
        ), (
            f"Expected WARNING about 'lifecycle_debouncer not wired', got: {warning_messages}"
        )
        sched._golden_backend.list_repos.assert_not_called()

    def test_returns_zero_when_refresh_scheduler_not_wired(self, caplog):
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._refresh_scheduler = None  # break this one

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "refresh_scheduler" in m and "not wired" in m for m in warning_messages
        ), (
            f"Expected WARNING about 'refresh_scheduler not wired', got: {warning_messages}"
        )
        sched._golden_backend.list_repos.assert_not_called()

    def test_returns_zero_when_job_tracker_not_wired(self, caplog):
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._job_tracker = None  # break this one

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("job_tracker" in m and "not wired" in m for m in warning_messages), (
            f"Expected WARNING about 'job_tracker not wired', got: {warning_messages}"
        )
        sched._golden_backend.list_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Class 2: Scan outcomes
# ---------------------------------------------------------------------------


class TestReconcileBrokenLifecycleMetadataScan:
    """Scan phase outcomes when all collaborators are wired."""

    def test_returns_zero_when_no_golden_repos(self, caplog):
        """list_repos() returns [] → INFO log, return 0."""
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.return_value = []

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("no golden repos" in m for m in info_messages), (
            f"Expected INFO 'no golden repos', got: {info_messages}"
        )

    def test_returns_zero_when_no_broken_aliases_found(self, caplog):
        """list_repos() returns 3 aliases; scanner finds none broken → INFO, return 0."""
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.return_value = [
            {"alias": "repo-a"},
            {"alias": "repo-b"},
            {"alias": "repo-c"},
        ]

        with patch(
            "code_indexer.global_repos.lifecycle_batch_runner.LifecycleFleetScanner"
        ) as mock_scanner_cls:
            mock_scanner = MagicMock()
            mock_scanner.find_broken_or_missing.return_value = []
            mock_scanner_cls.return_value = mock_scanner

            with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
                result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("no broken lifecycle metadata" in m for m in info_messages), (
            f"Expected INFO about no broken metadata, got: {info_messages}"
        )

    def test_list_repos_exception_returns_zero_and_logs_error(self, caplog):
        """list_repos() raises → ERROR log, return 0, no thread dispatched."""
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.side_effect = RuntimeError("DB down")

        dispatched: list = []

        with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
            mock_threading.Thread.side_effect = (
                lambda **kw: dispatched.append(kw) or MagicMock()
            )

            with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
                result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        error_messages = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert any("list_repos failed" in m for m in error_messages), (
            f"Expected ERROR 'list_repos failed', got: {error_messages}"
        )
        assert len(dispatched) == 0, (
            "Thread must NOT be dispatched when list_repos fails"
        )

    def test_fleet_scan_exception_returns_zero_and_logs_error(self, caplog):
        """Fleet scan raises → ERROR log, return 0, no thread dispatched."""
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.return_value = [
            {"alias": "repo-a"},
            {"alias": "repo-b"},
        ]

        thread_started = []

        with patch(
            "code_indexer.global_repos.lifecycle_batch_runner.LifecycleFleetScanner"
        ) as mock_scanner_cls:
            mock_scanner = MagicMock()
            mock_scanner.find_broken_or_missing.side_effect = RuntimeError(
                "scan exploded"
            )
            mock_scanner_cls.return_value = mock_scanner

            with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
                mock_threading.Thread.side_effect = (
                    lambda **kw: thread_started.append(kw) or MagicMock()
                )

                with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
                    result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 0
        error_messages = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert any("fleet scan failed" in m for m in error_messages), (
            f"Expected ERROR 'fleet scan failed', got: {error_messages}"
        )
        assert len(thread_started) == 0, "Thread must NOT be dispatched when scan fails"


# ---------------------------------------------------------------------------
# Class 3: Async dispatch
# ---------------------------------------------------------------------------


class TestReconcileBrokenLifecycleMetadataDispatch:
    """Verify async thread is dispatched correctly when broken aliases are found."""

    def test_broken_aliases_dispatches_async_thread_and_returns_count(self):
        """
        list_repos returns 3 aliases; scanner returns 2 broken ones.
        Assert: returns 2; threading.Thread called once with daemon=True,
        name='lifecycle-backfill'; .start() called; target is the bound
        method _run_lifecycle_backfill_async; args is the list of broken aliases.
        """
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.return_value = [
            {"alias": "alias-a"},
            {"alias": "alias-b"},
            {"alias": "alias-c"},
        ]
        broken_aliases = ["alias-a", "alias-b"]

        captured_thread_kwargs: dict = {}
        fake_thread_obj = MagicMock()

        def fake_thread(**kwargs):
            captured_thread_kwargs.update(kwargs)
            return fake_thread_obj

        with patch(
            "code_indexer.global_repos.lifecycle_batch_runner.LifecycleFleetScanner"
        ) as mock_scanner_cls:
            mock_scanner = MagicMock()
            mock_scanner.find_broken_or_missing.return_value = list(broken_aliases)
            mock_scanner_cls.return_value = mock_scanner

            with patch(f"{SCHEDULER_MODULE}.threading") as mock_threading:
                mock_threading.Thread.side_effect = fake_thread

                result = sched.reconcile_broken_lifecycle_metadata()

        assert result == 2

        # threading.Thread was called exactly once
        assert mock_threading.Thread.call_count == 1

        # Verify kwargs: daemon=True, name='lifecycle-backfill'
        assert captured_thread_kwargs.get("daemon") is True
        assert captured_thread_kwargs.get("name") == "lifecycle-backfill"

        # target is the bound method _run_lifecycle_backfill_async
        target = captured_thread_kwargs.get("target")
        assert target is not None
        assert target.__func__.__name__ == "_run_lifecycle_backfill_async"
        assert target.__self__ is sched

        # args is a tuple containing a list of the broken aliases
        args = captured_thread_kwargs.get("args")
        assert args is not None
        assert len(args) == 1  # single positional arg — the list
        assert list(args[0]) == broken_aliases

        # .start() was called on the returned thread object
        fake_thread_obj.start.assert_called_once()

    def test_alias_filter_excludes_non_string_and_empty(self):
        """
        list_repos returns mixed entries; only 'good' passes the alias filter.
        Verify LifecycleFleetScanner is constructed with only ['good'].
        """
        sched = _make_scheduler_bare()
        _wire_all(sched)
        sched._golden_backend.list_repos.return_value = [
            {"alias": "good"},
            {"alias": ""},
            {"alias": None},
            {"no_alias_key": True},
            {"alias": 123},
        ]

        with patch(
            "code_indexer.global_repos.lifecycle_batch_runner.LifecycleFleetScanner"
        ) as mock_scanner_cls:
            mock_scanner = MagicMock()
            mock_scanner.find_broken_or_missing.return_value = []
            mock_scanner_cls.return_value = mock_scanner

            with patch(f"{SCHEDULER_MODULE}.threading"):
                sched.reconcile_broken_lifecycle_metadata()

        # Assert LifecycleFleetScanner was constructed with only 'good'
        mock_scanner_cls.assert_called_once()
        call_kwargs = mock_scanner_cls.call_args[1]
        passed_aliases = call_kwargs.get("repo_aliases")
        assert passed_aliases == ["good"], (
            f"Only 'good' should pass the alias filter, got: {passed_aliases}"
        )


# ---------------------------------------------------------------------------
# Class 4: Async worker registration
# ---------------------------------------------------------------------------


class TestRunLifecycleBackfillAsync:
    """Verify _run_lifecycle_backfill_async registers job and invokes runner."""

    def test_async_worker_registers_job_and_invokes_runner(self):
        """
        Direct call to _run_lifecycle_backfill_async(['x', 'y']).
        Assert:
          - job_tracker.register_job called once with operation_type='lifecycle_backfill',
            username='system', and a UUID-shaped job_id string.
          - LifecycleBatchRunner constructed with 6 expected kwargs.
          - runner.run called with positional ['x', 'y'] and kwarg parent_job_id=<job_id>.
        """
        sched = _make_scheduler_bare()
        _wire_all(sched)

        mock_job_tracker = MagicMock()
        sched._job_tracker = mock_job_tracker
        sched._tracking_backend = MagicMock()

        captured_job_id: list = []

        def capture_register(**kwargs):
            captured_job_id.append(kwargs.get("job_id"))

        mock_job_tracker.register_job.side_effect = capture_register

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            sched._run_lifecycle_backfill_async(["x", "y"])

        # 1. register_job called once
        mock_job_tracker.register_job.assert_called_once()
        call_kwargs = mock_job_tracker.register_job.call_args[1]

        assert call_kwargs.get("operation_type") == "lifecycle_backfill"
        assert call_kwargs.get("username") == "system"

        # Verify job_id is UUID-shaped
        job_id = call_kwargs.get("job_id")
        assert job_id is not None, "job_id must be passed as kwarg"
        try:
            _uuid_module.UUID(job_id)
        except ValueError:
            pytest.fail(f"job_id is not a valid UUID: {job_id!r}")

        # 2. LifecycleBatchRunner constructed with 6 expected kwargs
        mock_runner_cls.assert_called_once()
        runner_kwargs = mock_runner_cls.call_args[1]
        assert runner_kwargs.get("golden_repos_dir") is sched._golden_repos_dir
        assert runner_kwargs.get("job_tracker") is mock_job_tracker
        assert runner_kwargs.get("refresh_scheduler") is sched._refresh_scheduler
        assert runner_kwargs.get("debouncer") is sched._lifecycle_debouncer
        assert runner_kwargs.get("claude_cli_invoker") is sched._lifecycle_invoker
        assert "tracking_backend" in runner_kwargs

        # 3. runner.run called with positional aliases list and parent_job_id kwarg
        mock_runner.run.assert_called_once()
        run_call = mock_runner.run.call_args
        run_positional = run_call[0]
        run_kwargs = run_call[1]
        assert list(run_positional[0]) == ["x", "y"]
        assert run_kwargs.get("parent_job_id") == job_id

    def test_async_worker_swallows_register_job_exception_and_skips_runner(
        self, caplog
    ):
        sched = _make_scheduler_bare()
        _wire_all(sched)

        mock_job_tracker = MagicMock()
        mock_job_tracker.register_job.side_effect = RuntimeError("simulated")
        sched._job_tracker = mock_job_tracker
        sched._tracking_backend = MagicMock()

        with patch(f"{SCHEDULER_MODULE}.LifecycleBatchRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

            with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
                sched._run_lifecycle_backfill_async(["a", "b"])

        mock_job_tracker.register_job.assert_called_once()
        mock_runner_cls.assert_not_called()
        mock_runner.run.assert_not_called()
        error_messages = [
            r.message for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert any("repair thread failed" in m for m in error_messages), (
            f"Expected ERROR 'repair thread failed', got: {error_messages}"
        )
