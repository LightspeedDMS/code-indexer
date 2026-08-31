"""Bug #1549: BackgroundJobManager orphan-cleanup must not stomp jobs
owned by a still-alive primary instance.

See test_job_tracker_primary_instance_guard_1549.py for the full root
cause narrative (a duplicate cidx-server.service process, crash-looping
on a port-bind conflict against an already-running out-of-band server,
runs the full startup sequence -- including the unscoped SQLite orphan
sweep -- against the same on-disk database seconds after the live
instance creates a scheduler job).

BackgroundJobManager's constructor runs this sweep implicitly (via
_load_jobs_sqlite), so the guard must be threaded through as a
constructor parameter, not a call-site argument.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def seeded_backend(tmp_path: Path):
    """A real BackgroundJobsSqliteBackend with one pre-existing 'running'
    job, simulating a job the live primary instance just created."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    backend = BackgroundJobsSqliteBackend(str(db_path))

    now = datetime.now(timezone.utc).isoformat()
    backend.save_job(
        job_id="live-running-job",
        operation_type="hnsw_orphan_repair_sweep",
        status="running",
        created_at=now,
        started_at=now,
        username="system",
        progress=10,
    )
    return str(db_path), backend


class TestBackgroundJobManagerConstructionRespectsPrimaryInstanceFlag:
    def test_non_primary_instance_does_not_touch_live_running_job(
        self, seeded_backend
    ) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, backend = seeded_backend

        manager = BackgroundJobManager(
            use_sqlite=True, db_path=db_path, is_primary_instance=False
        )
        try:
            job = backend.get_job("live-running-job")
            assert job is not None
            assert job["status"] == "running"
        finally:
            manager.shutdown()

    def test_default_primary_instance_still_cleans_up_genuine_orphans(
        self, seeded_backend
    ) -> None:
        """Regression: the default (is_primary_instance=True, matching
        every pre-#1549 caller) must still fail real orphaned jobs."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, backend = seeded_backend

        manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
        try:
            job = backend.get_job("live-running-job")
            assert job is not None
            assert job["status"] == "failed"
            assert job["error"] == "Job interrupted by server restart"
        finally:
            manager.shutdown()


@pytest.fixture
def empty_backend(tmp_path: Path):
    """A real BackgroundJobsSqliteBackend with no pre-existing jobs."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    db_path = tmp_path / "empty.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    backend = BackgroundJobsSqliteBackend(str(db_path))
    try:
        yield str(db_path), backend
    finally:
        backend.close()


class TestFailOrphanedJobsRespectsPrimaryInstanceFlag:
    """Covers the SECOND unscoped sweep (lifespan.py's explicit
    fail_orphaned_jobs() call, error='Orphaned by server restart') --
    distinct from the constructor-time sweep tested above."""

    def test_non_primary_instance_does_not_touch_job_created_after_construction(
        self, empty_backend
    ) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, backend = empty_backend
        manager = BackgroundJobManager(
            use_sqlite=True, db_path=db_path, is_primary_instance=False
        )
        try:
            now = datetime.now(timezone.utc).isoformat()
            backend.save_job(
                job_id="live-refresh-job",
                operation_type="global_repo_refresh",
                status="running",
                created_at=now,
                started_at=now,
                username="system",
                progress=5,
            )

            manager.fail_orphaned_jobs()

            job = backend.get_job("live-refresh-job")
            assert job is not None
            assert job["status"] == "running"
        finally:
            manager.shutdown()

    def test_default_primary_instance_still_fails_genuine_orphans(
        self, empty_backend
    ) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, backend = empty_backend
        manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            backend.save_job(
                job_id="genuine-refresh-orphan",
                operation_type="global_repo_refresh",
                status="running",
                created_at=now,
                started_at=now,
                username="system",
                progress=5,
            )

            manager.fail_orphaned_jobs()

            job = backend.get_job("genuine-refresh-orphan")
            assert job is not None
            assert job["status"] == "failed"
            assert job["error"] == "Orphaned by server restart"
        finally:
            manager.shutdown()


