"""
Bug #1563 scope-boundary regression guard.

SyncJobsSqliteBackend.cleanup_orphaned_jobs_on_startup was NOT given the
same worker-pid-liveness fix as BackgroundJobsSqliteBackend /
BackgroundJobsPostgresBackend: the `sync_jobs` table has no owning-node
or owning-worker identity column at all, so there is nothing to check
liveness against without a schema change (storage/database_manager.py)
and a caller change (jobs/manager.py's SyncJobManager) -- both outside
this bug fix's authorized file scope. Only a documentation comment was
added to that method.

This test proves that comment-only edit did NOT alter behavior: the
method still unconditionally fails every running/pending sync job on
every call, exactly as before Bug #1563.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def backend(tmp_path: Path) -> Generator:
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import SyncJobsSqliteBackend

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    created = SyncJobsSqliteBackend(str(db_path))
    try:
        yield created
    finally:
        created.close()


@pytest.mark.slow
def test_cleanup_still_fails_every_running_and_pending_job_unconditionally(
    backend,
) -> None:
    """No node/worker scoping exists for sync_jobs -- every running or
    pending row is still reclaimed on every call, unconditionally."""
    now_iso = datetime.now(timezone.utc).isoformat()

    backend.create_job(
        job_id="sync-running-1",
        username="admin",
        user_alias="repo-a",
        job_type="pull",
        status="running",
    )
    backend.create_job(
        job_id="sync-pending-1",
        username="admin",
        user_alias="repo-b",
        job_type="pull",
        status="pending",
    )
    backend.create_job(
        job_id="sync-completed-1",
        username="admin",
        user_alias="repo-c",
        job_type="pull",
        status="completed",
    )
    backend.update_job("sync-completed-1", completed_at=now_iso)

    cleaned = backend.cleanup_orphaned_jobs_on_startup()

    assert cleaned == 2
    for job_id in ("sync-running-1", "sync-pending-1"):
        job = backend.get_job(job_id)
        assert job is not None
        assert job["status"] == "failed"
        assert job["error_message"] == "Job interrupted by server restart"

    untouched = backend.get_job("sync-completed-1")
    assert untouched is not None
    assert untouched["status"] == "completed"
