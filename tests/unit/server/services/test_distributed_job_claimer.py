"""
Unit tests for DistributedJobClaimer.

Story #421: Atomic Distributed Job Claiming with Conflict Resolution

Mock hierarchy (no real PostgreSQL required):
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchone() / cur.fetchall()
    cur.rowcount
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


from code_indexer.server.services.distributed_job_claimer import DistributedJobClaimer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchone=None, fetchall=None, rowcount=1):
    """Build a mocked ConnectionPool, returning cur for assertion checks."""
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
    status="running",
    created_at="2026-01-01T00:00:00+00:00",
    started_at="2026-01-01T00:01:00+00:00",
    completed_at=None,
    result=None,
    error=None,
    progress=0,
    username="alice",
    is_admin=False,
    cancelled=False,
    repo_alias="my-repo",
    resolution_attempts=0,
    claude_actions=None,
    failure_reason=None,
    extended_error=None,
    language_resolution_status=None,
    executing_node="node-1",
    claimed_at="2026-01-01T00:01:00+00:00",
):
    """Build a tuple matching the _SELECT_COLS column order."""
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
        executing_node,
        claimed_at,
    )


NODE_ID = "node-1"


# ---------------------------------------------------------------------------
# claim_next_job
# ---------------------------------------------------------------------------


class TestClaimNextJob:
    def test_claim_executes_update_with_for_update_skip_locked(self):
        """
        Given a pending job row returned by the DB
        When claim_next_job() is called
        Then the SQL contains FOR UPDATE SKIP LOCKED and RETURNING.
        """
        row = _make_job_row()
        pool, _, cur = _make_pool(fetchone=row)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.claim_next_job()

        assert result is not None
        assert result["job_id"] == "job-1"
        assert result["status"] == "running"
        assert result["executing_node"] == "node-1"

        sql_called = cur.execute.call_args[0][0]
        assert "FOR UPDATE SKIP LOCKED" in sql_called
        assert "RETURNING" in sql_called

    def test_claim_sets_executing_node_in_sql(self):
        """
        The UPDATE SET clause must bind self._node_id as executing_node.
        """
        row = _make_job_row()
        pool, _, cur = _make_pool(fetchone=row)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.claim_next_job()

        params = cur.execute.call_args[0][1]
        assert params[0] == NODE_ID

    def test_claim_with_job_type_filter_adds_operation_type_param(self):
        """
        When job_type is provided the SQL adds an operation_type filter and
        the corresponding parameter is appended to the param list.
        """
        row = _make_job_row(operation_type="refresh")
        pool, _, cur = _make_pool(fetchone=row)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.claim_next_job(job_type="refresh")

        sql_called = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]

        assert "AND operation_type = %s" in sql_called
        assert "refresh" in params

    def test_claim_without_job_type_omits_operation_type_filter(self):
        """
        When job_type is None the SQL must NOT contain an operation_type filter.
        """
        pool, _, cur = _make_pool(fetchone=None)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.claim_next_job()

        sql_called = cur.execute.call_args[0][0]
        assert "AND operation_type = %s" not in sql_called

    def test_claim_returns_none_when_no_pending_jobs(self):
        """
        When the UPDATE RETURNING returns no row (no pending jobs),
        claim_next_job() must return None.
        """
        pool, _, _ = _make_pool(fetchone=None)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.claim_next_job()

        assert result is None

    def test_claim_deserialises_json_fields(self):
        """
        JSON-encoded fields (result, claude_actions, extended_error,
        language_resolution_status) must be deserialised to Python objects.
        """
        row = _make_job_row(
            result={"output": "done"},
            claude_actions=["step1"],
            extended_error={"traceback": "..."},
            language_resolution_status={"python": {"resolved": True}},
        )
        pool, _, _ = _make_pool(fetchone=row)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        job = claimer.claim_next_job()

        assert job["result"] == {"output": "done"}
        assert job["claude_actions"] == ["step1"]
        assert job["extended_error"] == {"traceback": "..."}
        assert job["language_resolution_status"] == {"python": {"resolved": True}}

    def test_claim_param_count_matches_placeholders_no_type(self):
        """
        Without job_type there must be exactly ONE positional param
        (for executing_node = %s in SET).
        """
        pool, _, cur = _make_pool(fetchone=None)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.claim_next_job()

        params = cur.execute.call_args[0][1]
        assert len(params) == 1

    def test_claim_param_count_matches_placeholders_with_type(self):
        """
        With job_type there must be exactly TWO positional params
        (executing_node + operation_type).
        """
        pool, _, cur = _make_pool(fetchone=None)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.claim_next_job(job_type="refresh")

        params = cur.execute.call_args[0][1]
        assert len(params) == 2
        assert params[0] == NODE_ID
        assert params[1] == "refresh"


# ---------------------------------------------------------------------------
# release_job
# ---------------------------------------------------------------------------


class TestReleaseJob:
    def test_release_updates_status_to_pending(self):
        """
        release_job() must UPDATE status='pending' and clear executing_node.
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.release_job("job-1")

        assert result is True
        sql = cur.execute.call_args[0][0]
        assert "status" in sql
        assert "pending" in sql
        assert "executing_node = NULL" in sql

    def test_release_filters_by_node_id(self):
        """
        The WHERE clause must include AND executing_node = %s so a node
        cannot release another node's job.
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.release_job("job-1")

        params = cur.execute.call_args[0][1]
        assert NODE_ID in params

    def test_release_returns_false_when_not_found(self):
        """
        When rowcount == 0 (job not found or owned by another node),
        release_job() must return False.
        """
        pool, _, _ = _make_pool(rowcount=0)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.release_job("nonexistent")

        assert result is False


# ---------------------------------------------------------------------------
# complete_job
# ---------------------------------------------------------------------------


class TestCompleteJob:
    def test_complete_sets_status_completed(self):
        """
        complete_job() must UPDATE status='completed' and set completed_at.
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.complete_job("job-1")

        assert result is True
        sql = cur.execute.call_args[0][0]
        assert "completed" in sql
        assert "completed_at" in sql

    def test_complete_with_result_serialises_json(self):
        """
        When result dict is provided it must be JSON-serialised in the params.
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        payload = {"files_indexed": 42}
        claimer.complete_job("job-1", result=payload)

        params = cur.execute.call_args[0][1]
        assert params[0] == json.dumps(payload)

    def test_complete_without_result_passes_none(self):
        """
        When result is None the first param must be None (not 'null').
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.complete_job("job-1")

        params = cur.execute.call_args[0][1]
        assert params[0] is None

    def test_complete_filters_by_node_id(self):
        """WHERE clause must include executing_node = %s."""
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.complete_job("job-1")

        params = cur.execute.call_args[0][1]
        assert NODE_ID in params

    def test_complete_returns_false_when_not_found(self):
        pool, _, _ = _make_pool(rowcount=0)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.complete_job("no-such-job")

        assert result is False


