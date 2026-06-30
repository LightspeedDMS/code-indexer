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
from unittest import mock

import pytest

from code_indexer.server.services.job_tracker import (
    CONCURRENT_COMPLETED_SENTINEL,
    DuplicateJobError,
    JobTracker,
    TrackedJob,
)


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
            metadata TEXT,
            actor_username TEXT
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


# ---------------------------------------------------------------------------
# Bug #1252 — transient TOCTOU race must retry, not fail the caller
#
# Root cause: the INSERT conflicts with idx_active_job_per_repo (an active
# job for the same repo exists). _atomic_insert_or_raise then SELECTs the
# conflicting row to build DuplicateJobError. But between the failed INSERT
# and that SELECT, the OTHER job can COMPLETE (falls outside the partial
# index predicate WHERE status IN ('pending','running')), so the lookup
# returns None. The slot is now genuinely free -- the correct behavior is to
# retry the insert (bounded), not to immediately treat a free slot as a
# duplicate-skip (or, pre-Bug-#1235, crash with a fatal RuntimeError).
# ---------------------------------------------------------------------------


class TestAtomicInsertOrRaiseTransientRaceRetryBug1252:
    """_atomic_insert_or_raise must retry (bounded) when the conflicting row
    vanishes between the failed INSERT and the follow-up lookup, rather than
    immediately treating a since-freed slot as a duplicate."""

    def test_transient_race_retries_and_succeeds_instead_of_raising(
        self, atomic_tracker: JobTracker, atomic_db_path: str
    ):
        """
        Simulates the exact race from Bug #1252: a blocking active job exists,
        the new INSERT conflicts, but by the time the conflict lookup runs the
        blocking job has already completed (real DB mutation performed inside
        the patched lookup, mirroring genuine concurrent completion). Since
        the partial index no longer has any conflicting active row, the retry
        INSERT must succeed -- the caller must get back a registered job, not
        an exception.
        """
        atomic_tracker.register_job_if_no_conflict(
            job_id="blocking-job",
            operation_type="global_repo_refresh",
            username="admin",
            repo_alias="spdlog-global",
        )

        real_lookup = JobTracker._find_blocking_active_job_id
        call_count = {"n": 0}

        def racing_lookup(self, operation_type, repo_alias):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate the blocking job completing concurrently, between
                # the failed INSERT and this lookup -- a real DB mutation
                # through the tracker's own connection, exactly as a
                # concurrent worker committing a status transition would.
                conn = self._conn_manager.get_connection()
                conn.execute(
                    "UPDATE background_jobs SET status='completed' WHERE job_id=?",
                    ("blocking-job",),
                )
                conn.commit()
                return None
            return real_lookup(self, operation_type, repo_alias)

        with mock.patch.object(
            JobTracker, "_find_blocking_active_job_id", racing_lookup
        ):
            job = atomic_tracker.register_job_if_no_conflict(
                job_id="new-job",
                operation_type="global_repo_refresh",
                username="admin",
                repo_alias="spdlog-global",
            )

        assert job.job_id == "new-job"
        assert job.status == "pending"
        assert call_count["n"] == 1, (
            "expected exactly one vanished-row lookup before the retry "
            f"succeeded; lookup was called {call_count['n']} time(s)"
        )

        with closing(sqlite3.connect(atomic_db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM background_jobs WHERE job_id = ?",
                ("new-job",),
            ).fetchone()
        assert row == ("pending",), (
            "the retried insert must actually persist the new job row -- "
            "the slot was free, so the job must run, not be skipped"
        )

    def test_genuine_duplicate_with_active_blocking_row_still_raises(
        self, atomic_tracker: JobTracker
    ):
        """
        Regression guard: when the blocking row is a REAL active duplicate
        (still pending/running at lookup time), DuplicateJobError must still
        be raised with the real existing_job_id -- no retry must be
        attempted, and a genuine duplicate must never be silently dropped.
        """
        atomic_tracker.register_job_if_no_conflict(
            job_id="active-job",
            operation_type="global_repo_refresh",
            username="admin",
            repo_alias="repo-real-dup",
        )

        with pytest.raises(DuplicateJobError) as exc_info:
            atomic_tracker.register_job_if_no_conflict(
                job_id="dup-job",
                operation_type="global_repo_refresh",
                username="admin",
                repo_alias="repo-real-dup",
            )

        assert exc_info.value.existing_job_id == "active-job"
        assert exc_info.value.operation_type == "global_repo_refresh"
        assert exc_info.value.repo_alias == "repo-real-dup"

    def test_persistent_contradiction_after_bounded_retries_never_raises_runtime_error(
        self, atomic_tracker: JobTracker
    ):
        """
        Pathological case: the no-active-row state persists across every
        bounded retry attempt (sustained churn / contradictory state that
        never resolves). _atomic_insert_or_raise must still terminate -- no
        unbounded loop (Messi Rule #14) -- and must NOT raise a fatal
        RuntimeError. It falls back to DuplicateJobError with the
        CONCURRENT_COMPLETED_SENTINEL marker, preserving the Bug #1235
        invariant (see test_bug1235_pg_duplicate_claim_race.py) that this
        race must never crash the caller.
        """
        job = TrackedJob(
            job_id="stuck-job",
            operation_type="global_repo_refresh",
            status="pending",
            username="admin",
            repo_alias="repo-stuck",
        )

        lookup_calls = {"n": 0}

        def always_integrity_error(*args, **kwargs):
            raise sqlite3.IntegrityError("UNIQUE constraint failed")

        def always_none(self, operation_type, repo_alias):
            lookup_calls["n"] += 1
            return None

        with (
            mock.patch.object(
                JobTracker,
                "_atomic_insert_impl",
                side_effect=always_integrity_error,
            ),
            mock.patch.object(JobTracker, "_find_blocking_active_job_id", always_none),
        ):
            with pytest.raises(DuplicateJobError) as exc_info:
                atomic_tracker._atomic_insert_or_raise(job)

        assert exc_info.value.existing_job_id == CONCURRENT_COMPLETED_SENTINEL
        assert lookup_calls["n"] >= 2, (
            "must retry at least once before giving up; "
            f"lookup called only {lookup_calls['n']} time(s)"
        )
        assert lookup_calls["n"] <= 10, (
            "retry must be bounded (Messi Rule #14 anti-unbounded-loop); "
            f"lookup called {lookup_calls['n']} time(s)"
        )
