"""
Tests for Bug #1256: benign idx_active_job_per_repo race on update_status(running)
must log at DEBUG (no traceback), not WARNING+traceback.

ROOT CAUSE:
    BackgroundJobManager._execute_job calls
    self._job_tracker.update_status(job_id, status="running") after a job
    transitions to RUNNING. On a multi-node cluster restart, this UPDATE can
    collide with the partial unique index idx_active_job_per_repo
    (UNIQUE(operation_type, repo_alias) WHERE status IN ('pending','running')
    AND repo_alias IS NOT NULL) when a stale row for the same
    (operation_type, repo_alias) singleton key is already active. This raises
    a genuine sqlite3.IntegrityError (SQLite) / psycopg.errors.UniqueViolation
    (PostgreSQL) that bubbles into the generic `except Exception:` handler and
    is logged at WARNING with a full traceback on every occurrence — even
    though the job still executes correctly either way. This pollutes logs
    and fails the Story #1122 log-audit gate.

FIX CONTRACT (tested here):
1.  is_active_job_unique_violation(exc) classifies IntegrityError/
    UniqueViolation-shaped exceptions as True, everything else False.
2.  A genuine sqlite3.IntegrityError from the real idx_active_job_per_repo
    constraint (triggered end-to-end via JobTracker.update_status) is
    classified True by the helper.
3.  BackgroundJobManager._execute_job logs the benign case at DEBUG (no
    exc_info / traceback) and still executes the job's work function.
4.  An unrelated exception from update_status still logs at WARNING with a
    traceback (Messi #13 anti-silent-failure — no regression).

Anti-mock rule: real SQLite DB (DatabaseSchema.initialize_database() +
BackgroundJobsSqliteBackend) is used to trigger genuine constraint
violations. No mocking of the code under test.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import (
    JobTracker,
    is_active_job_unique_violation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_sqlite_integrity_error() -> sqlite3.IntegrityError:
    """Trigger a genuine sqlite3.IntegrityError via a real unique-constraint
    violation (not hand-constructed), for fidelity to the actual bug shape.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (x INTEGER UNIQUE)")
        conn.execute("INSERT INTO t VALUES (1)")
        try:
            conn.execute("INSERT INTO t VALUES (1)")
        except sqlite3.IntegrityError as exc:
            return exc
        raise AssertionError("expected sqlite3.IntegrityError was not raised")
    finally:
        conn.close()


class _MinimalUniqueViolation(Exception):
    """Stand-in for psycopg.errors.UniqueViolation.

    psycopg is not a project dependency guaranteed to be importable in the
    unit-test environment (it is only present when the PostgreSQL extra is
    installed), so a minimal local class with the exact same __name__ is
    used to prove the helper's classification is driver-agnostic and based
    purely on exception class name, matching the existing INSERT-path
    precedent in _atomic_insert_impl (Bug #1252/#1235).
    """


_MinimalUniqueViolation.__name__ = "UniqueViolation"


def _make_sqlite_backed_tracker(db_path: str) -> JobTracker:
    """Real SQLite-backed JobTracker via the full production schema.

    Mirrors tests/unit/server/storage/test_background_jobs_active_unique_index.py:
    uses DatabaseSchema.initialize_database() so the exact production
    idx_active_job_per_repo partial unique index is present, then wires a
    real BackgroundJobsSqliteBackend + JobTracker on top of it.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    backend = BackgroundJobsSqliteBackend(db_path)
    return JobTracker(db_path=db_path, storage_backend=backend)


def _insert_background_job_row(
    db_path: str,
    job_id: str,
    operation_type: str,
    status: str,
    repo_alias: Optional[str],
    username: str = "system",
) -> None:
    """Insert one minimal background_jobs row directly (bypasses JobTracker)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, created_at, username, repo_alias)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                operation_type,
                status,
                "2026-06-30T00:00:00Z",
                username,
                repo_alias,
            ),
        )
        conn.commit()


