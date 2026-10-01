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
from typing import Any, Dict, Generator, List, Optional

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

# Termination reasons recorded in get_health_status() (Bug #1541).
_TERMINATION_REASON_CLOSED_DATABASE = "closed_database"
_TERMINATION_REASON_MAX_CONSECUTIVE_FAILURES = "max_consecutive_failures"

# Number of consecutive flush/prune failures (any error type OTHER than a
# transient SQLite lock/busy contention -- see _TRANSIENT_LOCK_SUBSTRINGS
# below) before the writer thread gives up and terminates — prevents infinite
# loops on a genuinely broken backend. Bug #1541: this threshold is preserved
# unchanged for genuinely unrecoverable errors; it is no longer reachable via
# transient lock contention, which is handled separately and never counts
# toward it.
_MAX_CONSECUTIVE_FAILURES = 5

# Substrings present in sqlite3.OperationalError when a write is blocked by
# another connection's transaction (SQLITE_BUSY) -- a transient, self-healing
# condition. Bug #1541: every other scheduler sharing this SQLite backend
# survives an identical burst of these errors by logging and continuing;
# DependencyLatencyTracker must do the same instead of counting them toward
# _MAX_CONSECUTIVE_FAILURES. Note: the underlying connection already sets
# `PRAGMA busy_timeout = 30000` (DatabaseConnectionManager.get_connection),
# so this error still only surfaces after sustained contention beyond that
# 30s window -- retrying here is defence in depth on top of that existing
# mitigation, not a replacement for it.
_TRANSIENT_LOCK_SUBSTRINGS = ("database is locked", "database is busy")

# Cap on the writer loop's retry backoff (seconds) once a transient lock
# failure occurs. Bounded exponential backoff -- not unbounded retry -- so a
# persistently contended database cannot make the loop hammer SQLite at full
# speed indefinitely.
_MAX_BACKOFF_S = 60.0

# Cap on the backoff exponent itself: defensive against unbounded big-int
# growth over a very long streak of consecutive transient failures. The
# _MAX_BACKOFF_S cap above already bounds the resulting wait time long before
# this exponent would be reached for any realistic flush_interval_s.
_BACKOFF_EXPONENT_CAP = 10


