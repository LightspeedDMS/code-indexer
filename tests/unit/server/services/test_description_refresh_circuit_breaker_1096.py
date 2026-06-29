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


class _DeferringExecutor:
    """
    Queues submitted callables without running them.

    Used by dedup tests so the job stays in 'pending' state in the DB while a
    second scheduler attempts to register the same repo — exactly modeling the
    real multi-worker scenario where worker-2 dispatches before worker-1's
    background thread has completed.
    """

    def __init__(self) -> None:
        self.queued: list = []

    def submit(self, fn, *args, **kwargs) -> Future:
        self.queued.append((fn, args, kwargs))
        fut: Future = Future()
        fut.set_result(None)  # placeholder — task not yet run
        return fut

    def run_all(self) -> None:
        """Run all queued tasks; exceptions propagate to surface real failures."""
        for fn, args, kwargs in self.queued:
            fn(*args, **kwargs)
        self.queued.clear()

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
    sched._failure_commit = {}  # type: ignore[attr-defined]
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

        # Quarantine the repo — set both the counter AND the failure fingerprint
        # (what on_refresh_complete would have recorded at the time of failure).
        # The auto-clear gate compares _failure_commit[alias] against the CURRENT
        # on-disk fingerprint; last_known_commit in the tracking record is no longer
        # the decision criterion.
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD
        sched._failure_commit[alias] = (
            "old-broken-commit"  # fingerprint at failure time
        )

        # Disk has a NEW commit ("new-commit-after-fix"), so auto-clear should fire
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

        # Quarantine with same commit — set both counter AND failure fingerprint so
        # the gate sees no transition (current on-disk == failure_commit) and holds.
        sched._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD
        sched._failure_commit[alias] = same_commit  # fingerprint at failure time = same

        _set_stale(sched, alias, str(repo_path), last_known_commit=same_commit)

        sched._run_loop_single_pass()

        assert len(calls) == 0, (
            "Quarantined repo with unchanged commit must remain quarantined"
        )
        assert (
            sched._prompt_failure_counts[alias] == PROMPT_FAILURE_QUARANTINE_THRESHOLD
        )


# ---------------------------------------------------------------------------
# Tests 7-8: NULL last_known_commit must NOT bypass quarantine (Bug #1096 review fix)
#
# The original bug: has_changes_since_last_run returns True when last_known_commit
# is None (the #1094 revert). But the failure branch never writes last_known_commit.
# So a repo that NEVER succeeds keeps last_known_commit=NULL forever, which means
# has_changes returns True every cycle, auto-clear fires, and quarantine NEVER binds.
#
# The fix: track the on-disk commit fingerprint at failure time in _failure_commit,
# and compare the CURRENT on-disk fingerprint against it — not against last_known_commit.
# ---------------------------------------------------------------------------


class TestQuarantineBindsForNullLastKnownCommit:
    def test_quarantine_holds_with_null_marker_stable_commit(
        self, tmp_path: Path
    ) -> None:
        """
        CRITICAL: quarantine must HOLD when last_known_commit=NULL and the
        on-disk commit has been STABLE since failures started.

        This is the exact case the code-reviewer's repro broke:
        - repo never succeeded -> last_known_commit stays NULL
        - has_changes_since_last_run(NULL) == True
        - old code: auto-clear gate fires, counter resets, dispatches EVERY cycle
        - correct behavior: compare current on-disk commit to _failure_commit[alias],
          they are the same, so quarantine HOLDS.
        """
        alias = "repo-null-stable"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "stable-sha-never-changes")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        dispatch_count = 0

        def _runner_factory():
            nonlocal dispatch_count
            dispatch_count += 1
            return _FailingBatchRunner()

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Drive threshold failures end-to-end (last_known_commit=None — never succeeded)
        for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        assert (
            sched._prompt_failure_counts[alias] == PROMPT_FAILURE_QUARANTINE_THRESHOLD
        )
        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Now: subsequent cycles with NULL marker + SAME on-disk commit — must NOT dispatch
        for extra_cycle in range(5):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()
            assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD, (
                f"Quarantined repo dispatched on extra cycle {extra_cycle + 1} "
                f"with NULL last_known_commit and stable on-disk commit — "
                f"quarantine MUST hold"
            )

    def test_quarantine_holds_across_many_cycles_null_marker(
        self, tmp_path: Path
    ) -> None:
        """
        Quarantine must HOLD for at least 10 subsequent cycles when
        last_known_commit=NULL and on-disk commit does not change.
        """
        alias = "repo-null-hold"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "sha-frozen")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        dispatch_count = 0

        def _runner_factory():
            nonlocal dispatch_count
            dispatch_count += 1
            return _FailingBatchRunner()

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Reach quarantine
        for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        pre_count = dispatch_count
        # 10 more cycles — none should dispatch
        for _ in range(10):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        assert dispatch_count == pre_count, (
            f"Dispatch count grew from {pre_count} to {dispatch_count} — "
            f"quarantine did not hold with NULL marker"
        )


