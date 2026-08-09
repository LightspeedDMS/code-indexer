"""Bug #1549: JobTracker orphan-cleanup must not stomp jobs owned by a
still-alive primary instance.

Root cause (proven via live evidence on the local dev cidx-server,
solo/SQLite mode): `cidx-server.service` is configured with
`Restart=always` and repeatedly fails to bind port 8000 because a
DIFFERENT, already-running server process (started outside systemd) holds
it. Each doomed systemd-launched attempt nonetheless runs the ENTIRE
FastAPI lifespan startup sequence to completion BEFORE discovering the
port conflict -- including JobTracker.cleanup_orphaned_jobs_on_startup(),
which issues an UNSCOPED `UPDATE background_jobs SET status='failed' ...
WHERE status IN ('running','pending')` against the SAME on-disk SQLite
database the live instance is using, with zero created_at/time filtering
and zero process-identity filtering (the solo/SQLite path deliberately
assumes "always single process", per the comment on
BackgroundJobsSqliteBackend.cleanup_orphaned_jobs_on_startup). That
assumption is false whenever a duplicate process's startup races the real
instance, so any job the live instance created moments earlier -- even
mid-flight ones like the scheduler-created hnsw_orphan_repair_sweep /
global_repo_refresh jobs reported in Bug #1549 -- gets marked
"Job interrupted by server restart" despite the server never restarting.

Fix: cleanup_orphaned_jobs_on_startup() accepts an `is_primary_instance`
flag (default True, preserving all existing behavior). When False, the
unscoped destructive SQLite sweep is skipped entirely.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from code_indexer.server.services.job_tracker import JobTracker


def _insert_job_directly(db_path: str, job_id: str, status: str) -> None:
    """Insert a row directly into SQLite, simulating a job the LIVE
    primary instance created moments before this (duplicate) process's
    own startup cleanup runs."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, created_at, progress, username,
                is_admin, cancelled, resolution_attempts)
               VALUES (?, 'test_op', ?, ?, 0, 'admin', 0, 0, 0)""",
            (job_id, status, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


class TestJobTrackerLegacyPathRespectsPrimaryInstanceFlag:
    """Covers JobTracker's direct-SQLite (no injected backend) path."""

    @pytest.mark.parametrize("live_status", ["running", "pending"])
    def test_non_primary_instance_does_not_touch_live_job(
        self, db_path: str, live_status: str
    ) -> None:
        _insert_job_directly(db_path, f"live-{live_status}-job", live_status)

        duplicate_tracker = JobTracker(db_path)
        count = duplicate_tracker.cleanup_orphaned_jobs_on_startup(
            is_primary_instance=False
        )

        job = duplicate_tracker.get_job(f"live-{live_status}-job")
        assert job is not None
        assert job.status == live_status
        assert count == 0

    def test_primary_instance_still_cleans_up_genuine_orphans(
        self, db_path: str
    ) -> None:
        """Regression: a genuine restart (is_primary_instance=True, the
        default) must still fail real orphaned jobs -- this guard must
        never weaken true orphan detection."""
        _insert_job_directly(db_path, "genuine-orphan", "running")

        fresh_tracker = JobTracker(db_path)
        count = fresh_tracker.cleanup_orphaned_jobs_on_startup(is_primary_instance=True)

        job = fresh_tracker.get_job("genuine-orphan")
        assert job is not None
        assert job.status == "failed"
        assert count == 1


class _FakeJobTrackerBackend:
    """Plain test double (anti-mock) mirroring the real backend's public
    surface, matching the pattern in
    test_job_tracker_node_scoped_orphan_cleanup_1400.py."""

    def __init__(self) -> None:
        self.cleanup_calls: list = []

    def cleanup_orphaned_jobs_on_startup(self, node_id=None) -> int:
        self.cleanup_calls.append({"node_id": node_id})
        return 5


class BackgroundJobsPostgresBackend:
    """Real stub whose class name matches the production PG backend."""

    def __init__(self) -> None:
        self.cleanup_calls: list = []

    def cleanup_orphaned_jobs_on_startup(self, node_id=None) -> int:
        self.cleanup_calls.append({"node_id": node_id})
        return 0


class TestJobTrackerInjectedBackendRespectsPrimaryInstanceFlag:
    def test_non_primary_instance_skips_unscoped_sqlite_backend_call(
        self, tmp_path
    ) -> None:
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(
            str(tmp_path / "jobs.db"), storage_backend=backend, node_id="node-a"
        )

        count = tracker.cleanup_orphaned_jobs_on_startup(is_primary_instance=False)

        assert backend.cleanup_calls == []
        assert count == 0

    def test_non_primary_instance_still_calls_postgres_backend(self, tmp_path) -> None:
        backend = BackgroundJobsPostgresBackend()
        tracker = JobTracker(
            str(tmp_path / "jobs.db"), storage_backend=backend, node_id="node-a"
        )

        tracker.cleanup_orphaned_jobs_on_startup(is_primary_instance=False)

        assert backend.cleanup_calls == [{"node_id": "node-a"}]

    def test_primary_instance_still_calls_sqlite_backend(self, tmp_path) -> None:
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(
            str(tmp_path / "jobs.db"), storage_backend=backend, node_id="node-a"
        )

        count = tracker.cleanup_orphaned_jobs_on_startup(is_primary_instance=True)

        assert backend.cleanup_calls == [{"node_id": "node-a"}]
        assert count == 5


class TestSkipUnscopedOrphanSweepDoesNotLogAtWarningLevel:
    """Bug #1549 Finding 2b (Codex-confirmed): under `uvicorn --workers N`,
    every worker runs initialize_services(); exactly one acquires the
    primary-instance lock and the other N-1 correctly, routinely take the
    non-primary path -- entirely expected on EVERY multi-worker startup,
    not a signal of a caller bug. The pre-fix WARNING here is not in
    LOG_AUDIT_ALLOWLIST, so a normal multi-worker startup failed the
    mandatory post-E2E log-audit gate (Phase 3/4). Per the established
    Bug #1535 precedent (demote a happy-path, by-design log line to
    DEBUG rather than allowlisting it), this is demoted."""

    def test_skip_does_not_log_warning(self, db_path: str, caplog) -> None:
        import logging

        tracker = JobTracker(db_path)
        with caplog.at_level(logging.DEBUG):
            skipped_sweep_count = tracker.cleanup_orphaned_jobs_on_startup(
                is_primary_instance=False
            )

        assert skipped_sweep_count == 0, (
            "a skipped sweep must report zero orphans cleaned"
        )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            "non-primary-instance skip must not log at WARNING+ (routine "
            f"multi-worker startup), found: {warnings}"
        )
