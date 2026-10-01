"""
Bug #1950 Part 1: JobTracker / SQLite backend
cleanup_orphaned_jobs_on_startup() must not classify a restart-interrupted
job as a genuine 'failed' job -- see the module docstring in
test_restart_interrupted_jobs_health_1950.py for the full root-cause
writeup (kept there to avoid repeating it in every split file).
"""

from __future__ import annotations

from datetime import timedelta, timezone, datetime
from pathlib import Path

import pytest

from tests.unit.server.repositories._bug1950_test_helpers import (
    SEED_PROGRESS_MID,
    make_sqlite_backend,
)


@pytest.fixture
def sqlite_backend_with_running_job(tmp_path: Path):
    """A real BackgroundJobsSqliteBackend with one 'running' job seeded --
    simulating a job in flight when the server was restarted."""
    backend = make_sqlite_backend(tmp_path)
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    backend.save_job(
        job_id="restart-victim-1",
        operation_type="global_repo_refresh",
        status="running",
        created_at=one_hour_ago,
        started_at=one_hour_ago,
        username="system",
        progress=SEED_PROGRESS_MID,
        repo_alias="typescript-global",
    )
    return backend


def test_cleanup_reclassifies_running_job_away_from_failed(
    sqlite_backend_with_running_job,
) -> None:
    """The restart-orphaned row must not end up status='failed' -- that is
    the exact bucket /health counts toward `degraded` forever."""
    backend = sqlite_backend_with_running_job

    backend.cleanup_orphaned_jobs_on_startup()

    job = backend.get_job("restart-victim-1")
    assert job is not None
    assert job["status"] != "failed", (
        "Bug #1950: a restart-interrupted job must not be classified as a "
        "genuine 'failed' job -- /health counts 'failed' rows forever, "
        "with no time window, making 'degraded' permanent."
    )


def test_cleanup_still_returns_reclaimed_count(
    sqlite_backend_with_running_job,
) -> None:
    """Reclassifying must not stop the sweep from reporting how many rows
    it touched."""
    backend = sqlite_backend_with_running_job
    count = backend.cleanup_orphaned_jobs_on_startup()
    assert count == 1


def test_cleanup_does_not_touch_genuinely_failed_jobs(tmp_path: Path) -> None:
    """A pre-existing genuinely failed row (unrelated to a restart) must
    be left completely alone by the startup sweep."""
    backend = make_sqlite_backend(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    backend.save_job(
        job_id="already-failed",
        operation_type="add_golden_repo",
        status="failed",
        created_at=now,
        completed_at=now,
        username="system",
        progress=SEED_PROGRESS_MID,
        error="Git clone failed: repository not found",
    )

    backend.cleanup_orphaned_jobs_on_startup()

    job = backend.get_job("already-failed")
    assert job is not None
    assert job["status"] == "failed"
    assert job["error"] == "Git clone failed: repository not found"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
