"""
Unit tests for Bug #1348: JobTracker.update_status / _upsert_job reverts a
terminal job row back to "running" (same out-of-lock commit-ordering race as
Bug #1344, but on the JobTracker persist path, which #1344 did NOT cover).

ROOT CAUSE (see issue #1348):
    JobTracker.update_status() snapshots the in-memory job UNDER self._lock
    (status=job.status), then persists the snapshot OUTSIDE the lock via
    _upsert_job(snapshot). For a progress-only call (status=None), job.status
    is unchanged (stays "running"), so the snapshot still carries
    status="running". _upsert_job() writes this unconditionally via BOTH
    the backend.update_job(...) call and a raw SQLite UPDATE. A concurrent
    terminal write (cancel_job/complete_job/fail_job) can be persisted
    first, and the stale "running" snapshot can then land AFTER it,
    reverting the row's status back to "running" forever.

FIX CONTRACT (tested here, mirrors #1344 exactly):
1.  job_tracker.py's own _TERMINAL_JOB_STATUSES constant must match the one
    already used by both storage backends exactly (it was previously
    missing "completed_partial").
2.  _upsert_job's backend.update_job(...) call passes
    guard_terminal_status=True, so a stale non-terminal write is a no-op on
    an already-terminal row (backend path: BackgroundJobsSqliteBackend).
3.  _upsert_job's raw SQLite UPDATE path applies the same conditional guard
    (AND status NOT IN (<terminal>)) when the new job.status is itself
    non-terminal; a terminal job.status is written unconditionally.
4.  A genuine (never-terminal) running progress update must still persist
    "running" normally -- the guard must only block reverting an
    ALREADY-terminal row, not ordinary progress persistence.
5.  Terminal writes (complete_job/fail_job/cancel_job) must always land,
    even onto a row that is currently "running".

Anti-mock rule: every test drives the REAL JobTracker against a REAL
temporary SQLite database (raw path: JobTracker(db_path) with no backend;
backend path: JobTracker(db_path, storage_backend=BackgroundJobsSqliteBackend)),
mirroring tests/unit/server/repositories/test_background_jobs_cancel_1344.py
and tests/unit/server/services/test_job_tracker_update_dedup_1256.py. No
mocks of the code under test.
"""

from __future__ import annotations

import sqlite3

import pytest

from code_indexer.server.services.job_tracker import JobTracker, TrackedJob
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

pytestmark = pytest.mark.slow


@pytest.fixture
def db_path(tmp_path):
    """Real production background_jobs schema (raw-SQLite path, no backend).

    Self-contained (does not rely on tests/unit/server/services/conftest.py's
    same-named fixture) so this file's fixtures are unambiguous on their own.
    """
    path = str(tmp_path / "jobs_raw_1348.db")
    DatabaseSchema(db_path=path).initialize_database()
    return path


@pytest.fixture
def tracker(db_path):
    """JobTracker with storage_backend=None -- exercises the raw SQLite
    _upsert_job UPDATE path directly (no backend indirection)."""
    return JobTracker(db_path=db_path)


def _make_sqlite_backed_tracker(db_path: str) -> JobTracker:
    """Real SQLite-backed JobTracker via the full production schema + a real
    BackgroundJobsSqliteBackend (exercises the backend.update_job() path).

    Mirrors _make_sqlite_backed_tracker in
    test_job_tracker_update_dedup_1256.py.
    """
    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    backend = BackgroundJobsSqliteBackend(db_path)
    return JobTracker(db_path=db_path, storage_backend=backend)


def _snapshot_like_update_status(job: TrackedJob) -> TrackedJob:
    """Build a TrackedJob snapshot exactly as update_status() does internally
    (job_tracker.py lines ~505-520) -- used to reproduce the adverse commit
    ordering deterministically instead of relying on real thread timing."""
    return TrackedJob(
        job_id=job.job_id,
        operation_type=job.operation_type,
        status=job.status,
        username=job.username,
        repo_alias=job.repo_alias,
        progress=job.progress,
        progress_info=job.progress_info,
        metadata=job.metadata,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
        result=job.result,
    )


# ---------------------------------------------------------------------------
# 0. Terminal-status constant parity (issue #1348 explicitly calls this out)
# ---------------------------------------------------------------------------


class TestTerminalStatusConstantMatchesBackends:
    def test_job_tracker_terminal_statuses_match_sqlite_backend(self):
        from code_indexer.server.services.job_tracker import (
            _TERMINAL_JOB_STATUSES as jt_terminal,
        )
        from code_indexer.server.storage.sqlite_backends import (
            _TERMINAL_JOB_STATUSES as sqlite_terminal,
        )

        assert set(jt_terminal) == set(sqlite_terminal), (
            "job_tracker.py's _TERMINAL_JOB_STATUSES must match the SQLite "
            "backend's exactly (Bug #1348) -- a divergent copy means the "
            "raw-SQLite guard and the backend guard disagree on what "
            "'terminal' means for the SAME background_jobs table"
        )

    def test_job_tracker_terminal_statuses_match_postgres_backend(self):
        from code_indexer.server.services.job_tracker import (
            _TERMINAL_JOB_STATUSES as jt_terminal,
        )
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            _TERMINAL_JOB_STATUSES as pg_terminal,
        )

        assert set(jt_terminal) == set(pg_terminal), (
            "job_tracker.py's _TERMINAL_JOB_STATUSES must match the "
            "PostgreSQL backend's exactly (Bug #1348)"
        )


# ---------------------------------------------------------------------------
# 1. Raw-SQLite path (JobTracker with storage_backend=None)
# ---------------------------------------------------------------------------


