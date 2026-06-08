"""
Tests for Bug #1078 fix: SQLiteLogHandler async writer thread.

Root cause: SQLiteLogHandler.emit() did a synchronous blocking DB write WHILE
holding the Python logging handler lock.  Under concurrent server load all
request threads stalled on logging.Handler.acquire().

Fix: emit() only extracts fields and enqueues them (fast, CPU-only).  A
dedicated daemon writer thread drains the queue and performs the actual DB
write.  This means the logging handler lock is released long before any I/O.

Tests verify:
1. emit() returns immediately on the CALLING thread — the DB write happens on
   the writer thread, NOT the caller.
2. queue.Full drops the record and bumps self._dropped — no block, no raise.
3. close() flushes remaining queued records and stops the writer thread.
4. Records emitted by the writer thread itself do NOT re-enqueue (guard works).
5. Existing DB-row semantics are preserved: same columns, same content.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TrackingLogsBackend:
    """Logs backend that records which thread performed the insert_log() call
    and optionally blocks for a brief period so the caller-thread check is
    reliable.
    """

    def __init__(self, block_seconds: float = 0.0) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.call_thread_names: List[str] = []
        self._block_seconds = block_seconds
        self._lock = threading.Lock()

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: str,
        message: str,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
        alias: Optional[str] = None,
    ) -> None:
        if self._block_seconds > 0:
            time.sleep(self._block_seconds)
        with self._lock:
            self.call_thread_names.append(threading.current_thread().name)
            self.calls.append(
                {
                    "timestamp": timestamp,
                    "level": level,
                    "source": source,
                    "message": message,
                    "correlation_id": correlation_id,
                    "user_id": user_id,
                    "request_path": request_path,
                    "extra_data": extra_data,
                    "node_id": node_id,
                    "alias": alias,
                }
            )

    def query_logs(self, *args: Any, **kwargs: Any):  # pragma: no cover
        return [], 0

    def cleanup_old_logs(self, days_to_keep: int) -> int:  # pragma: no cover
        return 0

    def close(self) -> None:  # pragma: no cover
        return None


def _make_record(
    msg: str = "test message", level: int = logging.INFO
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.async",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def _build_handler(tmp_path: Path, backend: Any) -> Any:
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    return SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)


# ---------------------------------------------------------------------------
# Test 1: emit() returns on calling thread; DB write happens on writer thread
# ---------------------------------------------------------------------------


def test_emit_returns_on_caller_thread_db_write_on_writer_thread(
    tmp_path: Path,
) -> None:
    """
    Core regression assertion for Bug #1078:
    After emit() returns, the record is enqueued and the insert_log() call
    must run on the writer thread — NOT on the caller thread.

    Method: inject a backend that blocks for 50 ms per insert and records
    the calling thread name.  Call emit() from the main thread and measure
    that it returns in well under 50 ms (i.e. it did NOT block on the DB).
    """
    backend = _TrackingLogsBackend(block_seconds=0.05)  # 50 ms DB write
    handler = _build_handler(tmp_path, backend)
    try:
        record = _make_record("Bug1078 regression record")
        caller_thread = threading.current_thread().name

        t_start = time.monotonic()
        handler.emit(record)
        elapsed_ms = (time.monotonic() - t_start) * 1000

        # emit() must return well before the 50 ms DB write completes
        assert elapsed_ms < 30, (
            f"emit() took {elapsed_ms:.1f} ms — it is blocking on the DB "
            f"(Bug #1078). Expected <30 ms (non-blocking enqueue)."
        )

        # Wait for the writer thread to actually perform the insert
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with backend._lock:
                if backend.calls:
                    break
            time.sleep(0.01)

        with backend._lock:
            assert len(backend.calls) == 1, (
                "Record was not written by the writer thread within 2 seconds."
            )
            writer_thread = backend.call_thread_names[0]

        assert writer_thread != caller_thread, (
            f"insert_log() ran on the CALLING thread '{caller_thread}' — "
            "emit() is still blocking on the DB (Bug #1078). "
            "The write must happen on a separate writer thread."
        )
        assert "writer" in writer_thread.lower() or "log" in writer_thread.lower(), (
            f"Writer thread name '{writer_thread}' does not look like a dedicated "
            "writer thread (expected 'writer' or 'log' in the name)."
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 2: queue.Full drops record and bumps _dropped counter
# ---------------------------------------------------------------------------


def test_queue_full_drops_record_and_bumps_dropped_counter(tmp_path: Path) -> None:
    """
    When the queue is full, emit() must drop the record silently (no block,
    no raise) and increment self._dropped.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    # Create handler, then clog the queue so put_nowait always raises Full
    backend = _TrackingLogsBackend()
    handler = SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)
    try:
        # Replace the queue with a size-1 queue that is already full
        blocker: queue.Queue = queue.Queue(maxsize=1)
        blocker.put_nowait(("sentinel",))  # fill it
        handler._queue = blocker  # type: ignore[attr-defined]

        initial_dropped = getattr(handler, "_dropped", 0)

        # This emit should hit queue.Full — must not raise, must not block
        t_start = time.monotonic()
        handler.emit(_make_record("should be dropped"))
        elapsed_ms = (time.monotonic() - t_start) * 1000

        assert elapsed_ms < 100, (
            f"emit() took {elapsed_ms:.1f} ms on a full queue — it appears to be "
            "blocking instead of dropping."
        )

        dropped_after = getattr(handler, "_dropped", None)
        assert dropped_after is not None, (
            "SQLiteLogHandler must have a _dropped counter attribute."
        )
        assert dropped_after == initial_dropped + 1, (
            f"_dropped counter not incremented on queue.Full: "
            f"before={initial_dropped}, after={dropped_after}"
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 2b: ERROR/CRITICAL records survive a full queue; DEBUG/INFO/WARNING drop
# ---------------------------------------------------------------------------


def test_error_record_not_dropped_on_full_queue_but_debug_is(
    tmp_path: Path,
) -> None:
    """
    Severity-protection (anti-silent-failure):
    - When queue is full, DEBUG/INFO/WARNING records are dropped immediately
      (put_nowait) and _dropped is incremented.
    - ERROR and CRITICAL records must NOT be dropped: a bounded blocking
      put(timeout=...) is used; if it drains in time, the record is written.

    Method: use a size-1 queue (already full with sentinel), then drain it
    quickly so the ERROR's bounded put can succeed.  Assert:
    1. DEBUG emit increments _dropped.
    2. ERROR emit does NOT increment _dropped and the record is eventually written.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    backend = _TrackingLogsBackend()
    handler = SQLiteLogHandler(db_path=tmp_path / "unused.db", logs_backend=backend)
    try:
        # Pause the writer thread so the queue stays full during our test window
        drain_allowed = threading.Event()
        original_get = handler._queue.get

        def blocking_get(*args, **kwargs):
            drain_allowed.wait()  # block writer until we allow it
            return original_get(*args, **kwargs)

        handler._queue.get = blocking_get  # type: ignore[method-assign]

        # Fill the queue until put_nowait actually rejects. A fixed-count loop is
        # racy: the writer thread can consume one in-flight item before it blocks
        # on the patched get(), freeing a slot. Filling until queue.Full
        # guarantees the queue is at capacity at the DEBUG emit below.
        filler = ("ts", "INFO", "src", "fill", None, None, None, None, None)
        while True:
            try:
                handler._queue.put_nowait(filler)
            except queue.Full:
                break

        # DEBUG into full queue — must drop immediately, never block
        debug_record = _make_record("should be dropped", level=logging.DEBUG)
        initial_dropped = handler._dropped
        t0 = time.monotonic()
        handler.emit(debug_record)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"DEBUG emit blocked {elapsed:.2f}s on full queue"
        assert handler._dropped == initial_dropped + 1, (
            "DEBUG record on full queue must increment _dropped"
        )

        # Now allow the writer to drain (unblock it), then emit ERROR
        # The ERROR's bounded put should succeed once the queue has room
        drain_allowed.set()
        handler._queue.get = original_get  # type: ignore[method-assign]

        # Drain the fill items so there's room for the ERROR record
        deadline = time.monotonic() + 3.0
        while handler._queue.qsize() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)

        error_record = _make_record("error must survive", level=logging.ERROR)
        dropped_before_error = handler._dropped
        handler.emit(error_record)

        # Wait for the writer to actually process the ERROR record
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with backend._lock:
                found = any("error must survive" in c["message"] for c in backend.calls)
            if found:
                break
            time.sleep(0.02)

        with backend._lock:
            assert any("error must survive" in c["message"] for c in backend.calls), (
                "ERROR record was dropped on full queue — severity protection failed"
            )
        assert handler._dropped == dropped_before_error, (
            "ERROR record must not increment _dropped counter"
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 3: close() flushes remaining records and stops writer thread
# ---------------------------------------------------------------------------


def test_close_flushes_remaining_records_and_stops_writer(tmp_path: Path) -> None:
    """
    close() must drain all queued records and join the writer thread.
    Records enqueued before close() must appear in the backend.
    """
    backend = _TrackingLogsBackend()
    handler = _build_handler(tmp_path, backend)

    # Emit several records without waiting for them to be drained
    for i in range(5):
        handler.emit(_make_record(f"flush test record {i}"))

    # close() must block until the queue is empty and writer is done
    handler.close()

    assert len(backend.calls) == 5, (
        f"close() did not flush all queued records: "
        f"expected 5, got {len(backend.calls)}"
    )
    messages = {c["message"] for c in backend.calls}
    for i in range(5):
        assert f"flush test record {i}" in messages, (
            f"Record 'flush test record {i}' was not written after close()."
        )


# ---------------------------------------------------------------------------
# Test 4: writer-thread guard prevents re-enqueueing recursion
# ---------------------------------------------------------------------------


def test_writer_thread_guard_prevents_recursive_requeue(tmp_path: Path) -> None:
    """
    While the writer thread is executing insert_log(), any logging that
    insert_log() itself triggers (e.g. from DatabaseConnectionManager or
    backend internals) must NOT be re-enqueued.  The writer sets its own
    _emit_guard.active = True to drop such records cleanly.

    Method: use a backend whose insert_log() itself calls emit() on the
    same handler.  Verify the recursive call is dropped (queue size stays 0
    after the recursive emit) and does not cause infinite growth.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    outer_calls: List[str] = []
    recursive_attempted = threading.Event()
    handler_holder: List[Any] = []

    class _RecursiveBackend:
        def insert_log(self, *, message: str, **kwargs: Any) -> None:
            outer_calls.append(message)
            if not recursive_attempted.is_set():
                recursive_attempted.set()
                # This fires from inside the writer thread — should be dropped
                rec = _make_record("RECURSIVE — must be dropped")
                handler_holder[0].emit(rec)

        def query_logs(self, *a: Any, **kw: Any):  # pragma: no cover
            return [], 0

        def cleanup_old_logs(self, days_to_keep: int) -> int:  # pragma: no cover
            return 0

        def close(self) -> None:  # pragma: no cover
            return None

    handler = SQLiteLogHandler(
        db_path=tmp_path / "recursive.db", logs_backend=_RecursiveBackend()
    )
    handler_holder.append(handler)
    try:
        handler.emit(_make_record("outer record"))

        # Wait for writer to process the outer record
        assert recursive_attempted.wait(timeout=2.0), (
            "Backend insert_log() was never called — writer thread stalled."
        )

        # Give a brief window for any recursive enqueue to propagate
        time.sleep(0.1)

        # Only the outer record must have been inserted, not the recursive one
        assert outer_calls == ["outer record"], (
            f"Recursive emit was re-enqueued and inserted: outer_calls={outer_calls}. "
            "The writer-thread guard (_emit_guard.active = True) must prevent "
            "recursive records from being added to the queue."
        )
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# Test 5: existing DB-row semantics preserved (columns identical)
# ---------------------------------------------------------------------------


def test_direct_sqlite_path_columns_preserved_async(tmp_path: Path) -> None:
    """
    With the async writer, the direct-SQLite path (no backend) must still
    write rows with the same columns and content as before.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    db_path = tmp_path / "logs.db"
    handler = SQLiteLogHandler(db_path=db_path)  # no backend → direct-SQLite
    try:
        record = _make_record("async direct sqlite content check")
        setattr(record, "correlation_id", "corr-123")
        setattr(record, "user_id", "user-456")
        setattr(record, "alias", "my-repo")

        handler.emit(record)
        # close() flushes
        handler.close()

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT level, source, message, correlation_id, user_id, alias "
                "FROM logs"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        level, source, message, corr_id, user_id, alias = rows[0]
        assert level == "INFO"
        assert source == "test.async"
        assert "async direct sqlite content check" in message
        assert corr_id == "corr-123"
        assert user_id == "user-456"
        assert alias == "my-repo"
    except Exception:
        # handler already closed above; re-raise for test failure reporting
        raise
