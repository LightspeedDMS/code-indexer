"""
Circuit-breaker tests for DescriptionRefreshScheduler — Issue #1096.

Drives the LIVE failure path:
    refresh_task -> on_refresh_complete(success=False)

and the skip-gate path:
    _run_loop_single_pass -> quarantine check

Tests:
1. counter increments by 1 on each on_refresh_complete(success=False)
2. repo NOT quarantined at threshold-1 failures (still scheduled)
3. repo IS quarantined (skipped) at exactly threshold consecutive failures
4. counter resets to 0 on on_refresh_complete(success=True)
5. quarantined repo whose commit CHANGES is auto-cleared and retried
6. exactly ONE ERROR log on crossing into quarantine; NO additional ERROR on
   subsequent skipped cycles (subsequent skips stay at DEBUG)

Messi Rule #1: real DescriptionRefreshScheduler + real tracking backend
(SQLite tmp).  Only the Claude/LifecycleBatchRunner boundary is stubbed.
get_stale_repos is stubbed (same pattern as test_description_refresh_
integration_1094.py) because GoldenRepoMetadataSqliteBackend has no
upsert_repo API — the real DescriptionRefreshScheduler.get_stale_repos
joins tracking with the golden backend, but for circuit-breaker tests
we only need to control what repos are visible to _run_loop_single_pass.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.description_refresh_scheduler import (
    PROMPT_FAILURE_QUARANTINE_THRESHOLD,
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


class _SyncExecutor:
    """Runs submitted callables immediately and returns a completed Future."""

    def submit(self, fn, *args, **kwargs) -> Future:
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:
        pass


class _StubScheduler:
    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        pass


class _StubDebouncer:
    def signal_dirty(self) -> None:
        pass


class _StubJobTracker:
    def register_job(self, *a: Any, **k: Any) -> None:
        pass

    def update_status(self, *a: Any, **k: Any) -> None:
        pass

    def complete_job(self, *a: Any, **k: Any) -> None:
        pass

    def fail_job(self, *a: Any, **k: Any) -> None:
        pass


class _FailingBatchRunner:
    """Always raises to simulate a lifecycle refresh failure."""

    def __init__(self, error_msg: str = "simulated lifecycle failure") -> None:
        self.error_msg = error_msg
        self.call_count = 0

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        raise RuntimeError(self.error_msg)


class _SucceedingBatchRunner:
    """Always returns successfully."""

    def __init__(self) -> None:
        self.call_count = 0

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.call_count += 1


class _ConfigManager:
    class _Cfg:
        class claude_integration_config:
            max_concurrent_claude_cli = 1
            description_refresh_interval_hours = 24

    def load_config(self) -> Any:
        return self._Cfg()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_metadata(repo_path: Path, commit: str) -> None:
    """Write a minimal metadata-voyage.json with a given commit hash."""
    ci_dir = repo_path / ".code-indexer"
    ci_dir.mkdir(parents=True, exist_ok=True)
    meta = ci_dir / "metadata-voyage-code-3.json"
    meta.write_text(json.dumps({"current_commit": commit, "files_processed": 1}))


def _write_existing_md(meta_dir: Path, alias: str) -> None:
    """Write a non-empty cidx-meta/<alias>.md so _has_existing_description passes."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(
        "---\nlast_analyzed: 2024-01-01\n---\n# Repo\n\nA description.\n"
    )


