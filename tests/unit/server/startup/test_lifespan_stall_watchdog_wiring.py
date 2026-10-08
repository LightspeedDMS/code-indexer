"""Wiring guard (Story S12): the server lifespan runs the worker-stall watchdog.

uvicorn runs the lifespan once per worker, so starting the watchdog there gives
every ``--workers N`` worker its own heartbeat. Source-text + source-order
guards, mirroring ``test_lifespan_async_logging_wiring.py``. The behaviour of
the same start/stop helpers in real uvicorn workers is covered by
``tests/unit/server/utils/test_stall_watchdog_uvicorn.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_YIELD = "yield  # Server is now running"
_START_CALL = "start_stall_watchdog(app, server_data_dir)"
_STOP_CALL = "await stop_stall_watchdog(app)"


def _split_at_yield() -> "tuple[str, str]":
    source = _LIFESPAN_PATH.read_text()
    yield_pos = source.find(_YIELD)
    assert yield_pos != -1
    return source[:yield_pos], source[yield_pos:]


def test_watchdog_started_in_startup_after_queue_logging() -> None:
    startup, _ = _split_at_yield()
    start_pos = startup.find(_START_CALL)
    assert start_pos != -1, (
        f"lifespan startup must call {_START_CALL} so every uvicorn worker "
        "leaves a stack dump behind before a health-check kill (S12)"
    )
    assert startup.find("install_queue_logging(") < start_pos, (
        "start the watchdog after logging is installed so the startup sweep's "
        "ERROR lines about killed workers reach logs.db"
    )


def test_watchdog_stopped_in_shutdown_before_log_listener_stops() -> None:
    _, shutdown = _split_at_yield()
    stop_pos = shutdown.find(_STOP_CALL)
    assert stop_pos != -1, f"lifespan shutdown must call {_STOP_CALL}"
    listener_stop_pos = shutdown.find("shutdown_queue_logging()")
    assert listener_stop_pos != -1
    assert stop_pos < listener_stop_pos, (
        "stop the watchdog before the log listener drains so a stall reported "
        "during the stop is still logged"
    )


def test_real_lifespan_starts_and_stops_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL make_lifespan: the watchdog runs (arms in this process)
    while the app is up and is gone, armed files removed, after shutdown."""
    from fastapi import FastAPI

    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from code_indexer.server.utils.stall_watchdog import StallWatchdog
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
        _write_minimal_config,
    )

    data_dir = tmp_path / "server"
    data_dir.mkdir()
    logs_dir = data_dir / "logs"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    _write_minimal_config(str(data_dir))
    app = FastAPI()
    lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())
    seen: Dict[str, Any] = {}

    async def _run() -> None:
        async with lifespan_fn(app):
            seen["watchdog"] = getattr(app.state, "stall_watchdog", None)
            deadline = time.monotonic() + 10
            pattern = f"worker-stall-{os.getpid()}-*.evidence"
            while time.monotonic() < deadline:
                armed = [p for p in logs_dir.glob(pattern) if p.stat().st_size > 0]
                if armed:
                    seen["armed"] = armed
                    break
                await asyncio.sleep(0.05)

    root = logging.getLogger()
    try:
        asyncio.run(_run())
    finally:
        for handler in list(root.handlers):  # never leak handlers to other tests
            if isinstance(handler, (SQLiteLogHandler, IdentityQueueHandler)):
                root.removeHandler(handler)

    assert isinstance(seen.get("watchdog"), StallWatchdog), seen
    assert seen.get("armed"), "the lifespan's watchdog never armed in this pid"
    assert getattr(app.state, "stall_watchdog", "missing") is None
    assert _lifetime_files(logs_dir) == []


def _lifetime_files(logs_dir: Path) -> List[Path]:
    """A watchdog lifetime's evidence, dump and temp files."""
    if not logs_dir.is_dir():
        return []
    return sorted(
        p
        for p in logs_dir.glob("worker-stall-*")
        if p.suffix in (".evidence", ".dump", ".tmp")
    )


_WATCHDOG_THREAD = "cidx-stall-watchdog"
# A stop that was requested but whose join was cancelled completes on the
# watchdog thread's own; allow it this long. A watchdog nobody asked to stop
# never exits, so it is still caught.
_WATCHDOG_EXIT_WAIT_S = 5.0
_WATCHDOG_EXIT_POLL_S = 0.05


