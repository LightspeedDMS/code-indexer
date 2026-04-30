"""
Fire-and-forget latency tracker for external dependency calls.

Story #680: External Dependency Latency Observability

Provides:
- DependencyLatencyTracker: thread-safe deque buffer with daemon writer thread
  that flushes samples to SQLite and prunes samples older than retention window.
"""

import logging
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Generator, List, Optional

logger = logging.getLogger(__name__)

# ── Module-level singleton ─────────────────────────────────────────────────────
_tracker_instance: "Optional[DependencyLatencyTracker]" = None
_tracker_lock = threading.Lock()


def set_instance(tracker: "Optional[DependencyLatencyTracker]") -> None:
    """Register (or clear) the module-level DependencyLatencyTracker singleton.

    Called once at server startup (service_init.py) after the tracker is
    created and started. Passing None clears the singleton (used in tests).

    Both set_instance() and get_instance() acquire _tracker_lock to provide
    a clear thread-safety contract beyond CPython assignment atomicity.
    """
    global _tracker_instance
    with _tracker_lock:
        _tracker_instance = tracker


def get_instance() -> "Optional[DependencyLatencyTracker]":
    """Return the registered DependencyLatencyTracker, or None if not set."""
    with _tracker_lock:
        return _tracker_instance


# Writer thread: flush every this many seconds
_DEFAULT_FLUSH_INTERVAL_S = 5.0

# Retain samples for this many seconds by default (5 minutes)
_DEFAULT_RETENTION_S = 300.0

# Buffer capacity — oldest entry is silently dropped on overflow
_BUFFER_MAXLEN = 10000

# Status code recorded when the instrumented block raises an exception
_EXCEPTION_STATUS_CODE = -1

# Node ID placeholder: resolved from environment / config if available
_DEFAULT_NODE_ID = "local"

# Substring present in sqlite3.ProgrammingError when the database connection is
# closed — used to detect a terminal condition in the writer thread.
_CLOSED_DB_SUBSTRING = "closed database"

# Number of consecutive flush/prune failures (any error type) before the writer
# thread gives up and terminates — prevents infinite loops on persistent errors.
_MAX_CONSECUTIVE_FAILURES = 5