def _build_scheduler(
    tmp_path: Path,
    alias: str,
    batch_runner_factory,
    *,
    stale_commit: Optional[str] = None,
) -> DescriptionRefreshScheduler:
    """
    Build a real DescriptionRefreshScheduler backed by real SQLite tracking.

    Uses the same pattern as test_description_refresh_integration_1094.py:
    - real DescriptionRefreshTrackingBackend (so on_refresh_complete writes real rows)
    - get_stale_repos is stubbed per-test via _set_stale (controlled list)
    - calculate_next_run is stubbed to a far-future ISO string

    The batch runner factory is called on each refresh attempt so tests can
    swap between failing and succeeding runners between calls.
    """
    repo_path = tmp_path / "repos" / alias
    repo_path.mkdir(parents=True, exist_ok=True)
    meta_dir = tmp_path / "cidx-meta"

    db_path = tmp_path / "tracking.db"

    # Initialize the real schema so description_refresh_tracking table exists
    DatabaseSchema(str(db_path)).initialize_database()

    sched = object.__new__(DescriptionRefreshScheduler)

    # Real SQLite tracking backend so on_refresh_complete persists correctly
    tracking = DescriptionRefreshTrackingBackend(str(db_path))

    sched._tracking_backend = tracking
    sched._golden_backend = MagicMock()  # not used when get_stale_repos is stubbed
    sched._golden_repos_dir = tmp_path / "repos"
    sched._meta_dir = meta_dir
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()
    sched._shutdown_event = threading.Event()
    sched._prompt_failure_counts = defaultdict(int)
    sched._executor = _SyncExecutor()
    sched._claude_cli_manager = object()  # truthy: enables refresh branch
    sched._lifecycle_invoker = None
    sched._lifecycle_debouncer = _StubDebouncer()
    sched._refresh_scheduler = _StubScheduler()
    sched._job_tracker = _StubJobTracker()
    sched._config_manager = _ConfigManager()

    # Patch _run_lifecycle_via_batch_runner to use the factory
    def _run_lifecycle(alias_arg: str, job_id: Any) -> None:
        runner = batch_runner_factory()
        runner.run(alias_arg)

    sched._run_lifecycle_via_batch_runner = _run_lifecycle  # type: ignore[method-assign]

    # Patch _has_existing_description to always return True
    sched._has_existing_description = lambda a: True  # type: ignore[method-assign]

    # Stub calculate_next_run (no need to hit the real config math)
    sched.calculate_next_run = lambda a: "2099-01-01T00:00:00+00:00"  # type: ignore[method-assign]

    # Stub get_stale_repos; tests override this via _set_stale(sched, ...)
    sched.get_stale_repos = lambda: []  # type: ignore[method-assign]

    return sched


def _set_stale(
    sched: DescriptionRefreshScheduler,
    alias: str,
    clone_path: str,
    *,
    last_known_commit: Optional[str] = "abc123",
) -> None:
    """Override get_stale_repos to return one repo entry."""
    entry: Dict[str, Any] = {
        "repo_alias": alias,
        "clone_path": clone_path,
        "last_known_commit": last_known_commit,
        "last_run": None,
        "next_run": None,
        "status": "completed",
        "error": None,
    }
    sched.get_stale_repos = lambda: [entry]  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Test 1: counter increments on each on_refresh_complete(success=False)
# ---------------------------------------------------------------------------


class TestCounterIncrementsOnFailure:
    def test_counter_increments_by_one_per_failure(self, tmp_path: Path) -> None:
        """Each on_refresh_complete(success=False) increments the counter by 1."""
        alias = "repo-a"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        sched = _build_scheduler(tmp_path, alias, lambda: _FailingBatchRunner())

        assert sched._prompt_failure_counts[alias] == 0

        sched.on_refresh_complete(alias, str(repo_path), success=False)
        assert sched._prompt_failure_counts[alias] == 1

        sched.on_refresh_complete(alias, str(repo_path), success=False)
        assert sched._prompt_failure_counts[alias] == 2

        sched.on_refresh_complete(alias, str(repo_path), success=False)
        assert sched._prompt_failure_counts[alias] == 3

    def test_counter_starts_at_zero_for_fresh_alias(self, tmp_path: Path) -> None:
        """A brand-new alias has a counter of 0 (defaultdict behavior)."""
        alias = "repo-new"
        sched = _build_scheduler(tmp_path, alias, lambda: _FailingBatchRunner())
        assert sched._prompt_failure_counts[alias] == 0


# ---------------------------------------------------------------------------
# Test 2: repo NOT quarantined at threshold-1 failures
# ---------------------------------------------------------------------------


