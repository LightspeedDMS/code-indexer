"""
PostgreSQL backend for background job management.

Story #413: PostgreSQL Backend for BackgroundJobs and SyncJobs

Drop-in replacement for BackgroundJobsSqliteBackend that satisfies the
BackgroundJobsBackend Protocol defined in storage/protocols.py.

Uses psycopg v3 via the ConnectionPool from connection_pool.py.
All JSON-valued columns (result, claude_actions, extended_error,
language_resolution_status) are serialised/deserialised with json.dumps/loads.
Boolean columns (is_admin, cancelled) are stored as native PG BOOLEAN.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psutil

from .connection_pool import ConnectionPool

# ---------------------------------------------------------------------------
# Deadlock retry constants (Bug #1235 GAP B)
# ---------------------------------------------------------------------------

# PG SQLSTATE codes that indicate a transient locking conflict — safe to retry.
_RETRYABLE_PG_SQLSTATES = frozenset(
    {
        "40P01",  # deadlock_detected
        "40001",  # serialization_failure
    }
)

# Maximum retry attempts for update_job on a transient PG locking failure.
_UPDATE_JOB_MAX_RETRIES = 3

# Base backoff in seconds; each retry waits attempt_index * _UPDATE_JOB_BACKOFF_BASE.
_UPDATE_JOB_BACKOFF_BASE = 0.05

logger = logging.getLogger(__name__)

# Bug #1344: terminal job statuses. update_job() guards against a stale
# non-terminal status write (e.g. a delayed cancel_job() persist for a
# RUNNING job) reverting a row that has already reached one of these
# statuses via the worker's own terminal write. Tuple (not set/frozenset)
# for deterministic SQL placeholder ordering. Mirrors the same constant in
# storage/sqlite_backends.py so both backends enforce the guard identically.
_TERMINAL_JOB_STATUSES = ("completed", "completed_partial", "failed", "cancelled")

_ALLOWED_JOB_COLUMNS = frozenset(
    {
        "status",
        "progress",
        "error",
        "result",
        "completed_at",
        "started_at",
        "cancelled",
        "repo_alias",
        "resolution_attempts",
        "claude_actions",
        "failure_reason",
        "extended_error",
        "language_resolution_status",
        "progress_info",
        "metadata",
        "executing_node",
        "claimed_at",
        "current_phase",
        "phase_detail",
    }
)


def _owning_worker_process_is_alive(executing_pid: Optional[int]) -> bool:
    """Bug #1563: True only when a job's recorded owning-worker PID is a
    live OS process on THIS host.

    Under `uvicorn --workers N`, every worker runs its own lifespan and
    each lifespan calls cleanup_orphaned_jobs_on_startup(node_id=...).
    The node-scoped predicate alone cannot distinguish "a sibling worker
    on this SAME node is still alive and genuinely executing this job"
    from "the owning process is provably gone" -- it unconditionally
    failed every running/pending row owned by the node, including jobs
    still executing inside a healthy sibling worker. This helper adds a
    worker-level (PID) identity check to resolve that ambiguity, mirrored
    identically in storage/sqlite_backends.py's helper of the same name.

    - executing_pid is None: no worker identity was ever recorded for
      this row (a legacy row from before this fix, or a row claimed via
      DistributedJobClaimer's cross-node work-stealing queue, which does
      not stamp this column). Returns False so the caller's pre-existing
      unconditional-fail behavior for such rows is preserved exactly.
    - executing_pid is a live PID: the owning worker is still running --
      returns True so the caller does NOT fail this job.
    - executing_pid is a dead PID: the owner is provably gone (a real
      crash, or specifically the worker that owned it was recycled) --
      returns False so the caller still reclaims it. A genuine full-node
      restart kills every worker process on that node at once, so every
      recorded pid becomes dead and every row is still correctly
      reclaimed -- crash recovery is unaffected by this change.

    MUST NOT be applied to the Bug #1512 `executing_node IS NULL` branch:
    that branch's stale executing_pid (if any) can originate from a
    DIFFERENT host -- job_reconciliation_service resets executing_node to
    NULL (without touching executing_pid) specifically when the OWNING
    node has left the cluster's active-node list, a cross-host scenario.
    Checking pid liveness on THIS host for that branch would risk a false
    "still alive" from an unrelated local process coincidentally reusing
    that foreign pid number, silently reintroducing the exact bug Bug
    #1512 fixed. That branch's own invariant ("no legitimate code path
    ever leaves a running row with a NULL owner") already proves every
    such row is a genuine orphan without needing a pid check at all --
    see cleanup_orphaned_jobs_on_startup for where this split is enforced.

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


