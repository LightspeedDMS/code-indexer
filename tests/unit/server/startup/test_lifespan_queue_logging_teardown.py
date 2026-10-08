"""The lifespan removes the async-logging queue handler it installed on EVERY
exit path: a normal shutdown, a startup step that raises, and an exception
thrown into the lifespan at its yield (which is also what a lifespan
cancelled by a startup/shutdown timeout goes through).

Each test drives the REAL make_lifespan and inspects the root logger and the
async_logging module handle BEFORE its own hygiene cleanup, so a leak is
observed, not masked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


def _run_lifespan(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_extra: Dict[str, Any],
    body: Any,
) -> Tuple[Optional[BaseException], List[logging.Handler], Any]:
    """Run the real lifespan around ``body``.

    Returns (exception raised or None, queue handlers newly left on the root
    logger, the async_logging active listener) -- all captured before cleanup.
    """
    from fastapi import FastAPI

    from code_indexer.server.services import async_logging
    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
    )

    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    data_dir.mkdir()
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    config = {"server_dir": str(data_dir), "log_level": "INFO", **config_extra}
    (data_dir / "config.json").write_text(json.dumps(config))
    app = FastAPI()
    lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())

    async def _run() -> None:
        async with lifespan_fn(app):
            await body(app)

    raised: Optional[BaseException] = None
    try:
        asyncio.run(_run())
    except BaseException as exc:  # the exception under test
        raised = exc
    # Evidence first ...
    leaked: List[logging.Handler] = [
        h
        for h in root.handlers
        if isinstance(h, IdentityQueueHandler) and h not in before
    ]
    active_listener = async_logging.get_active_listener()
    # ... hygiene second, so a failing run never leaks into other tests:
    # startup moved the original root handlers into the queue listener, so
    # put back exactly the handlers and level the root logger started with.
    async_logging.shutdown_queue_logging()
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(before_level)
    watchdog = getattr(app.state, "stall_watchdog", None)
    if watchdog is not None:
        watchdog.stop()
    return raised, leaked, active_listener


async def _serve_briefly(app: Any) -> None:
    await asyncio.sleep(0)


async def _never_reached(app: Any) -> None:
    raise AssertionError("startup should have failed")


async def _fail_while_serving(app: Any) -> None:
    raise RuntimeError("failure while serving")


def test_normal_shutdown_removes_queue_handler_and_clears_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised, leaked, active_listener = _run_lifespan(
        tmp_path / "server", monkeypatch, {}, _serve_briefly
    )
    assert raised is None, repr(raised)
    assert leaked == []
    assert active_listener is None, "shutdown left the queue listener registered"


def test_startup_error_removes_queue_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup step that raises after logging is installed (a real config
    error: mcp_dispatch_pool_size=0) never runs the post-yield shutdown."""
    raised, leaked, active_listener = _run_lifespan(
        tmp_path / "server",
        monkeypatch,
        {"mcp_dispatch_pool_size": 0},
        _never_reached,
    )
    assert isinstance(raised, ValueError), repr(raised)
    assert leaked == [], "startup failed but the queue handler stayed on root"
    assert active_listener is None


def test_exception_at_yield_removes_queue_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised, leaked, active_listener = _run_lifespan(
        tmp_path / "server", monkeypatch, {}, _fail_while_serving
    )
    assert isinstance(raised, RuntimeError), repr(raised)
    assert leaked == [], "the lifespan ended by exception but left the queue handler"
    assert active_listener is None


TICK_INTERVAL_S = 0.01
WAIT_LIMIT_S = 10.0
MIN_TICKS_DURING_SHUTDOWN = 100


class _BlockingSink(logging.Handler):
    """A root sink whose emit() blocks until ``unblock`` is set."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.entered = threading.Event()
        self.unblock = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.entered.set()
        self.unblock.wait()


def test_shutdown_with_blocked_log_sink_keeps_event_loop_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded logging shutdown waits on a stuck sink in a worker
    thread: the event loop keeps running other tasks meanwhile."""
    sink = _BlockingSink()
    root = logging.getLogger()
    root.addHandler(sink)
    ticks_after_body: List[int] = []

    async def _block_sink_then_return(app: Any) -> None:
        loop = asyncio.get_running_loop()
        state = {"body_done": False, "ticks": 0}

        async def _ticker() -> None:
            while True:
                if state["body_done"]:
                    state["ticks"] += 1
                    ticks_after_body[:] = [state["ticks"]]
                await asyncio.sleep(TICK_INTERVAL_S)

        app.state.test_ticker = asyncio.ensure_future(_ticker())
        logging.getLogger("tests.lifespan.blocked_sink").warning("block the sink")
        entered = await loop.run_in_executor(None, sink.entered.wait, WAIT_LIMIT_S)
        assert entered, "the listener never reached the blocking sink"
        state["body_done"] = True

    try:
        raised, leaked, active_listener = _run_lifespan(
            tmp_path / "server", monkeypatch, {}, _block_sink_then_return
        )
    finally:
        sink.unblock.set()
        root.removeHandler(sink)
    assert raised is None, repr(raised)
    assert leaked == []
    assert active_listener is None
    assert ticks_after_body and ticks_after_body[0] >= MIN_TICKS_DURING_SHUTDOWN, (
        f"event loop starved during logging shutdown: {ticks_after_body}"
    )