class TestNotQuarantinedBelowThreshold:
    def test_repo_still_dispatched_at_threshold_minus_one(self, tmp_path: Path) -> None:
        """At threshold-1 failures the repo is still dispatched (not skipped)."""
        alias = "repo-b"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "commit-1")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        calls: List[str] = []

        def _runner_factory():
            runner = MagicMock()
            runner.run = lambda a: calls.append(a)
            return runner

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Inject threshold-1 failures directly (bypass run_loop for control)
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD - 1

        _set_stale(sched, alias, str(repo_path), last_known_commit="old-commit")

        sched._run_loop_single_pass()

        assert len(calls) == 1, (
            f"Expected 1 dispatch at threshold-1 failures, got {len(calls)}"
        )


# ---------------------------------------------------------------------------
# Test 3: repo IS quarantined at exactly threshold consecutive failures
# ---------------------------------------------------------------------------


class TestQuarantinedAtThreshold:
    def test_repo_skipped_when_count_equals_threshold(self, tmp_path: Path) -> None:
        """When failure count == threshold, _run_loop_single_pass skips the repo."""
        alias = "repo-c"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "commit-x")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        calls: List[str] = []

        def _runner_factory():
            runner = MagicMock()
            runner.run = lambda a: calls.append(a)
            return runner

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Set count to exactly threshold
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Repo has same commit in tracking and on disk — no change
        _set_stale(sched, alias, str(repo_path), last_known_commit="commit-x")

        sched._run_loop_single_pass()

        assert len(calls) == 0, (
            f"Expected repo to be quarantined (0 dispatches), got {len(calls)}"
        )

    def test_end_to_end_quarantine_after_n_real_failures(self, tmp_path: Path) -> None:
        """
        Drive N consecutive real failures end-to-end through _run_loop_single_pass.
        After threshold failures, the repo is quarantined and no further dispatches occur.
        """
        alias = "repo-d"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        # Use None last_known_commit so has_changes_since_last_run returns True each time
        _write_metadata(repo_path, "sha-001")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        dispatch_count = 0

        def _runner_factory():
            nonlocal dispatch_count
            dispatch_count += 1
            return _FailingBatchRunner()

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Run threshold passes — each should fire, fail, and increment counter
        for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        assert (
            sched._prompt_failure_counts[alias] == PROMPT_FAILURE_QUARANTINE_THRESHOLD
        )
        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # One more pass: should NOT dispatch (quarantined, same commit)
        _set_stale(sched, alias, str(repo_path), last_known_commit="sha-001")
        sched._run_loop_single_pass()

        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD, (
            "Quarantined repo must not be dispatched on subsequent passes"
        )


# ---------------------------------------------------------------------------
# Test 4: counter resets to 0 on success
# ---------------------------------------------------------------------------


class TestCounterResetsOnSuccess:
    def test_success_resets_counter_to_zero(self, tmp_path: Path) -> None:
        """on_refresh_complete(success=True) resets the counter to 0."""
        alias = "repo-e"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)

        sched = _build_scheduler(tmp_path, alias, lambda: _SucceedingBatchRunner())

        # Inject some failures
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

        sched.on_refresh_complete(alias, str(repo_path), success=True)

        assert sched._prompt_failure_counts[alias] == 0

    def test_success_allows_rescheduling(self, tmp_path: Path) -> None:
        """After reset-on-success the repo can be dispatched again."""
        alias = "repo-f"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "sha-fresh")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        calls: List[str] = []

        def _runner_factory():
            runner = MagicMock()
            runner.run = lambda a: calls.append(a)
            return runner

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Start quarantined
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Verify quarantined (same commit in tracking and on disk)
        _set_stale(sched, alias, str(repo_path), last_known_commit="sha-fresh")
        sched._run_loop_single_pass()
        assert len(calls) == 0

        # Reset via success
        sched.on_refresh_complete(alias, str(repo_path), success=True)
        assert sched._prompt_failure_counts[alias] == 0

        # Now should dispatch again (different commit to trigger has_changes)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-commit")
        sched._run_loop_single_pass()
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Test 5: quarantined repo auto-cleared on commit change
# ---------------------------------------------------------------------------