class _RaisingTrackerWrapper:
    """Test double that wraps a REAL JobTracker and raises a specific
    exception only from update_status(status="running"), delegating every
    other call (register_job, register_job_if_no_conflict, complete_job,
    fail_job, etc.) unchanged to the real tracker.

    This is not a mock of the code under test: register/complete/fail all
    execute against the real SQLite-backed tracker. Only the single
    update_status(running) call site is intercepted, exactly matching the
    scenario the bug describes (a real exception raised from that call).
    """

    def __init__(self, real_tracker: JobTracker, exc_to_raise: BaseException):
        self._real = real_tracker
        self._exc = exc_to_raise
        self.update_status_running_calls = 0

    def update_status(self, job_id: str, status: Optional[str] = None, **kwargs: Any):
        if status == "running":
            self.update_status_running_calls += 1
            raise self._exc
        return self._real.update_status(job_id, status=status, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _wait_for_terminal_status(
    manager: BackgroundJobManager, job_id: str, timeout_seconds: float = 5.0
):
    """Bounded poll for job completion (Messi #14: provable termination bound).

    Fails loudly via AssertionError if the job never reaches a terminal
    status within the bound, rather than hanging or silently passing.
    """
    from code_indexer.server.repositories.background_jobs import JobStatus

    terminal = {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = manager.jobs.get(job_id)
        if job is not None and job.status in terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach a terminal status within "
        f"{timeout_seconds}s (bounded poll expired)"
    )


def _make_manager_with_wrapped_tracker(
    db_path: str, exc_to_raise: BaseException
) -> tuple[BackgroundJobManager, _RaisingTrackerWrapper]:
    real_tracker = _make_sqlite_backed_tracker(db_path)
    wrapper = _RaisingTrackerWrapper(real_tracker, exc_to_raise)
    manager = BackgroundJobManager(storage_path=None)
    manager._job_tracker = wrapper  # type: ignore[assignment]
    return manager, wrapper


# ---------------------------------------------------------------------------
# 1. Unit tests for is_active_job_unique_violation
# ---------------------------------------------------------------------------


class TestIsActiveJobUniqueViolation:
    def test_real_sqlite_integrity_error_classified_true(self):
        exc = _real_sqlite_integrity_error()
        assert is_active_job_unique_violation(exc) is True

    def test_unique_violation_class_name_classified_true(self):
        exc = _MinimalUniqueViolation("duplicate key value violates unique constraint")
        assert is_active_job_unique_violation(exc) is True

    def test_runtime_error_classified_false(self):
        assert (
            is_active_job_unique_violation(RuntimeError("db connection lost")) is False
        )

    def test_value_error_classified_false(self):
        assert is_active_job_unique_violation(ValueError("bad input")) is False


# ---------------------------------------------------------------------------
# 2. Real end-to-end reproduction of the actual bug via real SQLite
# ---------------------------------------------------------------------------


class TestRealEndToEndReproduction:
    def test_update_status_running_raises_real_integrity_error_on_collision(
        self, tmp_path: Path
    ):
        """
        Pre-seed job A as the active occupant of the (operation_type,
        repo_alias) singleton slot. Job B, registered into the tracker's
        in-memory map with a TERMINAL DB status (so the initial INSERT does
        not collide), then has update_status(status="running") called on
        it. This must raise the same real sqlite3.IntegrityError the bug
        describes because the UPDATE now conflicts with job A's active row.
        """
        db_path = str(tmp_path / "jobs.db")
        tracker = _make_sqlite_backed_tracker(db_path)

        operation_type = "reap_activated_repos"
        repo_alias = "server"

        # Job A occupies the active slot.
        _insert_background_job_row(
            db_path,
            job_id="job-a",
            operation_type=operation_type,
            status="running",
            repo_alias=repo_alias,
        )
        # Job B exists with a terminal status — does not collide at INSERT time.
        _insert_background_job_row(
            db_path,
            job_id="job-b",
            operation_type=operation_type,
            status="completed",
            repo_alias=repo_alias,
        )

        # Register job B into the tracker's in-memory active-jobs map so
        # update_status does not early-return (mirrors a job the tracker
        # believes is still active/pending in memory).
        from code_indexer.server.services.job_tracker import TrackedJob

        job_b = TrackedJob(
            job_id="job-b",
            operation_type=operation_type,
            status="completed",
            username="system",
            repo_alias=repo_alias,
        )
        with tracker._lock:
            tracker._active_jobs["job-b"] = job_b

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            tracker.update_status("job-b", status="running")

        assert is_active_job_unique_violation(exc_info.value) is True


# ---------------------------------------------------------------------------
# 3. BackgroundJobManager._execute_job DEBUG-not-WARNING behavior
# ---------------------------------------------------------------------------


class TestExecuteJobBenignRaceLogging:
    def test_benign_unique_violation_logs_debug_not_warning_and_job_still_runs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        db_path = str(tmp_path / "jobs.db")
        exc = _real_sqlite_integrity_error()
        manager, wrapper = _make_manager_with_wrapped_tracker(db_path, exc)

        executed = threading.Event()

        def work() -> Dict[str, Any]:
            executed.set()
            return {"success": True}

        try:
            with caplog.at_level(logging.DEBUG):
                job_id = manager.submit_job(
                    "reap_activated_repos",
                    work,
                    submitter_username="system",
                    is_admin=True,
                    repo_alias="server",
                )
                job = _wait_for_terminal_status(manager, job_id)

            assert executed.is_set(), (
                "job work function must still execute when update_status(running) "
                "raises a benign unique-violation"
            )
            from code_indexer.server.repositories.background_jobs import JobStatus

            assert job.status == JobStatus.COMPLETED
            assert wrapper.update_status_running_calls == 1

            debug_records = [
                r
                for r in caplog.records
                if r.levelno == logging.DEBUG and job_id in r.getMessage()
            ]
            assert debug_records, (
                f"expected a DEBUG record mentioning job_id {job_id}; "
                f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
            )
            assert any(
                (
                    "benign" in r.getMessage().lower()
                    or "dedup" in r.getMessage().lower()
                )
                for r in debug_records
            ), (
                "DEBUG record must mention 'benign' or 'dedup' so operators can "
                f"identify it as the known idx_active_job_per_repo race: {debug_records}"
            )
            for r in debug_records:
                assert r.exc_info is None, (
                    "DEBUG record for the benign race must NOT carry a traceback "
                    f"(exc_info must be None); got exc_info={r.exc_info}"
                )

            warning_records = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and job_id in r.getMessage()
            ]
            assert not warning_records, (
                f"no WARNING record should be logged for job {job_id} on the "
                f"benign race path; got: {[r.getMessage() for r in warning_records]}"
            )
        finally:
            manager.shutdown()


