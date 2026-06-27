"""Tests for Bug #1235 — PG multi-worker duplicate-claim race must never raise RuntimeError.

ROOT CAUSE:
    _atomic_insert_or_raise catches a unique-violation (_BackendUniqueViolation),
    then calls _find_blocking_active_job_id().  If the blocking row completes
    between the INSERT and the SELECT (transitions to 'completed'/'failed' and
    falls outside the partial-index predicate), the lookup returns None and
    _atomic_insert_or_raise raises RuntimeError instead of DuplicateJobError.

    DataRetentionScheduler._execute_cleanup only catches DuplicateJobError; the
    RuntimeError escapes and marks the cleanup as failed.

FIX CONTRACT (tested here):
1.  Unique-violation + blocking-row-found -> DuplicateJobError with correct existing_job_id.
2.  Unique-violation + blocking-row-gone (None from lookup) -> DuplicateJobError, NOT RuntimeError.
3.  SQLite single-worker path unchanged: duplicate active insert raises DuplicateJobError.
4.  DataRetentionScheduler._execute_cleanup skips silently when DuplicateJobError is raised
    (including the race case where the blocking row vanished).

NOTE on TEST_POSTGRES_DSN:
    TEST_POSTGRES_DSN is not set in this environment.  Race simulation uses the
    _BackendUniqueViolation marker injected directly via a fake backend, faithfully
    reproducing how the PG path signals a unique-index violation to _atomic_insert_or_raise.
    A real-PG barrier test is skip-gated on TEST_POSTGRES_DSN.
"""

