"""
Cleanup Manager for automatic deletion of old index versions.

Monitors reference counts and deletes old index directories when
no active queries remain. Runs as a background thread with configurable
check interval.
"""

import errno
import gc
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from .query_tracker import QueryTracker


logger = logging.getLogger(__name__)


class CleanupManager:
    """
    Background manager for cleaning up old index versions.

    Monitors the reference counts from QueryTracker and deletes
    index directories when their ref count reaches zero and they
    are scheduled for cleanup.

    Includes exponential backoff, circuit breaker, and FD monitoring
    to prevent FD exhaustion during cleanup (issue #297).
    """

    MAX_FAILURES = 5
    MAX_BACKOFF_DELAY = 60.0  # seconds
    BASE_BACKOFF_DELAY = 1.0  # seconds
    FD_USAGE_THRESHOLD = 0.80  # 80%

    #: Story #1457 AC13: default minimum retention age (seconds) a
    #: superseded versioned snapshot must remain undeleted after being
    #: scheduled for cleanup, even once its refcount reaches zero. Closes
    #: the cross-process residual the in-process resolution-scope pin
    #: cannot close (QueryTracker is process-local). Mirrors the existing
    #: PayloadCache default TTL (also 900s).
    MIN_RETENTION_AGE_SECONDS = 900.0

    def __init__(
        self,
        query_tracker: QueryTracker,
        check_interval: float = 1.0,
        job_tracker=None,
        min_retention_age_seconds: float = MIN_RETENTION_AGE_SECONDS,
        min_retention_age_getter: Optional[Callable[[], float]] = None,
        persistence_backend: Optional[Any] = None,
    ):
        """
        Initialize the cleanup manager.

        Args:
            query_tracker: QueryTracker instance for ref count monitoring
            check_interval: How often to check for cleanups (seconds)
            job_tracker: Optional JobTracker for dashboard visibility (Story #314)
            min_retention_age_seconds: Story #1457 AC13 -- minimum seconds a
                scheduled path must remain undeleted since being enqueued,
                independent of and in addition to the refcount-zero gate.
                Defaults to MIN_RETENTION_AGE_SECONDS (900s / 15 min). Used
                as-is when min_retention_age_getter is not provided.
            min_retention_age_getter: Story #1457 AC13 PT-13 follow-up --
                optional callable consulted LIVE on every retention check
                (never cached), taking priority over
                min_retention_age_seconds when provided. Lets the caller wire
                a runtime-configurable source (e.g. Web UI Config Screen)
                without cleanup_manager.py importing the server-only
                ConfigService directly -- keeps this shared/CLI-reachable
                module free of a server dependency and avoids a background
                thread accidentally constructing a real ConfigService() in a
                unit-test context with no server fixtures. None (default)
                preserves today's byte-identical static-value behavior.
            persistence_backend: Bug #1567 -- optional durable backend
                (GoldenRepoMetadataBackend-shaped: schedule_cleanup_deletion/
                list_cleanup_pending_deletions/remove_cleanup_pending_deletion)
                backing the pending-deletion queue. When provided, every
                scheduled path is durably persisted with a WALL-CLOCK
                scheduled_at, and this constructor immediately hydrates the
                in-memory queue from whatever is already durably pending
                (recovering entries a PRIOR process scheduled -- the fix for
                a restart/worker-recycle silently discarding the queue).
                None (default) preserves today's pure in-process behavior
                (e.g. standalone CLI usage with no shared metadata backend).
        """
        self._query_tracker = query_tracker
        self._check_interval = check_interval
        self._cleanup_queue: Set[str] = set()
        self._queue_lock = threading.Lock()
        self._min_retention_age_seconds = min_retention_age_seconds
        self._min_retention_age_getter = min_retention_age_getter
        # Story #1457 AC13 / Bug #1567: enqueue-time per scheduled path, set
        # on first enqueue only (schedule_cleanup may be called more than
        # once for the same path; the ORIGINAL supersession moment is what
        # "scheduled_at" must mean). WALL-CLOCK (time.time()), never
        # time.monotonic() -- the retention floor must survive a process
        # restart, and time.monotonic() has no meaning across processes.
        self._scheduled_at: Dict[str, float] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Per-path failure tracking for backoff and circuit breaker
        self._failure_counts: Dict[str, int] = {}
        self._next_retry_times: Dict[str, float] = {}
        self._stats_lock = threading.Lock()
        self._job_tracker = job_tracker  # Story #314: dashboard visibility
        # Bug #1084 Phase A5: backend-aware deletion. When set, snapshot-shaped
        # paths are deleted via VersionedSnapshotManager.delete_snapshot (daemon
        # DELETE / FlexClone free / local rmtree) instead of a bare rmtree that
        # would leave a ghost daemon row or leak a FlexClone volume. The refcount
        # gate in _process_cleanup_queue is unchanged — deletion still only fires
        # once QueryTracker reports zero active queries for the path.
        self._snapshot_manager: Optional[object] = None
        # Bug #1567: durable pending-deletion queue backend.
        self._persistence_backend: Optional[Any] = None
        if persistence_backend is not None:
            self.set_persistence_backend(persistence_backend)

    def set_snapshot_manager(self, snapshot_manager: object) -> None:
        """Wire the VersionedSnapshotManager for backend-correct deletion (Bug #1084).

        Set post-construction in lifecycle wiring because the snapshot manager is
        built after the CleanupManager. Non-None enables backend-aware deletion of
        versioned snapshots; deletion still occurs only behind the refcount gate.
        """
        self._snapshot_manager = snapshot_manager

    def set_persistence_backend(self, persistence_backend: Any) -> None:
        """Wire the durable pending-deletion backend, post-construction or
        at construction time (Bug #1567).

        Mirrors set_snapshot_manager's post-hoc wiring pattern (the shared
        metadata backend may become available after construction, e.g. in
        lifespan.py). Immediately hydrates the in-memory queue from
        whatever is already durably pending -- recovering exactly what a
        PRIOR process/worker already scheduled.
        """
        self._persistence_backend = persistence_backend
        self._hydrate_from_backend()

    def _hydrate_from_backend(self) -> None:
        """Load every durably-pending path into the in-memory queue/cache
        (Bug #1567).

        Fail-soft: a hydrate failure logs an error and leaves the manager
        running with whatever it already had in memory, rather than
        crashing startup -- the periodic orphan sweep
        (server/services/versioned_snapshot_reconciler.py) is the backstop
        that re-discovers anything a failed hydrate missed.
        """
        if self._persistence_backend is None:
            return
        try:
            pending = self._persistence_backend.list_cleanup_pending_deletions()
        except Exception as exc:
            logger.error(
                f"CleanupManager: failed to hydrate pending-deletion queue "
                f"from durable backend (non-fatal): {exc}"
            )
            return
        with self._queue_lock:
            for entry in pending:
                path = entry["index_path"]
                self._cleanup_queue.add(path)
                self._scheduled_at.setdefault(path, float(entry["scheduled_at"]))
        if pending:
            logger.info(
                f"CleanupManager: hydrated {len(pending)} pending deletion(s) "
                f"from durable backend"
            )

    def schedule_cleanup(self, index_path: str) -> None:
        """Schedule index_path for deletion once its ref count reaches zero
        AND the minimum retention age has elapsed (Story #1457 AC13).

        Bug #1567: scheduled_at is a WALL-CLOCK timestamp (time.time()),
        never time.monotonic() -- the retention floor must survive a
        process restart, and monotonic time has no cross-process meaning.
        When a durable persistence_backend is wired, the schedule is ALSO
        persisted there (idempotent -- a re-schedule of an already-queued
        path returns and preserves the ORIGINAL scheduled_at, never
        resetting its age) so a restart/worker-recycle no longer silently
        discards it. A backend write failure is fail-soft (logged, not
        raised) so a transient DB hiccup never blocks a refresh -- the
        periodic orphan sweep is the backstop for anything left
        unpersisted.
        """
        now = time.time()
        scheduled_at = now
        if self._persistence_backend is not None:
            try:
                scheduled_at = float(
                    self._persistence_backend.schedule_cleanup_deletion(index_path, now)
                )
            except Exception as exc:
                logger.error(
                    f"CleanupManager: failed to durably persist scheduled "
                    f"cleanup for {index_path} (non-fatal, in-memory-only "
                    f"for this process until the next orphan sweep): {exc}"
                )
                scheduled_at = now

        with self._queue_lock:
            self._cleanup_queue.add(index_path)
            # setdefault: schedule_cleanup fires AT the swap that supersedes
            # a version, so "scheduled_at" genuinely is the supersession
            # moment -- a re-schedule of an already-queued path must NOT
            # reset its age.
            self._scheduled_at.setdefault(index_path, scheduled_at)
            logger.info(f"Scheduled cleanup for: {index_path}")

    def get_pending_cleanups(self) -> Set[str]:
        """Return set of paths currently scheduled for cleanup."""
        with self._queue_lock:
            return set(self._cleanup_queue)

    def is_running(self) -> bool:
        """Return True if background cleanup thread is active."""
        return self._running

    def start(self) -> None:
        """Start the cleanup manager background thread. Idempotent."""
        if self._running:
            logger.debug("Cleanup manager already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._thread.start()
        logger.info("Cleanup manager started")

    def stop(self) -> None:
        """Stop the cleanup manager background thread. Idempotent."""
        if not self._running:
            logger.debug("Cleanup manager already stopped")
            return
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Cleanup manager stopped")

    # ------------------------------------------------------------------
    # Per-path failure tracking (backoff + circuit breaker)
    # ------------------------------------------------------------------

    def _record_failure(self, index_path: str) -> None:
        """Increment failure count for path and schedule next retry via backoff."""
        with self._stats_lock:
            count = self._failure_counts.get(index_path, 0) + 1
            self._failure_counts[index_path] = count
            delay = min(
                self.BASE_BACKOFF_DELAY * (2 ** (count - 1)), self.MAX_BACKOFF_DELAY
            )
            self._next_retry_times[index_path] = time.monotonic() + delay

    def _get_failure_count(self, index_path: str) -> int:
        """Return current consecutive failure count for path."""
        with self._stats_lock:
            return self._failure_counts.get(index_path, 0)

    def _reset_failure_count(self, index_path: str) -> None:
        """Clear failure count and retry time for path after successful deletion."""
        with self._stats_lock:
            self._failure_counts.pop(index_path, None)
            self._next_retry_times.pop(index_path, None)

    def _get_backoff_delay(self, index_path: str) -> float:
        """Return backoff delay in seconds for current failure count (capped at MAX_BACKOFF_DELAY)."""
        with self._stats_lock:
            count = self._failure_counts.get(index_path, 0)
        if count == 0:
            return 0.0
        return float(
            min(self.BASE_BACKOFF_DELAY * (2 ** (count - 1)), self.MAX_BACKOFF_DELAY)
        )

    def _is_ready_for_retry(self, index_path: str) -> bool:
        """Return True if backoff period for path has elapsed."""
        with self._stats_lock:
            next_retry = self._next_retry_times.get(index_path, 0.0)
        return time.monotonic() >= next_retry

    # ------------------------------------------------------------------
    # FD monitoring
    # ------------------------------------------------------------------

    def _is_fd_usage_high(self) -> bool:
        """Return True if process FD usage exceeds FD_USAGE_THRESHOLD. Non-Linux: always False."""
        try:
            fd_dir = "/proc/self/fd"
            if not os.path.isdir(fd_dir):
                return False
            try:
                import resource

                soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            except Exception:
                return False
            if soft_limit <= 0:
                return False
            return len(os.listdir(fd_dir)) / soft_limit >= self.FD_USAGE_THRESHOLD
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def _robust_delete(self, path: Path) -> None:
        """
        Delete directory robustly, handling EMFILE errors.

        Tries shutil.rmtree with an onerror callback that runs gc.collect()
        on EMFILE and retries. Falls back to bottom-up os.walk deletion if
        rmtree itself raises EMFILE.

        Raises:
            OSError: If deletion ultimately fails
        """

        def _onerror(func, failed_path, exc_info):  # type: ignore[no-untyped-def]
            exc = exc_info[1]
            if isinstance(exc, OSError) and exc.errno == errno.EMFILE:
                gc.collect()
                time.sleep(0.05)
                try:
                    func(failed_path)
                except (OSError, TypeError):
                    pass
            else:
                logger.debug(f"rmtree onerror: {func.__name__}({failed_path}): {exc}")
                raise exc

        try:
            shutil.rmtree(str(path), onerror=_onerror)
            return
        except OSError as e:
            if e.errno != errno.EMFILE:
                raise
            logger.warning(
                f"EMFILE during rmtree for {path}, switching to bottom-up deletion"
            )

        # Bottom-up fallback: files first, then empty dirs
        for dirpath, dirnames, filenames in os.walk(str(path), topdown=False):
            for fname in filenames:
                try:
                    os.unlink(os.path.join(dirpath, fname))
                except OSError as e:
                    logger.debug(f"Fallback unlink failed: {e}")
            for dname in dirnames:
                try:
                    os.rmdir(os.path.join(dirpath, dname))
                except OSError as e:
                    logger.debug(f"Fallback rmdir failed: {e}")
            gc.collect()

        try:
            os.rmdir(str(path))
        except OSError:
            pass

        if path.exists():
            raise OSError(
                errno.ENOTEMPTY, "Partial deletion - directory still exists", str(path)
            )

    def _remove_persisted(self, index_path: str) -> None:
        """Remove a durably-pending deletion row for ``index_path`` (Bug
        #1567), a no-op when no persistence_backend is wired.

        Called after a path is actually deleted, and after the circuit
        breaker permanently abandons a path. Fail-soft: a backend failure
        here must never affect the caller, which already completed the
        action this row was tracking -- a stale row that survives will
        just be re-hydrated on the next restart and no-op harmlessly (the
        path is already gone, or already circuit-breaker-abandoned).
        """
        if self._persistence_backend is None:
            return
        try:
            self._persistence_backend.remove_cleanup_pending_deletion(index_path)
        except Exception as exc:
            logger.error(
                f"CleanupManager: failed to remove durable pending-deletion "
                f"row for {index_path} (non-fatal): {exc}"
            )

    def _delete_index(self, index_path: str) -> None:
        """Delete an index path.

        Bug #1084 Phase A5: versioned snapshots are deleted through the wired
        :class:`VersionedSnapshotManager` so the deletion is backend-correct
        (cow-daemon: daemon DELETE keeps the SQLite registry consistent; ONTAP:
        FlexClone client frees the volume; local: rmtree inside the manager).
        Non-snapshot paths (and the case where no snapshot manager is wired) fall
        back to the original robust rmtree. This method only runs AFTER the
        refcount-zero gate in :meth:`_process_cleanup_queue`.
        """
        sm = self._snapshot_manager
        if sm is not None and sm.is_versioned_snapshot(index_path):  # type: ignore[attr-defined]
            logger.debug(f"Backend-deleting versioned snapshot: {index_path}")
            # version_path is the authoritative identifier; backend implementations
            # derive (namespace, name) from the path. alias is unused in the
            # CloneBackend-delegated path, so pass empty string.
            sm.delete_snapshot("", index_path)  # type: ignore[attr-defined]
            return

        path = Path(index_path)
        if not path.exists():
            logger.debug(f"Index path already deleted: {index_path}")
            return
        if not path.is_dir():
            logger.warning(f"Index path is not a directory: {index_path}")
            return
        self._robust_delete(path)
        logger.debug(f"Removed directory: {index_path}")

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _cleanup_loop(self) -> None:
        """Background thread: poll cleanup queue at check_interval."""
        logger.debug("Cleanup loop started")
        while self._running:
            try:
                self._process_cleanup_queue()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
            sleep_remaining = self._check_interval
            while sleep_remaining > 0 and self._running:
                sleep_chunk = min(0.1, sleep_remaining)
                time.sleep(sleep_chunk)
                sleep_remaining -= sleep_chunk
        logger.debug("Cleanup loop exited")

    def _process_cleanup_queue(self) -> None:
        """
        Process the cleanup queue and delete eligible paths.

        Applies FD monitoring (skip cycle), circuit breaker (remove permanently
        after MAX_FAILURES), and exponential backoff (skip until delay elapses).
        Registers index_cleanup jobs in JobTracker for dashboard visibility (Story #314).
        """
        if self._is_fd_usage_high():
            logger.warning(
                "File descriptor usage is above threshold; "
                "skipping cleanup cycle to avoid FD exhaustion"
            )
            return

        with self._queue_lock:
            paths_to_check = list(self._cleanup_queue)

        for path in paths_to_check:
            failure_count = self._get_failure_count(path)
            if failure_count >= self.MAX_FAILURES:
                with self._queue_lock:
                    self._cleanup_queue.discard(path)
                    # Story #1457 AC13: drop the age-tracking entry too, so
                    # a circuit-breaker-abandoned path does not leak in
                    # _scheduled_at forever.
                    self._scheduled_at.pop(path, None)
                # Bug #1567 (Codex review): deliberately do NOT call
                # self._remove_persisted(path) here. failure_count is
                # intentionally NOT persisted (it is per-process, unlike
                # scheduled_at), so removing the durable row on a
                # circuit-breaker trip would abandon a genuinely-stuck
                # deletion FOREVER -- reintroducing the exact leak class
                # this bug exists to close, just triggered by a different
                # failure mode. Leaving the row durable means the NEXT
                # process/worker restart (or the periodic orphan sweep's
                # own schedule_cleanup call, which is a no-op against an
                # already-present row) re-hydrates it with a FRESH
                # in-memory failure budget -- a real retry-after, driven
                # by the natural process lifecycle rather than a second
                # persisted counter. Only THIS process's in-memory queue
                # entry is dropped, so it stops hot-looping on a path it
                # has already proven it cannot delete.
                logger.critical(
                    f"Circuit breaker tripped for {path}: "
                    f"{failure_count} consecutive failures. "
                    f"Dropping from this process's in-memory queue "
                    f"(the durable row survives for a future retry)."
                )
                continue

            if not self._is_ready_for_retry(path):
                logger.debug(f"Path {path} is in backoff window, skipping")
                continue

            ref_count = self._query_tracker.get_ref_count(path)
            if ref_count != 0:
                logger.debug(f"Skipping cleanup for {path}: {ref_count} active queries")
                continue

            # Story #1457 AC13: minimum-retention-age floor, ADDED IN
            # ADDITION TO the refcount-zero gate above. Closes the
            # cross-process residual a process-local QueryTracker cannot
            # see (another worker/node's in-flight reader). Re-evaluated
            # on the next poll -- the path stays queued, never dropped.
            with self._queue_lock:
                scheduled_at = self._scheduled_at.get(path, 0.0)
            # Bug #1567: WALL-CLOCK age, matching scheduled_at's now-wall-
            # clock semantics -- time.monotonic() would be meaningless here
            # since scheduled_at may have been hydrated from a durable
            # backend written by a PRIOR process.
            age = time.time() - scheduled_at
            # Story #1457 AC13 PT-13 follow-up: consult the live getter (if
            # provided) on EVERY check rather than caching it -- lets a
            # runtime config change (e.g. Web UI Config Screen) take effect
            # without a server restart, mirroring how refresh_scheduler.py
            # reads snapshot_retention_keep_last live at the point of use.
            effective_min_retention_age_seconds = (
                self._min_retention_age_getter()
                if self._min_retention_age_getter is not None
                else self._min_retention_age_seconds
            )
            if age < effective_min_retention_age_seconds:
                logger.debug(
                    f"Skipping cleanup for {path}: minimum retention age "
                    f"not yet elapsed ({age:.1f}s < "
                    f"{effective_min_retention_age_seconds:.1f}s)"
                )
                continue

            # Story #314: Register index_cleanup job for dashboard visibility
            tracked_job_id = None
            if self._job_tracker is not None:
                try:
                    tracked_job_id = f"index-cleanup-{uuid.uuid4().hex[:8]}"
                    self._job_tracker.register_job(
                        tracked_job_id,
                        "index_cleanup",
                        username="system",
                        repo_alias="server",
                    )
                    self._job_tracker.update_status(tracked_job_id, status="running")
                except Exception as e:
                    logger.debug(f"Failed to register index_cleanup job: {e}")
                    tracked_job_id = None

            try:
                self._delete_index(path)
                with self._queue_lock:
                    self._cleanup_queue.discard(path)
                    # Story #1457 AC13: clear the age-tracking entry now
                    # that deletion actually happened.
                    self._scheduled_at.pop(path, None)
                self._remove_persisted(path)
                self._reset_failure_count(path)
                logger.info(f"Deleted old index: {path}")

                if tracked_job_id and self._job_tracker is not None:
                    try:
                        self._job_tracker.complete_job(tracked_job_id)
                    except Exception as e:
                        logger.debug(
                            f"Failed to complete index_cleanup job {tracked_job_id}: {e}"
                        )
            except Exception as e:
                logger.error(f"Failed to clean up {path}: {e}", exc_info=True)
                self._record_failure(path)

                if tracked_job_id and self._job_tracker is not None:
                    try:
                        self._job_tracker.fail_job(tracked_job_id, error=str(e))
                    except Exception as e2:
                        logger.debug(
                            f"Failed to mark index_cleanup job {tracked_job_id} as failed: {e2}"
                        )
