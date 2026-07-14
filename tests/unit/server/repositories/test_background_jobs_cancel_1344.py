"""
Unit tests for BackgroundJobManager commit-ordering race (Bug #1344).

Follow-up to Bug #1342. `BackgroundJobManager.cancel_job()` for a currently
RUNNING job marks `job.cancelled = True` (status stays RUNNING -- the worker
thread is expected to detect cancellation and write the terminal status
itself), then persists a snapshot of that in-memory state via
`_persist_jobs()` -> `_persist_single_job_sqlite()` -> `_persist_job_to_sqlite()`.

The snapshot is taken UNDER `self._lock`, but the actual DB write happens
OUTSIDE the lock (Story #267 Component 4, by design, so SQLite I/O never
blocks the in-memory critical section). This means the write is not
ordered relative to the worker thread's own later terminal-status write:
if the cancel path's outside-lock write for the STALE snapshot
(status="running", cancelled=True) lands on the DB AFTER the worker's
terminal write (status="cancelled"), it reverts the persisted row's status
column back to "running" -- a job that already finished (from the worker's
point of view) appears to be running forever on the dashboard.

These tests reproduce the adverse ordering DETERMINISTICALLY (no reliance
on real thread scheduling / timing, which would be flaky) by driving the
exact production methods (`_snapshot_job`, `_persist_job_to_sqlite`,
`_persist_jobs`) directly against a REAL BackgroundJobManager backed by a
REAL temp SQLite database -- mirroring the `bgm`/`tracker`/`db_path`
fixture conventions from test_background_jobs_cancel_1342.py. No mocks.

Key assertions:
1. A stale "running" snapshot captured before cancellation-detection,
   persisted AFTER the worker's real terminal "cancelled" write, must be a
   no-op on the `status` column -- the terminal status must never revert.
2. A genuine (never-cancelled) running job must still persist as "running"
   -- the fix must not block ordinary progress persistence.
"""

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

pytestmark = pytest.mark.slow


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(temp_dir):
    path = str(Path(temp_dir) / "test_bgm_cancel_1344.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture
def tracker(db_path):
    return JobTracker(db_path)


@pytest.fixture
def bgm(db_path, tracker):
    manager = BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(max_concurrent_background_jobs=10),
        job_tracker=tracker,
    )
    yield manager
    manager.shutdown()


def _make_running_job(job_id: str) -> BackgroundJob:
    now = datetime.now(timezone.utc)
    return BackgroundJob(
        job_id=job_id,
        operation_type="test_op_1344",
        status=JobStatus.RUNNING,
        created_at=now,
        started_at=now,
        completed_at=None,
        result=None,
        error=None,
        progress=10,
        username="admin",
    )


class TestStaleRunningSnapshotNeverRevertsTerminalStatus:
    def test_stale_running_snapshot_after_terminal_write_does_not_revert_status(
        self, bgm
    ):
        """Reproduces Bug #1344's adverse commit ordering.

        Sequence under test (mirrors the real race exactly, but ordered
        deterministically instead of relying on thread timing):

        1. cancel_job()'s in-lock snapshot is taken while the job is still
           RUNNING with cancelled=True just set -- this is the snapshot that
           would normally be written to the DB by the caller's thread.
        2. The worker thread reaches its own terminal write first (this is
           the NORMAL, expected order -- cancellation fires quickly but the
           worker's cleanup + terminal persist can still land first under
           contention) and persists status=CANCELLED.
        3. The STALE snapshot from step 1 is now persisted (simulating the
           delayed outside-lock commit of the caller's thread finally
           landing on the DB).

        The persisted row's status must remain "cancelled" -- the stale
        "running" write must be a no-op on the status column.
        """
        job_id = "job-1344-race"
        job = _make_running_job(job_id)

        with bgm._lock:
            bgm.jobs[job_id] = job
            # cancel_job()'s exact in-lock behavior for a RUNNING job:
            # only `cancelled` flips; `status` is left RUNNING for the
            # worker to finalize.
            job.cancelled = True
            stale_snapshot = bgm._snapshot_job(job)

        assert stale_snapshot["status"] == "running"
        assert stale_snapshot["cancelled"] is True

        # Worker reaches its terminal write first.
        with bgm._lock:
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(timezone.utc)
            terminal_snapshot = bgm._snapshot_job(job)

        assert bgm._persist_job_to_sqlite(job_id, terminal_snapshot) is True

        persisted_after_terminal = bgm._sqlite_backend.get_job(job_id)
        assert persisted_after_terminal is not None
        assert persisted_after_terminal["status"] == "cancelled"

        # The stale snapshot's write lands LATE (the actual race).
        write_result = bgm._persist_job_to_sqlite(job_id, stale_snapshot)
        assert write_result is True, (
            "the stale write must not raise/fail -- it must be a silent "
            "guarded no-op, not an error"
        )

        final = bgm._sqlite_backend.get_job(job_id)
        assert final is not None
        assert final["status"] == "cancelled", (
            "Bug #1344: a stale 'running' snapshot persisted after the "
            "worker's terminal 'cancelled' write must NEVER revert the "
            "persisted status column back to 'running'"
        )


class TestGenuineRunningJobStillPersistsAsRunning:
    def test_genuine_running_job_progress_persist_still_writes_running(self, bgm):
        """Guard test: a job that is actually still running (never
        cancelled, never reached a terminal status) must still persist its
        'running' status normally -- the terminal-status guard must only
        block reverting an ALREADY-terminal row, not ordinary progress
        persistence of a genuinely running job."""
        job_id = "job-1344-genuine-running"
        job = _make_running_job(job_id)

        with bgm._lock:
            bgm.jobs[job_id] = job

        assert bgm._persist_jobs(job_id=job_id) is True

        persisted = bgm._sqlite_backend.get_job(job_id)
        assert persisted is not None
        assert persisted["status"] == "running"

        # A subsequent, still-non-terminal progress tick must also persist.
        with bgm._lock:
            job.progress = 55

        assert bgm._persist_jobs(job_id=job_id) is True

        persisted_after_tick = bgm._sqlite_backend.get_job(job_id)
        assert persisted_after_tick is not None
        assert persisted_after_tick["status"] == "running"
        assert persisted_after_tick["progress"] == 55
