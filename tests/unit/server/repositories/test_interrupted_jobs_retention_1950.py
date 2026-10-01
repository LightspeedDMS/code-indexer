"""
Bug #1950 consumer audit: every place that enumerates terminal job statuses
for retention/eviction purposes must include 'interrupted' (the status Bug
#1950 introduced for a job terminated by an orderly server restart/shutdown,
distinct from a genuine 'failed').

Without these fixes, an INTERRUPTED job:
  - is never evicted from BackgroundJobManager.jobs / JobTracker._active_jobs
    in-memory dicts (a permanent per-process memory leak), and
  - is never deleted by any retention/cleanup path (startup sweep, the
    scheduled DataRetentionScheduler, or the admin manual-cleanup endpoint),
    so its row accumulates in the background_jobs table forever.

Each test below reproduces one such consumer and proves it now treats
'interrupted' exactly like 'completed'/'failed'/'cancelled'.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.services.job_tracker import JobTracker, TrackedJob
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.background_jobs_backend import (
    BackgroundJobsSqliteBackend,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

_OLD_ISO = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
_RECENT_ISO = datetime.now(timezone.utc).isoformat()


def _insert_job_row(
    db_path: str,
    job_id: str,
    status: str,
    completed_at: str,
    operation_type: str = "test_op",
) -> None:
    """Insert a background_jobs row directly, bypassing all backends."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO background_jobs
           (job_id, operation_type, status, created_at, completed_at,
            progress, username, is_admin, cancelled, resolution_attempts)
           VALUES (?, ?, ?, ?, ?, 100, 'admin', 0, 0, 0)""",
        (job_id, operation_type, status, _OLD_ISO, completed_at),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Sqlite backend cleanup_old_jobs
# ---------------------------------------------------------------------------


class TestSqliteBackendCleanupOldJobs:
    def test_deletes_old_interrupted_row(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "jobs.db")
        DatabaseSchema(db_path).initialize_database()
        _insert_job_row(db_path, "interrupted-old", "interrupted", _OLD_ISO)
        _insert_job_row(db_path, "completed-recent", "completed", _RECENT_ISO)

        backend = BackgroundJobsSqliteBackend(db_path)
        deleted = backend.cleanup_old_jobs(max_age_hours=1)

        assert deleted == 1, (
            f"Expected exactly the old interrupted row deleted, got {deleted}"
        )
        assert backend.get_job("interrupted-old") is None, (
            "Bug #1950: an old INTERRUPTED row must be retention-cleaned "
            "like completed/failed/cancelled, not kept forever."
        )
        assert backend.get_job("completed-recent") is not None, (
            "A recent row (within retention) must NOT be deleted."
        )

    def test_deletes_old_completed_partial_row(self, tmp_path: Path) -> None:
        """Same gap existed for completed_partial (Bug #679) before this fix."""
        db_path = str(tmp_path / "jobs.db")
        DatabaseSchema(db_path).initialize_database()
        _insert_job_row(db_path, "partial-old", "completed_partial", _OLD_ISO)

        backend = BackgroundJobsSqliteBackend(db_path)
        deleted = backend.cleanup_old_jobs(max_age_hours=1)

        assert deleted == 1
        assert backend.get_job("partial-old") is None


# ---------------------------------------------------------------------------
# 2. Postgres backend cleanup_old_jobs (mocked ConnectionPool -- this repo's
#    established pattern for this backend's unit tests; no real PG required)
# ---------------------------------------------------------------------------


def _make_pg_pool(rowcount: int = 0):
    cur = MagicMock()
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cur


class TestPostgresBackendCleanupOldJobs:
    def test_where_clause_includes_interrupted_and_completed_partial(self) -> None:
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, cur = _make_pg_pool(rowcount=2)
        backend = BackgroundJobsPostgresBackend(pool)

        backend.cleanup_old_jobs(max_age_hours=24)

        executed_sql, params = cur.execute.call_args[0]
        assert executed_sql.count("%s") == len(params), (
            "SQL placeholder count must match the bound params tuple"
        )
        assert "interrupted" in params, (
            "Bug #1950: cleanup_old_jobs' bound params must include "
            "'interrupted' or restart-artifact rows accumulate forever in "
            "the cluster-shared table."
        )
        assert "completed_partial" in params


# ---------------------------------------------------------------------------
# 3. BackgroundJobManager.cleanup_old_jobs (in-memory eviction)
# ---------------------------------------------------------------------------


class TestBackgroundJobManagerCleanupOldJobs:
    def test_evicts_old_interrupted_job_from_memory(self) -> None:
        manager = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=5,
            ),
        )
        old_completed_at = datetime.now(timezone.utc) - timedelta(hours=48)
        manager.jobs["interrupted-job"] = BackgroundJob(
            job_id="interrupted-job",
            operation_type="test_op",
            status=JobStatus.INTERRUPTED,
            created_at=old_completed_at,
            started_at=old_completed_at,
            completed_at=old_completed_at,
            result=None,
            error="Job interrupted by server restart",
            progress=0,
            username="testuser",
        )

        cleaned = manager.cleanup_old_jobs(max_age_hours=1)

        assert cleaned >= 1
        assert "interrupted-job" not in manager.jobs, (
            "Bug #1950: an old INTERRUPTED job must be evicted from "
            "in-memory manager.jobs, not retained forever."
        )