class TestExecuteJobUnrelatedExceptionStillWarns:
    def test_unrelated_exception_logs_warning_with_traceback_and_job_still_runs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        db_path = str(tmp_path / "jobs.db")
        exc = RuntimeError("db connection lost")
        manager, wrapper = _make_manager_with_wrapped_tracker(db_path, exc)

        executed = threading.Event()

        def work() -> Dict[str, Any]:
            executed.set()
            return {"success": True}

        try:
            with caplog.at_level(logging.DEBUG):
                job_id = manager.submit_job(
                    "reap_activated_repos",
                    work,
                    submitter_username="system",
                    is_admin=True,
                    repo_alias="server",
                )
                job = _wait_for_terminal_status(manager, job_id)

            assert executed.is_set(), (
                "job work function must still execute even when update_status(running) "
                "raises an unrelated exception (no execution-behavior change, Messi #13)"
            )
            from code_indexer.server.repositories.background_jobs import JobStatus

            assert job.status == JobStatus.COMPLETED
            assert wrapper.update_status_running_calls == 1

            warning_records = [
                r
                for r in caplog.records
                if r.levelno == logging.WARNING and job_id in r.getMessage()
            ]
            assert warning_records, (
                f"expected a WARNING record mentioning job_id {job_id} for an "
                f"unrelated exception; got records: "
                f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
            )
            assert any(r.exc_info is not None for r in warning_records), (
                "WARNING record for an unrelated exception must carry a traceback "
                f"(exc_info must not be None); got: "
                f"{[r.exc_info for r in warning_records]}"
            )
        finally:
            manager.shutdown()