import os
import sqlite3
import threading
import uuid
from contextlib import closing
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.job_tracker import (
    DuplicateJobError,
    JobTracker,
    TrackedJob,
    _BackendUniqueViolation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_tracker(db_path: str) -> JobTracker:
    """Real SQLite-backed JobTracker with the partial unique index."""
    with closing(sqlite3.connect(str(db_path))) as conn:
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
            progress_info TEXT,
            metadata TEXT,
            actor_username TEXT,
            executing_node TEXT,
            claimed_at TEXT
        )"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs (operation_type, repo_alias)
            WHERE status IN ('pending', 'running') AND repo_alias IS NOT NULL"""
        )
        conn.commit()
    return JobTracker(db_path=db_path)


class _FakeBackend:
    """
    Minimal backend that faithfully reproduces the PG atomic_claim_insert contract.

    atomic_claim_insert raises _BackendUniqueViolation (the same marker that
    _atomic_insert_impl translates from psycopg's IntegrityError/UniqueViolation)
    when inject_violation=True.  find_active_job_by_type_and_alias returns
    blocking_job_id (str or None) to simulate the post-violation lookup.
    """

    def __init__(
        self,
        inject_violation: bool = False,
        blocking_job_id: Optional[str] = None,
    ):
        self._inject_violation = inject_violation
        self._blocking_job_id = blocking_job_id

    def atomic_claim_insert(self, **kwargs: Any) -> None:
        if self._inject_violation:
            raise _BackendUniqueViolation(
                "duplicate key value violates unique constraint idx_active_job_per_repo"
            )

    def find_active_job_by_type_and_alias(
        self, operation_type: str, repo_alias: str
    ) -> Optional[str]:
        return self._blocking_job_id

    # Minimal stubs for other backend methods used by JobTracker internals
    def save_job(self, **kwargs: Any) -> None:
        pass

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        pass

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return None

    def list_jobs(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def count_jobs(self, **kwargs: Any) -> int:
        return 0


def _make_backend_tracker(backend: _FakeBackend) -> JobTracker:
    """Create a JobTracker with an injected fake backend (no SQLite)."""
    tracker = JobTracker.__new__(JobTracker)
    tracker._backend = backend
    tracker._conn_manager = None  # type: ignore[assignment]
    active_jobs: Dict[str, TrackedJob] = {}
    tracker._active_jobs = active_jobs
    tracker._lock = threading.Lock()
    return tracker


def _make_job(job_id: str = "test-job") -> TrackedJob:
    return TrackedJob(
        job_id=job_id,
        operation_type="data_retention_cleanup",
        status="pending",
        username="system",
        repo_alias="server",
    )


# ---------------------------------------------------------------------------
# TestUniqueViolationTranslation
# ---------------------------------------------------------------------------


class TestUniqueViolationTranslation:
    """_atomic_insert_or_raise must translate all unique violations into DuplicateJobError."""

    def test_blocking_row_found_raises_duplicate_job_error_with_id(self):
        """Violation fires, blocking row found -> DuplicateJobError with correct existing_job_id."""
        blocking_id = "existing-job-abc"
        tracker = _make_backend_tracker(
            _FakeBackend(inject_violation=True, blocking_job_id=blocking_id)
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker._atomic_insert_or_raise(_make_job("new-job-001"))

        err = exc_info.value
        assert err.existing_job_id == blocking_id
        assert err.operation_type == "data_retention_cleanup"
        assert err.repo_alias == "server"

    def test_blocking_row_gone_raises_duplicate_job_error_not_runtime_error(self):
        """
        Race case: violation fires, blocking row already completed (lookup returns None).

        Before the fix, this raised:
            RuntimeError("atomic insert raised IntegrityError for ... but no active row was found")

        After the fix: DuplicateJobError is raised — the concurrent worker already
        ran the cleanup; the scheduler must skip silently.
        """
        tracker = _make_backend_tracker(
            _FakeBackend(inject_violation=True, blocking_job_id=None)
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker._atomic_insert_or_raise(_make_job("new-job-002"))

        err = exc_info.value
        assert err.operation_type == "data_retention_cleanup"
        assert err.repo_alias == "server"
        # existing_job_id must be a string (sentinel for "concurrent job ran and finished")
        assert isinstance(err.existing_job_id, str)

    def test_runtime_error_does_not_escape_for_none_lookup(self):
        """Explicit regression guard: RuntimeError must not escape when lookup returns None."""
        tracker = _make_backend_tracker(
            _FakeBackend(inject_violation=True, blocking_job_id=None)
        )

        try:
            tracker._atomic_insert_or_raise(_make_job("new-job-003"))
        except DuplicateJobError:
            pass  # correct
        except RuntimeError as e:
            pytest.fail(
                f"RuntimeError must not escape from _atomic_insert_or_raise when "
                f"find_active_job_by_type_and_alias returns None: {e}"
            )


# ---------------------------------------------------------------------------
# TestSqlitePathUnchanged
# ---------------------------------------------------------------------------


class TestSqlitePathUnchanged:
    """SQLite single-worker path must be unchanged by the fix."""

    def test_sqlite_duplicate_raises_duplicate_job_error(self, tmp_path):
        """Two active inserts for same (op, repo_alias) -> second raises DuplicateJobError."""
        tracker = _make_sqlite_tracker(str(tmp_path / "test.db"))

        tracker._atomic_insert_or_raise(_make_job("job-a"))

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker._atomic_insert_or_raise(_make_job("job-b"))

        assert exc_info.value.existing_job_id == "job-a"

    def test_sqlite_completed_job_allows_new_registration(self, tmp_path):
        """After completion, a new insert for the same key must succeed."""
        db_path = str(tmp_path / "test2.db")
        tracker = _make_sqlite_tracker(db_path)

        tracker._atomic_insert_or_raise(_make_job("job-first"))

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE background_jobs SET status='completed' WHERE job_id=?",
                ("job-first",),
            )
            conn.commit()

        # Must not raise — previous job is no longer active
        tracker._atomic_insert_or_raise(_make_job("job-second"))


# ---------------------------------------------------------------------------
# TestDataRetentionSchedulerSkipsOnRace
# ---------------------------------------------------------------------------


class TestDataRetentionSchedulerSkipsOnRace:
    """DataRetentionScheduler._execute_cleanup must skip silently on DuplicateJobError."""

    def test_scheduler_skips_when_blocking_row_is_active(self, tmp_path):
        """A pre-existing active cleanup job -> _execute_cleanup returns without error."""
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        db_path = str(tmp_path / "sched.db")
        tracker = _make_sqlite_tracker(db_path)

        # Pre-register a blocking job
        blocking_job = _make_job(f"pre-existing-{uuid.uuid4().hex[:8]}")
        tracker._atomic_insert_or_raise(blocking_job)
        with tracker._lock:
            tracker._active_jobs[blocking_job.job_id] = blocking_job

        scheduler = DataRetentionScheduler.__new__(DataRetentionScheduler)
        scheduler._job_tracker = tracker
        scheduler._storage_mode = "sqlite"
        scheduler._backend_registry = None
        scheduler._db_path = db_path
        scheduler._retention_days = 30
        scheduler._batch_size = 100
        scheduler._conn = None

        # Must NOT raise — DuplicateJobError is caught and silently skipped
        scheduler._execute_cleanup()

    def test_scheduler_skips_on_race_where_blocking_row_is_gone(self, tmp_path):
        """
        Race: violation fires but blocking row vanished (None from lookup).
        The fix makes this DuplicateJobError; the scheduler must still skip.
        """
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        # Inject a backend that always raises the race-case violation
        race_backend = _FakeBackend(inject_violation=True, blocking_job_id=None)
        tracker = _make_backend_tracker(race_backend)

        scheduler = DataRetentionScheduler.__new__(DataRetentionScheduler)
        scheduler._job_tracker = tracker
        scheduler._storage_mode = "sqlite"
        scheduler._backend_registry = None
        scheduler._db_path = str(tmp_path / "race.db")
        scheduler._retention_days = 30
        scheduler._batch_size = 100
        scheduler._conn = None

        # Must NOT raise RuntimeError — must be treated as a benign skip
        scheduler._execute_cleanup()


# ---------------------------------------------------------------------------
# TestRealPgConcurrencyBarrier (requires TEST_POSTGRES_DSN)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set; skipping real-PG concurrency test",
)
class TestRealPgConcurrencyBarrier:
    """Real PG: two concurrent register_job_if_no_conflict calls — one wins, other is DuplicateJobError."""

    def test_concurrent_claims_deterministic_across_iterations(self):
        """Across 20 barrier iterations: exactly one success, one DuplicateJobError, zero RuntimeErrors."""
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        dsn = os.environ["TEST_POSTGRES_DSN"]
        backend = BackgroundJobsPostgresBackend(dsn)
        results: List[Any] = []
        errors: List[Exception] = []
        barrier = threading.Barrier(2)

        def claim(worker_id: str) -> None:
            try:
                job_id = f"race-{worker_id}-{uuid.uuid4().hex[:8]}"
                tracker = _make_backend_tracker(
                    _FakeBackend(inject_violation=False, blocking_job_id=None)
                )
                tracker._backend = backend
                job = _make_job(job_id)
                barrier.wait()
                tracker._atomic_insert_or_raise(job)
                results.append(("success", job_id))
            except DuplicateJobError as e:
                results.append(("duplicate", e))
            except Exception as e:
                errors.append(e)

        ITERATIONS = 20
        for _ in range(ITERATIONS):
            results.clear()
            errors.clear()

            threads = [
                threading.Thread(target=claim, args=(f"w{i}",)) for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            with backend._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM background_jobs WHERE operation_type = %s AND repo_alias = %s",
                        ("data_retention_cleanup", "server"),
                    )

            assert not errors, f"Unexpected exceptions in iteration: {errors}"
            assert len([r for r in results if r[0] == "success"]) == 1
            assert len([r for r in results if r[0] == "duplicate"]) == 1


# ---------------------------------------------------------------------------
# TestSentinelConstant  (GAP C — Bug #1235 NIT)
# ---------------------------------------------------------------------------


class TestSentinelConstant:
    """job_tracker must export CONCURRENT_COMPLETED_SENTINEL as a module-level constant.

    The magic string "(concurrent-completed)" is used both at the raise site in
    _atomic_insert_or_raise and in test assertions.  A module-level constant
    couples producer and consumer without brittle string duplication.
    """

    def test_sentinel_constant_importable(self):
        """CONCURRENT_COMPLETED_SENTINEL must be importable from job_tracker."""
        from code_indexer.server.services.job_tracker import (
            CONCURRENT_COMPLETED_SENTINEL,
        )

        assert isinstance(CONCURRENT_COMPLETED_SENTINEL, str)
        assert len(CONCURRENT_COMPLETED_SENTINEL) > 0

    def test_sentinel_constant_used_at_raise_site(self):
        """_atomic_insert_or_raise must use CONCURRENT_COMPLETED_SENTINEL (not a magic string)."""
        from code_indexer.server.services.job_tracker import (
            CONCURRENT_COMPLETED_SENTINEL,
        )

        # Race case: violation fires, blocking row already completed (lookup returns None)
        tracker = _make_backend_tracker(
            _FakeBackend(inject_violation=True, blocking_job_id=None)
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker._atomic_insert_or_raise(_make_job("sentinel-test"))

        assert exc_info.value.existing_job_id == CONCURRENT_COMPLETED_SENTINEL, (
            f"Raise site must use CONCURRENT_COMPLETED_SENTINEL constant; "
            f"got {exc_info.value.existing_job_id!r}"
        )


# ---------------------------------------------------------------------------
# TestDeadlockRetryOnUpdateJob  (GAP B — Bug #1235)
# ---------------------------------------------------------------------------


def _make_mock_pg_backend():
    """Return (backend, mock_cur) with BackgroundJobsPostgresBackend wired to a mock pool."""
    from unittest.mock import MagicMock

    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )

    backend = BackgroundJobsPostgresBackend.__new__(BackgroundJobsPostgresBackend)
    backend._pool = MagicMock()

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: mock_cur
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: mock_conn
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    backend._pool.connection.return_value = mock_conn

    return backend, mock_cur


class TestDeadlockRetryOnUpdateJob:
    """BackgroundJobsPostgresBackend.update_job must retry on PG deadlock errors.

    PG deadlocks (SQLSTATE 40P01) on background_jobs UPDATE are transient.
    Retrying up to 3 times with small backoff must produce overall success.
    The SQLite path and non-deadlock errors must not be affected.
    """

    @pytest.fixture
    def deadlock_exc(self):
        """Return a real psycopg DeadlockDetected instance, or skip if unavailable."""
        try:
            import psycopg.errors

            return psycopg.errors.DeadlockDetected("deadlock detected")
        except ImportError:
            pytest.skip("psycopg not installed")

    def test_deadlock_on_first_attempt_succeeds_on_retry(self, deadlock_exc):
        """DeadlockDetected on attempt 1, succeeds on attempt 2 -> overall success, no exception."""
        backend, mock_cur = _make_mock_pg_backend()

        attempt = {"n": 0}

        def execute_side_effect(sql, params):
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise deadlock_exc

        mock_cur.execute.side_effect = execute_side_effect

        # Must not raise
        backend.update_job("job-123", status="completed")

        assert mock_cur.execute.call_count >= 2, (
            "update_job must retry on deadlock; execute called only once"
        )

    def test_deadlock_on_all_attempts_raises_after_bounded_retries(self, deadlock_exc):
        """DeadlockDetected on every attempt -> raises after bounded retries (no infinite loop)."""
        backend, mock_cur = _make_mock_pg_backend()
        mock_cur.execute.side_effect = deadlock_exc

        with pytest.raises(Exception) as exc_info:
            backend.update_job("job-456", status="completed")

        assert mock_cur.execute.call_count >= 2, (
            f"Must retry before raising; called only {mock_cur.execute.call_count} time(s)"
        )
        assert "deadlock" in str(exc_info.value).lower() or (
            type(exc_info.value).__name__ == "DeadlockDetected"
        )

    def test_non_deadlock_error_not_retried(self):
        """A non-deadlock error must NOT be retried — propagates immediately on first attempt."""
        backend, mock_cur = _make_mock_pg_backend()

        class _OtherError(Exception):
            pass

        mock_cur.execute.side_effect = _OtherError("network error")

        with pytest.raises(_OtherError):
            backend.update_job("job-789", status="failed")

        assert mock_cur.execute.call_count == 1, (
            f"Non-deadlock errors must not be retried; called {mock_cur.execute.call_count} time(s)"
        )

    def test_sqlite_update_job_unaffected(self, tmp_path):
        """SQLite-backed tracker update_status path works normally (no retry machinery there)."""
        tracker = _make_sqlite_tracker(str(tmp_path / "retry_test.db"))
        job = _make_job("sqlite-update-job")
        tracker._atomic_insert_or_raise(job)
        # Register into in-memory so update_status can find it
        with tracker._lock:
            tracker._active_jobs[job.job_id] = job
        # Must succeed without error — SQLite path has no deadlock retry
        tracker.update_status(job.job_id, status="running")
