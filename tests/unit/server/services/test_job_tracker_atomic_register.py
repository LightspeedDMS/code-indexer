"""
Unit tests for JobTracker.register_job_if_no_conflict (Story #876 Phase B-1).

This method is the cluster-atomic replacement for the two-call TOCTOU pattern
(check_operation_conflict + register_job). It wraps the INSERT in a single
atomic transaction that relies on the partial unique index
idx_active_job_per_repo to reject duplicate active jobs — no read-then-write
race window.

Semantics:
    tracker.register_job_if_no_conflict(
        job_id=...,
        operation_type=...,
        username=...,
        repo_alias=...,
        metadata=...,
    ) -> TrackedJob

    - If NO active/pending job exists for the same (operation_type, repo_alias)
      pair, the method inserts the row and returns the new TrackedJob.
    - If an active/pending job DOES exist, the database-level partial unique
      index rejects the INSERT and the method raises DuplicateJobError with
      .operation_type, .repo_alias, and .existing_job_id populated from the
      blocking row.

The partial index has the predicate:
    WHERE status IN ('pending', 'running') AND repo_alias IS NOT NULL

So the atomic guarantee only applies to repo-scoped active jobs. Completed
and failed jobs are outside the predicate, so retries after terminal states
succeed.

Test structure (split across sequential test classes):
  1. TestRegisterJobIfNoConflictHappyPath — success cases (this file)
  2. (follow-up increments) — conflict detection, concurrency, arg plumbing
"""

import sqlite3
from contextlib import closing

import pytest

from code_indexer.server.services.job_tracker import JobTracker, TrackedJob


# ---------------------------------------------------------------------------
# Fixture: extends the shared conftest fixture to include the partial unique
# index idx_active_job_per_repo (Story #876 Phase C).
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db_path(tmp_path):
    """
    Temporary SQLite DB with background_jobs table AND the partial unique
    index that enforces single-active-job-per-(operation_type, repo_alias).

    Uses contextlib.closing() to guarantee the connection is closed even
    if the CREATE TABLE or CREATE UNIQUE INDEX statements raise.
    """
    db = tmp_path / "test_atomic.db"
    with closing(sqlite3.connect(str(db))) as conn:
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
            progress_info TEXT,
            metadata TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()
    return str(db)


@pytest.fixture
def atomic_tracker(atomic_db_path):
    """JobTracker connected to the DB that has the partial unique index."""
    return JobTracker(atomic_db_path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRegisterJobIfNoConflictHappyPath:
    """Success cases: no conflict exists, insert proceeds normally."""

    def test_first_call_returns_tracked_job(self, atomic_tracker: JobTracker):
        """
        First call with a fresh (operation_type, repo_alias) pair inserts
        and returns a TrackedJob with status='pending' and matching fields.
        """
        job = atomic_tracker.register_job_if_no_conflict(
            job_id="job-happy-001",
            operation_type="dep_map_analysis",
            username="admin",
            repo_alias="repo-a",
        )
        assert isinstance(job, TrackedJob)
        assert job.job_id == "job-happy-001"
        assert job.operation_type == "dep_map_analysis"
        assert job.status == "pending"
        assert job.repo_alias == "repo-a"
        assert job.username == "admin"

    def test_first_call_persists_row_to_database(
        self, atomic_tracker: JobTracker, atomic_db_path: str
    ):
        """
        Row is immediately visible via a fresh SQLite connection — not just
        in the in-memory dict.
        """
        atomic_tracker.register_job_if_no_conflict(
            job_id="job-persist-001",
            operation_type="dep_map_analysis",
            username="admin",
            repo_alias="repo-a",
        )
        with closing(sqlite3.connect(atomic_db_path)) as conn:
            row = conn.execute(
                "SELECT status, operation_type, repo_alias, username "
                "FROM background_jobs WHERE job_id = ?",
                ("job-persist-001",),
            ).fetchone()
        assert row == ("pending", "dep_map_analysis", "repo-a", "admin")

    def test_first_call_adds_job_to_in_memory_dict(self, atomic_tracker: JobTracker):
        """
        New job is reachable via get_job (in-memory fast path) immediately
        after insert.
        """
        atomic_tracker.register_job_if_no_conflict(
            job_id="job-mem-001",
            operation_type="dep_map_analysis",
            username="admin",
            repo_alias="repo-a",
        )
        fetched = atomic_tracker.get_job("job-mem-001")
        assert fetched is not None
        assert fetched.job_id == "job-mem-001"
        assert fetched.status == "pending"
