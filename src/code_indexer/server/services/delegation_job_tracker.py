"""
Delegation Job Tracker Service.

Story #720: Callback-Based Delegation Job Completion

Singleton service for tracking pending delegation jobs with asyncio Futures.
Enables callback-based completion where Claude Server POSTs results back to CIDX.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from code_indexer.server.cache.payload_cache import PayloadCache
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


@dataclass
class JobResult:
    """
    Result from callback payload.

    Contains the data sent by Claude Server when a delegation job completes.
    """

    job_id: str
    status: str  # completed, failed
    output: str  # The Output field from callback - main result content
    exit_code: Optional[int]
    error: Optional[str]


class DelegationJobTracker:
    """
    Singleton service for tracking pending delegation jobs.

    Uses asyncio Futures to enable blocking wait for job completion via callback.

    Flow:
    1. execute_delegation_function calls register_job() after starting a job
    2. poll_delegation_job calls wait_for_job() to block until callback arrives
    3. Callback endpoint calls complete_job() when Claude Server POSTs result
    4. wait_for_job() unblocks and returns the JobResult
    """

    _instance: Optional["DelegationJobTracker"] = None

    @classmethod
    def get_instance(cls) -> "DelegationJobTracker":
        """
        Get the singleton instance.

        Returns:
            The singleton DelegationJobTracker instance
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        """Initialize the tracker with empty pending jobs dict."""
        self._pending_jobs: Dict[str, asyncio.Future] = {}
        self.__lock: Optional[asyncio.Lock] = None
        self._payload_cache: Optional["PayloadCache"] = None
        self._pool: Any = None
        self._sqlite_db_path: Optional[str] = None

    @property
    def _lock(self) -> asyncio.Lock:
        if self.__lock is None:
            self.__lock = asyncio.Lock()
        return self.__lock

    def set_connection_pool(self, pool: Any) -> None:
        """
        Set PostgreSQL connection pool for cluster mode.

        Bug #577: DB is the cross-node source of truth.

        Args:
            pool: psycopg connection pool instance
        """
        self._pool = pool
        logger.info("DelegationJobTracker: using PostgreSQL (cluster mode)")

    def set_sqlite_path(self, db_path: str) -> None:
        """
        Set SQLite database path for standalone mode.

        Bug #577: DB persistence for delegation job results.

        Args:
            db_path: Path to SQLite database file
        """
        self._sqlite_db_path = db_path

    def set_payload_cache(self, cache: "PayloadCache") -> None:
        """
        Set the PayloadCache instance for result caching.

        Story #720: Delegation Result Caching

        Args:
            cache: PayloadCache instance for caching delegation results
        """
        self._payload_cache = cache

    def _db_register_job(self, job_id: str) -> None:
        """Write pending job to DB. Bug #577."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO delegation_job_results (job_id, status) "
                    "VALUES (%s, 'pending') ON CONFLICT (job_id) DO NOTHING",
                    (job_id,),
                )
                conn.commit()
        elif self._sqlite_db_path:
            import sqlite3

            conn = sqlite3.connect(self._sqlite_db_path)
            conn.execute(
                "INSERT OR IGNORE INTO delegation_job_results "
                "(job_id, status) VALUES (?, 'pending')",
                (job_id,),
            )
            conn.commit()
            conn.close()

    def _db_complete_job(self, result: "JobResult") -> None:
        """Write completed job result to DB. Bug #577."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE delegation_job_results SET status = %s, output = %s, "
                    "exit_code = %s, error = %s, completed_at = CURRENT_TIMESTAMP "
                    "WHERE job_id = %s",
                    (
                        result.status,
                        result.output,
                        result.exit_code,
                        result.error,
                        result.job_id,
                    ),
                )
                conn.commit()
        elif self._sqlite_db_path:
            import sqlite3

            conn = sqlite3.connect(self._sqlite_db_path)
            conn.execute(
                "UPDATE delegation_job_results SET status = ?, output = ?, "
                "exit_code = ?, error = ?, completed_at = datetime('now') "
                "WHERE job_id = ?",
                (
                    result.status,
                    result.output,
                    result.exit_code,
                    result.error,
                    result.job_id,
                ),
            )
            conn.commit()
            conn.close()

    def _db_get_result(self, job_id: str) -> Optional["JobResult"]:
        """Read completed job result from DB. Returns None if pending or missing."""
        row = None
        if self._pool is not None:
            from psycopg.rows import dict_row

            with self._pool.connection() as conn:
                conn.row_factory = dict_row
                row = conn.execute(
                    "SELECT * FROM delegation_job_results "
                    "WHERE job_id = %s AND status != 'pending'",
                    (job_id,),
                ).fetchone()
        elif self._sqlite_db_path:
            import sqlite3

            conn = sqlite3.connect(self._sqlite_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM delegation_job_results "
                "WHERE job_id = ? AND status != 'pending'",
                (job_id,),
            ).fetchone()
            conn.close()
        if row is None:
            return None
        return JobResult(
            job_id=row["job_id"],
            status=row["status"],
            output=row["output"] or "",
            exit_code=row["exit_code"],
            error=row["error"],
        )

    def _db_has_pending(self, job_id: str) -> bool:
        """Check if job exists in DB (any status). Bug #577."""
        if self._pool is not None:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM delegation_job_results WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
            return row is not None
        elif self._sqlite_db_path:
            import sqlite3

            conn = sqlite3.connect(self._sqlite_db_path)
            row = conn.execute(
                "SELECT 1 FROM delegation_job_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            conn.close()
            return row is not None
        return False

    async def register_job(self, job_id: str) -> None:
        """
        Register a pending job and create its Future.

        This method is idempotent - calling it multiple times with the same
        job_id will not overwrite an existing Future.

        Args:
            job_id: The unique job identifier from Claude Server
        """
        async with self._lock:
            if job_id not in self._pending_jobs:
                loop = asyncio.get_event_loop()
                self._pending_jobs[job_id] = loop.create_future()
                logger.debug(f"Registered pending job: {job_id}")
        # Bug #577: Also register in DB for cross-node visibility
        self._db_register_job(job_id)

    async def has_job(self, job_id: str) -> bool:
        """
        Check if a job is registered in the tracker or has a cached result.

        Args:
            job_id: The job identifier to check

        Returns:
            True if the job is registered (pending completion) or result is cached,
            False otherwise
        """
        async with self._lock:
            if job_id in self._pending_jobs:
                return True
        # Also check cache — if result was cached, job "exists" for retry purposes
        if self._payload_cache is not None:
            try:
                cache_key = f"delegation:{job_id}"
                return bool(self._payload_cache.has_key(cache_key))
            except Exception as e:
                logger.debug(f"has_job: cache lookup failed for job {job_id}: {e}")
        # Bug #577: Check DB for cross-node visibility
        if self._db_get_result(job_id) is not None or self._db_has_pending(job_id):
            return True
        return False

    async def get_result(self, job_id: str) -> Optional[JobResult]:
        """
        Non-blocking check for job result. Does NOT remove the job from tracking.

        Checks:
        1. Cache (PayloadCache with key delegation:{job_id}) — result persisted by callback
        2. Future — if done, returns result without removing it from pending

        The job stays in the tracker so the client can poll again if needed.

        Args:
            job_id: The job identifier to check

        Returns:
            JobResult if the job is done (via cache or completed future), None if not ready
        """
        # Bug #577: Check DB first (cross-node source of truth)
        db_result = self._db_get_result(job_id)
        if db_result is not None:
            return db_result

        # Check cache (result persisted from complete_job callback)
        if self._payload_cache is not None:
            try:
                cache_key = f"delegation:{job_id}"
                if self._payload_cache.has_key(cache_key):
                    cached = self._payload_cache.retrieve(cache_key, page=0)
                    cached_dict = json.loads(cached.content)
                    logger.debug(
                        f"get_result: returning cached result for job: {job_id}"
                    )
                    return JobResult(**cached_dict)
            except Exception as e:
                logger.debug(f"get_result: cache lookup failed for job {job_id}: {e}")

        # Check if future is done (non-blocking — no await on the future itself)
        async with self._lock:
            future = self._pending_jobs.get(job_id)

        if future is not None and future.done():
            try:
                result: JobResult = future.result()
                logger.debug(f"get_result: returning future result for job: {job_id}")
                return result
            except Exception as e:
                logger.debug(
                    f"get_result: failed to get future result for job {job_id}: {e}"
                )
                return None

        return None  # Not ready yet

    async def complete_job(self, result: JobResult) -> bool:
        """
        Complete a pending job with the callback result.

        Resolves the Future associated with the job_id, allowing any
        wait_for_job() calls to unblock and receive the result.

        Story #720: Also caches the result in PayloadCache for retry scenarios.

        Args:
            result: The JobResult from the callback payload

        Returns:
            True if the job was found and completed, False otherwise
        """
        async with self._lock:
            future = self._pending_jobs.get(result.job_id)
            if future is None:
                logger.warning(
                    format_error_log(
                        "AUTH-GENERAL-028",
                        f"complete_job called for unknown job: {result.job_id}",
                    )
                )
                return False

            if future.done():
                logger.warning(
                    format_error_log(
                        "AUTH-GENERAL-029",
                        f"complete_job called for already completed job: {result.job_id}",
                    )
                )
                return False

            future.set_result(result)
            logger.debug(f"Completed job: {result.job_id} with status: {result.status}")

        # Cache result for retry scenarios (outside lock to avoid blocking)
        if self._payload_cache is not None:
            try:
                cache_key = f"delegation:{result.job_id}"
                serialized = json.dumps(asdict(result))
                self._payload_cache.store_with_key(cache_key, serialized)
                logger.debug(f"Cached result for job: {result.job_id}")
            except Exception as e:
                # Log but don't fail - caching is optional optimization
                logger.warning(
                    format_error_log(
                        "CACHE-GENERAL-004",
                        f"Failed to cache result for job {result.job_id}: {e}",
                    )
                )

        # Bug #577: Persist to DB for cross-node visibility
        self._db_complete_job(result)

        return True

    async def wait_for_job(
        self, job_id: str, timeout: float = 600.0
    ) -> Optional[JobResult]:
        """
        Wait for job completion via callback.

        Story #720: Checks cache FIRST for retry scenarios. If result is cached
        (from previous callback), returns immediately without waiting on Future.

        Blocks until complete_job() is called for this job_id, or timeout expires.

        On cache hit: Returns cached JobResult immediately.
        On timeout: Returns None but KEEPS the job in pending (caller can retry).
        On callback: Returns JobResult and REMOVES the job from pending.

        Args:
            job_id: The job identifier to wait for
            timeout: Maximum time to wait in seconds (default: 600s / 10 minutes)

        Returns:
            JobResult if callback arrived or cached, None if job not found or timeout
        """
        # Story #720: Check cache FIRST for retry scenarios
        if self._payload_cache is not None:
            try:
                cache_key = f"delegation:{job_id}"
                if self._payload_cache.has_key(cache_key):
                    cached = self._payload_cache.retrieve(cache_key, page=0)
                    cached_dict = json.loads(cached.content)
                    result = JobResult(**cached_dict)
                    logger.debug(f"Returning cached result for job: {job_id}")
                    # Remove job from pending since we're returning cached result
                    async with self._lock:
                        self._pending_jobs.pop(job_id, None)
                    return result
            except Exception as e:
                # Log but continue to wait on Future if cache fails
                logger.debug(f"Cache lookup failed for job {job_id}: {e}")

        # Bug #577: Check DB for cross-node completion
        db_result = self._db_get_result(job_id)
        if db_result is not None:
            async with self._lock:
                self._pending_jobs.pop(job_id, None)
            return db_result

        async with self._lock:
            future = self._pending_jobs.get(job_id)

        if future is None:
            logger.warning(
                format_error_log(
                    "CACHE-GENERAL-005",
                    f"wait_for_job called for unknown job: {job_id}",
                )
            )
            return None

        try:
            # Use asyncio.shield() to prevent wait_for from cancelling the Future
            # This allows retry after timeout - the Future stays intact
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            # Only remove on successful callback receipt
            async with self._lock:
                self._pending_jobs.pop(job_id, None)
            logger.debug(f"wait_for_job returned result for job: {job_id}")
            return result
        except asyncio.TimeoutError:
            # DO NOT remove on timeout - job is still valid, caller can retry
            logger.debug(
                f"wait_for_job timed out for job: {job_id}, keeping in tracker"
            )
            return None
        except asyncio.CancelledError:
            # Shield was cancelled but Future may still be valid - propagate
            raise

    async def cancel_job(self, job_id: str) -> bool:
        """
        Explicitly remove a job from tracking (caller gave up).

        Use this method when the caller decides to stop waiting for a job
        and wants to clean up the tracker. This cancels the Future and
        removes the job from pending.

        Args:
            job_id: The job identifier to cancel

        Returns:
            True if the job was found and cancelled, False otherwise
        """
        async with self._lock:
            future = self._pending_jobs.pop(job_id, None)
            if future is None:
                return False

            if not future.done():
                future.cancel()
            logger.debug(f"Cancelled job: {job_id}")
            return True