# ---------------------------------------------------------------------------
# 4. JobTracker._evict_stale_from_memory
# ---------------------------------------------------------------------------


class TestJobTrackerEvictStaleFromMemory:
    def test_removes_interrupted_job(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "tracker.db")
        DatabaseSchema(db_path).initialize_database()
        tracker = JobTracker(db_path)

        old_completed_at = datetime.now(timezone.utc) - timedelta(hours=48)
        tracker._active_jobs["interrupted-active"] = TrackedJob(
            job_id="interrupted-active",
            operation_type="test_op",
            status="interrupted",
            username="admin",
            completed_at=old_completed_at,
        )

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        tracker._evict_stale_from_memory("test_op", cutoff)

        assert "interrupted-active" not in tracker._active_jobs, (
            "Bug #1950: an old INTERRUPTED TrackedJob must be evicted from "
            "_active_jobs, not leak in memory forever."
        )


# ---------------------------------------------------------------------------
# 5. JobTracker.cleanup_old_jobs -- raw SQLite fallback path (no backend)
# ---------------------------------------------------------------------------


class TestJobTrackerCleanupOldJobsRawSqlite:
    def test_deletes_old_interrupted_row(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "tracker.db")
        DatabaseSchema(db_path).initialize_database()
        _insert_job_row(db_path, "interrupted-raw", "interrupted", _OLD_ISO)

        tracker = JobTracker(db_path)
        deleted = tracker.cleanup_old_jobs("test_op", max_age_hours=1)

        assert deleted == 1
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT 1 FROM background_jobs WHERE job_id = ?", ("interrupted-raw",)
        ).fetchone()
        conn.close()
        assert row is None, (
            "Bug #1950: JobTracker.cleanup_old_jobs' raw-SQLite DELETE path "
            "must remove an old INTERRUPTED row."
        )


# ---------------------------------------------------------------------------
# 6. JobTracker.cleanup_old_jobs -- backend-delegated path
# ---------------------------------------------------------------------------


class TestJobTrackerCleanupOldJobsBackendPath:
    def test_deletes_old_interrupted_row(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "tracker.db")
        DatabaseSchema(db_path).initialize_database()
        _insert_job_row(db_path, "interrupted-backend", "interrupted", _OLD_ISO)

        backend = BackgroundJobsSqliteBackend(db_path)
        tracker = JobTracker(db_path, storage_backend=backend)
        deleted = tracker.cleanup_old_jobs("test_op", max_age_hours=1)

        assert deleted == 1
        assert backend.get_job("interrupted-backend") is None, (
            "Bug #1950: JobTracker.cleanup_old_jobs' backend-delegated path "
            "must delete an old INTERRUPTED row (not just completed/failed/"
            "cancelled)."
        )


