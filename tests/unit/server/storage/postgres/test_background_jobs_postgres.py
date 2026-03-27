"""
Unit tests for BackgroundJobsPostgresBackend.

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
    job_id="job-1",
    operation_type="index",
    status="completed",
    created_at="2026-01-01T00:00:00+00:00",
    started_at=None,
    completed_at=None,
    result=None,
    error=None,
    progress=100,
    username="alice",
    is_admin=False,
    cancelled=False,
    repo_alias="my-repo",
    resolution_attempts=0,
    claude_actions=None,
    failure_reason=None,
    extended_error=None,
    language_resolution_status=None,
):
    """Return a tuple matching the _SELECT_COLS column order."""
    return (
        job_id,
        operation_type,
        status,
        created_at,
        started_at,
        completed_at,
        json.dumps(result) if result else None,
        error,
        progress,
        username,
        is_admin,
        cancelled,
        repo_alias,
        resolution_attempts,
        json.dumps(claude_actions) if claude_actions else None,
        failure_reason,
        json.dumps(extended_error) if extended_error else None,
        json.dumps(language_resolution_status) if language_resolution_status else None,
    )


# ---------------------------------------------------------------------------
# save_job
# ---------------------------------------------------------------------------


class TestSaveJob:
    def test_save_job_executes_insert(self):
        """
        Given a BackgroundJobsPostgresBackend with a mocked pool
        When save_job() is called
        Then it executes an INSERT statement with correct parameters.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.save_job(
            job_id="job-1",
            operation_type="index",
            status="pending",
            created_at="2026-01-01T00:00:00+00:00",
            username="alice",
            progress=0,
        )

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO background_jobs" in sql
        assert params[0] == "job-1"
        assert params[1] == "index"
        assert params[2] == "pending"
        assert params[9] == "alice"

    def test_save_job_serialises_result_as_json(self):
        """
        Given a result dict
        When save_job() is called
        Then the result is serialised as JSON in the INSERT parameters.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        result_data = {"files_indexed": 42}
        backend.save_job(
            job_id="job-2",
            operation_type="index",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
            username="bob",
            progress=100,
            result=result_data,
        )

        _, params = cur.execute.call_args[0]
        assert json.loads(params[6]) == result_data

    def test_save_job_none_result_stored_as_none(self):
        """
        Given no result
        When save_job() is called
        Then the result parameter is None (not the string 'null').
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.save_job(
            job_id="job-3",
            operation_type="index",
            status="pending",
            created_at="2026-01-01T00:00:00+00:00",
            username="carol",
            progress=0,
        )

        _, params = cur.execute.call_args[0]
        assert params[6] is None


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_get_job_returns_dict_when_found(self):
        """
        Given a row in background_jobs
        When get_job() is called
        Then it returns a dict with all fields deserialised correctly.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_job_row(job_id="job-1", status="completed", progress=100)
        pool, conn, cur = _make_pool(fetchone=row)
        backend = BackgroundJobsPostgresBackend(pool)

        result = backend.get_job("job-1")

        assert result is not None
        assert result["job_id"] == "job-1"
        assert result["status"] == "completed"
        assert result["progress"] == 100

    def test_get_job_returns_none_when_not_found(self):
        """
        Given no matching row
        When get_job() is called
        Then None is returned.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchone=None)
        backend = BackgroundJobsPostgresBackend(pool)

        result = backend.get_job("nonexistent")

        assert result is None

    def test_get_job_deserialises_result_json(self):
        """
        Given a job row with a JSON result column
        When get_job() is called
        Then result is returned as a dict (not a JSON string).
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_job_row(job_id="job-1", result={"files": 7})
        pool, conn, cur = _make_pool(fetchone=row)
        backend = BackgroundJobsPostgresBackend(pool)

        job = backend.get_job("job-1")

        assert job["result"] == {"files": 7}

    def test_get_job_bool_fields_are_booleans(self):
        """
        Given a job row with boolean is_admin and cancelled columns
        When get_job() is called
        Then is_admin and cancelled are Python booleans.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_job_row(is_admin=True, cancelled=False)
        pool, conn, cur = _make_pool(fetchone=row)
        backend = BackgroundJobsPostgresBackend(pool)

        job = backend.get_job("job-1")

        assert job["is_admin"] is True
        assert job["cancelled"] is False


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
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.update_job("job-1", status="completed", progress=100)

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "UPDATE background_jobs SET" in sql
        assert "status = %s" in sql
        assert "progress = %s" in sql
        assert "WHERE job_id = %s" in sql
        assert "completed" in params
        assert 100 in params
        assert "job-1" in params

    def test_update_job_serialises_json_fields(self):
        """
        Given a result dict passed to update_job
        When update_job() is called
        Then the result value is serialised as a JSON string in params.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.update_job("job-1", result={"ok": True})

        _, params = cur.execute.call_args[0]
        assert json.loads(params[0]) == {"ok": True}

    def test_update_job_noop_when_no_kwargs(self):
        """
        Given no keyword arguments
        When update_job() is called
        Then no SQL is executed.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.update_job("job-1")

        cur.execute.assert_not_called()


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_list_jobs_returns_all_when_no_filters(self):
        """
        Given two jobs in the database
        When list_jobs() is called with no filters
        Then both are returned.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        rows = [_make_job_row(job_id="j1"), _make_job_row(job_id="j2")]
        pool, conn, cur = _make_pool(fetchall=rows)
        backend = BackgroundJobsPostgresBackend(pool)

        jobs = backend.list_jobs()

        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "j1"
        assert jobs[1]["job_id"] == "j2"

    def test_list_jobs_adds_username_filter(self):
        """
        Given a username filter
        When list_jobs() is called
        Then the SQL contains a username = %s condition.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[])
        backend = BackgroundJobsPostgresBackend(pool)

        backend.list_jobs(username="alice")

        sql, params = cur.execute.call_args[0]
        assert "username = %s" in sql
        assert "alice" in params

    def test_list_jobs_adds_status_filter(self):
        """
        Given a status filter
        When list_jobs() is called
        Then the SQL contains a status = %s condition.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[])
        backend = BackgroundJobsPostgresBackend(pool)

        backend.list_jobs(status="failed")

        sql, params = cur.execute.call_args[0]
        assert "status = %s" in sql
        assert "failed" in params


# ---------------------------------------------------------------------------
# list_jobs_filtered
# ---------------------------------------------------------------------------


class TestListJobsFiltered:
    def _make_filtered_pool(self, count=1, rows=None):
        """Build a mock pool for list_jobs_filtered() which runs 2 execute() calls."""
        cur = MagicMock()
        cur.fetchone.return_value = (count,)
        cur.fetchall.return_value = rows if rows is not None else []
        cur.rowcount = 0

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        return pool, cur

    def test_returns_tuple_of_jobs_and_total(self):
        """
        When list_jobs_filtered() is called
        Then it returns a (list, int) tuple.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_job_row()
        pool, cur = self._make_filtered_pool(count=1, rows=[row])
        backend = BackgroundJobsPostgresBackend(pool)

        jobs, total = backend.list_jobs_filtered()

        assert isinstance(jobs, list)
        assert isinstance(total, int)
        assert total == 1
        assert len(jobs) == 1

    def test_search_text_filter_uses_lower_like(self):
        """
        Given a search_text value
        When list_jobs_filtered() is called
        Then the SQL includes LOWER(.) LIKE LOWER(%s) conditions.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, cur = self._make_filtered_pool(count=0, rows=[])
        backend = BackgroundJobsPostgresBackend(pool)

        backend.list_jobs_filtered(search_text="myrepo")

        all_sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert any("LOWER" in sql for sql in all_sqls)

    def test_exclude_ids_produces_not_in_clause(self):
        """
        Given a set of exclude_ids
        When list_jobs_filtered() is called
        Then the SQL contains a NOT IN clause.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, cur = self._make_filtered_pool(count=0, rows=[])
        backend = BackgroundJobsPostgresBackend(pool)

        backend.list_jobs_filtered(exclude_ids={"job-x", "job-y"})

        all_sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert any("NOT IN" in sql for sql in all_sqls)


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
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=1)
        backend = BackgroundJobsPostgresBackend(pool)

        result = backend.delete_job("job-1")

        assert result is True

    def test_delete_job_returns_false_when_not_found(self):
        """
        Given a job that does not exist
        When delete_job() is called
        Then False is returned.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = BackgroundJobsPostgresBackend(pool)

        result = backend.delete_job("nonexistent")

        assert result is False


# ---------------------------------------------------------------------------
# cleanup_old_jobs
# ---------------------------------------------------------------------------


class TestCleanupOldJobs:
    def test_cleanup_old_jobs_returns_count(self):
        """
        When cleanup_old_jobs() is called
        Then it returns the number of rows deleted.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=3)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_old_jobs(max_age_hours=24)

        assert count == 3

    def test_cleanup_old_jobs_sql_targets_terminal_statuses(self):
        """
        When cleanup_old_jobs() is called
        Then the DELETE SQL targets completed, failed, and cancelled statuses.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = BackgroundJobsPostgresBackend(pool)

        backend.cleanup_old_jobs()

        sql, _ = cur.execute.call_args[0]
        assert "completed" in sql
        assert "failed" in sql
        assert "cancelled" in sql


# ---------------------------------------------------------------------------
# count_jobs_by_status
# ---------------------------------------------------------------------------


class TestCountJobsByStatus:
    def test_count_jobs_by_status_returns_dict(self):
        """
        When count_jobs_by_status() is called
        Then it returns a dict mapping status strings to counts.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(
            fetchall=[("completed", 5), ("failed", 2), ("pending", 1)]
        )
        backend = BackgroundJobsPostgresBackend(pool)

        counts = backend.count_jobs_by_status()

        assert counts == {"completed": 5, "failed": 2, "pending": 1}


