"""
Story #876 — PostgreSQL symmetry tests for executing_node / claimed_at fields.

Nit #5 (codex): BackgroundJobsPostgresBackend.save_job() and get_job() must
accept and return executing_node / claimed_at to match the SQLite backend and
the PostgreSQL schema (migrations 001 + 005).

Four tests mirror the mock pattern established in test_background_jobs_postgres.py:
  1. save_job INSERT params include executing_node/claimed_at when values supplied.
  2. save_job INSERT params default both to None when kwargs are omitted.
  3. get_job row-mapper exposes both keys with correct values when row has data.
  4. get_job row-mapper exposes both keys as None when row has NULL.

No real PostgreSQL connection is required; all tests use the _make_pool() mock
helper copied from the sibling test file.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers (identical pattern to test_background_jobs_postgres.py)
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


def _make_full_row(
    job_id="job-sym-1",
    operation_type="refresh_golden_repo",
    status="running",
    created_at="2026-04-21T00:00:00+00:00",
    started_at=None,
    completed_at=None,
    result=None,
    error=None,
    progress=0,
    username="admin",
    is_admin=False,
    cancelled=False,
    repo_alias="repo-x",
    resolution_attempts=0,
    claude_actions=None,
    failure_reason=None,
    extended_error=None,
    language_resolution_status=None,
    progress_info=None,
    metadata=None,
    executing_node="node-1",
    claimed_at="2026-04-21T00:01:00+00:00",
):
    """Return a tuple matching _SELECT_COLS column order after Fix #5.

    Column positions 0-19 mirror the existing 20-column _SELECT_COLS.
    Positions 20 and 21 are the new executing_node and claimed_at columns.
    """
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
        progress_info,
        json.dumps(metadata) if metadata else None,
        executing_node,
        claimed_at,
    )


# ---------------------------------------------------------------------------
# Test 1 — save_job INSERT params include values when supplied
# ---------------------------------------------------------------------------


class TestSaveJobSymmetry:
    def test_save_job_with_executing_node_and_claimed_at_includes_both_in_params(self):
        """
        When save_job() is called with executing_node and claimed_at
        Then both column names appear in the INSERT SQL and both values
        appear in the parameter tuple.

        Symmetry invariant: PG backend must accept the same kwargs as
        BackgroundJobsSqliteBackend (Story #876 Nit #5).
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.save_job(
            job_id="job-sym-1",
            operation_type="refresh_golden_repo",
            status="running",
            created_at="2026-04-21T00:00:00+00:00",
            username="admin",
            progress=0,
            executing_node="node-1",
            claimed_at="2026-04-21T00:01:00+00:00",
        )

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "executing_node" in sql, (
            "INSERT statement must name executing_node column (Story #876 Nit #5)."
        )
        assert "claimed_at" in sql, (
            "INSERT statement must name claimed_at column (Story #876 Nit #5)."
        )
        assert "node-1" in params, (
            "executing_node value 'node-1' must appear in INSERT params."
        )
        assert "2026-04-21T00:01:00+00:00" in params, (
            "claimed_at value must appear in INSERT params."
        )

    def test_save_job_defaults_executing_node_and_claimed_at_to_none(self):
        """
        When save_job() is called WITHOUT executing_node/claimed_at kwargs
        Then both columns are still named in the INSERT SQL and their
        positional slots in the parameter tuple are None.

        Backward-compat: existing callers that never set these fields must
        not break after Fix #5 is applied.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        pool, conn, cur = _make_pool()
        backend = BackgroundJobsPostgresBackend(pool)

        backend.save_job(
            job_id="job-sym-2",
            operation_type="dependency_map_refresh",
            status="pending",
            created_at="2026-04-21T00:00:00+00:00",
            username="admin",
            progress=0,
        )

        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args[0]
        assert "executing_node" in sql, (
            "INSERT must include executing_node column even when value is None."
        )
        assert "claimed_at" in sql, (
            "INSERT must include claimed_at column even when value is None."
        )
        # The last two positional params must be None (executing_node, claimed_at)
        assert params[-2] is None, (
            "executing_node param must be None when not supplied to save_job."
        )
        assert params[-1] is None, (
            "claimed_at param must be None when not supplied to save_job."
        )


# ---------------------------------------------------------------------------
# Test 3 — get_job row-mapper exposes both keys with correct values
# ---------------------------------------------------------------------------


class TestGetJobSymmetry:
    def test_get_job_returns_executing_node_and_claimed_at_when_present(self):
        """
        Given a row where executing_node='node-2' and claimed_at is set
        When get_job() is called
        Then the returned dict contains both keys with the correct values.

        _row_to_dict must map positions 20 and 21 (after the existing 20
        columns selected by _SELECT_COLS) to executing_node and claimed_at.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_full_row(
            job_id="job-sym-3",
            executing_node="node-2",
            claimed_at="2026-04-21T10:00:00+00:00",
        )
        pool, conn, cur = _make_pool(fetchone=row)
        backend = BackgroundJobsPostgresBackend(pool)

        job = backend.get_job("job-sym-3")

        assert job is not None
        assert "executing_node" in job, (
            "get_job result must include 'executing_node' key (Story #876 Nit #5)."
        )
        assert job["executing_node"] == "node-2", (
            f"executing_node must be 'node-2', got {job.get('executing_node')!r}."
        )
        assert "claimed_at" in job, (
            "get_job result must include 'claimed_at' key (Story #876 Nit #5)."
        )
        assert job["claimed_at"] == "2026-04-21T10:00:00+00:00", (
            f"claimed_at must be preserved by _row_to_dict, got {job.get('claimed_at')!r}."
        )

    def test_get_job_returns_none_for_executing_node_and_claimed_at_when_null(self):
        """
        Given a row where executing_node and claimed_at are NULL (None)
        When get_job() is called
        Then both keys are present in the dict with value None.

        Jobs created before Story #876 have NULL in these columns and must
        still be readable without raising KeyError or AttributeError.
        """
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )

        row = _make_full_row(
            job_id="job-sym-4",
            executing_node=None,
            claimed_at=None,
        )
        pool, conn, cur = _make_pool(fetchone=row)
        backend = BackgroundJobsPostgresBackend(pool)

        job = backend.get_job("job-sym-4")

        assert job is not None
        assert "executing_node" in job, (
            "get_job must always include 'executing_node' key (Story #876)."
        )
        assert job["executing_node"] is None, (
            "executing_node must be None for pre-876 rows."
        )
        assert "claimed_at" in job, (
            "get_job must always include 'claimed_at' key (Story #876)."
        )
        assert job["claimed_at"] is None, "claimed_at must be None for pre-876 rows."