def _is_transient_lock_error(exc: BaseException) -> bool:
    """
    Return True if exc is a transient SQLite busy/locked contention error.

    Only sqlite3.OperationalError whose message contains one of
    _TRANSIENT_LOCK_SUBSTRINGS qualifies -- this is a self-healing condition
    (Bug #1541) and must never count toward _MAX_CONSECUTIVE_FAILURES. Any
    other exception (including other OperationalError messages, and the
    closed-database sqlite3.ProgrammingError handled separately) is NOT
    transient by this definition.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(substring in message for substring in _TRANSIENT_LOCK_SUBSTRINGS)


# Multiplier applied per consecutive transient lock failure when computing
# the backoff wait (Bug #1541) -- named to avoid a magic "2" in the formula.
_BACKOFF_MULTIPLIER = 2


def _compute_backoff_wait_s(consecutive_failures: int, base_interval_s: float) -> float:
    """
    Bounded exponential backoff wait, in seconds (Bug #1541).

    Doubles (via _BACKOFF_MULTIPLIER) per consecutive transient lock
    failure starting from ``base_interval_s``, capped at ``_MAX_BACKOFF_S``
    so sustained contention cannot make the writer loop hammer SQLite at
    full speed indefinitely. The exponent itself is capped at
    _BACKOFF_EXPONENT_CAP, defensive against unbounded big-int growth over
    a very long failure streak (_MAX_BACKOFF_S already bounds the result
    long before that exponent would be reached for any realistic
    base_interval_s).

    Raises:
        ValueError: if consecutive_failures < 1 or base_interval_s <= 0.
    """
    if consecutive_failures < 1:
        raise ValueError(
            f"consecutive_failures must be >= 1, got {consecutive_failures}"
        )
    if base_interval_s <= 0:
        raise ValueError(f"base_interval_s must be > 0, got {base_interval_s}")
    exponent = min(consecutive_failures - 1, _BACKOFF_EXPONENT_CAP)
    multiplier = float(_BACKOFF_MULTIPLIER**exponent)
    return min(base_interval_s * multiplier, _MAX_BACKOFF_S)


def _validate_positive_float(value: Any, name: str) -> None:
    """Raise ValueError if value is not a positive (non-bool) float or int."""
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(
            f"{name} must be a positive number, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")


class _WriterHealthState:
    """
    Thread-safe health/observability state for the writer thread (Bug #1541).

    Mutated by the writer thread on every flush attempt; read by
    ``get_health_status()`` from any thread (e.g. a health check or a test).
    A single internal lock keeps every read internally consistent without
    requiring the caller to coordinate with the writer thread's own
    execution.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_flush_attempts = 0
        self._consecutive_transient_lock_failures = 0
        self._last_error_message: Optional[str] = None
        self._last_error_at: Optional[float] = None
        self._terminated = False
        self._termination_reason: Optional[str] = None

    def record_attempt(self) -> None:
        with self._lock:
            self._total_flush_attempts += 1

    def record_transient_lock_failure(self, exc: BaseException) -> int:
        """Record a transient lock failure; return the new consecutive count."""
        with self._lock:
            self._consecutive_transient_lock_failures += 1
            self._last_error_message = str(exc)
            self._last_error_at = time.time()
            return self._consecutive_transient_lock_failures

    def record_terminal(self, reason: str) -> None:
        """Record a genuinely terminal condition (Bug #1541: must be observable)."""
        with self._lock:
            self._terminated = True
            self._termination_reason = reason


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
        self._health = _WriterHealthState()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_health_status(self) -> Dict[str, Any]:
        """
        Return a snapshot of the writer thread's health for diagnostics.

        Bug #1541: once a terminal condition stops the writer thread,
        samples silently stop being persisted unless this state is
        observable somewhere. This method is that observation point.
        """
        thread = self._writer_thread
        alive = thread is not None and thread.is_alive()
        with self._health._lock:
            return {
                "alive": alive,
                "terminated": self._health._terminated,
                "termination_reason": self._health._termination_reason,
                "total_flush_attempts": self._health._total_flush_attempts,
                "consecutive_transient_lock_failures": (
                    self._health._consecutive_transient_lock_failures
                ),
                "last_error_message": self._health._last_error_message,
                "last_error_at": self._health._last_error_at,
            }

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
           genuinely unrecoverable (non-lock) exceptions, the loop sets
           ``_stop_event`` and logs exactly ONE error, then breaks. Final flush is
           skipped.

        Bug #1541: a transient SQLite lock/busy contention error (see
        ``_is_transient_lock_error``) NEVER counts toward path 3's threshold.
        It is retried instead, with a capped exponential backoff
        (``_compute_backoff_wait_s``) so sustained contention cannot make
        this loop hammer SQLite at full speed indefinitely.

        A successful iteration resets the consecutive-failures counter and
        the backoff wait to their base values.
        """
        consecutive_failures = 0
        terminal_failure = False
        wait_s = self._flush_interval_s
        # Daemon-service pattern: loop is bounded by stop_event (set by shutdown).
        # Event.wait(timeout) ensures the thread wakes at most once per wait_s
        # and exits immediately when the event fires — termination is event-driven.
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=wait_s)
            if self._stop_event.is_set():
                break
            self._health.record_attempt()
            try:
                self._flush_and_prune()
                consecutive_failures = 0
                wait_s = self._flush_interval_s
            except Exception as exc:
                # If _flush_buffer/_prune_stale already set _stop_event (closed-db),
                # exit immediately — they already logged the terminal warning.
                if self._stop_event.is_set():
                    terminal_failure = True
                    break
                if _is_transient_lock_error(exc):
                    count = self._health.record_transient_lock_failure(exc)
                    logger.warning(
                        "DependencyLatencyTracker: transient SQLite lock "
                        "contention (%d consecutive) -- retrying: %s",
                        count,
                        exc,
                    )
                    wait_s = _compute_backoff_wait_s(count, self._flush_interval_s)
                    continue
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

    def _restore_unflushed_samples(self, samples: List) -> None:
        """
        Put samples pulled out of the buffer back after a failed insert.

        Bug #1541: samples drained for a flush attempt were previously
        discarded regardless of whether the insert succeeded, so a
        transient failure silently lost them even though the writer thread
        now survives and retries. Restoring them at the front (their
        original position) lets the next successful attempt persist them.
        Bounded by the buffer's existing maxlen: on the rare double-overflow
        case (more new samples arrive than capacity allows while
        restoring), the newest arrivals are evicted first via
        deque.extendleft's natural semantics -- an accepted edge case
        rather than a second overflow policy to maintain.
        """
        if not samples:
            return
        with self._buffer_lock:
            self._buffer.extendleft(reversed(samples))

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
                self._health.record_terminal(_TERMINATION_REASON_CLOSED_DATABASE)
                # Bug #1227: DEBUG, not WARNING/ERROR -- this fires on every
                # routine graceful shutdown teardown (the shared connection
                # manager is closed independently of tracker.shutdown()), so
                # logging it loudly would be noise on the expected path.
                # Bug #1541: the terminal state is still observable via
                # get_health_status() (record_terminal above), a queryable
                # channel deliberately independent of log verbosity.
                logger.debug(
                    "DependencyLatencyTracker: database closed — writer thread terminating"
                )
            self._restore_unflushed_samples(samples)
            raise
        except Exception:
            self._restore_unflushed_samples(samples)
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
                self._health.record_terminal(_TERMINATION_REASON_CLOSED_DATABASE)
                # Bug #1227: DEBUG, not WARNING/ERROR -- this fires on every
                # routine graceful shutdown teardown (the shared connection
                # manager is closed independently of tracker.shutdown()), so
                # logging it loudly would be noise on the expected path.
                # Bug #1541: the terminal state is still observable via
                # get_health_status() (record_terminal above), a queryable
                # channel deliberately independent of log verbosity.
                logger.debug(
                    "DependencyLatencyTracker: database closed — writer thread terminating"
                )
            raise
        except Exception:
            raise