# ---------------------------------------------------------------------------
# get_job_stats
# ---------------------------------------------------------------------------


class TestGetJobStats:
    def test_get_job_stats_returns_completed_and_failed(self):
        """
        When get_job_stats() is called
        Then it returns a dict with at least 'completed' and 'failed' keys.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[("completed", 10), ("failed", 3)])
        backend = BackgroundJobsPostgresBackend(pool)

        stats = backend.get_job_stats("24h")

        assert stats["completed"] == 10
        assert stats["failed"] == 3

    def test_get_job_stats_defaults_missing_keys_to_zero(self):
        """
        Given no rows for 'failed'
        When get_job_stats() is called
        Then 'failed' defaults to 0.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(fetchall=[("completed", 7)])
        backend = BackgroundJobsPostgresBackend(pool)

        stats = backend.get_job_stats("24h")

        assert stats["failed"] == 0


# ---------------------------------------------------------------------------
# cleanup_orphaned_jobs_on_startup
# ---------------------------------------------------------------------------


class TestCleanupOrphanedJobsOnStartup:
    def test_cleanup_orphaned_marks_running_and_pending_as_failed(self):
        """
        When cleanup_orphaned_jobs_on_startup() is called
        Then the UPDATE SQL targets 'running' and 'pending' statuses.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=2)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 2
        sql, params = cur.execute.call_args[0]
        assert "running" in sql
        assert "pending" in sql
        assert "failed" in sql
        assert "Job interrupted by server restart" in params

    def test_cleanup_orphaned_returns_zero_when_no_orphans(self):
        """
        Given no running/pending jobs
        When cleanup_orphaned_jobs_on_startup() is called
        Then 0 is returned.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 0


