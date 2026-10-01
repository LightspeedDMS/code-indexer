"""
Regression tests for Bug #1839: a claimed global_repo_refresh job fails its
own dedup check by matching itself.

Root cause (traced end to end, confirmed against the real production code):

  1. DistributedJobWorkerService._process_one_job() claims job X for repo Y
     via DistributedJobClaimer.claim_next_job() -- that claim's UPDATE marks
     X's row 'running' in background_jobs, and that row IS the
     idx_active_job_per_repo slot for (operation_type, repo_alias) =
     ("global_repo_refresh", Y).
  2. _execute_retryable_job() called RefreshScheduler.trigger_refresh_for_repo(Y).
  3. In server mode (background_job_manager set) trigger_refresh_for_repo is
     SUBMISSION-ONLY: it calls _submit_refresh_job(), which calls
     BackgroundJobManager.submit_job(operation_type="global_repo_refresh",
     repo_alias=Y, ...) -- a SECOND registration attempt for the SAME
     (operation_type, repo_alias) pair.
  4. submit_job's atomic gate (register_job_if_no_conflict / the
     idx_active_job_per_repo partial unique index) correctly rejects the
     second registration -- but the row it collides with is job X's OWN row.
     DuplicateJobError.existing_job_id == X, i.e. the message names itself.
  5. The DuplicateJobError propagates out of _execute_retryable_job();
     _process_one_job()'s except block calls claimer.fail_job(X, ...) --
     the refresh never runs. 100% failure rate for this path.

Fix: the worker, having ALREADY claimed the row that legitimately occupies
the dedup slot, must perform the refresh WORK directly via
RefreshScheduler.execute_refresh_for_claimed_job() (which calls
_execute_refresh(..., tracked_by_caller=True) -- exactly what the normal
BackgroundJobManager-submitted job's worker closure runs) instead of
re-entering the SUBMISSION path.

This file exercises the REAL BackgroundJobManager + JobTracker + SQLite
schema (including the real idx_active_job_per_repo partial unique index and
the real DuplicateJobError) so the RED failure is driven by the actual
production dedup mechanism, not a simulated stand-in. Only RefreshScheduler
itself (heavy git/filesystem machinery, unrelated to this bug) is replaced
by a small test double that mirrors trigger_refresh_for_repo's real
submission contract using the real BackgroundJobManager.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    DuplicateJobError,
)
from code_indexer.server.services.distributed_job_worker import (
    DistributedJobWorkerService,
)
from code_indexer.server.services.job_tracker import JobTracker


# ---------------------------------------------------------------------------
# Real SQLite schema helpers (mirrors test_submit_job_atomic_dedup.py)
# ---------------------------------------------------------------------------


def _create_schema(db_path: str) -> None:
    """Create background_jobs table and idx_active_job_per_repo unique index."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            current_phase TEXT,
            phase_detail TEXT,
            actor_username TEXT,
            progress_info TEXT,
            metadata TEXT,
            executing_node TEXT,
            claimed_at TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()


def _make_manager(db_path: str) -> BackgroundJobManager:
    """Real BackgroundJobManager wired to a real JobTracker on a real DB."""
    tracker = JobTracker(db_path=db_path)
    manager = BackgroundJobManager(storage_path=None)
    manager._job_tracker = tracker  # type: ignore[assignment]
    return manager