class TestFailOrphanedJobsInMemoryMarkingRespectsPrimaryInstanceFlag:
    """Bug #1549 Finding 2a (Codex-confirmed): fail_orphaned_jobs()'s
    IN-MEMORY marking loop ran unconditionally, BEFORE any primary-
    instance check -- only the DATABASE-level sweep further down was
    gated. Each uvicorn worker's own self.jobs dict is loaded from the
    shared backend at construction time (_load_jobs_sqlite's
    list_jobs(status='running'/'pending') query, which can legitimately
    contain OTHER live workers'/nodes' running jobs), so a non-primary
    worker still reported another worker's genuinely-live job as
    'failed' from memory even though the shared DB correctly kept it
    'running'."""

    def test_non_primary_instance_does_not_mark_in_memory_jobs_failed(
        self, empty_backend
    ) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            BackgroundJobManager,
            JobStatus,
        )

        db_path, backend = empty_backend
        manager = BackgroundJobManager(
            use_sqlite=True, db_path=db_path, is_primary_instance=False
        )
        try:
            # Simulate a job belonging to another live worker/node that
            # this process's own in-memory dict happens to hold (in
            # production this arrives via list_jobs() at construction
            # time; injected directly here for test determinism).
            now = datetime.now(timezone.utc)
            manager.jobs["other-worker-live-job"] = BackgroundJob(
                job_id="other-worker-live-job",
                operation_type="global_repo_refresh",
                status=JobStatus.RUNNING,
                created_at=now,
                started_at=now,
                completed_at=None,
                result=None,
                error=None,
                progress=10,
                username="system",
            )

            manager.fail_orphaned_jobs()

            assert manager.jobs["other-worker-live-job"].status == JobStatus.RUNNING
        finally:
            manager.shutdown()

    def test_default_primary_instance_still_marks_in_memory_jobs_failed(
        self, empty_backend
    ) -> None:
        """Regression: the default (is_primary_instance=True) must still
        mark genuinely orphaned in-memory jobs FAILED -- this guard must
        never weaken true orphan detection."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            BackgroundJobManager,
            JobStatus,
        )

        db_path, backend = empty_backend
        manager = BackgroundJobManager(use_sqlite=True, db_path=db_path)
        try:
            now = datetime.now(timezone.utc)
            manager.jobs["genuine-in-memory-orphan"] = BackgroundJob(
                job_id="genuine-in-memory-orphan",
                operation_type="global_repo_refresh",
                status=JobStatus.RUNNING,
                created_at=now,
                started_at=now,
                completed_at=None,
                result=None,
                error=None,
                progress=10,
                username="system",
            )

            count = manager.fail_orphaned_jobs()

            assert manager.jobs["genuine-in-memory-orphan"].status == JobStatus.FAILED
            assert count >= 1
        finally:
            manager.shutdown()


class TestPrimaryInstanceSkipDoesNotLogAtWarningLevel:
    """Bug #1549 Finding 2b (Codex-confirmed): under `uvicorn --workers N`,
    every worker runs initialize_services(); exactly one acquires the
    primary-instance lock and the other N-1 correctly, routinely take the
    non-primary path -- entirely expected on EVERY multi-worker startup,
    not a signal of a caller bug. The pre-fix WARNINGs here are not in
    LOG_AUDIT_ALLOWLIST, so a normal multi-worker startup failed the
    mandatory post-E2E log-audit gate (Phase 3/4). Per the established
    Bug #1535 precedent (demote a happy-path, by-design log line to
    DEBUG rather than allowlisting it), these are demoted."""

    def test_load_jobs_sqlite_skip_does_not_log_warning(
        self, empty_backend, caplog
    ) -> None:
        import logging

        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, _backend = empty_backend
        with caplog.at_level(logging.DEBUG):
            manager = BackgroundJobManager(
                use_sqlite=True, db_path=db_path, is_primary_instance=False
            )
        try:
            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert warnings == [], (
                "non-primary-instance skip during construction must not log "
                f"at WARNING+ (routine multi-worker startup), found: {warnings}"
            )
        finally:
            manager.shutdown()

    def test_fail_orphaned_jobs_sqlite_skip_does_not_log_warning(
        self, empty_backend, caplog
    ) -> None:
        import logging

        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        db_path, _backend = empty_backend
        manager = BackgroundJobManager(
            use_sqlite=True, db_path=db_path, is_primary_instance=False
        )
        try:
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                manager.fail_orphaned_jobs()

            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert warnings == [], (
                "non-primary-instance skip in fail_orphaned_jobs must not "
                f"log at WARNING+ (routine multi-worker startup), found: {warnings}"
            )
        finally:
            manager.shutdown()
