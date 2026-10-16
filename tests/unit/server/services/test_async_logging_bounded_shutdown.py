"""shutdown_queue_logging honours its timeout and can run off the event loop.

A log sink that never returns must not stop a server from finishing its
shutdown: the listener gets its stop sentinel, the listener thread is joined
for at most ``timeout`` seconds, a warning goes to stderr (never through the
queue), and the queue handler is detached either way. A healthy sink still
receives every record queued before shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Iterator, List

import pytest

from code_indexer.server.services import async_logging

SHUTDOWN_TIMEOUT_S = 0.3
OFF_LOOP_SHUTDOWN_TIMEOUT_S = 0.5
HEALTHY_SHUTDOWN_TIMEOUT_S = 5.0
FIXTURE_SHUTDOWN_TIMEOUT_S = 0.1
WAIT_LIMIT_S = 5.0
MAX_BLOCKED_SHUTDOWN_S = 2.0
TICK_INTERVAL_S = 0.01
MIN_TICKS_WHILE_SHUTTING_DOWN = 10
RECORD_COUNT = 50
WARNING_TEXT = "did not stop within"


class _BlockingHandler(logging.Handler):
    """A sink whose emit() blocks until ``unblock`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.unblock = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.entered.set()
        self.unblock.wait()


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def target_logger() -> Iterator[logging.Logger]:
    log = logging.getLogger("tests.async_logging.bounded_shutdown")
    log.propagate = False
    log.setLevel(logging.INFO)
    before = list(log.handlers)
    yield log
    for handler in list(log.handlers):
        if handler not in before:
            log.removeHandler(handler)
    async_logging.shutdown_queue_logging(timeout=FIXTURE_SHUTDOWN_TIMEOUT_S)


def _queue_handlers(log: logging.Logger) -> List[logging.Handler]:
    return [
        h for h in log.handlers if isinstance(h, async_logging.IdentityQueueHandler)
    ]


def _block_sink(log: logging.Logger) -> "tuple[_BlockingHandler, object]":
    sink = _BlockingHandler()
    listener = async_logging.install_queue_logging([sink], root=log)
    log.info("this record blocks the sink")
    assert sink.entered.wait(WAIT_LIMIT_S), "listener never reached the sink"
    return sink, listener


def _release_sink(sink: _BlockingHandler, listener: object) -> None:
    sink.unblock.set()
    thread = getattr(listener, "_thread", None)
    if thread is not None:
        thread.join(WAIT_LIMIT_S)


def test_shutdown_with_blocked_sink_finishes_within_timeout_and_detaches_handler(
    target_logger: logging.Logger, capsys: pytest.CaptureFixture
) -> None:
    sink, listener = _block_sink(target_logger)
    try:
        runner = threading.Thread(
            target=async_logging.shutdown_queue_logging,
            kwargs={"timeout": SHUTDOWN_TIMEOUT_S},
            daemon=True,
        )
        started = time.monotonic()
        runner.start()
        runner.join(WAIT_LIMIT_S)
        elapsed = time.monotonic() - started

        assert not runner.is_alive(), "shutdown blocked on the stuck sink"
        assert elapsed < MAX_BLOCKED_SHUTDOWN_S, elapsed
        assert _queue_handlers(target_logger) == []
        assert async_logging.get_active_listener() is None
        assert WARNING_TEXT in capsys.readouterr().err
    finally:
        _release_sink(sink, listener)


def test_shutdown_delivers_queued_records_to_healthy_sink(
    target_logger: logging.Logger, capsys: pytest.CaptureFixture
) -> None:
    sink = _ListHandler()
    async_logging.install_queue_logging([sink], root=target_logger)
    for i in range(RECORD_COUNT):
        target_logger.info("record %d", i)

    async_logging.shutdown_queue_logging(timeout=HEALTHY_SHUTDOWN_TIMEOUT_S)

    assert sink.messages == [f"record {i}" for i in range(RECORD_COUNT)]
    assert _queue_handlers(target_logger) == []
    assert async_logging.get_active_listener() is None
    assert WARNING_TEXT not in capsys.readouterr().err


def test_off_loop_shutdown_keeps_event_loop_running(
    target_logger: logging.Logger,
) -> None:
    """While the bounded shutdown waits on a stuck sink, the loop keeps
    running other tasks -- it would not tick at all if the wait ran on it."""
    sink, listener = _block_sink(target_logger)

    async def _main() -> int:
        ticks = 0
        done = asyncio.Event()

        async def _ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(TICK_INTERVAL_S)

        ticker = asyncio.ensure_future(_ticker())
        result = await async_logging.shutdown_queue_logging_off_loop(
            timeout=OFF_LOOP_SHUTDOWN_TIMEOUT_S
        )
        done.set()
        await ticker
        assert result is None
        return ticks

    try:
        ticks = asyncio.run(_main())
        assert ticks >= MIN_TICKS_WHILE_SHUTTING_DOWN, (
            f"event loop starved during shutdown ({ticks} ticks)"
        )
        assert _queue_handlers(target_logger) == []
        assert async_logging.get_active_listener() is None
    finally:
        _release_sink(sink, listener)