def _insert_active_row(
    db_path: str,
    job_id: str,
    operation_type: str,
    repo_alias: str,
    status: str = "running",
) -> None:
    """Directly insert a row occupying the idx_active_job_per_repo slot --
    simulating the job DistributedJobWorkerService has already claimed
    (DistributedJobClaimer.claim_next_job's real UPDATE marks it 'running')."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, created_at, username,
                is_admin, cancelled, repo_alias, resolution_attempts, progress)
               VALUES (?, ?, ?, ?, 'system', 1, 0, ?, 0, 0)""",
            (
                job_id,
                operation_type,
                status,
                datetime.now(timezone.utc).isoformat(),
                repo_alias,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Test double for RefreshScheduler -- mirrors ONLY the submission contract
# (trigger_refresh_for_repo -> _submit_refresh_job -> submit_job) using the
# REAL BackgroundJobManager, so the dedup conflict this test proves is real,
# not simulated. Heavy git/filesystem refresh internals are out of scope.
# ---------------------------------------------------------------------------

ProgressCallback = Callable[..., None]


class _RealisticRefreshSchedulerDouble:
    """Stands in for RefreshScheduler.

    trigger_refresh_for_repo() mirrors the real server-mode branch of
    RefreshScheduler.trigger_refresh_for_repo() exactly: it SUBMITS a new
    job via the real BackgroundJobManager.submit_job(). This is the call
    the CURRENT (buggy) DistributedJobWorkerService makes.

    execute_refresh_for_claimed_job() is the entry point the FIXED
    DistributedJobWorkerService must call instead: it performs the refresh
    work directly, WITHOUT going through submission -- mirroring
    RefreshScheduler.execute_refresh_for_claimed_job()'s real
    tracked_by_caller=True path.
    """

    def __init__(self, background_job_manager: BackgroundJobManager) -> None:
        self._bgm = background_job_manager
        self.claimed_execute_calls: List[Tuple[str, Optional[ProgressCallback]]] = []

    def trigger_refresh_for_repo(
        self, alias_name: str, submitter_username: str = "system"
    ) -> Optional[str]:
        job_id: str = self._bgm.submit_job(
            operation_type="global_repo_refresh",
            func=lambda: {"success": True},
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias_name,
        )
        return job_id

    def execute_refresh_for_claimed_job(
        self, alias_name: str, progress_callback: Optional[ProgressCallback] = None
    ) -> Dict[str, Any]:
        self.claimed_execute_calls.append((alias_name, progress_callback))
        return {"success": True, "alias": alias_name, "message": "refreshed"}


class _FakeClaimer:
    """Mirrors the established FakeClaimer shape from test_distributed_job_worker.py."""

    def __init__(self) -> None:
        self.jobs_to_return: List[Dict[str, str]] = []
        self.completed_jobs: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        self.failed_jobs: List[Tuple[str, str]] = []

    def claim_next_job(
        self,
        job_type: Optional[str] = None,
        *,
        job_types: Optional[List[str]] = None,
        exclude_types: Optional[List[str]] = None,
    ) -> Optional[Dict[str, str]]:
        if self.jobs_to_return:
            return self.jobs_to_return.pop(0)
        return None

    def complete_job(
        self, job_id: str, result: Optional[Dict[str, Any]] = None
    ) -> bool:
        self.completed_jobs.append((job_id, result))
        return True

    def fail_job(self, job_id: str, error: str) -> bool:
        self.failed_jobs.append((job_id, error))
        return True


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "jobs.db")
    _create_schema(path)
    return path


class TestClaimedJobDoesNotFailItsOwnDedupCheck:
    """Bug #1839 AC2: a claimed refresh for a repo with NO OTHER active job
    must complete, not fail with DuplicateJobError naming its own id."""

    def test_claimed_global_repo_refresh_completes_not_duplicate_failed(
        self, db_path: str
    ) -> None:
        manager = _make_manager(db_path)
        scheduler = _RealisticRefreshSchedulerDouble(manager)

        claimed_job_id = "claimed-job-1"
        # The claim itself already occupies the dedup slot -- this row IS
        # job X, exactly as DistributedJobClaimer.claim_next_job's UPDATE
        # would have made it in production.
        _insert_active_row(
            db_path, claimed_job_id, "global_repo_refresh", "my-repo-global"
        )

        claimer = _FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": claimed_job_id,
                "operation_type": "global_repo_refresh",
                "repo_alias": "my-repo-global",
            }
        )
        worker = DistributedJobWorkerService(
            claimer=claimer, refresh_scheduler=scheduler
        )

        worker._process_one_job()

        assert (
            len(claimer.completed_jobs) == 1
            and claimer.completed_jobs[0][0] == claimed_job_id
        ), (
            f"Expected job {claimed_job_id} to complete. "
            f"completed={claimer.completed_jobs} failed={claimer.failed_jobs}"
        )
        assert claimer.failed_jobs == [], (
            "Bug #1839: the claimed job must not fail its own dedup check "
            f"(self-referential DuplicateJobError). failed_jobs={claimer.failed_jobs}"
        )

    def test_fix_calls_execute_not_submit(self, db_path: str) -> None:
        """The fixed worker must call execute_refresh_for_claimed_job, never
        trigger_refresh_for_repo (which re-enters submission)."""
        manager = _make_manager(db_path)
        scheduler = _RealisticRefreshSchedulerDouble(manager)
        claimed_job_id = "claimed-job-2"
        _insert_active_row(
            db_path, claimed_job_id, "global_repo_refresh", "other-repo-global"
        )
        claimer = _FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": claimed_job_id,
                "operation_type": "global_repo_refresh",
                "repo_alias": "other-repo-global",
            }
        )
        worker = DistributedJobWorkerService(
            claimer=claimer, refresh_scheduler=scheduler
        )

        worker._process_one_job()

        assert scheduler.claimed_execute_calls, (
            "Worker did not call execute_refresh_for_claimed_job -- still "
            "using the submission path (trigger_refresh_for_repo)."
        )
        assert scheduler.claimed_execute_calls[0][0] == "other-repo-global"


class TestGenuineDuplicateStillRejected:
    """Bug #1839 AC5: a genuinely different active job for the SAME repo
    must still be rejected -- the fix must not loosen the dedup guard."""

    def test_genuine_concurrent_duplicate_still_raises(self, db_path: str) -> None:
        manager = _make_manager(db_path)
        scheduler = _RealisticRefreshSchedulerDouble(manager)

        other_job_id = "other-node-job-99"
        _insert_active_row(
            db_path, other_job_id, "global_repo_refresh", "shared-repo-global"
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            scheduler.trigger_refresh_for_repo("shared-repo-global")

        assert exc_info.value.existing_job_id == other_job_id
        assert exc_info.value.repo_alias == "shared-repo-global"
