"""Tests for async server logging via QueueHandler/QueueListener.

Performance follow-up to Bug #1078 / py-spy profiling: under concurrent
/api/query load the dominant active leaf frame across worker threads was
``acquire (logging/__init__.py:901)`` -- the per-Handler lock held while the
synchronous console StreamHandler formatted + wrote + flushed under its lock.

Fix: route the root logger through a single ``QueueHandler`` whose
``QueueListener`` owns the REAL handlers (console StreamHandler, SQLiteLogHandler,
telemetry handler). On a request thread, ``logger.info()`` does at most one fast
lock acquire + one ``queue.put`` -- all formatting and handler I/O happen on the
single listener thread, off the hot path.

These tests verify:
1. The custom QueueHandler.prepare() is an identity no-op (does NOT format the
   message or null out args) -- formatting must happen on the listener side.
2. install_queue_logging() routes the root logger through ONE QueueHandler and
   moves the previously-attached real handlers behind the listener.
3. A logged record is delivered to the underlying real handlers via the listener
   (after a flush()/drain()).
4. emit() on the hot path only enqueues -- it does NOT perform synchronous
   handler I/O on the caller thread (a blocking real handler does not block the
   caller).
5. A saturated queue drops low-severity records (and counts them) but blocks
   briefly for ERROR/CRITICAL (mirrors SQLiteLogHandler's discipline), and
   surfaces a drop count.
6. stop() drains the queue and flushes the listener so no records are lost on
   clean shutdown.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import List


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    """Real handler that records the records it receives and the thread name."""

    def __init__(self, block_seconds: float = 0.0) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []
        self.thread_names: List[str] = []
        self._block_seconds = block_seconds
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if self._block_seconds > 0:
            time.sleep(self._block_seconds)
        with self._lock:
            self.records.append(record)
            self.thread_names.append(threading.current_thread().name)


def _make_record(msg: str = "msg", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.async_queue",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# Test 1: identity prepare() does NOT format/copy the record
# ---------------------------------------------------------------------------


def test_identity_prepare_returns_record_unchanged() -> None:
    from code_indexer.server.services.async_logging import IdentityQueueHandler

    q: queue.Queue = queue.Queue()
    handler = IdentityQueueHandler(q)
    record = _make_record("hello %s", level=logging.INFO)
    record.args = ("world",)

    prepared = handler.prepare(record)

    # Identity: same object, args NOT nulled, message NOT pre-formatted.
    assert prepared is record, "prepare() must return the same record object"
    assert prepared.args == ("world",), "prepare() must NOT null record.args"
    # The default QueueHandler.prepare sets record.message; identity must not.
    assert getattr(prepared, "message", None) is None, (
        "prepare() must NOT pre-format record.message (formatting belongs on the "
        "listener thread)"
    )


# ---------------------------------------------------------------------------
# Test 2: install routes the root logger through a single QueueHandler
# ---------------------------------------------------------------------------


def test_install_routes_root_through_queue_handler() -> None:
    from code_indexer.server.services.async_logging import (
        IdentityQueueHandler,
        install_queue_logging,
    )

    root = logging.getLogger()
    saved = list(root.handlers)
    real = _RecordingHandler()
    try:
        root.handlers = [real]
        listener = install_queue_logging([real])
        try:
            # Root must now have exactly one handler: the IdentityQueueHandler.
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0], IdentityQueueHandler)
            # The real handler must be owned by the listener, not the root.
            assert real not in root.handlers
            assert real in listener.handlers
        finally:
            listener.stop()
    finally:
        root.handlers = saved


# ---------------------------------------------------------------------------
# Test 3: record delivered to real handler via the listener after flush
# ---------------------------------------------------------------------------


def test_record_delivered_to_real_handler_via_listener_after_flush() -> None:
    from code_indexer.server.services.async_logging import install_queue_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    real = _RecordingHandler()
    try:
        root.handlers = [real]
        root.setLevel(logging.INFO)
        listener = install_queue_logging([real])
        try:
            logging.getLogger("test.async_queue").info("delivered via listener")
            listener.flush()  # synchronous drain for deterministic assertion
            messages = [r.getMessage() for r in real.records]
            assert "delivered via listener" in messages, (
                f"record not delivered to real handler via listener: {messages}"
            )
        finally:
            listener.stop()
    finally:
        root.handlers = saved
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Test 4: hot path only enqueues -- no synchronous handler I/O on caller thread
# ---------------------------------------------------------------------------


def test_emit_does_not_perform_sync_handler_io_on_caller_thread() -> None:
    from code_indexer.server.services.async_logging import install_queue_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    # Real handler blocks 80 ms per emit -- if the caller blocks, it is doing
    # synchronous handler I/O on the hot path (the exact regression).
    real = _RecordingHandler(block_seconds=0.08)
    try:
        root.handlers = [real]
        root.setLevel(logging.INFO)
        listener = install_queue_logging([real])
        try:
            caller_thread = threading.current_thread().name
            t0 = time.monotonic()
            logging.getLogger("test.async_queue").info("non-blocking enqueue")
            elapsed_ms = (time.monotonic() - t0) * 1000
            assert elapsed_ms < 40, (
                f"logging took {elapsed_ms:.1f} ms on the caller thread -- the "
                "hot path is blocking on handler I/O instead of enqueuing."
            )

            listener.flush()
            with real._lock:
                assert real.records, "record never reached the real handler"
                handler_thread = real.thread_names[0]
            assert handler_thread != caller_thread, (
                f"real handler ran on the CALLING thread '{caller_thread}' -- "
                "handler I/O must happen on the listener thread."
            )
        finally:
            listener.stop()
    finally:
        root.handlers = saved
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Test 5: saturated queue drops low-severity but not ERROR; surfaces count
# ---------------------------------------------------------------------------


def test_queue_full_drops_low_severity_but_not_error_and_counts() -> None:
    from code_indexer.server.services.async_logging import IdentityQueueHandler

    # Size-1 queue, pre-filled so put_nowait always raises Full.
    q: queue.Queue = queue.Queue(maxsize=1)
    q.put_nowait(_make_record("sentinel"))
    handler = IdentityQueueHandler(q)

    assert handler.dropped_count == 0

    # INFO into full queue -> dropped, counter bumped, no block, no raise.
    t0 = time.monotonic()
    handler.emit(_make_record("drop me", level=logging.INFO))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"INFO emit blocked {elapsed:.2f}s on full queue"
    assert handler.dropped_count == 1, "INFO drop must bump dropped_count"

    # ERROR into full queue: drain the queue from another thread shortly after,
    # so the bounded blocking put can succeed (ERROR must NOT be dropped).
    def _drain_soon() -> None:
        time.sleep(0.05)
        try:
            q.get_nowait()
        except queue.Empty:
            pass

    drainer = threading.Thread(target=_drain_soon)
    drainer.start()
    dropped_before = handler.dropped_count
    handler.emit(_make_record("error survives", level=logging.ERROR))
    drainer.join()

    assert handler.dropped_count == dropped_before, (
        "ERROR record must NOT be dropped on a full queue (severity protection)"
    )
    # The ERROR record should now be on the queue (it blocked until room freed).
    remaining = []
    while True:
        try:
            remaining.append(q.get_nowait())
        except queue.Empty:
            break
    assert any(r.getMessage() == "error survives" for r in remaining), (
        "ERROR record was not enqueued after room freed up"
    )


# ---------------------------------------------------------------------------
# Test 5b: listener.flush() drains the SQLiteLogHandler's own writer queue too
# ---------------------------------------------------------------------------


def test_listener_flush_drains_sqlite_handler_writer_queue(tmp_path) -> None:
    """End-to-end barrier: after listener.flush(), a record logged via the root
    QueueHandler must be PERSISTED by the real SQLiteLogHandler -- not merely
    handed to it. SQLiteLogHandler buffers on its own writer thread (Bug #1078),
    so the listener flush must drain that second stage too.
    """
    import sqlite3

    from code_indexer.server.services.async_logging import install_queue_logging
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    db_path = tmp_path / "listener_e2e.db"
    sqlite_handler = SQLiteLogHandler(db_path=db_path)

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers = [sqlite_handler]
        root.setLevel(logging.INFO)
        listener = install_queue_logging([sqlite_handler])
        try:
            logging.getLogger("test.async_queue").info("e2e persisted record")
            listener.flush()  # must drain listener AND the sqlite writer queue

            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    "SELECT message FROM logs WHERE message LIKE '%e2e persisted record%'"
                ).fetchall()
            finally:
                conn.close()
            assert len(rows) == 1, (
                f"record not persisted after listener.flush(): {rows}"
            )
        finally:
            listener.stop()
    finally:
        root.handlers = saved
        root.setLevel(saved_level)
        sqlite_handler.close()


# ---------------------------------------------------------------------------
# Test 5c: module-level shutdown helper + active-listener handle
# ---------------------------------------------------------------------------


def test_get_active_listener_and_shutdown_queue_logging() -> None:
    from code_indexer.server.services import async_logging as al

    root = logging.getLogger()
    saved = list(root.handlers)
    real = _RecordingHandler()
    try:
        root.handlers = [real]
        listener = al.install_queue_logging([real])
        assert al.get_active_listener() is listener

        al.shutdown_queue_logging()
        assert al.get_active_listener() is None
        # Second call is a safe no-op (handle already cleared).
        al.shutdown_queue_logging()
        assert al.get_active_listener() is None
    finally:
        root.handlers = saved


def test_flush_surfaces_handler_failure_but_drains_remaining(capsys) -> None:
    """A failing handler.flush() must not abort the drain of the others, and the
    failure must be surfaced (stderr), not silently swallowed (Messi #13)."""
    from code_indexer.server.services.async_logging import DrainableQueueListener

    class _BoomFlushHandler(_RecordingHandler):
        def flush(self) -> None:
            raise RuntimeError("boom flush")

    class _CountingFlushHandler(_RecordingHandler):
        def __init__(self) -> None:
            super().__init__()
            self.flush_calls = 0

        def flush(self) -> None:
            self.flush_calls += 1

    boom = _BoomFlushHandler()
    counting = _CountingFlushHandler()
    q: queue.Queue = queue.Queue()
    listener = DrainableQueueListener(q, boom, counting)
    listener.start()
    try:
        listener.flush()
        # The second handler was still flushed despite the first raising.
        assert counting.flush_calls >= 1
        err = capsys.readouterr().err
        assert "handler flush failed" in err, (
            "flush failure must be surfaced to stderr, not swallowed silently"
        )
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Test 6: stop() drains the queue and flushes the listener
# ---------------------------------------------------------------------------


def test_stop_drains_and_flushes_listener() -> None:
    from code_indexer.server.services.async_logging import install_queue_logging

    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    real = _RecordingHandler()
    try:
        root.handlers = [real]
        root.setLevel(logging.INFO)
        listener = install_queue_logging([real])
        log = logging.getLogger("test.async_queue")
        for i in range(20):
            log.info("shutdown record %d", i)
        # stop() must drain all enqueued records before returning.
        listener.stop()
        messages = [r.getMessage() for r in real.records]
        for i in range(20):
            assert f"shutdown record {i}" in messages, (
                f"record {i} lost on shutdown; got {len(messages)} records"
            )
    finally:
        root.handlers = saved
        root.setLevel(saved_level)
