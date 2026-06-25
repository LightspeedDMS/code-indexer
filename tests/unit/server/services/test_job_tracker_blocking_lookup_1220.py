"""
Tests for Bug #1220: Duplicate background-job insert surfaces as RuntimeError
instead of a benign DuplicateJobError.

Root cause: _find_blocking_active_job_id() uses self._backend.list_jobs(...)
which is paginated and filters repo_alias in Python. When the blocking row is
not in the top-N results, the lookup returns None, and _atomic_insert_or_raise
raises RuntimeError("database state is inconsistent") instead of DuplicateJobError.

Fix: Add find_active_job_by_type_and_alias(operation_type, repo_alias) to both
SQLite and Postgres backends — a direct non-paginated lookup — and use it in
_find_blocking_active_job_id before falling back to the RuntimeError path.

Test strategy:
1. Regression (backend path): Backend where list_jobs returns empty (simulating
   pagination miss) but find_active_job_by_type_and_alias returns the blocking
   job_id must produce DuplicateJobError, not RuntimeError.
   FAILS on unfixed code (method does not exist → AttributeError, or list_jobs
   returns empty → RuntimeError).

2. Regression (SQLite direct path): Real SQLite with a blocking pending row must
   produce DuplicateJobError with correct .existing_job_id.
   Currently passes via the direct sqlite3 query in _find_blocking_active_job_id,
   but we verify the new backend method also works.

3. SQLite backend: find_active_job_by_type_and_alias returns job_id for pending
   and running rows, None for completed/failed/absent rows. FAILS on unfixed code
   (method does not exist).

4. Defensive RuntimeError: preserved only when no active row exists after violation.
   Must continue to pass (behavior must not regress).

Uses REAL SQLite only (anti-mock rule). PG backend validated via a faithful fake
that mirrors psycopg3 cursor semantics.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.job_tracker import DuplicateJobError, JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _init_db(db_path: Any) -> None:
    """Initialize a real SQLite DB with full schema including partial unique index."""
    DatabaseSchema(str(db_path)).initialize_database()


def _make_tracker(db_path: Any) -> JobTracker:
    _init_db(db_path)
    return JobTracker(str(db_path))


def _insert_pending_directly(
    db_path: Any,
    job_id: str,
    operation_type: str,
    repo_alias: str,
    status: str = "pending",
) -> None:
    """Insert a job row directly via sqlite3 (bypasses JobTracker in-memory cache)."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, created_at, username,
                progress, repo_alias, is_admin, cancelled, resolution_attempts)
               VALUES (?, ?, ?, ?, 'system', 0, ?, 0, 0, 0)""",
            (job_id, operation_type, status, now, repo_alias),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Faithful fake backend for testing the backend path
# ---------------------------------------------------------------------------


class _PaginationMissBackend:
    """
    Faithful test-double of BackgroundJobsPostgresBackend that simulates the
    pagination-miss scenario described in Bug #1220.

    - atomic_claim_insert: raises UniqueViolation when a blocking active row exists.
    - list_jobs: always returns [] (simulates the case where the blocking row is
      not in the top-N paginated results — the root cause of the bug).
    - find_active_job_by_type_and_alias: implemented correctly (direct lookup).
      When the fix is applied, _find_blocking_active_job_id calls this instead
      of relying on list_jobs.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def _seed_blocking_job(
        self, job_id: str, operation_type: str, repo_alias: str, status: str = "pending"
    ) -> None:
        self._jobs[job_id] = {
            "job_id": job_id,
            "operation_type": operation_type,
            "status": status,
            "repo_alias": repo_alias,
            "username": "system",
            "progress": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

    def atomic_claim_insert(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        repo_alias: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # Simulate partial unique index violation for active duplicate
        for job in self._jobs.values():
            if (
                job["operation_type"] == operation_type
                and job["repo_alias"] == repo_alias
                and job["status"] in ("pending", "running")
                and repo_alias is not None
            ):

                class _FakeUniqueViolation(Exception):
                    pass

                _FakeUniqueViolation.__name__ = "UniqueViolation"
                raise _FakeUniqueViolation(
                    'duplicate key value violates unique constraint "idx_active_job_per_repo"'
                )
        self._jobs[job_id] = {
            "job_id": job_id,
            "operation_type": operation_type,
            "status": status,
            "repo_alias": repo_alias,
            "username": username,
            "progress": progress,
            "created_at": created_at,
        }

    def list_jobs(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        # Intentionally returns empty to simulate pagination miss (Bug #1220 root cause)
        return []

    def find_active_job_by_type_and_alias(
        self, operation_type: str, repo_alias: str
    ) -> Optional[str]:
        """Direct lookup: returns job_id or None. Correct implementation."""
        for job in self._jobs.values():
            if (
                job["operation_type"] == operation_type
                and job["repo_alias"] == repo_alias
                and job["status"] in ("pending", "running")
            ):
                return str(job["job_id"])
        return None

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].update(kwargs)

    def save_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def count_jobs_by_status(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for job in self._jobs.values():
            s = str(job["status"])
            counts[s] = counts.get(s, 0) + 1
        return counts

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        return 0

    def delete_job(self, job_id: str) -> bool:
        return bool(self._jobs.pop(job_id, None))

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        return 0


# ---------------------------------------------------------------------------
# Vanishing-row backend for defensive RuntimeError test
# ---------------------------------------------------------------------------


class _VanishingRowBackend:
    """
    Backend that signals UniqueViolation on insert but reports no active row
    when find_active_job_by_type_and_alias is called (genuine race: row vanished
    between INSERT failure and lookup). Must produce RuntimeError.
    """

    def atomic_claim_insert(self, *args: Any, **kwargs: Any) -> None:
        class _FakeUniqueViolation(Exception):
            pass

        _FakeUniqueViolation.__name__ = "UniqueViolation"
        raise _FakeUniqueViolation("unique violation")

    def list_jobs(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def find_active_job_by_type_and_alias(
        self, operation_type: str, repo_alias: str
    ) -> Optional[str]:
        return None  # Genuinely no active row — the true inconsistency

    def save_job(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        pass

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return None

    def count_jobs_by_status(self) -> Dict[str, int]:
        return {}

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        return 0


# ===========================================================================
# 1. Regression: backend path collision -> DuplicateJobError, not RuntimeError
# ===========================================================================


class TestBackendPathCollisionProducesDuplicateJobError:
    """
    Bug #1220: When list_jobs misses the blocking row (pagination miss or any
    ordering issue), _find_blocking_active_job_id must use the new
    find_active_job_by_type_and_alias direct lookup to find the blocking row
    and raise DuplicateJobError — never RuntimeError.

    FAILS on unfixed code because:
    - The method find_active_job_by_type_and_alias does not exist on the backend
      → AttributeError, or
    - The old code only calls list_jobs which returns [] → existing_id is None
      → RuntimeError("database state is inconsistent")
    """

    def test_collision_raises_duplicate_job_error_not_runtime_error(self) -> None:
        """
        Core regression test: list_jobs returns empty (pagination miss), but
        find_active_job_by_type_and_alias returns the blocking job_id.
        Result must be DuplicateJobError with correct .existing_job_id.
        """
        backend = _PaginationMissBackend()
        blocking_job_id = str(uuid.uuid4())
        backend._seed_blocking_job(
            blocking_job_id, "data_retention_cleanup", "server", "pending"
        )

        tracker = JobTracker(db_path="", storage_backend=backend)
        conflicting_job_id = str(uuid.uuid4())

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker.register_job_if_no_conflict(
                job_id=conflicting_job_id,
                operation_type="data_retention_cleanup",
                username="system",
                repo_alias="server",
            )

        err = exc_info.value
        assert err.operation_type == "data_retention_cleanup"
        assert err.repo_alias == "server"
        assert err.existing_job_id == blocking_job_id, (
            f"existing_job_id must be {blocking_job_id!r} (the blocking job); "
            f"got {err.existing_job_id!r}"
        )

    def test_collision_with_running_blocking_job(self) -> None:
        """Blocking row in 'running' status (not just 'pending') is also found."""
        backend = _PaginationMissBackend()
        blocking_job_id = str(uuid.uuid4())
        backend._seed_blocking_job(
            blocking_job_id, "data_retention_cleanup", "server", "running"
        )

        tracker = JobTracker(db_path="", storage_backend=backend)
        conflicting_job_id = str(uuid.uuid4())

        with pytest.raises(DuplicateJobError) as exc_info:
            tracker.register_job_if_no_conflict(
                job_id=conflicting_job_id,
                operation_type="data_retention_cleanup",
                username="system",
                repo_alias="server",
            )

        assert exc_info.value.existing_job_id == blocking_job_id

    def test_no_collision_when_previous_job_completed(self) -> None:
        """Completed previous job must NOT block a new registration."""
        backend = _PaginationMissBackend()
        old_job_id = str(uuid.uuid4())
        backend._seed_blocking_job(
            old_job_id, "data_retention_cleanup", "server", "completed"
        )

        tracker = JobTracker(db_path="", storage_backend=backend)
        new_job_id = str(uuid.uuid4())

        # Must succeed (no DuplicateJobError, no RuntimeError)
        job = tracker.register_job_if_no_conflict(
            job_id=new_job_id,
            operation_type="data_retention_cleanup",
            username="system",
            repo_alias="server",
        )
        assert job.job_id == new_job_id


# ===========================================================================
# 2. Regression: SQLite direct path still works correctly
# ===========================================================================


class TestSqliteDirectPathCollision:
    """
    The SQLite direct path in _find_blocking_active_job_id already works correctly
    via a direct SQL query. Verify it continues to work after the fix.
    """

    def test_sqlite_collision_raises_duplicate_job_error(self, tmp_path: Any) -> None:
        """
        Real SQLite: register a blocking job, then a conflicting registration
        must raise DuplicateJobError with correct .existing_job_id.
        """
        db_path = tmp_path / "test.db"
        tracker = _make_tracker(db_path)
        blocking_job_id = str(uuid.uuid4())

        tracker.register_job_if_no_conflict(
            job_id=blocking_job_id,
            operation_type="data_retention_cleanup",
            username="system",
            repo_alias="server",
        )

        conflicting_job_id = str(uuid.uuid4())
        with pytest.raises(DuplicateJobError) as exc_info:
            tracker.register_job_if_no_conflict(
                job_id=conflicting_job_id,
                operation_type="data_retention_cleanup",
                username="system",
                repo_alias="server",
            )

        err = exc_info.value
        assert err.operation_type == "data_retention_cleanup"
        assert err.repo_alias == "server"
        assert err.existing_job_id == blocking_job_id


# ===========================================================================
# 3. SQLite backend: find_active_job_by_type_and_alias direct lookup method
# ===========================================================================


class TestSqliteBackendFindActiveJobByTypeAndAlias:
    """
    BackgroundJobsSqliteBackend.find_active_job_by_type_and_alias must:
    - Return job_id for a pending row
    - Return job_id for a running row
    - Return None for a completed/failed row
    - Return None when no row matches

    FAILS on unfixed code because the method does not yet exist.
    Uses REAL SQLite.
    """

    @pytest.fixture
    def backend_and_db(self, tmp_path: Any):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        return BackgroundJobsSqliteBackend(str(db_path)), db_path

    def test_pending_job_found(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "data_retention_cleanup", "server", "pending"
        )

        result = backend.find_active_job_by_type_and_alias(
            "data_retention_cleanup", "server"
        )
        assert result == job_id, (
            f"find_active_job_by_type_and_alias must return {job_id!r} for pending row; "
            f"got {result!r}"
        )

    def test_running_job_found(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "dep_map_analysis", "myrepo", "running"
        )

        result = backend.find_active_job_by_type_and_alias("dep_map_analysis", "myrepo")
        assert result == job_id

    def test_completed_job_not_found(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "data_retention_cleanup", "server", "completed"
        )

        result = backend.find_active_job_by_type_and_alias(
            "data_retention_cleanup", "server"
        )
        assert result is None

    def test_failed_job_not_found(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "data_retention_cleanup", "server", "failed"
        )

        result = backend.find_active_job_by_type_and_alias(
            "data_retention_cleanup", "server"
        )
        assert result is None

    def test_no_row_returns_none(self, backend_and_db: Any) -> None:
        backend, _ = backend_and_db
        result = backend.find_active_job_by_type_and_alias(
            "nonexistent_op", "nonexistent_repo"
        )
        assert result is None

    def test_different_operation_type_not_returned(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "dep_map_analysis", "server", "pending"
        )

        result = backend.find_active_job_by_type_and_alias(
            "data_retention_cleanup", "server"
        )
        assert result is None

    def test_different_repo_alias_not_returned(self, backend_and_db: Any) -> None:
        backend, db_path = backend_and_db
        job_id = str(uuid.uuid4())
        _insert_pending_directly(
            db_path, job_id, "data_retention_cleanup", "other", "pending"
        )

        result = backend.find_active_job_by_type_and_alias(
            "data_retention_cleanup", "server"
        )
        assert result is None


# ===========================================================================
# 4. Defensive RuntimeError: preserved for genuine inconsistency
# ===========================================================================


class TestDefensiveRuntimeErrorPreserved:
    """
    The defensive RuntimeError in _atomic_insert_or_raise must still fire when
    there is genuinely no active row after a unique-index violation.

    This covers the true race: INSERT fails → blocking row vanishes between
    INSERT and lookup → find_active_job_by_type_and_alias returns None →
    RuntimeError("database state is inconsistent") is raised.
    """

    def test_runtime_error_fires_when_no_active_row_after_violation(self) -> None:
        """
        Vanishing-row backend: UniqueViolation on INSERT but find_active returns
        None. Must raise RuntimeError mentioning 'inconsistent', not DuplicateJobError.
        """
        tracker = JobTracker(db_path="", storage_backend=_VanishingRowBackend())

        with pytest.raises(RuntimeError) as exc_info:
            tracker.register_job_if_no_conflict(
                job_id=str(uuid.uuid4()),
                operation_type="data_retention_cleanup",
                username="system",
                repo_alias="server",
            )

        assert "inconsistent" in str(exc_info.value).lower(), (
            f"RuntimeError message must mention 'inconsistent'; got: {exc_info.value}"
        )
