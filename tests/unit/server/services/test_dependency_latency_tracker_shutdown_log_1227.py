"""
Tests for Bug #1227: DependencyLatencyTracker closed-db branch should log at DEBUG
not WARNING during graceful shutdown teardown.

Covers:
1. _flush_buffer closed-db ProgrammingError: zero WARNINGs, DEBUG logged with expected
   fragment, stop_event set, exception re-raised.
2. _flush_buffer non-closed-db ProgrammingError: closed-db DEBUG NOT emitted, stop_event
   NOT set, exception still propagates.
3. Writer loop consecutive-failures ERROR path fires for persistent non-closed-db errors
   (regression guard: the non-closed-db path is unaffected by the WARNING->DEBUG change).
"""

import logging
import sqlite3
import threading
import time
from typing import List

import pytest

# ── Module logger name (must match the source module) ─────────────────────────
_TRACKER_LOGGER_NAME = "code_indexer.server.services.dependency_latency_tracker"

# ── Timing constants ───────────────────────────────────────────────────────────
FAST_FLUSH_INTERVAL_S = 0.05
STANDARD_RETENTION_S = 300.0
STOP_EVENT_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.05

# ── Error messages ─────────────────────────────────────────────────────────────
CLOSED_DB_PROGRAMMINGERROR_MSG = "Cannot operate on a closed database"
NON_CLOSED_DB_PROGRAMMINGERROR_MSG = "syntax error in SQL"

# ── Expected debug message fragment (mirrors source constant) ──────────────────
EXPECTED_DEBUG_FRAGMENT = "database closed"


# ── Backend stubs ──────────────────────────────────────────────────────────────


class _ClosedDbInsertBackend:
    """Backend stub: insert_batch raises sqlite3.ProgrammingError(closed database)."""

    def __init__(self) -> None:
        self.insert_called = False

    def insert_batch(self, samples) -> None:
        self.insert_called = True
        raise sqlite3.ProgrammingError(CLOSED_DB_PROGRAMMINGERROR_MSG)

    def delete_older_than(self, cutoff) -> None:
        pass


class _NonClosedDbInsertBackend:
    """Backend stub: both insert_batch and delete_older_than raise non-closed-db errors.

    Both methods must raise so every writer-loop iteration fails and the
    consecutive-failures counter reaches _MAX_CONSECUTIVE_FAILURES.
    """

    def __init__(self) -> None:
        self.insert_called = False

    def insert_batch(self, samples) -> None:
        self.insert_called = True
        raise sqlite3.ProgrammingError(NON_CLOSED_DB_PROGRAMMINGERROR_MSG)

    def delete_older_than(self, cutoff) -> None:
        raise sqlite3.ProgrammingError(NON_CLOSED_DB_PROGRAMMINGERROR_MSG)


# ── Log capture helper ─────────────────────────────────────────────────────────


class _MultiLevelCapture:
    """
    Captures log records at specified levels from the tracker logger.

    Usage::

        with _MultiLevelCapture([logging.WARNING, logging.DEBUG]) as cap:
            ...
        cap.records_at(logging.WARNING)  # list of message strings
        cap.records_at(logging.DEBUG)
    """

    def __init__(self, levels: List[int]) -> None:
        self._levels = set(levels)
        self._captured: dict = {lvl: [] for lvl in levels}
        self._logger = logging.getLogger(_TRACKER_LOGGER_NAME)
        self._original_level = self._logger.level
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno in capture._levels:
                    capture._captured[record.levelno].append(record.getMessage())

        self._handler = _Handler()

    def records_at(self, level: int) -> List[str]:
        return list(self._captured.get(level, []))

    def __enter__(self) -> "_MultiLevelCapture":
        # Set logger level low enough to capture the lowest requested level
        min_level = min(self._levels)
        effective = (
            min(min_level, self._logger.level) if self._logger.level > 0 else min_level
        )
        self._logger.setLevel(effective)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *_) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._original_level)


# ── Tracker factory ────────────────────────────────────────────────────────────


def _make_tracker(backend):
    """Instantiate DependencyLatencyTracker (NOT started)."""
    from code_indexer.server.services.dependency_latency_tracker import (
        DependencyLatencyTracker,
    )

    return DependencyLatencyTracker(
        backend=backend,
        flush_interval_s=FAST_FLUSH_INTERVAL_S,
        retention_s=STANDARD_RETENTION_S,
    )