def _validate_positive_float(value: Any, name: str) -> None:
    """Raise ValueError if value is not a positive (non-bool) float or int."""
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(
            f"{name} must be a positive number, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


class DependencyLatencyTracker:
    """
    Thread-safe, fire-and-forget latency recorder for external dependencies.

    Uses a bounded deque protected by a ``threading.Lock`` as an in-memory
    buffer. A background daemon thread periodically flushes the buffer to
    SQLite and prunes samples older than ``retention_s`` seconds, bounding
    both memory and storage growth.

    ``record_sample()`` is O(1) and never blocks or raises — safe to call
    from any hot path without latency impact.

    Writer loop termination: the daemon loop is bounded by ``_stop_event``,
    which is set by ``shutdown()``. Each iteration uses ``Event.wait(timeout)``
    so the thread wakes at most once per ``flush_interval_s`` and exits
    immediately when the event fires. This is the standard daemon-service
    pattern: termination is event-driven, not iteration-count-bounded.
    """

    def __init__(
        self,
        backend: Any,
        flush_interval_s: float = _DEFAULT_FLUSH_INTERVAL_S,
        retention_s: float = _DEFAULT_RETENTION_S,
        node_id: str = _DEFAULT_NODE_ID,
    ) -> None:
        """
        Args:
            backend:          Storage backend (DependencyLatencyBackend or compatible).
            flush_interval_s: How often the writer thread flushes the buffer to storage.
                              Must be a positive number.
            retention_s:      Samples older than this many seconds are deleted from storage.
                              Must be a positive number.
            node_id:          Node identifier stamped on every persisted sample.
                              Must be a non-empty string.

        Raises:
            ValueError: If backend is None, node_id is empty, or numeric args are invalid.
        """
        if backend is None:
            raise ValueError("backend must not be None")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id must be a non-empty string")
        _validate_positive_float(flush_interval_s, "flush_interval_s")
        _validate_positive_float(retention_s, "retention_s")

        self._backend = backend
        self._flush_interval_s = flush_interval_s
        self._retention_s = retention_s
        self._node_id = node_id

        self._buffer: deque = deque(maxlen=_BUFFER_MAXLEN)
        self._buffer_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def record_sample(
        self,
        dependency_name: str,
        latency_ms: float,
        status_code: int,
    ) -> None:
        """
        Append a latency sample to the in-memory buffer.

        O(1), non-blocking. Exceptions are logged at DEBUG level and discarded
        so instrumentation never affects the calling thread.
        """
        try:
            sample = {
                "node_id": self._node_id,
                "dependency_name": dependency_name,
                "timestamp": time.time(),
                "latency_ms": latency_ms,
                "status_code": status_code,
            }
            with self._buffer_lock:
                self._buffer.append(sample)
        except Exception as exc:
            # Deliberately discarded: instrumentation must never affect callers.
            # Logged at DEBUG so failures are visible in diagnostics without noise.
            logger.debug(
                "DependencyLatencyTracker.record_sample failed (discarded): %s", exc
            )

    @contextmanager
    def track_latency(
        self,
        dependency_name: str,
        expected_status_code: int,
    ) -> Generator[None, None, None]:
        """
        Context manager that measures wall-clock latency and records a sample.

        On normal exit: records ``expected_status_code``.
        On any exception: records ``_EXCEPTION_STATUS_CODE`` (-1) and re-raises.

        Never swallows exceptions from the caller's block.
        """
        start = time.monotonic()
        status_code = expected_status_code
        try:
            yield
        except Exception:
            status_code = _EXCEPTION_STATUS_CODE
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.record_sample(dependency_name, elapsed_ms, status_code)

    def start(self) -> None:
        """Launch the background writer daemon thread."""
        self._stop_event.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="DependencyLatencyTracker-writer",
        )
        self._writer_thread.start()
        logger.info("DependencyLatencyTracker started")

    def shutdown(self, timeout: int = 10) -> None:
        """
        Signal the writer thread to stop and wait up to ``timeout`` seconds.

        Safe to call multiple times — idempotent.
        """
        self._stop_event.set()
        if self._writer_thread is not None and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=timeout)
        logger.info("DependencyLatencyTracker stopped")

    # ── Private: writer loop ───────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        """
        Background daemon loop: flush buffer to storage, delete stale samples.

        Termination paths:
        1. Normal shutdown: ``_stop_event`` set by ``shutdown()`` — final flush runs.
        2. Closed-database terminal: ``_flush_buffer`` / ``_prune_stale`` set
           ``_stop_event`` and re-raise; the loop detects the set event and breaks
           without additional logging. Final flush is skipped.
        3. Max consecutive failures: after ``_MAX_CONSECUTIVE_FAILURES`` consecutive
           exceptions (that did NOT already set ``_stop_event``), the loop sets
           ``_stop_event`` and logs exactly ONE error, then breaks. Final flush is
           skipped.

        A successful iteration resets the consecutive-failures counter to zero.
        """
        consecutive_failures = 0
        terminal_failure = False
        # Daemon-service pattern: loop is bounded by stop_event (set by shutdown).
        # Event.wait(timeout) ensures the thread wakes at most every flush_interval_s
        # and exits immediately when the event fires — termination is event-driven.
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._flush_interval_s)
            if self._stop_event.is_set():
                break
            try:
                self._flush_and_prune()
                consecutive_failures = 0
            except Exception:
                # If _flush_buffer/_prune_stale already set _stop_event (closed-db),
                # exit immediately — they already logged the terminal warning.
                if self._stop_event.is_set():
                    terminal_failure = True
                    break
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self._stop_event.set()
                    terminal_failure = True
                    logger.error(
                        "DependencyLatencyTracker: %d consecutive failures — "
                        "writer thread terminating",
                        consecutive_failures,
                    )
                    break

        # Final flush on normal shutdown only — skip when a terminal failure ended
        # the loop to avoid triggering additional log noise on a broken backend.
        if not terminal_failure:
            try:
                self._flush_and_prune()
            except Exception as exc:
                logger.warning(
                    "DependencyLatencyTracker: final flush on shutdown failed: %s", exc
                )

    def _flush_and_prune(self) -> None:
        """Drain the buffer into storage and delete samples outside the retention window."""
        self._flush_buffer()
        self._prune_stale()

    def _flush_buffer(self) -> None:
        """Drain all samples currently in the buffer into the storage backend.

        Raises:
            sqlite3.ProgrammingError: re-raised when the database is closed so
                the caller (_writer_loop) can treat it as a terminal condition.
        """
        with self._buffer_lock:
            if not self._buffer:
                return
            samples: List = list(self._buffer)
            self._buffer.clear()

        try:
            self._backend.insert_batch(samples)
        except sqlite3.ProgrammingError as exc:
            if _CLOSED_DB_SUBSTRING in str(exc).lower():
                self._stop_event.set()
                logger.warning(
                    "DependencyLatencyTracker: database closed — writer thread terminating"
                )
            raise
        except Exception:
            raise

    def _prune_stale(self) -> None:
        """Delete samples older than retention_s from the storage backend.

        Raises:
            sqlite3.ProgrammingError: re-raised when the database is closed so
                the caller (_writer_loop) can treat it as a terminal condition.
        """
        cutoff = time.time() - self._retention_s
        try:
            self._backend.delete_older_than(cutoff)
        except sqlite3.ProgrammingError as exc:
            if _CLOSED_DB_SUBSTRING in str(exc).lower():
                self._stop_event.set()
                logger.warning(
                    "DependencyLatencyTracker: database closed — writer thread terminating"
                )
            raise
        except Exception:
            raise