# ---------------------------------------------------------------------------
# fail_job
# ---------------------------------------------------------------------------


class TestFailJob:
    def test_fail_sets_status_failed(self):
        """
        fail_job() must UPDATE status='failed' with the error message.
        """
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.fail_job("job-1", "something went wrong")

        assert result is True
        sql = cur.execute.call_args[0][0]
        assert "failed" in sql
        assert "error" in sql

    def test_fail_passes_error_message_as_param(self):
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.fail_job("job-1", "timeout")

        params = cur.execute.call_args[0][1]
        assert params[0] == "timeout"

    def test_fail_filters_by_node_id(self):
        pool, _, cur = _make_pool(rowcount=1)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.fail_job("job-1", "err")

        params = cur.execute.call_args[0][1]
        assert NODE_ID in params

    def test_fail_returns_false_when_not_found(self):
        pool, _, _ = _make_pool(rowcount=0)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        result = claimer.fail_job("ghost-job", "err")

        assert result is False


# ---------------------------------------------------------------------------
# get_node_jobs
# ---------------------------------------------------------------------------


class TestGetNodeJobs:
    def test_get_node_jobs_returns_list_of_dicts(self):
        """
        get_node_jobs() must SELECT all running rows for this node
        and return them as a list of dicts.
        """
        rows = [
            _make_job_row(job_id="job-1"),
            _make_job_row(job_id="job-2"),
        ]
        pool, _, cur = _make_pool(fetchall=rows)
        claimer = DistributedJobClaimer(pool, NODE_ID)

        jobs = claimer.get_node_jobs()

        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "job-1"
        assert jobs[1]["job_id"] == "job-2"

    def test_get_node_jobs_filters_by_node_id(self):
        """
        The WHERE clause must include executing_node = %s.
        """
        pool, _, cur = _make_pool(fetchall=[])
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.get_node_jobs()

        sql = cur.execute.call_args[0][0]
        params = cur.execute.call_args[0][1]
        assert "executing_node = %s" in sql
        assert params[0] == NODE_ID

    def test_get_node_jobs_filters_running_status(self):
        """
        Only 'running' jobs must be returned (WHERE status = 'running').
        """
        pool, _, cur = _make_pool(fetchall=[])
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.get_node_jobs()

        sql = cur.execute.call_args[0][0]
        assert "status = 'running'" in sql

    def test_get_node_jobs_returns_empty_list_when_none(self):
        """
        When no rows are returned an empty list must be returned (not None).
        """
        pool, _, _ = _make_pool(fetchall=[])
        claimer = DistributedJobClaimer(pool, NODE_ID)

        jobs = claimer.get_node_jobs()

        assert jobs == []

    def test_get_node_jobs_orders_by_claimed_at(self):
        """
        The ORDER BY clause must be claimed_at ASC for FIFO processing.
        """
        pool, _, cur = _make_pool(fetchall=[])
        claimer = DistributedJobClaimer(pool, NODE_ID)

        claimer.get_node_jobs()

        sql = cur.execute.call_args[0][0]
        assert "claimed_at" in sql
