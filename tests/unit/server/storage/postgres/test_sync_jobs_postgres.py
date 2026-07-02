"""
Unit tests for SyncJobsPostgresBackend.

Story #413: PostgreSQL Backend for BackgroundJobs and SyncJobs

All tests mock the ConnectionPool — no real PostgreSQL required.
The mock hierarchy is:
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchone() / cur.fetchall()
    cur.rowcount
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchone=None, fetchall=None, rowcount=0):
    """
    Build a mock ConnectionPool whose .connection() context manager yields a
    mock connection whose .cursor() context manager yields a mock cursor.
    """
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


def _make_job_row(
    job_id="sync-1",
    username="alice",
    user_alias="alice-ws",
    job_type="full_sync",
    status="completed",
    created_at="2026-01-01T00:00:00+00:00",
    started_at=None,
    completed_at=None,
    repository_url=None,
    progress=100,
    error_message=None,
    phases=None,
    phase_weights=None,
    current_phase=None,
    progress_history=None,
    recovery_checkpoint=None,
    analytics_data=None,
):
    """Return a tuple matching the _SELECT_COLS column order."""
    return (
        job_id,
        username,
        user_alias,
        job_type,
        status,
        created_at,
        started_at,
        completed_at,
        repository_url,
        progress,
        error_message,
        json.dumps(phases) if phases else None,
        json.dumps(phase_weights) if phase_weights else None,
        current_phase,
        json.dumps(progress_history) if progress_history else None,
        json.dumps(recovery_checkpoint) if recovery_checkpoint else None,
        json.dumps(analytics_data) if analytics_data else None,
    )


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_create_job_executes_insert(self):
        """
        Given a SyncJobsPostgresBackend with a mocked pool
        When create_job() is called
        Then it executes an INSERT statement with correct parameters.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.create_job(
            job_id="sync-1",
            username="alice",
            user_alias="alice-ws",
            job_type="full_sync",
            status="pending",
        )

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO sync_jobs" in sql
        assert params[0] == "sync-1"
        assert params[1] == "alice"
        assert params[2] == "alice-ws"
        assert params[3] == "full_sync"
        assert params[4] == "pending"

    def test_create_job_sets_progress_to_zero(self):
        """
        When create_job() is called
        Then the progress column is inserted as 0.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.create_job(
            job_id="sync-2",
            username="bob",
            user_alias="bob-ws",
            job_type="incremental",
            status="pending",
        )

        _, params = cur.execute.call_args[0]
        assert params[-1] == 0  # progress is last param

    def test_create_job_passes_repository_url(self):
        """
        Given a repository_url
        When create_job() is called
        Then the URL is included in the INSERT parameters.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.create_job(
            job_id="sync-3",
            username="carol",
            user_alias="carol-ws",
            job_type="full_sync",
            status="pending",
            repository_url="https://github.com/org/repo.git",
        )

        _, params = cur.execute.call_args[0]
        assert "https://github.com/org/repo.git" in params


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_get_job_returns_dict_when_found(self):
        """
        Given a row in sync_jobs
        When get_job() is called
        Then it returns a dict with all fields deserialised correctly.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        row = _make_job_row(job_id="sync-1", status="completed", progress=100)
        pool, conn, cur = _make_pool(fetchone=row)
        backend = SyncJobsPostgresBackend(pool)

        result = backend.get_job("sync-1")

        assert result is not None
        assert result["job_id"] == "sync-1"
        assert result["status"] == "completed"
        assert result["progress"] == 100

    def test_get_job_returns_none_when_not_found(self):
        """
        Given no matching row
        When get_job() is called
        Then None is returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchone=None)
        backend = SyncJobsPostgresBackend(pool)

        result = backend.get_job("nonexistent")

        assert result is None

    def test_get_job_deserialises_phases_json(self):
        """
        Given a job row with a JSON phases column
        When get_job() is called
        Then phases is returned as a dict (not a JSON string).
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        phases_data = {"clone": "pending", "index": "pending"}
        row = _make_job_row(job_id="sync-1", phases=phases_data)
        pool, conn, cur = _make_pool(fetchone=row)
        backend = SyncJobsPostgresBackend(pool)

        job = backend.get_job("sync-1")

        assert job["phases"] == phases_data

    def test_get_job_none_json_columns_return_none(self):
        """
        Given a job row with NULL JSON columns
        When get_job() is called
        Then those fields are None (not JSON strings).
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        row = _make_job_row(job_id="sync-1", phases=None, analytics_data=None)
        pool, conn, cur = _make_pool(fetchone=row)
        backend = SyncJobsPostgresBackend(pool)

        job = backend.get_job("sync-1")

        assert job["phases"] is None
        assert job["analytics_data"] is None