class TestRawSqlitePathStaleRunningNeverRevertsTerminal:
    def test_stale_running_snapshot_after_terminal_write_does_not_revert_status(
        self, tracker, db_path
    ):
        """Reproduces Bug #1348 on the raw-SQLite _upsert_job UPDATE path.

        Sequence (mirrors the real race deterministically):
        1. update_status(status="running")'s in-lock snapshot is captured
           after a later progress bump -- this is the snapshot that would
           normally be written to the DB by the caller's thread.
        2. A concurrent cancel_job() reaches its terminal write first and
           persists status="cancelled".
        3. The STALE snapshot from step 1 is now persisted late (simulating
           the delayed outside-lock commit of the caller's thread finally
           landing on the DB).

        The persisted row's status must remain "cancelled" -- the stale
        "running" write must be a no-op on the status column.
        """
        job_id = "job-1348-raw-race"
        tracker.register_job(job_id, "test_op_1348", "testuser")
        tracker.update_status(job_id, status="running")

        with tracker._lock:
            job = tracker._active_jobs[job_id]
            job.progress = 90
            stale_snapshot = _snapshot_like_update_status(job)

        assert stale_snapshot.status == "running"

        # Concurrent terminal write lands first (the normal, expected order).
        tracker.cancel_job(job_id)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "cancelled"

        # The stale snapshot's write now lands late (the actual race).
        tracker._upsert_job(stale_snapshot)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "cancelled", (
            "Bug #1348: a stale 'running' snapshot persisted after a "
            "terminal 'cancelled' write must NEVER revert the persisted "
            "status column back to 'running' (raw SQLite UPDATE path)"
        )

    def test_genuine_running_progress_update_still_persists_running(
        self, tracker, db_path
    ):
        """Guard: an ordinary progress tick on a genuinely still-running job
        must still persist 'running' -- the guard must not block normal
        progress persistence, only reverting an already-terminal row."""
        job_id = "job-1348-raw-genuine"
        tracker.register_job(job_id, "test_op_1348", "testuser")
        tracker.update_status(job_id, status="running")
        tracker.update_status(job_id, progress=55)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, progress FROM background_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "running"
        assert row[1] == 55

    def test_terminal_complete_write_lands_onto_previously_running_row(
        self, tracker, db_path
    ):
        """complete_job's terminal write (new status IS terminal) must
        always land unconditionally, even though the persisted row is
        currently 'running' -- the guard must only apply to non-terminal
        new-status writes."""
        job_id = "job-1348-raw-complete"
        tracker.register_job(job_id, "test_op_1348", "testuser")
        tracker.update_status(job_id, status="running")

        tracker.complete_job(job_id, result={"ok": True})

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "completed"


# ---------------------------------------------------------------------------
# 2. Backend path (JobTracker with a real BackgroundJobsSqliteBackend)
# ---------------------------------------------------------------------------


class TestBackendPathStaleRunningNeverRevertsTerminal:
    def test_stale_running_snapshot_after_terminal_write_does_not_revert_status(
        self, tmp_path
    ):
        db_path = str(tmp_path / "jobs_backend_1348_race.db")
        tracker = _make_sqlite_backed_tracker(db_path)

        job_id = "job-1348-backend-race"
        tracker.register_job_if_no_conflict(
            job_id, "test_op_1348", "system", repo_alias="repo-1348-a"
        )
        tracker.update_status(job_id, status="running")

        with tracker._lock:
            job = tracker._active_jobs[job_id]
            job.progress = 90
            stale_snapshot = _snapshot_like_update_status(job)

        assert stale_snapshot.status == "running"

        # Concurrent terminal write lands first.
        tracker.cancel_job(job_id)

        persisted_after_terminal = tracker._backend.get_job(job_id)
        assert persisted_after_terminal is not None
        assert persisted_after_terminal["status"] == "cancelled"

        # The stale snapshot's write now lands late (the actual race).
        tracker._upsert_job(stale_snapshot)

        final = tracker._backend.get_job(job_id)
        assert final is not None
        assert final["status"] == "cancelled", (
            "Bug #1348: a stale 'running' snapshot persisted via the "
            "backend update_job() path after a terminal cancel write must "
            "never revert the persisted status column back to 'running'"
        )

    def test_genuine_running_progress_update_still_persists_running(self, tmp_path):
        db_path = str(tmp_path / "jobs_backend_1348_genuine.db")
        tracker = _make_sqlite_backed_tracker(db_path)

        job_id = "job-1348-backend-genuine"
        tracker.register_job_if_no_conflict(
            job_id, "test_op_1348", "system", repo_alias="repo-1348-b"
        )
        tracker.update_status(job_id, status="running")
        tracker.update_status(job_id, progress=55)

        persisted = tracker._backend.get_job(job_id)
        assert persisted is not None
        assert persisted["status"] == "running"
        assert persisted["progress"] == 55

    def test_terminal_complete_write_lands_onto_previously_running_row(self, tmp_path):
        db_path = str(tmp_path / "jobs_backend_1348_complete.db")
        tracker = _make_sqlite_backed_tracker(db_path)

        job_id = "job-1348-backend-complete"
        tracker.register_job_if_no_conflict(
            job_id, "test_op_1348", "system", repo_alias="repo-1348-c"
        )
        tracker.update_status(job_id, status="running")

        tracker.complete_job(job_id, result={"ok": True})

        persisted = tracker._backend.get_job(job_id)
        assert persisted is not None
        assert persisted["status"] == "completed"