def _wait_for_stop_event(stop_event: threading.Event, timeout_s: float) -> bool:
    """Poll until stop_event is set or timeout elapses. Returns True if set."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return True
        time.sleep(POLL_INTERVAL_S)
    return stop_event.is_set()


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestFlushBufferClosedDbShutdownLog1227:
    """
    Bug #1227: _flush_buffer closed-db branch must log at DEBUG (not WARNING).

    Three test methods covering:
    1. closed-db: zero WARNINGs + DEBUG emitted + stop_event set + exception re-raised
    2. non-closed-db: closed-db DEBUG NOT emitted + stop_event NOT set
    3. writer loop consecutive-failures ERROR path unaffected (regression guard)
    """

    def test_closed_db_logs_debug_not_warning_and_terminates_cleanly(self) -> None:
        """
        When insert_batch raises ProgrammingError('closed database'):
        - ZERO WARNING records logged (the WARNING->DEBUG fix)
        - At least one DEBUG record containing EXPECTED_DEBUG_FRAGMENT
        - _stop_event is set (clean termination preserved)
        - sqlite3.ProgrammingError is re-raised (so writer loop exits correctly)
        """
        backend = _ClosedDbInsertBackend()
        tracker = _make_tracker(backend)

        # Seed the buffer so _flush_buffer has samples to flush
        tracker.record_sample("test_dep", 42.0, 200)

        assert not tracker._stop_event.is_set(), (
            "_stop_event must not be pre-set before _flush_buffer call"
        )

        with _MultiLevelCapture([logging.WARNING, logging.DEBUG]) as cap:
            with pytest.raises(sqlite3.ProgrammingError):
                tracker._flush_buffer()

        warnings = cap.records_at(logging.WARNING)
        debugs = cap.records_at(logging.DEBUG)

        assert warnings == [], (
            f"Expected ZERO WARNING records for closed-db shutdown; got: {warnings}"
        )
        assert any(EXPECTED_DEBUG_FRAGMENT in msg for msg in debugs), (
            f"Expected a DEBUG record containing '{EXPECTED_DEBUG_FRAGMENT}'; "
            f"got DEBUG records: {debugs}"
        )
        assert tracker._stop_event.is_set(), (
            "_stop_event must be set after closed-db ProgrammingError"
        )

    def test_non_closed_db_error_does_not_emit_closed_db_debug_log(self) -> None:
        """
        A non-closed-db ProgrammingError must NOT trigger the closed-db DEBUG log
        and must NOT set _stop_event (it should propagate to the consecutive-failures
        path instead).
        """
        backend = _NonClosedDbInsertBackend()
        tracker = _make_tracker(backend)

        tracker.record_sample("test_dep", 42.0, 200)

        with _MultiLevelCapture([logging.DEBUG, logging.WARNING]) as cap:
            with pytest.raises(sqlite3.ProgrammingError):
                tracker._flush_buffer()

        debug_msgs = cap.records_at(logging.DEBUG)
        assert not any(EXPECTED_DEBUG_FRAGMENT in msg for msg in debug_msgs), (
            f"Non-closed-db error must NOT emit the shutdown DEBUG log; "
            f"got DEBUG records: {debug_msgs}"
        )
        assert not tracker._stop_event.is_set(), (
            "_stop_event must NOT be set for a non-closed-db ProgrammingError"
        )

    def test_non_closed_db_error_triggers_consecutive_failures_error_path(
        self,
    ) -> None:
        """
        Regression guard: a persistent non-closed-db backend exception must propagate
        through the writer loop and fire exactly ONE ERROR log via the
        consecutive-failures path — confirming that path is unaffected by the fix.

        The capture context wraps tracker.start() so no ERROR log is missed if the
        writer loop fires and terminates before the wait loop checks.
        """
        backend = _NonClosedDbInsertBackend()
        tracker = _make_tracker(backend)

        with _MultiLevelCapture([logging.ERROR]) as cap:
            tracker.start()
            try:
                tracker.record_sample("test_dep", 42.0, 200)
                terminated = _wait_for_stop_event(
                    tracker._stop_event, STOP_EVENT_TIMEOUT_S
                )
            finally:
                tracker._stop_event.set()
                if tracker._writer_thread is not None:
                    tracker._writer_thread.join(timeout=2.0)

        assert terminated, (
            "stop_event must be set after max consecutive non-closed-db failures"
        )
        error_msgs = cap.records_at(logging.ERROR)
        assert len(error_msgs) == 1, (
            f"Expected exactly 1 ERROR log from consecutive-failures path; "
            f"got: {error_msgs}"
        )
