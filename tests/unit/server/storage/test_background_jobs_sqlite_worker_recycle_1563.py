"""
Bug #1563: cleanup_orphaned_jobs_on_startup must not fail a sibling
worker's genuinely-running job merely because a DIFFERENT worker process
on the same node was recycled.

Under `uvicorn --workers N`, every worker runs its own app lifespan, and
each lifespan calls cleanup_orphaned_jobs_on_startup(). Before this fix,
the SQLite backend's implementation marked ALL running/pending jobs as
"failed" unconditionally on every call, with no way to distinguish "a
sibling worker on this node is still alive and genuinely executing this
job" from "the owning process is provably gone" (a real crash/restart).

These tests use a REAL SQLite-backed BackgroundJobsSqliteBackend --
no mocking of the store. Process liveness is proven with REAL OS
processes: the test's own live PID stands in for "a sibling worker is
still alive", and a real subprocess that has been spawned AND reaped via
Popen.wait() stands in for "the owning worker process is provably gone".
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest


def _dead_pid() -> int:
    """Spawn a trivial subprocess, wait for it to exit (reaping it), and
    return its PID. Once reaped, the PID is guaranteed absent from the OS
    process table (barring the accepted, documented PID-reuse residual
    risk noted in production code)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    exit_code = proc.wait()
    assert exit_code == 0, f"helper subprocess exited with {exit_code}"
    return proc.pid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def backend(tmp_path: Path) -> Generator:
    """Create a BackgroundJobsSqliteBackend with an initialized database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    created = BackgroundJobsSqliteBackend(str(db_path))
    try:
        yield created
    finally:
        created.close()


@pytest.mark.slow
class TestBackgroundJobsSqliteWorkerRecycle1563:
    def test_worker_recycle_does_not_fail_sibling_running_job(self, backend) -> None:
        """A job owned by a still-alive sibling worker must survive a
        cleanup sweep triggered by a DIFFERENT (recycled) worker's
        startup, while a job whose owning worker is provably dead is
        still correctly reclaimed."""
        alive_pid = os.getpid()
        dead_pid = _dead_pid()

        backend.atomic_claim_insert(
            job_id="job-sibling-alive",
            operation_type="fleet_migration",
            status="running",
            created_at=_now_iso(),
            username="admin",
            progress=10,
            repo_alias="repo-a",
            executing_node="node-22",
        )
        backend.update_job("job-sibling-alive", executing_pid=alive_pid)

        backend.atomic_claim_insert(
            job_id="job-recycled-worker",
            operation_type="fleet_migration",
            status="running",
            created_at=_now_iso(),
            username="admin",
            progress=10,
            repo_alias="repo-b",
            executing_node="node-22",
        )
        backend.update_job("job-recycled-worker", executing_pid=dead_pid)

        cleaned = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert cleaned == 1

        alive_job = backend.get_job("job-sibling-alive")
        assert alive_job is not None
        assert alive_job["status"] == "running"
        assert alive_job["error"] is None

        dead_job = backend.get_job("job-recycled-worker")
        assert dead_job is not None
        assert dead_job["status"] == "failed"
        assert dead_job["error"] == "Job interrupted by server restart"

    def test_full_node_restart_still_reclaims_orphans(self, backend) -> None:
        """A genuine full-node restart kills EVERY worker process on that
        node, so every recorded owning PID is dead -- this must still
        reclaim all of them, exactly as before this fix."""
        dead_pid_1 = _dead_pid()
        dead_pid_2 = _dead_pid()

        backend.atomic_claim_insert(
            job_id="job-orphan-1",
            operation_type="fleet_migration",
            status="running",
            created_at=_now_iso(),
            username="admin",
            progress=10,
            repo_alias="repo-c",
            executing_node="node-22",
        )
        backend.update_job("job-orphan-1", executing_pid=dead_pid_1)

        backend.atomic_claim_insert(
            job_id="job-orphan-2",
            operation_type="fleet_migration",
            status="pending",
            created_at=_now_iso(),
            username="admin",
            progress=0,
            repo_alias="repo-d",
            executing_node="node-22",
        )
        backend.update_job("job-orphan-2", executing_pid=dead_pid_2)

        cleaned = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert cleaned == 2
        for job_id in ("job-orphan-1", "job-orphan-2"):
            job = backend.get_job(job_id)
            assert job is not None
            assert job["status"] == "failed"
            assert job["error"] == "Job interrupted by server restart"

    def test_never_stamped_pid_still_reclaimed(self, backend) -> None:
        """A row whose executing_pid was never stamped (legacy row from
        before this fix, or a row claimed via a code path this fix does
        not touch) preserves the EXACT pre-fix behavior: unconditionally
        reclaimed, never left running forever."""
        backend.atomic_claim_insert(
            job_id="job-legacy-no-pid",
            operation_type="fleet_migration",
            status="running",
            created_at=_now_iso(),
            username="admin",
            progress=10,
            repo_alias="repo-e",
            executing_node="node-22",
        )
        backend.update_job("job-legacy-no-pid", executing_pid=None)

        cleaned = backend.cleanup_orphaned_jobs_on_startup(node_id="node-22")

        assert cleaned == 1
        job = backend.get_job("job-legacy-no-pid")
        assert job is not None
        assert job["status"] == "failed"


_ARBITRARY_TEST_PID = 12345


def test_owning_worker_process_is_alive_treats_probe_exception_as_alive(
    monkeypatch,
) -> None:
    """If psutil.pid_exists itself raises, the helper must conservatively
    return True (treat as alive / do not fail the job) -- never wrongly
    fail a job whose owner it could not disprove is still running."""
    from code_indexer.server.storage import sqlite_backends

    def _raising_pid_exists(pid: int) -> bool:
        assert pid == _ARBITRARY_TEST_PID
        raise OSError("simulated probe failure")

    monkeypatch.setattr(sqlite_backends.psutil, "pid_exists", _raising_pid_exists)

    assert sqlite_backends._owning_worker_process_is_alive(_ARBITRARY_TEST_PID) is True