# ---------------------------------------------------------------------------
# cleanup_orphaned_jobs_on_startup — node-scoped (Issue #535)
# ---------------------------------------------------------------------------


class TestCleanupOrphanedJobsOnStartupNodeScoped:
    """In PG mode, cleanup must be scoped to the restarting node only."""

    def test_cleanup_with_node_id_none_returns_zero_and_executes_no_sql(self):
        """
        When cleanup_orphaned_jobs_on_startup() is called with node_id=None
        in postgres mode, it must return 0 and NOT execute any UPDATE SQL.

        This is the safe default — without a node_id we cannot know which jobs
        belong to this node, so we do nothing rather than kill jobs on healthy
        nodes.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=5)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id=None)

        assert count == 0
        # No UPDATE must have been executed
        cur.execute.assert_not_called()

    def test_cleanup_with_node_id_scopes_where_to_executing_node(self):
        """
        When cleanup_orphaned_jobs_on_startup(node_id='node-A') is called,
        the UPDATE SQL must include AND executing_node = %s with 'node-A'.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=1)
        backend = BackgroundJobsPostgresBackend(pool)

        count = backend.cleanup_orphaned_jobs_on_startup(node_id="node-A")

        assert count == 1
        sql, params = cur.execute.call_args[0]
        assert "executing_node" in sql
        assert "node-A" in params

    def test_cleanup_with_node_id_does_not_affect_other_nodes(self):
        """
        When cleanup_orphaned_jobs_on_startup(node_id='node-A') is called,
        the WHERE clause must NOT match jobs from other nodes.
        Specifically, 'node-B' must not appear as a param or unbounded condition.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=1)
        backend = BackgroundJobsPostgresBackend(pool)

        backend.cleanup_orphaned_jobs_on_startup(node_id="node-A")

        sql, params = cur.execute.call_args[0]
        # The only node value in params must be 'node-A', not 'node-B'
        assert "node-B" not in params
        # The SQL must not be an unbounded UPDATE (no executing_node filter missing)
        assert "executing_node" in sql

    def test_cleanup_no_node_id_logs_warning(self, caplog):
        """
        When cleanup_orphaned_jobs_on_startup() is called with node_id=None,
        a WARNING must be logged explaining the no-op decision.
        """
        import logging

        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool(rowcount=0)
        backend = BackgroundJobsPostgresBackend(pool)

        with caplog.at_level(logging.WARNING):
            backend.cleanup_orphaned_jobs_on_startup(node_id=None)

        assert any(
            "node_id" in record.message.lower() or "node" in record.message.lower()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_backend_satisfies_background_jobs_backend_protocol(self):
        """
        BackgroundJobsPostgresBackend must satisfy the BackgroundJobsBackend Protocol
        (runtime_checkable via isinstance()).
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )
        from code_indexer.server.storage.protocols import BackgroundJobsBackend

        pool, _, _ = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        assert isinstance(backend, BackgroundJobsBackend)