class TestAutoClearOnRealCommitChange:
    def test_quarantine_clears_when_on_disk_commit_changes(
        self, tmp_path: Path
    ) -> None:
        """
        Auto-clear must fire when the on-disk commit GENUINELY changes after
        quarantine — independent of whether last_known_commit is NULL or not.
        """
        alias = "repo-real-change"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "sha-broken-code")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        dispatch_count = 0

        def _runner_factory():
            nonlocal dispatch_count
            dispatch_count += 1
            return _FailingBatchRunner()

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Reach quarantine with NULL last_known_commit (never succeeded)
        for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Confirm quarantined (same commit on disk, NULL in tracking)
        _set_stale(sched, alias, str(repo_path), last_known_commit=None)
        sched._run_loop_single_pass()
        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD, (
            "Must still be quarantined before commit change"
        )

        # NOW: developer pushed a fix — on-disk commit changes
        _write_metadata(repo_path, "sha-fixed-code")

        # Next cycle should auto-clear and dispatch
        _set_stale(sched, alias, str(repo_path), last_known_commit=None)
        sched._run_loop_single_pass()

        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD + 1, (
            "After real on-disk commit change, quarantined repo must be "
            "auto-cleared and dispatched"
        )
        # The auto-clear resets the counter to 0, then the new dispatch (which also
        # fails because we still use _FailingBatchRunner) increments it back to 1.
        # Counter == 1 proves: (a) reset happened (auto-clear fired), and (b) the new
        # failure was properly recorded. Counter > threshold would mean quarantine was
        # NOT reset and the old count persisted — that would be the bug.
        assert sched._prompt_failure_counts[alias] == 1, (
            "Failure counter must be reset to 0 on auto-clear, then re-incremented "
            "to 1 by the new (also-failing) dispatch"
        )

    def test_quarantine_auto_clear_uses_disk_commit_not_tracking_marker(
        self, tmp_path: Path
    ) -> None:
        """
        The auto-clear decision must use the CURRENT on-disk commit fingerprint,
        NOT the last_known_commit from the tracking record (which is NULL for
        repos that never succeeded).
        """
        alias = "repo-disk-vs-tracking"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True)
        _write_metadata(repo_path, "sha-v1")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        dispatch_count = 0
        success_runner_used = [False]

        def _runner_factory():
            nonlocal dispatch_count
            dispatch_count += 1
            if success_runner_used[0]:
                return _SucceedingBatchRunner()
            return _FailingBatchRunner()

        sched = _build_scheduler(tmp_path, alias, _runner_factory)

        # Quarantine via NULL-marker failures
        for _ in range(PROMPT_FAILURE_QUARANTINE_THRESHOLD):
            _set_stale(sched, alias, str(repo_path), last_known_commit=None)
            sched._run_loop_single_pass()

        # Verify quarantine holds with stable commit
        _set_stale(sched, alias, str(repo_path), last_known_commit=None)
        sched._run_loop_single_pass()
        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD

        # Change on-disk commit -> should auto-clear
        _write_metadata(repo_path, "sha-v2")
        success_runner_used[0] = True  # next dispatch will succeed

        _set_stale(sched, alias, str(repo_path), last_known_commit=None)
        sched._run_loop_single_pass()

        assert dispatch_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD + 1, (
            "Auto-clear must fire based on on-disk commit change, "
            "not on NULL last_known_commit"
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


# ---------------------------------------------------------------------------
# Story #1162: Cross-worker dedup via register_job_if_no_conflict
# ---------------------------------------------------------------------------


def _build_scheduler_with_real_job_tracker(
    tmp_path: Path,
    alias: str,
    batch_runner_factory,
    *,
    db_path: Path,
) -> tuple:
    """
    Thin wrapper around _build_scheduler that replaces the stub _job_tracker
    with a real JobTracker backed by db_path AND swaps in a _DeferringExecutor.

    The _DeferringExecutor keeps submitted tasks queued (not yet run) so the
    registered job stays in 'pending' state in the DB while a second scheduler
    attempts to claim the same repo — exactly the multi-worker race window.

    Returns (sched, deferred_executor) so tests can call executor.run_all()
    after both schedulers have attempted registration.
    """
    from code_indexer.server.services.job_tracker import JobTracker

    # Build with the stub tracker first (establishes all other real dependencies)
    sched = _build_scheduler(tmp_path, alias, batch_runner_factory)

    # Re-initialize the caller-provided shared db_path for the real JobTracker.
    DatabaseSchema(str(db_path)).initialize_database()

    # Swap stub tracker for a real one backed by the shared db_path
    sched._job_tracker = JobTracker(str(db_path))  # type: ignore[attr-defined]

    # Swap the sync executor for a deferring one so the job stays pending
    # when the second scheduler runs (simulates real async background thread).
    deferred_executor = _DeferringExecutor()
    sched._executor = deferred_executor  # type: ignore[attr-defined]

    return sched, deferred_executor


class TestCrossWorkerDedup1162:
    """
    AC1/AC2: Two schedulers sharing a real SQLite DB must dispatch exactly one
    lifecycle invocation for the same stale repo via register_job_if_no_conflict
    + idx_active_job_per_repo (no in-process lock — DB is sole arbiter).

    AC3: DuplicateJobError must log at DEBUG ("already claimed by another worker")
    and continue — it must NOT emit 'JobTracker registration failed' WARNING.
    """

    def test_two_schedulers_dispatch_exactly_one_invocation(
        self, tmp_path: Path
    ) -> None:
        """Two schedulers sharing one DB dispatch exactly one lifecycle invocation."""
        alias = "repo-dedup-1162"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-dedup")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        db_path = tmp_path / "shared.db"
        invocation_count = 0

        def _runner_factory() -> Any:
            nonlocal invocation_count
            invocation_count += 1
            runner = MagicMock()
            runner.run = lambda a: None
            return runner

        sched1, exec1 = _build_scheduler_with_real_job_tracker(
            tmp_path, alias, _runner_factory, db_path=db_path
        )
        sched2, exec2 = _build_scheduler_with_real_job_tracker(
            tmp_path, alias, _runner_factory, db_path=db_path
        )
        _set_stale(sched1, alias, str(repo_path), last_known_commit="old-sha")
        _set_stale(sched2, alias, str(repo_path), last_known_commit="old-sha")

        # Both schedulers attempt registration while the job is still pending
        sched1._run_loop_single_pass()
        sched2._run_loop_single_pass()

        # Now run the deferred tasks to count actual lifecycle invocations
        exec1.run_all()
        exec2.run_all()

        assert invocation_count == 1, (
            f"Expected exactly 1 lifecycle invocation across two schedulers, "
            f"got {invocation_count}. DB dedup gate must prevent double-dispatch."
        )

    def test_duplicate_does_not_emit_registration_failed_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DuplicateJobError must log DEBUG (skip), not WARNING 'registration failed'."""
        alias = "repo-dedup-warn-1162"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-warn")
        _write_existing_md(tmp_path / "cidx-meta", alias)
        db_path = tmp_path / "warn.db"

        def _runner_factory() -> Any:
            runner = MagicMock()
            runner.run = lambda a: None
            return runner

        sched1, exec1 = _build_scheduler_with_real_job_tracker(
            tmp_path, alias, _runner_factory, db_path=db_path
        )
        sched2, exec2 = _build_scheduler_with_real_job_tracker(
            tmp_path, alias, _runner_factory, db_path=db_path
        )
        _set_stale(sched1, alias, str(repo_path), last_known_commit="old-sha")
        _set_stale(sched2, alias, str(repo_path), last_known_commit="old-sha")

        scheduler_logger = "code_indexer.server.services.description_refresh_scheduler"
        with caplog.at_level(logging.DEBUG, logger=scheduler_logger):
            sched1._run_loop_single_pass()
            sched2._run_loop_single_pass()

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "registration failed" in r.message
        ]
        assert len(warning_records) == 0, (
            f"DuplicateJobError must NOT produce 'registration failed' WARNING. "
            f"Got: {[r.message for r in warning_records]}"
        )

        debug_skip = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and alias in r.message
            and any(
                kw in r.message
                for kw in ("already claimed", "another worker", "skipping")
            )
        ]
        assert len(debug_skip) >= 1, (
            f"Expected DEBUG skip log for {alias}. "
            f"DEBUG: {[r.message for r in caplog.records if r.levelno == logging.DEBUG]}"
        )
        exec1.run_all()
        exec2.run_all()


class TestSingleWorkerRegression1162:
    """AC3: Single scheduler dispatches exactly once — no regression from the change."""

    def test_single_scheduler_dispatches_exactly_once(self, tmp_path: Path) -> None:
        alias = "repo-single-1162"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-single")
        _write_existing_md(tmp_path / "cidx-meta", alias)
        db_path = tmp_path / "single.db"
        invocation_count = 0

        def _runner_factory() -> Any:
            nonlocal invocation_count
            invocation_count += 1
            runner = MagicMock()
            runner.run = lambda a: None
            return runner

        sched, executor = _build_scheduler_with_real_job_tracker(
            tmp_path, alias, _runner_factory, db_path=db_path
        )
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-sha")
        sched._run_loop_single_pass()
        executor.run_all()

        assert invocation_count == 1, (
            f"Single scheduler must dispatch exactly 1 invocation, got {invocation_count}"
        )