def _watchdog_threads() -> "set[threading.Thread]":
    return {t for t in threading.enumerate() if t.name == _WATCHDOG_THREAD}


def _run_real_lifespan(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_extra: Dict[str, Any],
    body: Any,
) -> "tuple[BaseException, List[threading.Thread], List[Path]]":
    """Run the REAL make_lifespan until it raises.

    Returns (exception, watchdog threads still alive, files left), all
    captured BEFORE any cleanup. Only afterwards is a watchdog the lifespan
    left running stopped, so a failing (RED) run cannot leak into other tests.
    start_stall_watchdog stores the watchdog on app.state right after starting
    its thread, so app.state always reaches a started watchdog.
    """
    from fastapi import FastAPI

    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
    )

    before = _watchdog_threads()
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
    deadline = time.monotonic() + _WATCHDOG_EXIT_WAIT_S
    alive = [t for t in _watchdog_threads() - before if t.is_alive()]
    while alive and time.monotonic() < deadline:
        time.sleep(_WATCHDOG_EXIT_POLL_S)
        alive = [t for t in alive if t.is_alive()]
    armed = _lifetime_files(data_dir / "logs")
    # ... hygiene second.
    leaked = getattr(app.state, "stall_watchdog", None)
    if leaked is not None:
        leaked.stop()
    root = logging.getLogger()
    for handler in list(root.handlers):  # never leak handlers to other tests
        if isinstance(handler, (SQLiteLogHandler, IdentityQueueHandler)):
            root.removeHandler(handler)
    assert raised is not None, "the lifespan was expected to raise"
    return raised, alive, armed


def test_startup_error_after_watchdog_start_still_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup step that raises after start_stall_watchdog (here a real
    config error: mcp_dispatch_pool_size=0) must not leave the heartbeat
    thread running."""

    async def _never_reached(app: Any) -> None:
        raise AssertionError("startup should have failed")

    raised, alive, armed = _run_real_lifespan(
        tmp_path / "server", monkeypatch, {"mcp_dispatch_pool_size": 0}, _never_reached
    )
    assert isinstance(raised, ValueError) and "mcp_dispatch_pool_size" in str(raised)
    assert alive == [], "startup failed but the stall watchdog kept running"
    assert armed == []


def test_exception_thrown_into_lifespan_at_yield_still_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception thrown into the lifespan at its yield skips the ordinary
    post-yield shutdown code; the watchdog must still be stopped."""
    logs_dir = tmp_path / "server" / "logs"

    async def _fail_once_armed(app: Any) -> None:
        deadline = time.monotonic() + 10
        while not list(logs_dir.glob("*.evidence")):
            assert time.monotonic() < deadline, "watchdog never armed"
            await asyncio.sleep(0.05)
        raise RuntimeError("boom while serving")

    raised, alive, armed = _run_real_lifespan(
        tmp_path / "server", monkeypatch, {}, _fail_once_armed
    )
    assert isinstance(raised, RuntimeError) and str(raised) == "boom while serving"
    assert alive == [], "the lifespan exited by exception but the watchdog ran on"
    assert armed == []


def test_cancellation_during_cleanup_keeps_the_original_error_and_stops_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown cancelled while the lifespan awaits the watchdog cleanup
    must still stop the watchdog and must not replace the error in flight.
    The body leaves a PENDING cancellation and then raises: it is delivered at
    the first suspension point on the exit path -- the cleanup await."""
    logs_dir = tmp_path / "server" / "logs"

    async def _cancel_then_fail(app: Any) -> None:
        deadline = time.monotonic() + 10
        while not list(logs_dir.glob("*.evidence")):
            assert time.monotonic() < deadline, "watchdog never armed"
            await asyncio.sleep(0.05)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise RuntimeError("boom with a pending cancel")

    raised, alive, armed = _run_real_lifespan(
        tmp_path / "server", monkeypatch, {}, _cancel_then_fail
    )
    assert isinstance(raised, RuntimeError), repr(raised)
    assert str(raised) == "boom with a pending cancel"
    assert alive == [], "a cancelled cleanup left the stall watchdog running"
    assert armed == []
