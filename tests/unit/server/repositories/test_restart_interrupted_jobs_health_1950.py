"""
Bug #1950: restart-interrupted jobs must not sit in the `failed` bucket
forever, poisoning /health's `degraded` computation permanently.

Root cause (traced from `/health`'s "N failed jobs detected" message in
server/routers/inline_misc.py, which reads
`background_job_manager.get_failed_job_count()` -- an UNBOUNDED, all-time
`COUNT(*) ... WHERE status='failed'` with no time window and no distinction
between a restart artifact and a genuine failure):

1. `JobTracker.cleanup_orphaned_jobs_on_startup()` / the SQLite + PostgreSQL
   `BackgroundJobsBackend.cleanup_orphaned_jobs_on_startup()` implementations
   marked stale running/pending rows `status='failed'`.
2. `BackgroundJobManager.fail_orphaned_jobs()` (in-memory AND its SQLite/PG
   backend counterpart) did the same.
3. `BackgroundJobManager._execute_job()`'s generic exception handler marked
   ANY exception FAILED, including the RuntimeError refresh_scheduler.py
   raises when a `cidx index` subprocess is killed by SIGTERM during a
   server shutdown/restart.

Fix: all three sites now write a distinct terminal status, "interrupted",
instead of "failed" -- so `get_failed_job_count()` (which counts ONLY
status='failed') naturally converges to 0 once these restart artifacts are
reclassified, while a GENUINE failure still writes status='failed' and
still trips /health's degraded computation.

This module covers the core acceptance criterion end to end through
BackgroundJobManager (the real server-startup path, service_init.py):
get_failed_job_count() is the EXACT scalar /health reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

import pytest

from tests.unit.server.repositories._bug1950_test_helpers import (
    SEED_PROGRESS_PARTIAL,
    make_sqlite_backend,
    utc_now_iso,
)

_RESTART_JOB_PROGRESS = 10


@pytest.fixture
def manager_factory(tmp_path: Path):
    """Factory fixture: builds a BackgroundJobManager over a fresh SQLite
    DB pre-seeded with one job, tracking every manager it builds so they
    are all shut down at teardown -- shared by every test in this module
    to avoid repeating the seed+construct+cleanup boilerplate."""
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )

    built: List[BackgroundJobManager] = []

    def _build(
        db_name: str,
        *,
        job_id: str,
        operation_type: str,
        status: str,
        progress: int,
        error: Optional[str] = None,
    ) -> BackgroundJobManager:
        db_path = tmp_path / db_name
        seed_backend = make_sqlite_backend(tmp_path, db_name)
        now = utc_now_iso()
        seed_backend.save_job(
            job_id=job_id,
            operation_type=operation_type,
            status=status,
            created_at=now,
            started_at=now if status == "running" else None,
            completed_at=now if status in ("failed", "completed") else None,
            username="system",
            progress=progress,
            error=error,
        )
        manager = BackgroundJobManager(use_sqlite=True, db_path=str(db_path))
        built.append(manager)
        return manager

    yield _build

    for manager in built:
        manager.shutdown()


def test_health_failed_job_count_converges_to_zero_after_restart(
    manager_factory: Callable,
) -> None:
    """After BackgroundJobManager loads (running its startup orphan-cleanup
    sweep), get_failed_job_count() must be 0 for a node whose only DB
    history is a restart-interrupted job."""
    manager = manager_factory(
        "health_convergence.db",
        job_id="restart-victim-health",
        operation_type="hnsw_orphan_repair_sweep",
        status="running",
        progress=_RESTART_JOB_PROGRESS,
    )

    assert manager.get_failed_job_count() == 0, (
        "Bug #1950: /health computes degraded from get_failed_job_count() "
        "with NO time window -- a restart artifact must never be counted "
        "here, or 'degraded' never converges."
    )


def test_genuinely_failed_job_still_counts_toward_degraded(
    manager_factory: Callable,
) -> None:
    """A job that failed for a REAL reason (unrelated to a restart) must
    still be counted -- the fix must not weaken genuine failure reporting."""
    manager = manager_factory(
        "genuine_failure.db",
        job_id="genuinely-failed-1",
        operation_type="add_golden_repo",
        status="failed",
        progress=SEED_PROGRESS_PARTIAL,
        error="Git clone failed: repository not found",
    )

    assert manager.get_failed_job_count() >= 1, (
        "A genuinely failed job must still make /health report 'degraded' "
        "-- Bug #1950's fix must never weaken real failure reporting."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