class TestAutoClearOnCommitChange:
    def test_quarantined_repo_retried_when_commit_changes(self, tmp_path: Path) -> None:
        """
        A quarantined repo whose current metadata commit differs from
        last_known_commit is un-quarantined and dispatched.
        """
        alias = "repo-g"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_existing_md(tmp_path / "cidx-meta", alias)

        # Write metadata with a NEW commit (different from what tracking records)
        _write_metadata(repo_path, "new-commit-after-fix")

        calls: List[str] = []

        def _runner_factory():
            runner = MagicMock()
            runner.run = lambda a: calls.append(a)
            return runner

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Quarantine the repo
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Tracking says last_known_commit is the OLD commit (different from disk)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-broken-commit")

        sched._run_loop_single_pass()

        assert len(calls) == 1, (
            "Quarantined repo with changed commit must be un-quarantined and retried"
        )
        assert sched._prompt_failure_counts[alias] == 0, (
            "Counter must be reset when auto-cleared by commit change"
        )

    def test_quarantined_repo_stays_skipped_when_commit_unchanged(
        self, tmp_path: Path
    ) -> None:
        """
        A quarantined repo whose commit has NOT changed stays quarantined.
        """
        alias = "repo-h"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_existing_md(tmp_path / "cidx-meta", alias)

        # Same commit in metadata AND in tracking
        same_commit = "same-sha-abc"
        _write_metadata(repo_path, same_commit)

        calls: List[str] = []

        def _runner_factory():
            runner = MagicMock()
            runner.run = lambda a: calls.append(a)
            return runner

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Quarantine with same commit
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

        _set_stale(sched, alias, str(repo_path), last_known_commit=same_commit)

        sched._run_loop_single_pass()

        assert len(calls) == 0, (
            "Quarantined repo with unchanged commit must remain quarantined"
        )
        assert (
            sched._prompt_failure_counts[alias] == PROMPT_FAILURE_QUARANTINE_THRESHOLD
        )


# ---------------------------------------------------------------------------
# Test 6: exactly ONE ERROR log on quarantine entry, not on subsequent skips
# ---------------------------------------------------------------------------


class TestSingleErrorLogOnQuarantineEntry:
    def test_error_logged_exactly_once_at_quarantine_threshold(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Exactly one ERROR is emitted when failure count reaches threshold.
        No additional ERROR on subsequent quarantined cycles.
        """
        alias = "repo-i"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "sha-err")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        sched = _build_scheduler(tmp_path, alias, lambda: _FailingBatchRunner())

        # Bring counter to threshold-1 without logging path
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD - 1

        scheduler_logger = "code_indexer.server.services.description_refresh_scheduler"

        with caplog.at_level(logging.DEBUG, logger=scheduler_logger):
            # This failure brings count to exactly threshold -> should log ERROR
            sched.on_refresh_complete(
                alias, str(repo_path), success=False, result={"error": "test error"}
            )

        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and alias in r.message
        ]
        assert len(error_records) == 1, (
            f"Expected exactly 1 ERROR log on quarantine entry, got {len(error_records)}: "
            f"{[r.message for r in error_records]}"
        )

        # Subsequent skips should not emit additional ERROR logs
        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger=scheduler_logger):
            _set_stale(sched, alias, str(repo_path), last_known_commit="sha-err")
            sched._run_loop_single_pass()

        subsequent_errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and alias in r.message
        ]
        assert len(subsequent_errors) == 0, (
            f"Expected 0 ERROR logs on quarantine skip, got {len(subsequent_errors)}: "
            f"{[r.message for r in subsequent_errors]}"
        )

    def test_no_error_logged_below_threshold(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No ERROR is logged on failures below quarantine threshold."""
        alias = "repo-j"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)

        sched = _build_scheduler(tmp_path, alias, lambda: _FailingBatchRunner())

        scheduler_logger = "code_indexer.server.services.description_refresh_scheduler"

        # Fire threshold-1 failures
        with caplog.at_level(logging.DEBUG, logger=scheduler_logger):
            for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD - 1):
                sched.on_refresh_complete(
                    alias, str(repo_path), success=False, result={"error": "err"}
                )

        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and alias in r.message
        ]
        assert len(error_records) == 0, (
            f"Expected 0 ERROR logs below threshold, got {len(error_records)}"
        )