# ---------------------------------------------------------------------------
# update_job
# ---------------------------------------------------------------------------


class TestUpdateJob:
    def test_update_job_builds_correct_sql(self):
        """
        Given a job_id and keyword arguments
        When update_job() is called
        Then it executes an UPDATE with SET clauses for each kwarg.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.update_job("sync-1", status="completed", progress=100)

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "UPDATE sync_jobs SET" in sql
        assert "status = %s" in sql
        assert "progress = %s" in sql
        assert "WHERE job_id = %s" in sql
        assert "completed" in params
        assert 100 in params
        assert "sync-1" in params

    def test_update_job_serialises_phases_as_json(self):
        """
        Given a phases dict passed to update_job
        When update_job() is called
        Then the phases value is serialised as a JSON string in params.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        phases = {"clone": "completed", "index": "running"}
        backend.update_job("sync-1", phases=phases)

        _, params = cur.execute.call_args[0]
        assert json.loads(params[0]) == phases

    def test_update_job_skips_none_values(self):
        """
        Given kwargs where all values are None
        When update_job() is called
        Then no SQL is executed (None values are skipped).
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.update_job("sync-1", error_message=None, completed_at=None)

        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_list_jobs_returns_all_jobs(self):
        """
        Given two jobs in the database
        When list_jobs() is called
        Then both are returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        rows = [_make_job_row(job_id="s1"), _make_job_row(job_id="s2")]
        pool, conn, cur = _make_pool(fetchall=rows)
        backend = SyncJobsPostgresBackend(pool)

        jobs = backend.list_jobs()

        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "s1"
        assert jobs[1]["job_id"] == "s2"

    def test_list_jobs_returns_empty_when_no_jobs(self):
        """
        Given no jobs in the database
        When list_jobs() is called
        Then an empty list is returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[])
        backend = SyncJobsPostgresBackend(pool)

        jobs = backend.list_jobs()

        assert jobs == []

    def test_list_jobs_executes_select_from_sync_jobs(self):
        """
        When list_jobs() is called
        Then the SQL selects from the sync_jobs table.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[])
        backend = SyncJobsPostgresBackend(pool)

        backend.list_jobs()

        sql = cur.execute.call_args[0][0]
        assert "sync_jobs" in sql


# ---------------------------------------------------------------------------
# delete_job
# ---------------------------------------------------------------------------


class TestDeleteJob:
    def test_delete_job_returns_true_when_deleted(self):
        """
        Given a job that exists
        When delete_job() is called
        Then True is returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=1)
        backend = SyncJobsPostgresBackend(pool)

        result = backend.delete_job("sync-1")

        assert result is True

    def test_delete_job_returns_false_when_not_found(self):
        """
        Given a job that does not exist
        When delete_job() is called
        Then False is returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = SyncJobsPostgresBackend(pool)

        result = backend.delete_job("nonexistent")

        assert result is False

    def test_delete_job_executes_delete_sql(self):
        """
        When delete_job() is called
        Then a DELETE FROM sync_jobs WHERE job_id = %s statement is executed.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=1)
        backend = SyncJobsPostgresBackend(pool)

        backend.delete_job("sync-1")

        sql, params = cur.execute.call_args[0]
        assert "DELETE FROM sync_jobs" in sql
        assert params == ("sync-1",)


# ---------------------------------------------------------------------------
# cleanup_orphaned_jobs_on_startup
# ---------------------------------------------------------------------------


class TestCleanupOrphanedJobsOnStartup:
    def test_cleanup_marks_running_and_pending_as_failed(self):
        """
        When cleanup_orphaned_jobs_on_startup() is called
        Then the UPDATE SQL targets 'running' and 'pending' statuses
        and sets status to 'failed'.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=3)
        backend = SyncJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 3
        sql, params = cur.execute.call_args[0]
        assert "running" in sql
        assert "pending" in sql
        assert "failed" in sql
        assert "Job interrupted by server restart" in params

    def test_cleanup_returns_zero_when_no_orphans(self):
        """
        Given no running/pending jobs
        When cleanup_orphaned_jobs_on_startup() is called
        Then 0 is returned.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = SyncJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 0


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_delegates_to_pool(self):
        """
        When close() is called
        Then pool.close() is invoked.
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        backend.close()

        pool.close.assert_called_once()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_backend_satisfies_sync_jobs_backend_protocol(self):
        """
        SyncJobsPostgresBackend must satisfy the SyncJobsBackend Protocol
        (runtime_checkable via isinstance()).
        """
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )
        from code_indexer.server.storage.protocols import SyncJobsBackend

        pool, _, _ = _make_pool()
        backend = SyncJobsPostgresBackend(pool)

        assert isinstance(backend, SyncJobsBackend)
