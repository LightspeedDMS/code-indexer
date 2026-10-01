"""
Bug #1950 Part 2: BackgroundJobManager.fail_orphaned_jobs() (the startup
sweep `lifespan.py` calls with error="Orphaned by server restart") must not
classify a restart artifact as a genuine 'failed' job. See
test_restart_interrupted_jobs_health_1950.py for the full root-cause
writeup (kept there to avoid repeating it in every split file).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.unit.server.repositories._bug1950_test_helpers import (
    SEED_PROGRESS_LOW,
    make_sqlite_backend,
    utc_now_iso,
)

_IN_MEMORY_SEED_PROGRESS = 50


def test_backend_fail_orphaned_jobs_does_not_mark_failed(tmp_path: Path) -> None:
    """Exercises BackgroundJobsSqliteBackend.fail_orphaned_jobs() directly
    (the exact method lifespan.py's startup sweep calls via
    background_job_manager.fail_orphaned_jobs(error=...)).  Testing the
    backend directly avoids the confound of
    BackgroundJobManager(use_sqlite=True, ...)'s OWN constructor already
    running cleanup_orphaned_jobs_on_startup() on the same row before
    fail_orphaned_jobs() is ever called."""
    backend = make_sqlite_backend(tmp_path, "fail_orphaned.db")
    now = utc_now_iso()
    backend.save_job(
        job_id="orphan-via-fail-orphaned",
        operation_type="global_repo_refresh",
        status="running",
        created_at=now,
        started_at=now,
        username="system",
        progress=SEED_PROGRESS_LOW,
        repo_alias="typescript-global",
    )

    backend.fail_orphaned_jobs(error="Orphaned by server restart")

    job = backend.get_job("orphan-via-fail-orphaned")
    assert job is not None
    assert job["status"] != "failed", (
        "Bug #1950: fail_orphaned_jobs() must not classify a restart "
        "artifact as a genuine 'failed' job."
    )


def test_manager_in_memory_fail_orphaned_jobs_does_not_mark_failed() -> None:
    """Exercises BackgroundJobManager.fail_orphaned_jobs()'s IN-MEMORY
    marking branch in isolation (no SQLite backend at all -- so there is no
    constructor-level DB sweep to confound this assertion)."""
    from code_indexer.server.repositories.background_jobs import (
        BackgroundJob,
        BackgroundJobManager,
        JobStatus,
    )

    manager = BackgroundJobManager(is_primary_instance=True)
    try:
        job_id = "in-memory-orphan"
        manager.jobs[job_id] = BackgroundJob(
            job_id=job_id,
            operation_type="hnsw_orphan_repair_sweep",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=_IN_MEMORY_SEED_PROGRESS,
            username="system",
        )

        manager.fail_orphaned_jobs(error="Orphaned by server restart")

        assert manager.jobs[job_id].status.value != "failed", (
            "Bug #1950: fail_orphaned_jobs()'s in-memory marking must not "
            "classify a restart artifact as a genuine 'failed' job."
        )
    finally:
        manager.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
