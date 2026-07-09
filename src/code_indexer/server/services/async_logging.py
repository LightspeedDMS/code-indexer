"""Async server logging via QueueHandler/QueueListener (py-spy logging-lock fix).

Performance follow-up to Bug #1078. py-spy under concurrent ``/api/query`` load
showed the dominant active leaf frame across all worker threads was
``acquire (logging/__init__.py:901)`` -- the per-Handler lock. Two causes:

  (a) the synchronous console ``StreamHandler`` doing formatting +
      ``stream.write()`` + ``flush()`` WHILE holding its handler lock, and
  (b) several ``logger.info()`` calls per query on the hot search path.

This module addresses (a) by routing the server's root logger through a single
:class:`IdentityQueueHandler` whose :class:`QueueListener` owns the REAL handlers
(console ``StreamHandler``, ``SQLiteLogHandler``, telemetry handler). On a request
thread a ``logger.info()`` call now does at most ONE fast lock acquire + one
``queue.put`` -- ALL formatting and handler I/O happen on the single listener
thread, off the hot path.

Design notes
------------
* ``IdentityQueueHandler.prepare()`` is an identity no-op: it returns the record
  unchanged. The stdlib ``QueueHandler.prepare`` formats ``record.message`` and
  nulls ``record.args`` (useful for cross-process pickling). Our handlers are
  in-process, so doing that on the enqueue side would move formatting back onto
  the hot path -- defeating the purpose. Formatting happens on the listener
  thread inside the real handlers.
* Bounded queue (anti-unbounded-memory, Messi Rule #14) with a drop policy that
  mirrors :class:`SQLiteLogHandler`: ERROR/CRITICAL block briefly on a full queue
  rather than be lost; lower-severity records drop and are counted + surfaced via
  a throttled stderr warning. Logging failures never crash the application.
* The listener is started by ``install_queue_logging`` and STOPPED/FLUSHED on
  server shutdown (wired into ``startup/lifespan.py``) so no logs are lost on a
  clean shutdown. ``flush()`` synchronously drains the queue for timing-sensitive
  tests.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from typing import List, Optional


logger = logging.getLogger(__name__)

# Maximum number of pending log records to buffer. Records beyond this are
# dropped with a counter increment (anti-unbounded-memory -- Messi Rule #14).
# Mirrors SQLiteLogHandler._QUEUE_MAXSIZE.
DEFAULT_QUEUE_MAXSIZE = 10_000

# How long to block (bounded) on queue.Full for ERROR/CRITICAL records before
# giving up and incrementing the dropped counter. Mirrors SQLiteLogHandler.
_HIGH_SEVERITY_QUEUE_TIMEOUT_S = 2.0

# Minimum seconds between throttled stderr drop-warnings (anti-spam).
_STDERR_THROTTLE_S = 10.0

# Sentinel pushed onto the queue by stop() to wake the listener thread for a
# clean drain-then-exit. logging.handlers.QueueListener uses ``None`` as its
# own sentinel; we reuse that contract.
_SENTINEL = None


class IdentityQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that enqueues records WITHOUT formatting them.

    Two deliberate departures from the stdlib ``QueueHandler``:

    1. :meth:`prepare` is an identity no-op -- it returns the record unchanged
       (does NOT set ``record.message`` and does NOT null ``record.args``). The
       in-process listener formats on its own thread; pre-formatting here would
       put the cost back on the request hot path.
    2. :meth:`enqueue` implements a bounded-queue drop policy (the parent class
       does an unbounded ``queue.put_nowait`` and lets ``queue.Full`` propagate).
       ERROR/CRITICAL records block briefly; lower-severity records drop and are
       counted + surfaced to stderr.
    """

    def __init__(self, q: "queue.Queue") -> None:
        super().__init__(q)
        # Explicitly-typed reference: QueueHandler.queue is typed as a loose
        # _QueueLike protocol that lacks put()/put_nowait() in typeshed. Keep our
        # own concrete queue.Queue handle for the bounded-queue drop policy.
        self._q: "queue.Queue" = q
        self._dropped: int = 0
        self._last_stderr_warn: float = 0.0

    @property
    def dropped_count(self) -> int:
        """Number of records dropped due to a saturated queue."""
        return self._dropped

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Return the record unchanged -- formatting happens on the listener.

        The stdlib implementation calls ``self.format(record)`` and then nulls
        ``record.args`` / ``record.exc_info`` so the record can be pickled across
        a process boundary. Our QueueListener and its handlers run in-process, so
        we keep the raw record and let the real handlers format it on the listener
        thread (off the request hot path).
        """
        return record

    def _record_drop(self) -> None:
        """Count a dropped record and surface it via a throttled stderr warning.

        Uses stderr rather than ``logging`` because we are inside a logging
        handler; logging here would recurse. Throttled to avoid flooding stderr
        under sustained saturation (anti-silent-failure -- drops must never be
        invisible).
        """
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_stderr_warn >= _STDERR_THROTTLE_S:
            self._last_stderr_warn = now
            sys.stderr.write(
                f"[async_logging] dropped {self._dropped} log record(s) "
                "(listener queue saturated)\n"
            )

    def enqueue(self, record: logging.LogRecord) -> None:
        """Enqueue a record with a bounded-queue drop policy.

        On ``queue.Full``: ERROR/CRITICAL records take a short bounded blocking
        ``put`` (they are rare and important); lower-severity records drop and
        are counted. Never blocks unboundedly, never raises.
        """
        try:
            self._q.put_nowait(record)
        except queue.Full:
            if record.levelno >= logging.ERROR:
                try:
                    self._q.put(record, timeout=_HIGH_SEVERITY_QUEUE_TIMEOUT_S)
                except queue.Full:
                    self._record_drop()
            else:
                self._record_drop()

    def emit(self, record: logging.LogRecord) -> None:
        """Prepare (identity) and enqueue; never let logging failures crash."""
        try:
            self.enqueue(self.prepare(record))
        except Exception:
            self.handleError(record)


class DrainableQueueListener(logging.handlers.QueueListener):
    """QueueListener with synchronous ``flush()`` and a draining ``stop()``.

    The stdlib ``QueueListener.stop()`` enqueues a sentinel and joins the thread,
    which drains everything already queued -- but it offers no way to *flush*
    without stopping. Tests and shutdown need a synchronous drain barrier, so we
    add :meth:`flush`.
    """

    def __init__(self, q: "queue.Queue", *handlers: logging.Handler) -> None:
        # respect_handler_level=True so a handler's own level still filters
        # (e.g. SQLiteLogHandler.setLevel(configured_level)).
        super().__init__(q, *handlers, respect_handler_level=True)
        # Explicitly-typed reference (see IdentityQueueHandler) so mypy resolves
        # put()/join() on the concrete queue.Queue rather than the loose
        # _QueueLike protocol QueueListener.queue is typed as.
        self._q: "queue.Queue" = q
        self._flush_lock = threading.Lock()

    def flush(self, timeout: float = 5.0) -> None:
        """Block until the queue is empty and in-flight records are handled.

        Pushes a flush-barrier record and waits for the listener to acknowledge
        it has been dequeued, guaranteeing every record enqueued before the call
        has been handed to the real handlers. Safe to call concurrently.
        """
        with self._flush_lock:
            barrier = threading.Event()

            # A lightweight marker record carrying the barrier event. handle()
            # sets the event when this record is dequeued, after all prior
            # records have been processed (queue is FIFO, single consumer).
            marker = logging.LogRecord(
                name="async_logging.flush_barrier",
                level=logging.DEBUG,
                pathname=__file__,
                lineno=0,
                msg="flush_barrier",
                args=(),
                exc_info=None,
            )
            setattr(marker, "_async_logging_flush_barrier", barrier)
            self._q.put(marker)
            barrier.wait(timeout=timeout)

            # End-to-end barrier: a real handler may itself buffer asynchronously
            # (e.g. SQLiteLogHandler's own Bug #1078 writer queue). Drain those
            # too so flush() guarantees records are actually persisted, not just
            # handed off. handler.flush() is a no-op for plain StreamHandlers.
            #
            # A failure in one handler's flush must NOT skip draining the others,
            # so we keep going — but it must NOT be swallowed silently either
            # (anti-silent-failure, Messi Rule #13). We surface it to stderr
            # rather than via logging: we are INSIDE the logging pipeline here and
            # logging would recurse (same rationale as the drop-counter stderr
            # path in SQLiteLogHandler / IdentityQueueHandler).
            for handler in self.handlers:
                try:
                    handler.flush()
                except Exception as exc:
                    sys.stderr.write(
                        f"[async_logging] handler flush failed during drain: {exc!r}\n"
                    )

    def handle(self, record: logging.LogRecord) -> None:
        """Process a record, intercepting the flush-barrier marker.

        For the flush barrier we set the event and return WITHOUT dispatching to
        the real handlers (it carries no user-visible message).
        """
        barrier = getattr(record, "_async_logging_flush_barrier", None)
        if barrier is not None:
            barrier.set()
            return
        super().handle(record)


# Module-level handle to the active listener so lifespan shutdown can stop it
# even if the caller did not retain the reference (belt-and-suspenders, matching
# the lifespan singleton-wiring patterns elsewhere in the server).
_active_listener: Optional[DrainableQueueListener] = None


def install_queue_logging(
    real_handlers: List[logging.Handler],
    maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    root: Optional[logging.Logger] = None,
) -> DrainableQueueListener:
    """Route ``root`` through a single QueueHandler backed by a QueueListener.

    The given ``real_handlers`` are REMOVED from the root logger and handed to a
    :class:`DrainableQueueListener` (started immediately). A single
    :class:`IdentityQueueHandler` is attached to the root in their place, so a
    ``logger.x()`` call on any thread does only a fast enqueue.

    Args:
        real_handlers: The handlers that should run on the listener thread
            (console StreamHandler, SQLiteLogHandler, telemetry handler, ...).
        maxsize: Bounded-queue size (anti-unbounded-memory).
        root: Target logger; defaults to the root logger.

    Returns:
        The started :class:`DrainableQueueListener`. The caller MUST ``stop()``
        it on shutdown (the lifespan does this) to drain queued records.
    """
    global _active_listener

    target = root if root is not None else logging.getLogger()

    log_queue: "queue.Queue" = queue.Queue(maxsize=maxsize)

    # Detach the real handlers from the root -- they now live behind the listener.
    for h in real_handlers:
        if h in target.handlers:
            target.removeHandler(h)

    listener = DrainableQueueListener(log_queue, *real_handlers)
    listener.start()

    queue_handler = IdentityQueueHandler(log_queue)
    target.addHandler(queue_handler)

    _active_listener = listener
    return listener


def get_active_listener() -> Optional[DrainableQueueListener]:
    """Return the listener installed by the last ``install_queue_logging`` call."""
    return _active_listener


def shutdown_queue_logging(timeout: float = 5.0) -> None:
    """Stop the active listener (drains the queue) and clear the module handle.

    Non-fatal: never raises -- mirrors the lifespan belt-and-suspenders shutdown
    discipline so a logging-shutdown error cannot abort the remaining chain.
    """
    global _active_listener
    listener = _active_listener
    if listener is None:
        return
    try:
        listener.stop()
    except Exception:  # pragma: no cover - shutdown best-effort
        pass
    finally:
        _active_listener = None