# Columns selected in every SELECT query (ordered — must match _row_to_dict)
_SELECT_COLS = """
    job_id, operation_type, status, created_at, started_at, completed_at,
    result, error, progress, username, is_admin, cancelled, repo_alias,
    resolution_attempts, claude_actions, failure_reason, extended_error,
    language_resolution_status, progress_info, metadata,
    executing_node, claimed_at, current_phase, phase_detail,
    actor_username
"""


class BackgroundJobsPostgresBackend:
    """
    PostgreSQL backend for background job management.

    Satisfies the BackgroundJobsBackend Protocol.  Intended as a drop-in
    replacement for BackgroundJobsSqliteBackend when the server is configured
    to use PostgreSQL.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialise the backend with a shared connection pool.

        Args:
            pool: A ConnectionPool instance (from connection_pool.py).
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a psycopg row (sequence) to a job dictionary."""

        # Convert PG datetime objects to ISO strings for consistency with SQLite
        def _dt(val: Any) -> Any:
            from datetime import datetime as _dt_cls

            return val.isoformat() if isinstance(val, _dt_cls) else val

        # Handle JSONB columns: psycopg returns dicts, but strings from migration
        def _json_col(val: Any) -> Any:
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return val  # Already parsed by psycopg
            if isinstance(val, str):
                return json.loads(val)
            return val

        return {
            "job_id": row[0],
            "operation_type": row[1],
            "status": row[2],
            "created_at": _dt(row[3]),
            "started_at": _dt(row[4]),
            "completed_at": _dt(row[5]),
            "result": _json_col(row[6]),
            "error": row[7],
            "progress": row[8],
            "username": row[9],
            "is_admin": bool(row[10]),
            "cancelled": bool(row[11]),
            "repo_alias": row[12],
            "resolution_attempts": row[13],
            "claude_actions": _json_col(row[14]),
            "failure_reason": row[15],
            "extended_error": _json_col(row[16]),
            "language_resolution_status": _json_col(row[17]),
            "progress_info": row[18] if len(row) > 18 else None,
            "metadata": _json_col(row[19]) if len(row) > 19 else None,
            "executing_node": row[20] if len(row) > 20 else None,
            "claimed_at": _dt(row[21]) if len(row) > 21 else None,
            "current_phase": row[22] if len(row) > 22 else None,
            "phase_detail": row[23] if len(row) > 23 else None,
            # Story #1032 AC12: actor_username audit trail
            "actor_username": row[24] if len(row) > 24 else None,
        }

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

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
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        executing_node: Optional[str] = None,
        claimed_at: Optional[str] = None,
        current_phase: Optional[str] = None,
        phase_detail: Optional[str] = None,
        actor_username: Optional[str] = None,
        *,
        duplicate_violation_log_level: int = logging.WARNING,
    ) -> None:
        """Insert a new background job row.

        Args:
            duplicate_violation_log_level: Bug #1565 -- severity for the
                "Duplicate active job rejected by database" log emitted
                when idx_active_job_per_repo rejects this INSERT. Defaults
                to WARNING, which is correct for this method's DIRECT
                callers (JobTracker.register_job()/_insert_job(), and the
                one-time migrate_background_jobs() JSON import) -- neither
                expects or tolerates a duplicate, so a violation there is a
                genuine anomaly. atomic_claim_insert() below -- the
                EXCLUSIVE call path behind
                register_job_if_no_conflict()'s by-design cross-node
                single-flight guard, whose DuplicateJobError is always
                caught and handled as an expected no-op by every real
                caller -- passes DEBUG instead. The caller decides
                severity; this method never guesses from context.

        Raises:
            IntegrityError: When a duplicate active job exists for the same
                (operation_type, repo_alias), enforced by partial unique index
                idx_active_job_per_repo (migration 004, Bug #536).
        """
        # Bug #1563: stamp the OWNING WORKER's OS pid alongside the node
        # whenever this row is being claimed (executing_node provided).
        # Computed internally via os.getpid() -- always correct because
        # this call always executes inside the very process taking
        # ownership -- so no caller change is required. Rows with no
        # owner (executing_node=None, e.g. a pod-pull-eligible row left
        # for cross-node work-stealing) get no pid either.
        executing_pid = os.getpid() if executing_node is not None else None
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO background_jobs (
                            job_id, operation_type, status, created_at, started_at,
                            completed_at, result, error, progress, username, is_admin,
                            cancelled, repo_alias, resolution_attempts, claude_actions,
                            failure_reason, extended_error, language_resolution_status,
                            progress_info, metadata,
                            executing_node, claimed_at,
                            current_phase, phase_detail,
                            actor_username, executing_pid
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s
                        )
                        ON CONFLICT (job_id) DO NOTHING
                        """,
                        (
                            job_id,
                            operation_type,
                            status,
                            created_at,
                            started_at,
                            completed_at,
                            json.dumps(result) if result is not None else None,
                            error,
                            progress,
                            username,
                            is_admin,
                            cancelled,
                            repo_alias,
                            resolution_attempts,
                            (
                                json.dumps(claude_actions)
                                if claude_actions is not None
                                else None
                            ),
                            failure_reason,
                            (
                                json.dumps(extended_error)
                                if extended_error is not None
                                else None
                            ),
                            (
                                json.dumps(language_resolution_status)
                                if language_resolution_status is not None
                                else None
                            ),
                            json.dumps(progress_info)
                            if isinstance(progress_info, dict)
                            else progress_info,
                            json.dumps(metadata) if metadata is not None else None,
                            executing_node,
                            claimed_at,
                            current_phase,
                            phase_detail,
                            actor_username,
                            executing_pid,
                        ),
                    )
        except Exception as exc:
            # Bug #536: Catch unique violation from partial index on active jobs.
            # psycopg wraps UniqueViolation as IntegrityError.
            if "UniqueViolation" in type(exc).__name__ or (
                hasattr(exc, "sqlstate") and getattr(exc, "sqlstate") == "23505"
            ):
                # Bug #1565: severity is caller-decided via
                # duplicate_violation_log_level (see this method's own
                # docstring) -- WARNING by default for save_job()'s direct
                # callers, DEBUG for atomic_claim_insert()'s by-design
                # single-flight path.
                logger.log(
                    duplicate_violation_log_level,
                    "Duplicate active job rejected by database: "
                    "operation_type=%s, repo_alias=%s (job_id=%s)",
                    operation_type,
                    repo_alias,
                    job_id,
                )
                raise
            raise
        logger.debug("Saved background job: %s", job_id)

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
        """Insert a new background job using a plain INSERT that surfaces violations.

        The Postgres save_job already uses a plain INSERT (ON CONFLICT (job_id)
        DO NOTHING covers only PK collisions, not the partial unique index
        idx_active_job_per_repo). So duplicate active-job violations already raise
        UniqueViolation. This method is the Protocol-compliant entry point that
        job_tracker._atomic_insert_impl uses on the backend path.

        Bug #1565: this method is the EXCLUSIVE call path behind
        JobTracker.register_job_if_no_conflict()'s by-design cross-node
        single-flight guard -- every real caller catches the resulting
        DuplicateJobError and treats it as an expected, handled no-op
        (skip this tick, return HTTP 409, etc.), never an unhandled crash.
        The duplicate-violation log is therefore emitted at DEBUG below,
        not save_job()'s default WARNING (which remains correct for
        save_job()'s OTHER, non-conflict-tolerant direct callers).

        Raises:
            psycopg.errors.UniqueViolation: When idx_active_job_per_repo rejects
                the INSERT due to a duplicate active job for (operation_type, repo_alias).
        """
        self.save_job(
            job_id=job_id,
            operation_type=operation_type,
            status=status,
            created_at=created_at,
            username=username,
            progress=progress,
            started_at=started_at,
            completed_at=completed_at,
            result=result,
            error=error,
            is_admin=is_admin,
            cancelled=cancelled,
            repo_alias=repo_alias,
            resolution_attempts=resolution_attempts,
            claude_actions=claude_actions,
            failure_reason=failure_reason,
            extended_error=extended_error,
            language_resolution_status=language_resolution_status,
            current_phase=current_phase,
            phase_detail=phase_detail,
            progress_info=progress_info,
            metadata=metadata,
            executing_node=executing_node,
            claimed_at=claimed_at,
            actor_username=actor_username,
            duplicate_violation_log_level=logging.DEBUG,
        )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return job dict by job_id, or None if not found."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM background_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update_job(
        self,
        job_id: str,
        executing_node: Optional[str] = None,
        *,
        guard_terminal_status: bool = False,
        **kwargs: Any,
    ) -> None:
        """Update arbitrary columns on a background job row.

        Args:
            job_id: The job to update.
            executing_node: If provided, adds ``AND executing_node = %s``
                to the WHERE clause so only the owning node can update
                ownership-sensitive fields (Bug #542).
            guard_terminal_status: Bug #1344 opt-in. When True and the new
                ``status`` kwarg is non-terminal, the UPDATE is guarded with
                ``AND status NOT IN (<terminal statuses>)`` so a stale
                non-terminal write cannot revert a row that has already
                reached a terminal status via a separate, later write.
                Defaults to False so unrelated callers (e.g. JobTracker's
                dedup mechanism, which deliberately relies on an
                unconditional write to surface a real unique-constraint
                violation -- Bug #1256) are unaffected.
            **kwargs: Column=value pairs to update.
        """
        _JSON_FIELDS = {
            "result",
            "claude_actions",
            "extended_error",
            "language_resolution_status",
            "metadata",
        }
        updates: List[str] = []
        params: List[Any] = []

        for key, value in kwargs.items():
            if key not in _ALLOWED_JOB_COLUMNS:
                raise ValueError(f"Column {key!r} is not allowed")
            updates.append(f"{key} = %s")
            if value is None:
                params.append(None)
            elif key in _JSON_FIELDS:
                params.append(json.dumps(value))
            elif key == "progress_info" and isinstance(value, dict):
                # Bug #892: dict progress_info must be JSON-serialized before binding.
                # str progress_info passes through unchanged.
                params.append(json.dumps(value))
            else:
                params.append(value)

        if not updates:
            return

        params.append(job_id)
        # Bug #542: Ownership guard — when executing_node is provided,
        # only allow update if the row belongs to this node.
        where = "WHERE job_id = %s"
        if executing_node is not None:
            where += " AND executing_node = %s"
            params.append(executing_node)

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
            placeholders = ", ".join(["%s"] * len(_TERMINAL_JOB_STATUSES))
            where += f" AND status NOT IN ({placeholders})"
            params.extend(_TERMINAL_JOB_STATUSES)

        sql = f"UPDATE background_jobs SET {', '.join(updates)} {where}"

        # Bug #1235 GAP B: PG deadlock/serialization failures on background_jobs
        # writes are transient under multi-worker concurrent access.  Retry up to
        # _UPDATE_JOB_MAX_RETRIES times with a fresh connection each attempt.
        # Non-retryable errors propagate immediately without retry.
        #
        # Detection: psycopg3 exposes the SQLSTATE as exc.sqlstate on its error
        # classes.  We also accept the instance-level attribute for forward compat.
        def _is_retryable(exc: BaseException) -> bool:
            """Return True if *exc* is a transient PG locking error worth retrying."""
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate in _RETRYABLE_PG_SQLSTATES:
                return True
            # Fallback: check via psycopg error class names (import may fail in tests)
            try:
                import psycopg.errors as _pge

                return isinstance(
                    exc, (_pge.DeadlockDetected, _pge.SerializationFailure)
                )
            except ImportError:
                return False

        last_exc: Optional[BaseException] = None
        for attempt in range(_UPDATE_JOB_MAX_RETRIES):
            try:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                return  # success — exit retry loop
            except Exception as exc:
                if not _is_retryable(exc):
                    raise  # non-transient — propagate immediately
                last_exc = exc
                if attempt < _UPDATE_JOB_MAX_RETRIES - 1:
                    logger.warning(
                        "update_job: PG deadlock/serialization on attempt %d "
                        "(job_id=%s, sqlstate=%s); retrying",
                        attempt + 1,
                        job_id,
                        getattr(exc, "sqlstate", type(exc).__name__),
                    )
                    time.sleep((attempt + 1) * _UPDATE_JOB_BACKOFF_BASE)
        # All retries exhausted — re-raise the last deadlock/serialization error
        raise last_exc  # type: ignore[misc]

    def fail_orphaned_jobs(self, error: str = "Orphaned by server restart") -> int:
        """Mark all running/pending jobs as failed. Called on startup."""
        sql = (
            "UPDATE background_jobs SET status = 'failed', error = %s, "
            "completed_at = NOW() "
            "WHERE status IN ('running', 'pending')"
        )
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (error,))
                return int(cur.rowcount)

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
        conditions: List[str] = []
        params: List[Any] = []

        if username:
            conditions.append("username = %s")
            params.append(username)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = %s")
            params.append(operation_type)
        if exclude_operation_types:
            placeholders = ", ".join(["%s"] * len(exclude_operation_types))
            conditions.append(
                f"(operation_type IS NULL OR operation_type NOT IN ({placeholders}))"
            )
            params.extend(exclude_operation_types)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT {_SELECT_COLS} FROM background_jobs"
            f"{where} ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # Safety cap for list_job_ids_filtered: worst-case upper bound.
    # At 14k jobs/day with 30-day retention the table holds ~420k rows; a cap
    # of 50,000 is an order-of-magnitude ceiling that keeps the query bounded.
    _JOB_IDS_CAP = 50_000

    @staticmethod
    def _build_jobs_filter_where(
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        username: Optional[str] = None,
        exclude_ids: Optional[Any] = None,
    ) -> tuple:
        """Build the WHERE clause and params list for background_jobs queries.

        Uses %s paramstyle (PostgreSQL).  Returns (where_clause: str, params: list).
        Both list_jobs_filtered and list_job_ids_filtered call this helper so
        the filter logic cannot drift between the two methods.

        Args:
            status: Filter by exact status value
            operation_type: Filter by exact operation_type value
            search_text: Case-insensitive LIKE match against repo_alias, username,
                         operation_type, error, and job_id columns
            username: When set, scope results to this owner's jobs (H2 non-admin)
            exclude_ids: Set of job_ids to exclude
        """
        conditions: List[str] = []
        params: List[Any] = []

        # H2: Non-admin username scoping for DB-stored completed jobs
        if username is not None:
            conditions.append("username = %s")
            params.append(username)

        if status:
            conditions.append("status = %s")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = %s")
            params.append(operation_type)
        if search_text:
            like = f"%{search_text}%"
            conditions.append(
                "(LOWER(repo_alias) LIKE LOWER(%s)"
                " OR LOWER(username) LIKE LOWER(%s)"
                " OR LOWER(operation_type) LIKE LOWER(%s)"
                " OR LOWER(COALESCE(error, '')) LIKE LOWER(%s)"
                " OR LOWER(job_id) LIKE LOWER(%s))"
            )
            params.extend([like, like, like, like, like])
        if exclude_ids:
            exclude_list = list(exclude_ids)
            placeholders = ", ".join(["%s"] * len(exclude_list))
            conditions.append(f"job_id NOT IN ({placeholders})")
            params.extend(exclude_list)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        username: Optional[str] = None,
    ) -> tuple:
        """
        Return (list_of_job_dicts, total_count) with dynamic WHERE filters.

        Mirrors BackgroundJobsSqliteBackend.list_jobs_filtered() behaviour.
        """
        where, params = self._build_jobs_filter_where(
            status=status,
            operation_type=operation_type,
            search_text=search_text,
            username=username,
            exclude_ids=exclude_ids,
        )

        # Total count (ignores limit/offset)
        count_sql = f"SELECT COUNT(*) FROM background_jobs{where}"
        data_sql = f"SELECT {_SELECT_COLS} FROM background_jobs{where} ORDER BY created_at DESC"
        data_params = list(params)

        if limit is not None:
            data_sql += " LIMIT %s OFFSET %s"
            data_params.extend([limit, offset])

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_sql, params)
                total_count: int = cur.fetchone()[0]
                cur.execute(data_sql, data_params)
                rows = cur.fetchall()

        jobs = [self._row_to_dict(r) for r in rows]
        return jobs, total_count

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
        effective_cap = cap if cap is not None else self._JOB_IDS_CAP
        where, params = self._build_jobs_filter_where(
            status=status,
            operation_type=operation_type,
            search_text=search_text,
            username=username,
        )
        query = f"SELECT job_id FROM background_jobs{where} ORDER BY created_at DESC LIMIT %s"
        params_with_cap = list(params) + [effective_cap]
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params_with_cap)
                return {row[0] for row in cur.fetchall()}

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID. Returns True if a row was deleted."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM background_jobs WHERE job_id = %s", (job_id,))
                deleted: bool = cur.rowcount > 0
        if deleted:
            logger.debug("Deleted background job: %s", job_id)
        return deleted

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Delete old completed/failed/cancelled jobs older than max_age_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM background_jobs
                    WHERE status IN ('completed', 'failed', 'cancelled')
                      AND completed_at IS NOT NULL
                      AND completed_at < %s
                    """,
                    (cutoff_iso,),
                )
                count: int = cur.rowcount
        if count > 0:
            logger.info("Cleaned up %d old background jobs", count)
        return count

    def count_jobs_by_status(self) -> Dict[str, int]:
        """Return a dict mapping status -> count for all jobs."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM background_jobs GROUP BY status"
                )
                rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]:
        """Return completed/failed counts for jobs within the specified time window."""
        now = datetime.now(timezone.utc)
        if time_filter == "7d":
            cutoff = now - timedelta(days=7)
        elif time_filter == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = now - timedelta(hours=24)

        cutoff_iso = cutoff.isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM background_jobs
                    WHERE completed_at IS NOT NULL AND completed_at >= %s
                    GROUP BY status
                    """,
                    (cutoff_iso,),
                )
                rows = cur.fetchall()

        stats = {"completed": 0, "failed": 0}
        for row in rows:
            if row[0] in stats:
                stats[row[0]] = row[1]
        return stats

    def cleanup_orphaned_jobs_on_startup(self, node_id: Optional[str] = None) -> int:
        """
        Mark running/pending jobs as failed on server startup.

        Any job still in 'running' or 'pending' state when the server starts
        was orphaned by a previous crash or restart.

        Bug #1512: a 'running' row with executing_node IS NULL is
        unreachable by the node-scoped ``executing_node = %s`` branch on
        EVERY node — SQL ``NULL = <anything>`` is never true, including
        another NULL. No legitimate code path ever leaves a 'running' row
        with a NULL owner: DistributedJobClaimer.claim_next_job's atomic
        UPDATE always sets ``executing_node`` and ``status = 'running'``
        together in the same statement, and register_job_if_no_conflict
        stamps a real node_id for every non-pod-pull-eligible operation
        type. Such a row can therefore only be a genuine bug/orphan, so ANY
        node's startup cleanup may safely reclaim it — added as a second,
        independent OR-branch scoped to ``status = 'running'`` only (never
        'pending', to avoid reclaiming the legitimate PENDING pod-pull
        work-stealing queue state, where executing_node IS NULL is the
        normal, expected, unclaimed state).

        Bug #1563: under `uvicorn --workers N`, every worker's own
        lifespan startup calls this method with the SAME node_id, so the
        node-scoped predicate alone cannot tell "this node crashed" apart
        from "a single sibling worker on this same node was merely
        recycled" — it unconditionally failed every running/pending job
        on the whole node, including jobs genuinely still executing
        inside a healthy sibling worker process. Fixed by adding a
        worker-identity liveness check (see
        _owning_worker_process_is_alive) SCOPED ONLY to the node-owned
        branch: a row's ``executing_pid`` (stamped by
        save_job/atomic_claim_insert at claim time) is checked against
        THIS host's live process table via psutil.pid_exists -- only a
        row whose owning worker process is provably gone is failed. A
        genuine full-node restart (every worker process dies together) is
        unaffected: every executing_pid recorded for this node becomes
        dead by definition, so every one of them is still correctly
        reclaimed.

        The `executing_node IS NULL` branch above (Bug #1512) is
        DELIBERATELY EXCLUDED from the pid-liveness check and always
        unconditionally failed, exactly as before this fix. That branch's
        executing_pid can be a STALE value from a DIFFERENT node --
        job_reconciliation_service resets executing_node to NULL (without
        touching executing_pid) specifically when the OWNING node has
        left the cluster's active-node list, i.e. a cross-host scenario --
        so checking pid liveness on THIS host for that branch would risk
        a false "still alive" from an unrelated local process
        coincidentally reusing that foreign pid number, silently
        reintroducing the exact bug Bug #1512 fixed. That branch's own
        invariant ("no legitimate code path ever leaves a running row
        with a NULL owner") already proves every such row is a genuine
        orphan without needing a pid check at all.

        A worker-identity column was chosen over a single-designated-
        sweeper (one primary worker per node runs the sweep, siblings
        skip -- the existing Bug #1549 primary_instance_lock pattern)
        because the sweeper approach only protects the case where a
        NON-primary worker recycles. If the primary worker itself is the
        one that dies, its replacement immediately re-acquires the
        now-free lock and performs the exact same node-wide sweep,
        reproducing this bug for every OTHER sibling's still-running job.
        A per-job pid-liveness check is correct regardless of which
        worker recycles.

        Returns:
            Number of orphaned jobs cleaned up.
        """
        if node_id is None:
            # Bug #535: In cluster mode, cleaning ALL jobs is dangerous —
            # it kills jobs on healthy nodes during rolling restarts.
            # Return 0 (no-op) as a safe default when node_id is not provided.
            logger.warning(
                "cleanup_orphaned_jobs_on_startup called without node_id — "
                "skipping cleanup to protect cross-node jobs"
            )
            return 0

        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # Branch A: rows this node owns. Candidate for reclaim
                # only if the owning worker process is provably gone.
                cur.execute(
                    """
                    SELECT job_id, executing_pid
                    FROM background_jobs
                    WHERE status IN ('running', 'pending')
                      AND executing_node = %s
                    """,
                    (node_id,),
                )
                node_owned_candidates = cur.fetchall()
                job_ids_to_fail: List[str] = [
                    row[0]
                    for row in node_owned_candidates
                    if not _owning_worker_process_is_alive(row[1])
                ]

                # Branch B (Bug #1512): running rows with no owner at
                # all. Always a genuine orphan -- never pid-checked (see
                # docstring above for why checking it would be unsafe).
                cur.execute(
                    """
                    SELECT job_id
                    FROM background_jobs
                    WHERE status = 'running' AND executing_node IS NULL
                    """
                )
                job_ids_to_fail.extend(row[0] for row in cur.fetchall())

                count = 0
                if job_ids_to_fail:
                    cur.execute(
                        """
                        UPDATE background_jobs
                        SET status = 'failed',
                            error = %s,
                            completed_at = %s
                        WHERE job_id = ANY(%s)
                          AND status IN ('running', 'pending')
                        """,
                        (error_message, interrupted_at, job_ids_to_fail),
                    )
                    count = cur.rowcount
        if count > 0:
            logger.info("Cleaned up %d orphaned jobs on server startup", count)
        return count

    def find_active_job_by_type_and_alias(
        self,
        operation_type: str,
        repo_alias: str,
    ) -> Optional[str]:
        """Return job_id of the active (pending/running) row for (operation_type, repo_alias).

        Direct non-paginated lookup — no Python-side filtering, no LIMIT/OFFSET on
        the match itself (LIMIT 1 is applied only to cap the result to one row).
        Called by JobTracker._find_blocking_active_job_id after a unique-index
        violation to locate the blocking row without risking a pagination miss
        (Bug #1220).

        Args:
            operation_type: Operation type to match exactly.
            repo_alias: Repository alias to match exactly.

        Returns:
            job_id string if a pending or running row exists, else None.
        """
        sql = (
            "SELECT job_id FROM background_jobs "
            "WHERE operation_type = %s AND repo_alias = %s "
            "AND status IN ('pending', 'running') LIMIT 1"
        )
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (operation_type, repo_alias))
                row = cur.fetchone()
        return str(row[0]) if row is not None else None

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()
