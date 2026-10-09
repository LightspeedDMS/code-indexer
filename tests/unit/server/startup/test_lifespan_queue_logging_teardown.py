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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI


def _run_lifespan(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_extra: Dict[str, Any],
    body: Any,
    app: Optional["FastAPI"] = None,
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
    if app is None:
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


def test_cancellation_during_logging_shutdown_still_finishes_the_shutdown_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation returned by the logging shutdown on the normal path is
    honoured only after the rest of the shutdown chain has run: the SQLite
    log handler is closed and the process-wide pools are shut down, and the
    cancellation still ends the lifespan."""
    from code_indexer.server.services import async_logging
    from code_indexer.server.web import routes as web_routes
    from code_indexer.storage import filesystem_vector_store

    real_off_loop = async_logging.shutdown_queue_logging_off_loop
    calls: List[str] = []

    async def _cancelled_on_first_call(
        timeout: float = 5.0,
    ) -> Optional[asyncio.CancelledError]:
        result = await real_off_loop(timeout)
        calls.append("logging")
        if calls.count("logging") == 1:
            return asyncio.CancelledError("cancelled during logging shutdown")
        return result

    def _spy(name: str, real: Any) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return real(*args, **kwargs)

        return _call

    monkeypatch.setattr(
        async_logging, "shutdown_queue_logging_off_loop", _cancelled_on_first_call
    )
    monkeypatch.setattr(
        web_routes,
        "shutdown_discovery_branch_fetch_executor",
        _spy("discovery", web_routes.shutdown_discovery_branch_fetch_executor),
    )
    monkeypatch.setattr(
        filesystem_vector_store,
        "shutdown_deep_fidelity_audit_executor",
        _spy(
            "deep_fidelity",
            filesystem_vector_store.shutdown_deep_fidelity_audit_executor,
        ),
    )

    async def _spy_on_sqlite_handler(app: Any) -> None:
        handler = app.state.sqlite_log_handler
        monkeypatch.setattr(handler, "close", _spy("sqlite_close", handler.close))

    raised, leaked, active_listener = _run_lifespan(
        tmp_path / "server", monkeypatch, {}, _spy_on_sqlite_handler
    )
    assert isinstance(raised, asyncio.CancelledError), repr(raised)
    for step in ("sqlite_close", "discovery", "deep_fidelity"):
        assert step in calls, f"shutdown chain skipped {step}: {calls}"
    assert calls.index("logging") < calls.index("sqlite_close"), calls
    assert leaked == []
    assert active_listener is None


def test_pending_cancellation_survives_a_later_shutdown_step_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation returned by the logging shutdown is still raised when a
    later shutdown step fails; that failure is logged and chained, never
    dropped."""
    from code_indexer.server.services import async_logging
    from code_indexer.server.web import routes as web_routes

    real_off_loop = async_logging.shutdown_queue_logging_off_loop
    seen: List[str] = []

    async def _cancelled_on_first_call(
        timeout: float = 5.0,
    ) -> Optional[asyncio.CancelledError]:
        result = await real_off_loop(timeout)
        seen.append("logging")
        if seen.count("logging") == 1:
            return asyncio.CancelledError("cancelled during logging shutdown")
        return result

    def _failing_step() -> None:
        raise RuntimeError("later shutdown step failed")

    monkeypatch.setattr(
        async_logging, "shutdown_queue_logging_off_loop", _cancelled_on_first_call
    )
    monkeypatch.setattr(
        web_routes, "shutdown_discovery_branch_fetch_executor", _failing_step
    )
    logged_errors: List[Tuple[str, str]] = []

    class _Collect(logging.Handler):
        # The server's redacting filter is attached to every handler,
        # including this one: it formats ``exc_info`` into a redacted
        # ``exc_text`` and clears ``exc_info``, so the traceback is read as
        # text.
        def emit(self, record: logging.LogRecord) -> None:
            logged_errors.append((record.getMessage(), record.exc_text or ""))

    lifespan_logger = logging.getLogger("code_indexer.server.startup.lifespan")
    collector = _Collect(level=logging.ERROR)
    lifespan_logger.addHandler(collector)
    try:
        raised, leaked, active_listener = _run_lifespan(
            tmp_path / "server", monkeypatch, {}, _serve_briefly
        )
    finally:
        lifespan_logger.removeHandler(collector)
    assert isinstance(raised, asyncio.CancelledError), repr(raised)
    shutdown_failures = [
        text
        for message, text in logged_errors
        if message == "Shutdown step failed after a pending cancellation"
    ]
    assert len(shutdown_failures) == 1, logged_errors
    assert "RuntimeError: later shutdown step failed" in shutdown_failures[0]
    assert leaked == []
    assert active_listener is None


def test_second_cancellation_after_a_pending_one_is_not_logged_as_a_step_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an ``Exception`` from a later shutdown step is a step failure; a
    second cancellation (or any other ``BaseException``) ends the lifespan
    as itself, with no step-failure ERROR."""
    from code_indexer.server.services import async_logging
    from code_indexer.server.web import routes as web_routes

    real_off_loop = async_logging.shutdown_queue_logging_off_loop
    calls: List[str] = []

    async def _cancelled_on_first_call(
        timeout: float = 5.0,
    ) -> Optional[asyncio.CancelledError]:
        result = await real_off_loop(timeout)
        calls.append("logging")
        if len(calls) == 1:
            return asyncio.CancelledError("cancelled during logging shutdown")
        return result

    second = asyncio.CancelledError("second cancellation")

    def _cancelled_step() -> None:  # the real step is a sync, un-awaited call
        raise second

    monkeypatch.setattr(
        async_logging, "shutdown_queue_logging_off_loop", _cancelled_on_first_call
    )
    monkeypatch.setattr(
        web_routes, "shutdown_discovery_branch_fetch_executor", _cancelled_step
    )
    messages: List[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    lifespan_logger = logging.getLogger("code_indexer.server.startup.lifespan")
    collector = _Collect(level=logging.ERROR)
    lifespan_logger.addHandler(collector)
    try:
        raised, leaked, active_listener = _run_lifespan(
            tmp_path / "server", monkeypatch, {}, _serve_briefly
        )
    finally:
        lifespan_logger.removeHandler(collector)
    # asyncio.run re-creates a cancelled task's CancelledError, so only the
    # type survives; the discriminating check is the absent ERROR.
    assert isinstance(raised, asyncio.CancelledError), repr(raised)
    assert "Shutdown step failed after a pending cancellation" not in messages
    assert leaked == []
    assert active_listener is None


def test_stale_pending_cancellation_from_an_earlier_run_is_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending shutdown cancellation is scoped to one lifespan run: a value
    already on ``app.state`` when the lifespan starts never replaces the
    exception that ends this run."""
    from fastapi import FastAPI

    app = FastAPI()
    app.state.pending_shutdown_cancellation = asyncio.CancelledError("stale")
    raised, leaked, active_listener = _run_lifespan(
        tmp_path / "server", monkeypatch, {}, _fail_while_serving, app=app
    )
    assert isinstance(raised, RuntimeError), repr(raised)
    assert app.state.pending_shutdown_cancellation is None
    assert leaked == []
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