# ---------------------------------------------------------------------------
# 7. DataRetentionScheduler background_jobs status_filter
# ---------------------------------------------------------------------------


def _make_retention_config_service(background_jobs_retention_hours: int = 1) -> Any:
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = 168
    ret_cfg.audit_logs_retention_hours = 720
    ret_cfg.sync_jobs_retention_hours = 168
    ret_cfg.dep_map_history_retention_hours = 720
    ret_cfg.background_jobs_retention_hours = background_jobs_retention_hours
    ret_cfg.cleanup_interval_hours = 1

    config = MagicMock()
    config.data_retention_config = ret_cfg
    config.jwt_expiration_minutes = 10

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


class TestDataRetentionSchedulerBackgroundJobsCleanup:
    def test_execute_cleanup_sqlite_deletes_old_interrupted_row(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        main_db_path = tmp_path / "main.db"
        DatabaseSchema(str(main_db_path)).initialize_database()
        _insert_job_row(
            str(main_db_path), "interrupted-retention", "interrupted", _OLD_ISO
        )

        # token_blacklist and elevated_session_manager are process-wide
        # singletons (code_indexer.server.app._token_blacklist and
        # code_indexer.server.auth.elevated_session_manager.elevated_session_manager)
        # touched by _execute_cleanup_sqlite() below. This test used to
        # defensively save/set/restore their SQLite path itself because
        # tests under tests/unit/server/web/ could leak a deleted tempdir
        # path into them. That leak is now prevented centrally by
        # tests/unit/server/web/conftest.py's autouse
        # _restore_dependency_globals fixture, which restores both
        # singletons' path state after every web/ test -- so by the time
        # this test runs, both are back to their process-default state and
        # no per-test workaround is needed here.
        scheduler = DataRetentionScheduler(
            log_db_path=tmp_path / "logs.db",
            main_db_path=main_db_path,
            groups_db_path=tmp_path / "groups.db",
            config_service=_make_retention_config_service(),
        )

        result = scheduler._execute_cleanup_sqlite()

        assert not result["failed_tables"], f"Unexpected cleanup failures: {result}"
        conn = sqlite3.connect(str(main_db_path))
        row = conn.execute(
            "SELECT 1 FROM background_jobs WHERE job_id = ?",
            ("interrupted-retention",),
        ).fetchone()
        conn.close()
        assert row is None, (
            "Bug #1950: DataRetentionScheduler's scheduled cleanup must "
            "delete an old INTERRUPTED background_jobs row, or it "
            "accumulates in the DB forever."
        )


# ---------------------------------------------------------------------------
# 8. Terminal write for INTERRUPTED flushes immediately (not debounced) --
#    proves the background_jobs.py:195 comment fix describes real behavior.
# ---------------------------------------------------------------------------


class TestInterruptedTerminalWriteFlushesImmediately:
    def test_interrupted_job_persists_to_sqlite_promptly(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "jobs.db")
        DatabaseSchema(db_path).initialize_database()
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=5,
            ),
        )
        try:

            def interrupted_by_shutdown():
                raise RuntimeError(
                    "Indexing interrupted by server shutdown for test-repo"
                )

            job_id = manager.submit_job(
                operation_type="test_op",
                func=interrupted_by_shutdown,
                submitter_username="testuser",
            )

            # Well under PROGRESS_DEBOUNCE_INTERVAL (0.5s) -- if the terminal
            # write were subject to that debounce, this would still show
            # 'running' at the deadline.
            deadline = time.monotonic() + 5.0
            status = None
            while time.monotonic() < deadline:
                conn = sqlite3.connect(db_path)
                row = conn.execute(
                    "SELECT status FROM background_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                conn.close()
                if row is not None and row[0] not in ("pending", "running"):
                    status = row[0]
                    break
                time.sleep(0.02)

            assert status == "interrupted", (
                f"Expected the DB row to reach 'interrupted' promptly, "
                f"got {status!r}. A stuck 'running' row here would mean the "
                "terminal write was silently subject to the progress "
                "debounce, losing the interruption status in the very "
                "shutdown that produced it."
            )
        finally:
            manager.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
