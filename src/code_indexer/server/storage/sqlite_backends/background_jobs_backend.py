"""
SQLite backend for background job management.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psutil

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Bug #1344: terminal job statuses. update_job() guards against a stale
# non-terminal status write (e.g. a delayed cancel_job() persist for a
# RUNNING job) reverting a row that has already reached one of these
# statuses via the worker's own terminal write. Tuple (not set/frozenset)
# for deterministic SQL placeholder ordering.
#
# Bug #1950: "interrupted" (a restart/shutdown artifact, distinct from a
# genuine "failed") is terminal too -- mirrored in job_tracker.py and
# postgres/background_jobs_backend.py per the same Bug #1348 sync
# requirement.
_TERMINAL_JOB_STATUSES = (
    "completed",
    "completed_partial",
    "failed",
    "cancelled",
    "interrupted",
)


def _owning_worker_process_is_alive(executing_pid: Optional[int]) -> bool:
    """Bug #1563: True only when a job's recorded owning-worker PID is a
    live OS process on THIS host.

    Under `uvicorn --workers N`, every worker runs its own lifespan and
    each lifespan calls cleanup_orphaned_jobs_on_startup(). The pre-fix
    behavior failed EVERY running/pending job unconditionally, including
    jobs genuinely still executing inside a healthy sibling worker
    process that merely happened to share the same node -- because
    "node" was the only identity ever recorded. This helper adds a
    worker-level (PID) identity check on top, resolving exactly the
    ambiguity between "a sibling worker on this node is still alive" and
    "the owning process is provably gone".

    - executing_pid is None: no worker identity was ever recorded for
      this row (a legacy row from before this fix). Returns False so the
      caller's pre-existing unconditional-fail behavior for such rows is
      preserved exactly.
    - executing_pid is a live PID: the owning worker is still running --
      returns True so the caller does NOT fail this job.
    - executing_pid is a dead PID: the owner is provably gone (a real
      crash, or the specific worker that owned it was recycled) --
      returns False so the caller still reclaims it, exactly as a genuine
      full-node restart requires (every worker's PID becomes dead at
      once, so every row is still correctly reclaimed).

    Known, accepted, bounded residual risk (documented rather than
    engineered away): PID reuse. If the OS recycles a PID number between
    the owning process's death and this check, an unrelated process could
    coincidentally occupy the same PID and be misread as "still alive",
    deferring reclamation of a genuine orphan until a later sweep. This
    never causes the opposite (and far worse) failure mode of killing a
    job that is still genuinely running.
    """
    if executing_pid is None:
        return False
    try:
        return bool(psutil.pid_exists(executing_pid))
    except Exception:
        # Fail conservatively toward "cannot disprove liveness" -- never
        # wrongly fail a job whose owner we could not prove is gone.
        logger.warning(
            "Bug #1563: liveness probe for owning worker pid %s raised; "
            "treating as alive (conservative)",
            executing_pid,
        )
        return True


class BackgroundJobsSqliteBackend:
    """
    SQLite backend for background job management.

    Bug fix: BackgroundJobManager SQLite migration - Jobs not showing in Dashboard.
    Replaces JSON file storage with atomic SQLite operations.
    Complex nested data (result, claude_actions, extended_error, etc.) stored as JSON blobs.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_executing_pid_column()

    def _ensure_executing_pid_column(self) -> None:
        """Bug #1563: idempotent, self-contained schema self-heal.

        The background_jobs table's base schema is owned by
        storage/database_manager.py, which is out of scope for this fix.
        Rather than touch that file, this backend defensively adds its
        own new column the same way GoldenRepoMetadataSqliteBackend
        already does further up in this module (PRAGMA table_info check +
        ALTER TABLE ADD COLUMN) -- idempotent and safe to run on every
        construction, and self-heals an already-deployed database exactly
        like a rolling PostgreSQL migration would (see migration 046).
        """

        def operation(conn):
            cursor = conn.execute("PRAGMA table_info(background_jobs)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            if not existing_cols:
                # Regression fix (post-#1563): PRAGMA table_info against a
                # table that does not exist AT ALL returns an EMPTY result
                # set rather than raising -- it never returns a non-empty
                # set with a missing column list, since a SQLite table
                # cannot have zero columns. An empty result therefore means
                # "the table itself does not exist yet", never "a table
                # with zero columns". Falling into the ALTER branch here
                # (as the original #1563 fix did) raised
                # sqlite3.OperationalError: no such table: background_jobs
                # whenever this backend is constructed BEFORE
                # DatabaseSchema.initialize_database() has created the
                # table -- the exact path StorageFactory._create_sqlite_backends()
                # / create_backends() exercise when called directly against
                # a fresh data_dir (several pre-existing unit tests do this
                # legitimately, never touching background_jobs at all).
                # There is nothing to migrate yet, so return without
                # altering. Real production always calls
                # DatabaseSchema.initialize_database() BEFORE
                # StorageFactory.create_backends() (see service_init.py /
                # lifespan.py), so by the time this constructor runs there
                # the table already exists (without executing_pid, since
                # CREATE_BACKGROUND_JOBS_TABLE never defines it) and the
                # ALTER branch below still fires and self-heals it, on both
                # a brand-new database and an already-deployed one.
                return None
            if "executing_pid" not in existing_cols:
                conn.execute(
                    "ALTER TABLE background_jobs ADD COLUMN executing_pid INTEGER"
                )
                logger.info(
                    "Migrated background_jobs: added executing_pid column (Bug #1563)"
                )
            return None

        self._conn_manager.execute_atomic(operation)

    def save_job(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        current_phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        executing_node: Optional[str] = None,
        claimed_at: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> None:
        """Save a new background job."""

        # Bug #1563: stamp the OWNING WORKER's OS pid alongside the node
        # whenever this row is being claimed (executing_node provided).
        # Computed internally via os.getpid() -- always correct because
        # this call always executes inside the very process taking
        # ownership -- so no caller change is required. Rows with no
        # owner (executing_node=None, e.g. a pod-pull-eligible row left
        # for cross-node work-stealing) get no pid either.
        executing_pid = os.getpid() if executing_node is not None else None

        def operation(conn):
            conn.execute(
                """INSERT OR IGNORE INTO background_jobs
                   (job_id, operation_type, status, created_at, started_at, completed_at,
                    result, error, progress, username, is_admin, cancelled, repo_alias,
                    resolution_attempts, claude_actions, failure_reason, extended_error,
                    language_resolution_status, current_phase, phase_detail,
                    progress_info, metadata, executing_node, claimed_at, actor_username,
                    executing_pid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
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
                    1 if is_admin else 0,
                    1 if cancelled else 0,
                    repo_alias,
                    resolution_attempts,
                    json.dumps(claude_actions) if claude_actions else None,
                    failure_reason,
                    json.dumps(extended_error) if extended_error else None,
                    (
                        json.dumps(language_resolution_status)
                        if language_resolution_status
                        else None
                    ),
                    current_phase,
                    phase_detail,
                    json.dumps(progress_info)
                    if isinstance(progress_info, dict)
                    else progress_info,
                    json.dumps(metadata) if metadata else None,
                    executing_node,
                    claimed_at,
                    actor_username,
                    executing_pid,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Saved background job: {job_id}")

    def atomic_claim_insert(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        current_phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        executing_node: Optional[str] = None,
        claimed_at: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> None:
        """Insert a new background job using a plain INSERT (no OR IGNORE).

        Unlike save_job which uses INSERT OR IGNORE, this method uses a plain
        INSERT so that sqlite3.IntegrityError is raised when
        idx_active_job_per_repo is violated by a duplicate pending/running row
        for the same (operation_type, repo_alias). The caller translates this
        into DuplicateJobError.

        Do NOT modify save_job — its INSERT OR IGNORE is intentional for other
        callers that must survive duplicate inserts.

        Raises:
            sqlite3.IntegrityError: When idx_active_job_per_repo rejects the
                INSERT due to a duplicate active job for (operation_type, repo_alias).
        """

        # Bug #1563: see save_job's identical comment -- stamp the owning
        # worker's OS pid whenever this row is claimed with an owning
        # node, computed internally so no caller change is required.
        executing_pid = os.getpid() if executing_node is not None else None

        def operation(conn):
            conn.execute(
                """INSERT INTO background_jobs
                   (job_id, operation_type, status, created_at, started_at, completed_at,
                    result, error, progress, username, is_admin, cancelled, repo_alias,
                    resolution_attempts, claude_actions, failure_reason, extended_error,
                    language_resolution_status, current_phase, phase_detail,
                    progress_info, metadata, executing_node, claimed_at, actor_username,
                    executing_pid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
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
                    1 if is_admin else 0,
                    1 if cancelled else 0,
                    repo_alias,
                    resolution_attempts,
                    json.dumps(claude_actions) if claude_actions else None,
                    failure_reason,
                    json.dumps(extended_error) if extended_error else None,
                    (
                        json.dumps(language_resolution_status)
                        if language_resolution_status
                        else None
                    ),
                    current_phase,
                    phase_detail,
                    json.dumps(progress_info)
                    if isinstance(progress_info, dict)
                    else progress_info,
                    json.dumps(metadata) if metadata else None,
                    executing_node,
                    claimed_at,
                    actor_username,
                    executing_pid,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Atomic claim insert background job: {job_id}")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job details by job ID."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT job_id, operation_type, status, created_at, started_at, completed_at,
                      result, error, progress, username, is_admin, cancelled, repo_alias,
                      resolution_attempts, claude_actions, failure_reason, extended_error,
                      language_resolution_status, current_phase, phase_detail,
                      progress_info, metadata, executing_node, claimed_at, actor_username
               FROM background_jobs WHERE job_id = ?""",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to job dictionary."""
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
            "current_phase": row[18] if len(row) > 18 else None,
            "phase_detail": row[19] if len(row) > 19 else None,
            "progress_info": row[20] if len(row) > 20 else None,
            "metadata": json.loads(row[21]) if len(row) > 21 and row[21] else None,
            "executing_node": row[22] if len(row) > 22 else None,
            "claimed_at": row[23] if len(row) > 23 else None,
            # Story #1032 AC12: actor_username audit trail
            "actor_username": row[24] if len(row) > 24 else None,
        }

    def update_job(
        self, job_id: str, *, guard_terminal_status: bool = False, **kwargs
    ) -> None:
        """Update job fields. Accepts any field from the background_jobs table.

        Args:
            guard_terminal_status: Bug #1344 opt-in. When True and the new
                ``status`` kwarg is non-terminal, the UPDATE is guarded with
                ``AND status NOT IN (<terminal statuses>)`` so a stale
                non-terminal write cannot revert a row that has already
                reached a terminal status via a separate, later write.
                Defaults to False so unrelated callers (e.g. JobTracker's
                dedup mechanism, which deliberately relies on an
                unconditional write to surface a real IntegrityError from
                ``idx_active_job_per_repo`` -- Bug #1256) are unaffected.
        """
        json_fields = {
            "result",
            "claude_actions",
            "extended_error",
            "language_resolution_status",
            "metadata",
        }
        bool_fields = {"is_admin", "cancelled"}
        updates: List[str] = []
        params: List[Any] = []

        for key, value in kwargs.items():
            updates.append(f"{key} = ?")
            if value is None:
                params.append(None)
            elif key in json_fields:
                params.append(json.dumps(value))
            elif key == "progress_info" and isinstance(value, dict):
                # Bug #892: dict progress_info must be JSON-serialized before binding.
                # str progress_info passes through unchanged.
                params.append(json.dumps(value))
            elif key in bool_fields:
                params.append(1 if value else 0)
            else:
                params.append(value)

        if not updates:
            return

        params.append(job_id)

        where_clause = "WHERE job_id = ?"
        # Bug #1344: when the new status being written is itself non-terminal
        # (e.g. "running"), guard the UPDATE so it cannot clobber a row that
        # has already reached a terminal status via a separate, later write
        # (e.g. the worker's own terminal persist racing a stale outside-lock
        # write from cancel_job()). A terminal new status is always allowed
        # through unconditionally. Opt-in only (see guard_terminal_status
        # docstring) -- other callers must keep their prior unconditional
        # write semantics.
        new_status = kwargs.get("status")
        if (
            guard_terminal_status
            and new_status is not None
            and new_status not in _TERMINAL_JOB_STATUSES
        ):
            placeholders = ", ".join("?" for _ in _TERMINAL_JOB_STATUSES)
            where_clause += f" AND status NOT IN ({placeholders})"
            params.extend(_TERMINAL_JOB_STATUSES)

        def operation(conn):
            conn.execute(
                f"UPDATE background_jobs SET {', '.join(updates)} {where_clause}",
                params,
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def fail_orphaned_jobs(self, error: str = "Orphaned by server restart") -> int:
        """Mark all running/pending jobs as interrupted. Called on startup.

        Bug #1950: writes status='interrupted' (not 'failed') -- a row
        still running/pending at startup was orphaned by this process's
        own restart, a restart artifact rather than a genuine failure, so
        it must not poison /health's get_failed_job_count() (which counts
        ONLY status='failed', with no time window) forever.
        """
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        result = {"count": 0}

        def operation(conn):
            cur = conn.execute(
                "UPDATE background_jobs SET status = 'interrupted', error = ?, "
                "completed_at = ? WHERE status IN ('running', 'pending')",
                (error, now_iso),
            )
            result["count"] = cur.rowcount
            return None

        self._conn_manager.execute_atomic(operation)
        return result["count"]

    def list_jobs(
        self,
        username: Optional[str] = None,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        exclude_operation_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """List background jobs with optional filtering and pagination."""
        conn = self._conn_manager.get_connection()

        query = """SELECT job_id, operation_type, status, created_at, started_at, completed_at,
                          result, error, progress, username, is_admin, cancelled, repo_alias,
                          resolution_attempts, claude_actions, failure_reason, extended_error,
                          language_resolution_status, current_phase, phase_detail,
                          progress_info, metadata, executing_node, claimed_at, actor_username
                   FROM background_jobs"""

        conditions = []
        params: List[Any] = []

        if username:
            conditions.append("username = ?")
            params.append(username)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = ?")
            params.append(operation_type)
        if exclude_operation_types:
            placeholders = ", ".join(["?"] * len(exclude_operation_types))
            conditions.append(
                f"(operation_type IS NULL OR operation_type NOT IN ({placeholders}))"
            )
            params.extend(exclude_operation_types)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = conn.execute(query, params)
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _build_jobs_filter_where(
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        username: Optional[str] = None,
        exclude_ids: Optional[set] = None,
    ) -> tuple:
        """Build the WHERE clause and params list for background_jobs queries.

        Returns (where_clause: str, params: List[Any]).  The where_clause is
        either empty or starts with ' WHERE '.  Both list_jobs_filtered and
        list_job_ids_filtered call this helper so the filter logic cannot drift
        between the two methods.

        Args:
            status: Filter by exact status value (e.g. 'completed', 'failed')
            operation_type: Filter by exact operation_type value
            search_text: Case-insensitive LIKE match against repo_alias, username,
                         operation_type, and error columns
            username: When set, scope results to this owner's jobs (H2 non-admin)
            exclude_ids: Set of job_ids to exclude
        """
        conditions: List[str] = []
        params: List[Any] = []

        # H2: Non-admin username scoping for DB-stored completed jobs
        if username is not None:
            conditions.append("username = ?")
            params.append(username)

        if status:
            conditions.append("status = ?")
            params.append(status)

        if operation_type:
            conditions.append("operation_type = ?")
            params.append(operation_type)

        if search_text:
            # Case-insensitive LIKE across key text columns
            like_pattern = f"%{search_text}%"
            conditions.append(
                "(LOWER(repo_alias) LIKE LOWER(?)"
                " OR LOWER(username) LIKE LOWER(?)"
                " OR LOWER(operation_type) LIKE LOWER(?)"
                " OR LOWER(COALESCE(error, '')) LIKE LOWER(?)"
                " OR LOWER(job_id) LIKE LOWER(?))"
            )
            params.extend(
                [like_pattern, like_pattern, like_pattern, like_pattern, like_pattern]
            )

        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            conditions.append(f"job_id NOT IN ({placeholders})")
            params.extend(list(exclude_ids))

        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        return where_clause, params

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[set] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        username: Optional[str] = None,
    ) -> tuple:
        """Return (list_of_job_dicts, total_count) with dynamic SQL WHERE filters.

        Story #271: Filtered jobs query with pagination support.

        Args:
            status: Filter by exact status value (e.g. 'completed', 'failed')
            operation_type: Filter by exact operation_type value
            search_text: Case-insensitive LIKE match against repo_alias, username,
                         operation_type, error, and job_id columns
            exclude_ids: Set of job_ids to exclude (used to skip in-memory active jobs)
            limit: Maximum number of rows to return (None = no limit)
            offset: Number of rows to skip for pagination (default 0)
            username: When set, scope results to this owner's jobs (H2: non-admin scope)

        Returns:
            Tuple of (jobs: List[Dict], total_count: int) where total_count reflects
            the full matching set ignoring limit/offset.
        """
        conn = self._conn_manager.get_connection()

        base_select = """SELECT job_id, operation_type, status, created_at, started_at,
                                completed_at, result, error, progress, username, is_admin,
                                cancelled, repo_alias, resolution_attempts, claude_actions,
                                failure_reason, extended_error, language_resolution_status,
                                current_phase, phase_detail, progress_info, metadata,
                                executing_node, claimed_at, actor_username
                         FROM background_jobs"""

        where_clause, params = self._build_jobs_filter_where(
            status=status,
            operation_type=operation_type,
            search_text=search_text,
            username=username,
            exclude_ids=exclude_ids,
        )

        # Count query (no LIMIT/OFFSET) for accurate total
        count_query = f"SELECT COUNT(*) FROM background_jobs{where_clause}"
        count_cursor = conn.execute(count_query, params)
        total_count: int = count_cursor.fetchone()[0]

        # Data query with ORDER BY and optional pagination
        data_query = base_select + where_clause + " ORDER BY created_at DESC"
        data_params = list(params)

        if limit is not None:
            data_query += " LIMIT ? OFFSET ?"
            data_params.extend([limit, offset])

        cursor = conn.execute(data_query, data_params)
        jobs = [self._row_to_dict(row) for row in cursor.fetchall()]

        return jobs, total_count

    # Safety cap for list_job_ids_filtered: worst-case upper bound.
    # At 14k jobs/day with 30-day retention the table holds ~420k rows; a cap
    # of 50,000 is an order-of-magnitude ceiling that keeps the query bounded.
    _JOB_IDS_CAP = 50_000

    def list_job_ids_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        username: Optional[str] = None,
        cap: Optional[int] = None,
    ) -> set:
        """Return the set of job_ids matching the given filters.

        Uses the same WHERE clause as list_jobs_filtered (via
        _build_jobs_filter_where) so the two methods cannot drift.

        A safety cap (default _JOB_IDS_CAP = 50,000) is applied as LIMIT so
        this query is always bounded regardless of table size.

        Args:
            status: Filter by exact status value
            operation_type: Filter by exact operation_type value
            search_text: Case-insensitive LIKE match (same columns as list_jobs_filtered)
            username: When set, scope results to this owner's jobs
            cap: Override the default safety cap (for testing)

        Returns:
            set[str] of matching job_ids.
        """
        conn = self._conn_manager.get_connection()
        effective_cap = cap if cap is not None else self._JOB_IDS_CAP

        where_clause, params = self._build_jobs_filter_where(
            status=status,
            operation_type=operation_type,
            search_text=search_text,
            username=username,
        )

        query = f"SELECT job_id FROM background_jobs{where_clause} ORDER BY created_at DESC LIMIT ?"
        params_with_cap = list(params) + [effective_cap]
        cursor = conn.execute(query, params_with_cap)
        return {row[0] for row in cursor.fetchall()}

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID."""

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM background_jobs WHERE job_id = ?", (job_id,)
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(f"Deleted background job: {job_id}")
        return deleted

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Clean up old jobs in any terminal status.

        Bug #1950: uses _TERMINAL_JOB_STATUSES (completed/completed_partial/
        failed/cancelled/interrupted) rather than a hardcoded subset -- an
        interrupted (restart-artifact) or completed_partial row must be
        retention-cleaned exactly like a completed/failed/cancelled one, or
        it accumulates in the table forever.
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff_time.isoformat()
        placeholders = ", ".join("?" for _ in _TERMINAL_JOB_STATUSES)

        def operation(conn):
            cursor = conn.execute(
                f"""DELETE FROM background_jobs
                   WHERE status IN ({placeholders})
                   AND completed_at IS NOT NULL
                   AND completed_at < ?""",
                (*_TERMINAL_JOB_STATUSES, cutoff_iso),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(f"Cleaned up {count} old background jobs")
        return count

    def count_jobs_by_status(self) -> Dict[str, int]:
        """Get count of jobs grouped by status."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT status, COUNT(*) FROM background_jobs GROUP BY status"
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]:
        """Get job statistics filtered by time period."""
        now = datetime.now(timezone.utc)

        if time_filter == "24h":
            cutoff = now - timedelta(hours=24)
        elif time_filter == "7d":
            cutoff = now - timedelta(days=7)
        elif time_filter == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = now - timedelta(hours=24)

        cutoff_iso = cutoff.isoformat()

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT status, COUNT(*) FROM background_jobs
               WHERE completed_at IS NOT NULL AND completed_at >= ?
               GROUP BY status""",
            (cutoff_iso,),
        )

        stats = {"completed": 0, "failed": 0}
        for row in cursor.fetchall():
            if row[0] in stats:
                stats[row[0]] = row[1]

        return stats

    def cleanup_orphaned_jobs_on_startup(self, node_id: Optional[str] = None) -> int:
        """
        Clean up orphaned jobs on server startup.

        On server restart, any jobs with status 'running' or 'pending' are orphaned
        because the processes that were executing them no longer exist.

        Bug #1950: this method marks them as 'interrupted' (not 'failed') --
        a restart artifact, distinct from a genuine failure -- with an
        appropriate error message and timestamp for audit trail.

        Story #723: Clean Up Orphaned Jobs on Server Startup

        Story #1400 CRITICAL 3: node_id is accepted for interface parity with
        the PostgreSQL backend (which uses it to scope cleanup to THIS node's
        jobs only in cluster mode) but is intentionally IGNORED here -- solo
        SQLite mode is always single-process/single-node, so every
        running/pending row in this database genuinely was orphaned by this
        same process's restart. Node scoping only matters when a shared
        cluster backend could see another node's still-running work.

        Bug #1563: even within a single node, a misconfigured or future
        multi-worker SQLite deployment would face the exact same hazard
        PostgreSQL cluster mode does -- a recycled worker's own startup
        sweep would otherwise fail every running/pending row in this
        database, including rows genuinely owned by a still-alive sibling
        worker process. This is defended the same way as the PostgreSQL
        backend: candidates are read first, then only rows whose recorded
        owning-worker PID (see save_job/atomic_claim_insert) is NOT a live
        OS process are actually failed. A genuine full-node/single-process
        restart is unaffected -- every recorded pid on this host becomes
        dead at once, so every row is still correctly reclaimed. See
        _owning_worker_process_is_alive for the full contract, including
        the documented residual PID-reuse risk.

        Returns:
            Number of orphaned jobs that were cleaned up.
        """
        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"

        def operation(conn):
            cursor = conn.execute(
                "SELECT job_id, executing_pid FROM background_jobs "
                "WHERE status IN ('running', 'pending')"
            )
            candidates = cursor.fetchall()
            job_ids_to_fail = [
                row[0]
                for row in candidates
                if not _owning_worker_process_is_alive(row[1])
            ]
            if not job_ids_to_fail:
                return 0

            # Bug #1950: status='interrupted' (not 'failed') -- a row
            # whose owning worker process is provably gone was orphaned by
            # a restart, a restart artifact rather than a genuine failure,
            # so it must not poison /health's get_failed_job_count()
            # (which counts ONLY status='failed', with no time window)
            # forever.
            placeholders = ", ".join("?" for _ in job_ids_to_fail)
            cursor = conn.execute(
                f"""UPDATE background_jobs
                    SET status = 'interrupted',
                        error = ?,
                        completed_at = ?
                    WHERE status IN ('running', 'pending')
                      AND job_id IN ({placeholders})""",
                [error_message, interrupted_at, *job_ids_to_fail],
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(f"Cleaned up {count} orphaned jobs on server startup")
        return count

    def find_active_job_by_type_and_alias(
        self,
        operation_type: str,
        repo_alias: str,
    ) -> Optional[str]:
        """Return job_id of the active (pending/running) row for (operation_type, repo_alias).

        Direct non-paginated lookup — no Python-side filtering.
        Called by JobTracker._find_blocking_active_job_id after a unique-index
        violation to locate the blocking row without risking a pagination miss
        (Bug #1220).

        Args:
            operation_type: Operation type to match exactly.
            repo_alias: Repository alias to match exactly.

        Returns:
            job_id string if a pending or running row exists, else None.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT job_id FROM background_jobs "
            "WHERE operation_type = ? AND repo_alias = ? "
            "AND status IN ('pending', 'running') LIMIT 1",
            (operation_type, repo_alias),
        )
        row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
