"""
Regression tests for Issue #1258 — JobTracker double-completion benign warning.

Story: LifecycleBatchRunner.run() (post-#1262) is the terminal authority for
the description-refresh parent job_id — it calls complete_job()/fail_job() on
the SAME job_id that description_refresh_scheduler.refresh_task subsequently
passes to on_refresh_complete(). Before this fix, on_refresh_complete's own
complete_job()/fail_job() call was a REDUNDANT second dispatch that always hit
"job <id> not in memory" at WARNING level, because run() had already popped
the job from JobTracker._active_jobs (240 occurrences/36h in production).

Fix: JobTracker.complete_job/fail_job now check persisted DB state when the
in-memory object is absent (see job_tracker.py::_finalize_absent_job). When
the DB row is ALREADY terminal (completed/failed/cancelled) — the benign
double-completion case — it logs DEBUG and returns without further writes
(first-terminal-write wins). When the DB row is NOT terminal (or does not
exist), this is the genuine "pop-before-persist" zombie edge: a direct
terminal DB update is forced and logged at WARNING.

on_refresh_complete's own job_tracker call is intentionally KEPT (not
removed) in description_refresh_scheduler.py, because it is the ONLY
finalization path when LifecycleBatchRunner.run() never reaches its own
terminal transition block — e.g. an exception raised before it
(debouncer.signal_dirty(), compute_sub_batch_size,
dispatch_parallel_with_jitter) or the _run_lifecycle_via_batch_runner
wiring-guard early return (a missing lifecycle collaborator, which the
codebase already treats as a non-exceptional, loudly-logged startup
misconfiguration — see _check_lifecycle_backfill_wiring +
lifespan.py APP-GENERAL-051). Making JobTracker idempotent on the DB row's
terminal state closes the loud 240x/36h warning flood while PRESERVING this
safety net; blind removal of on_refresh_complete's call would silently
reintroduce a zombie job whenever LifecycleBatchRunner.run() does not reach
its own terminal block. This file proves the fix end-to-end through the
REAL DescriptionRefreshScheduler + REAL JobTracker, stubbing only the
LifecycleBatchRunner boundary (already covered by
tests/unit/global_repos/test_lifecycle_batch_runner.py) with a fake that
mimics run()'s real terminal-authority contract exactly.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


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


class _ConfigManager:
    class _Cfg:
        class claude_integration_config:
            max_concurrent_claude_cli = 1
            description_refresh_interval_hours = 24

    def load_config(self) -> Any:
        return self._Cfg()


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


def _build_scheduler_with_real_job_tracker(
    tmp_path: Path,
    alias: str,
    run_lifecycle_stub,
) -> DescriptionRefreshScheduler:
    """
    Build a real DescriptionRefreshScheduler wired to a REAL JobTracker (not
    the _StubJobTracker used by test_description_refresh_circuit_breaker_1096
    .py), so on_refresh_complete's job_tracker calls exercise the actual
    complete_job/fail_job absent-fallback logic (Bug #1258).

    run_lifecycle_stub replaces _run_lifecycle_via_batch_runner with a fake
    that mimics the REAL contract exactly: it performs the job_tracker
    terminal transition itself (as LifecycleBatchRunner.run() does post
    #1262) and raises RuntimeError on failure (as the real
    _run_lifecycle_via_batch_runner does when the alias is present in the
    failed dict returned by run()).
    """
    repo_path = tmp_path / "repos" / alias
    repo_path.mkdir(parents=True, exist_ok=True)
    meta_dir = tmp_path / "cidx-meta"
    db_path = tmp_path / "tracking.db"

    DatabaseSchema(str(db_path)).initialize_database()

    sched = object.__new__(DescriptionRefreshScheduler)

    tracking = DescriptionRefreshTrackingBackend(str(db_path))

    sched._tracking_backend = tracking
    sched._golden_backend = None
    sched._golden_repos_dir = tmp_path / "repos"
    sched._meta_dir = meta_dir
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()
    sched._shutdown_event = threading.Event()
    sched._prompt_failure_counts = defaultdict(int)
    sched._executor = _SyncExecutor()
    sched._claude_cli_manager = object()  # truthy: enables refresh branch
    sched._failure_commit = {}  # type: ignore[attr-defined]
    sched._lifecycle_invoker = object()
    sched._lifecycle_debouncer = _StubDebouncer()
    sched._refresh_scheduler = _StubScheduler()
    sched._job_tracker = JobTracker(str(db_path))
    sched._config_manager = _ConfigManager()

    sched._run_lifecycle_via_batch_runner = run_lifecycle_stub  # type: ignore[method-assign]
    sched._has_existing_description = lambda a: True  # type: ignore[method-assign]
    sched.calculate_next_run = lambda a: "2099-01-01T00:00:00+00:00"  # type: ignore[method-assign]
    sched.get_stale_repos = lambda: []  # type: ignore[method-assign]

    return sched


JOB_TRACKER_LOGGER = "code_indexer.server.services.job_tracker"


class TestSuccessfulRefreshNoDoubleCompletionWarning:
    """Deliverable (a): successful desc-refresh no longer logs the benign
    JobTracker 'not in memory' WARNING, because the redundant on_refresh_
    complete call now finds the row already terminal and logs DEBUG."""

    def test_successful_refresh_logs_no_job_tracker_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        alias = "repo-success-1258"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-success")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        def _run_lifecycle(alias_arg: str, job_id: Any) -> None:
            # Mimics LifecycleBatchRunner.run()'s real terminal-authority
            # transition on success (job_tracker.complete_job call at
            # lifecycle_batch_runner.py:639).
            sched._job_tracker.complete_job(
                job_id, result={"phase": "lifecycle", "done": 1, "total": 1}
            )

        sched = _build_scheduler_with_real_job_tracker(tmp_path, alias, _run_lifecycle)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-sha")

        with caplog.at_level(logging.DEBUG, logger=JOB_TRACKER_LOGGER):
            sched._run_loop_single_pass()

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "not in memory" in r.message
        ]
        assert warnings == [], (
            f"Expected zero 'not in memory' WARNINGs on successful refresh, "
            f"got: {[r.message for r in warnings]}"
        )

    def test_successful_refresh_job_ends_completed_with_original_result(
        self, tmp_path: Path
    ) -> None:
        """The redundant on_refresh_complete->complete_job call must not
        clobber the result LifecycleBatchRunner.run() already persisted."""
        alias = "repo-success-result-1258"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-success-2")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        original_result = {"phase": "lifecycle", "done": 1, "total": 1}

        def _run_lifecycle(alias_arg: str, job_id: Any) -> None:
            sched._job_tracker.complete_job(job_id, result=original_result)

        sched = _build_scheduler_with_real_job_tracker(tmp_path, alias, _run_lifecycle)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-sha")

        sched._run_loop_single_pass()

        jobs = sched._job_tracker.query_jobs(operation_type="description_refresh")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["status"] == "completed"
        assert job["result"] == original_result


class TestFailedRefreshPreservesCircuitBreakerIncrement:
    """Deliverable (c): the #1262 circuit-breaker increment on failure STILL
    fires after this fix, and the redundant fail_job call is silent."""

    def test_failed_refresh_logs_no_job_tracker_warning_and_increments_counter(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        alias = "repo-failure-1258"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-failure")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        error_msg = "simulated lifecycle failure"

        def _run_lifecycle(alias_arg: str, job_id: Any) -> None:
            # Mimics LifecycleBatchRunner.run()'s real terminal-authority
            # transition on all-alias failure (fail_job at line 636) followed
            # by _run_lifecycle_via_batch_runner's raise (line 1479).
            sched._job_tracker.fail_job(job_id, error=error_msg)
            raise RuntimeError(error_msg)

        sched = _build_scheduler_with_real_job_tracker(tmp_path, alias, _run_lifecycle)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-sha")

        with caplog.at_level(logging.DEBUG, logger=JOB_TRACKER_LOGGER):
            sched._run_loop_single_pass()

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "not in memory" in r.message
        ]
        assert warnings == [], (
            f"Expected zero 'not in memory' WARNINGs on failed refresh, "
            f"got: {[r.message for r in warnings]}"
        )

        # Bug #1262: on_refresh_complete(success=False) must still increment
        # the quarantine circuit-breaker counter.
        assert sched._prompt_failure_counts[alias] == 1, (
            "Bug #1262 quarantine increment must still fire after the "
            "Bug #1258 JobTracker hardening"
        )

    def test_failed_refresh_job_ends_failed_with_original_error(
        self, tmp_path: Path
    ) -> None:
        """The redundant on_refresh_complete->fail_job call must not
        overwrite the error message LifecycleBatchRunner.run() already
        persisted."""
        alias = "repo-failure-error-1258"
        repo_path = tmp_path / "repos" / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        _write_metadata(repo_path, "sha-failure-2")
        _write_existing_md(tmp_path / "cidx-meta", alias)

        original_error = "original lifecycle error from run()"

        def _run_lifecycle(alias_arg: str, job_id: Any) -> None:
            sched._job_tracker.fail_job(job_id, error=original_error)
            raise RuntimeError(original_error)

        sched = _build_scheduler_with_real_job_tracker(tmp_path, alias, _run_lifecycle)
        _set_stale(sched, alias, str(repo_path), last_known_commit="old-sha")

        sched._run_loop_single_pass()

        jobs = sched._job_tracker.query_jobs(operation_type="description_refresh")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["status"] == "failed"
        assert job["error"] == original_error
