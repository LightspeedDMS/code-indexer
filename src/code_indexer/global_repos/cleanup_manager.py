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
from pathlib import Path
from typing import Dict, Optional, Set

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

    def __init__(self, query_tracker: QueryTracker, check_interval: float = 1.0):
        """
        Initialize the cleanup manager.

        Args:
            query_tracker: QueryTracker instance for ref count monitoring
            check_interval: How often to check for cleanups (seconds)
        """
        self._query_tracker = query_tracker
        self._check_interval = check_interval
        self._cleanup_queue: Set[str] = set()
        self._queue_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Per-path failure tracking for backoff and circuit breaker
        self._failure_counts: Dict[str, int] = {}
        self._next_retry_times: Dict[str, float] = {}
        self._stats_lock = threading.Lock()

    def schedule_cleanup(self, index_path: str) -> None:
        """Schedule index_path for deletion once its ref count reaches zero."""
        with self._queue_lock:
            self._cleanup_queue.add(index_path)
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
            delay = min(self.BASE_BACKOFF_DELAY * (2 ** (count - 1)), self.MAX_BACKOFF_DELAY)
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
        return float(min(self.BASE_BACKOFF_DELAY * (2 ** (count - 1)), self.MAX_BACKOFF_DELAY))

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
                except OSError:
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
            logger.warning(f"EMFILE during rmtree for {path}, switching to bottom-up deletion")

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
            raise OSError(errno.ENOTEMPTY, "Partial deletion - directory still exists", str(path))

    def _delete_index(self, index_path: str) -> None:
        """Delete an index directory using robust deletion with FD management."""
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
                logger.critical(
                    f"Circuit breaker tripped for {path}: "
                    f"{failure_count} consecutive failures. "
                    f"Removing from cleanup queue permanently."
                )
                continue

            if not self._is_ready_for_retry(path):
                logger.debug(f"Path {path} is in backoff window, skipping")
                continue

            try:
                ref_count = self._query_tracker.get_ref_count(path)
                if ref_count == 0:
                    self._delete_index(path)
                    with self._queue_lock:
                        self._cleanup_queue.discard(path)
                    self._reset_failure_count(path)
                    logger.info(f"Deleted old index: {path}")
                else:
                    logger.debug(f"Skipping cleanup for {path}: {ref_count} active queries")
            except Exception as e:
                logger.error(f"Failed to clean up {path}: {e}", exc_info=True)
                self._record_failure(path)
