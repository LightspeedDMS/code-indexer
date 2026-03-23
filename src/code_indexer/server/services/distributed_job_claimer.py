"""
Atomic distributed job claiming for cluster mode.

Story #421: Atomic Distributed Job Claiming with Conflict Resolution

Uses PostgreSQL's UPDATE...WHERE...FOR UPDATE SKIP LOCKED...RETURNING pattern
so multiple cluster nodes can safely compete for pending jobs without
duplicate execution or row-level contention.

This module is cluster-only: it is loaded exclusively when
storage_mode="postgres".  It has no dependency on any SQLite code.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column list (must match background_jobs table schema)
# ---------------------------------------------------------------------------

_SELECT_COLS = """
    job_id, operation_type, status, created_at, started_at, completed_at,
    result, error, progress, username, is_admin, cancelled, repo_alias,
    resolution_attempts, claude_actions, failure_reason, extended_error,
    language_resolution_status, executing_node, claimed_at
"""


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Convert a psycopg row (sequence) to a job dictionary."""
    return {
        "job_id": row[0],
        "operation_type": row[1],
        "status": row[2],
        "created_at": row[3],
        "started_at": row[4],
        "completed_at": row[5],
        "result": json.loads(row[6]) if row[6] else None,
        "error": row[7],
        "progress": row[8],
        "username": row[9],
        "is_admin": bool(row[10]),
        "cancelled": bool(row[11]),
        "repo_alias": row[12],
        "resolution_attempts": row[13],
        "claude_actions": json.loads(row[14]) if row[14] else None,
        "failure_reason": row[15],
        "extended_error": json.loads(row[16]) if row[16] else None,
        "language_resolution_status": json.loads(row[17]) if row[17] else None,
        "executing_node": row[18],
        "claimed_at": row[19],
    }


# ---------------------------------------------------------------------------
# DistributedJobClaimer
# ---------------------------------------------------------------------------


class DistributedJobClaimer:
    """
    Atomic job claiming for cluster mode using PostgreSQL.

    Multiple nodes can call claim_next_job() concurrently; PostgreSQL's
    FOR UPDATE SKIP LOCKED ensures that each pending job is claimed by
    exactly one node — no duplicate execution, no spin-wait.

    Lifecycle of a job from this class's perspective:

        pending (executing_node IS NULL)
            └─ claim_next_job()  ──>  running (executing_node = self._node_id)
                ├─ complete_job() ──>  completed
                ├─ fail_job()     ──>  failed
                └─ release_job()  ──>  pending (executing_node = NULL)
    """

    def __init__(self, pool: Any, node_id: str) -> None:
        """
        Initialise the claimer.

        Args:
            pool:    A ConnectionPool instance (from connection_pool.py).
            node_id: Unique identifier for this cluster node (e.g. hostname).
        """
        self._pool = pool
        self._node_id = node_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def claim_next_job(
        self, job_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically claim the next pending job for this node.

        Uses a single UPDATE...RETURNING statement with a sub-SELECT
        that holds a row-level FOR UPDATE SKIP LOCKED lock, guaranteeing
        that concurrent callers on other nodes (or threads) never claim
        the same row.

        Args:
            job_type: If provided, only claim jobs of this operation_type.

        Returns:
            A job dictionary (same schema as BackgroundJobsPostgresBackend
            rows plus ``executing_node`` and ``claimed_at``) if a job was
            claimed, or None if no pending jobs are available.
        """
        type_filter = "AND operation_type = %s" if job_type else ""
        # One positional param for SET executing_node = %s,
        # plus one optional param for the job_type filter.
        params: List[Any] = [self._node_id]
        if job_type:
            params.append(job_type)

        sql = f"""
            UPDATE background_jobs
            SET executing_node = %s,
                claimed_at     = NOW(),
                status         = 'running',
                started_at     = NOW()
            WHERE job_id = (
                SELECT job_id
                FROM   background_jobs
                WHERE  status = 'pending'
                  AND  executing_node IS NULL
                  {type_filter}
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING {_SELECT_COLS}
        """

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()

        if row is None:
            return None

        job = _row_to_dict(row)
        logger.debug(
            "Node %s claimed job %s (type=%s)",
            self._node_id,
            job["job_id"],
            job["operation_type"],
        )
        return job

    def release_job(self, job_id: str) -> bool:
        """
        Release a claimed job back to pending state.

        Intended for graceful shutdown — returns the job to the pool so
        another node can pick it up.  Only releases jobs currently owned
        by this node.

        Args:
            job_id: The job to release.

        Returns:
            True if the row was updated, False if not found or not owned
            by this node.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status         = 'pending',
                        executing_node = NULL,
                        claimed_at     = NULL,
                        started_at     = NULL
                    WHERE job_id = %s
                      AND executing_node = %s
                    """,
                    (job_id, self._node_id),
                )
                released: bool = cur.rowcount > 0
            conn.commit()

        if released:
            logger.info(
                "Node %s released job %s back to pending", self._node_id, job_id
            )
        return released

    def complete_job(
        self, job_id: str, result: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Mark a job as completed with an optional result payload.

        Only marks jobs currently owned by this node.

        Args:
            job_id: The job to complete.
            result: Optional result dictionary (serialised as JSON).

        Returns:
            True if the row was updated, False if not found or not owned
            by this node.
        """
        result_json = json.dumps(result) if result is not None else None

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status       = 'completed',
                        completed_at = NOW(),
                        result       = %s,
                        progress     = 100
                    WHERE job_id = %s
                      AND executing_node = %s
                    """,
                    (result_json, job_id, self._node_id),
                )
                completed: bool = cur.rowcount > 0
            conn.commit()

        if completed:
            logger.debug("Node %s completed job %s", self._node_id, job_id)
        return completed

    def fail_job(self, job_id: str, error: str) -> bool:
        """
        Mark a job as failed with an error message.

        Only marks jobs currently owned by this node.

        Args:
            job_id: The job to fail.
            error:  Human-readable error description.

        Returns:
            True if the row was updated, False if not found or not owned
            by this node.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status       = 'failed',
                        completed_at = NOW(),
                        error        = %s
                    WHERE job_id = %s
                      AND executing_node = %s
                    """,
                    (error, job_id, self._node_id),
                )
                failed: bool = cur.rowcount > 0
            conn.commit()

        if failed:
            logger.debug("Node %s failed job %s: %s", self._node_id, job_id, error)
        return failed

    def get_node_jobs(self) -> List[Dict[str, Any]]:
        """
        Return all jobs currently claimed (running) by this node.

        Returns:
            List of job dictionaries, ordered by claimed_at ascending.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_COLS}
                    FROM   background_jobs
                    WHERE  executing_node = %s
                      AND  status = 'running'
                    ORDER BY claimed_at ASC
                    """,
                    (self._node_id,),
                )
                rows = cur.fetchall()

        return [_row_to_dict(r) for r in rows]
