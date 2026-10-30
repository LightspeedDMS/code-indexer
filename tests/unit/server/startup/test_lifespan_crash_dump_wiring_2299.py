from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import shutil
import signal
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest

from code_indexer.server.utils import crash_dump
from code_indexer.server.utils.stall_watchdog_evidence import read_start_ticks

_LIFESPAN_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)
_YIELD = "yield  # Server is now running"
_CALL = "arm_crash_dumps(prepared)"


@pytest.fixture
def restore_faulthandler(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(crash_dump, "_active", None)
    yield
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)
    faulthandler.enable(file=2, all_threads=True)


def test_crash_dumps_enabled_in_startup_after_queue_logging() -> None:
    source = _LIFESPAN_PATH.read_text()
    startup = source[: source.index(_YIELD)]
    call_pos = startup.find(_CALL)
    assert call_pos != -1, f"lifespan startup must call {_CALL}"
    assert startup.find("install_queue_logging(") < call_pos


def test_real_lifespan_enables_crash_dumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_faulthandler: None
) -> None:
    from fastapi import FastAPI

    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
        _write_minimal_config,
    )

    data_dir = tmp_path / "server"
    data_dir.mkdir()
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    _write_minimal_config(str(data_dir))
    app = FastAPI()
    lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())
    seen: Dict[str, List[Path]] = {}
    expected = (
        data_dir
        / "logs"
        / f"faulthandler-{os.getpid()}-{read_start_ticks(os.getpid())}.log"
    )

    async def _run() -> None:
        async with lifespan_fn(app):
            seen["files"] = sorted((data_dir / "logs").glob("faulthandler-*.log"))

    root = logging.getLogger()
    try:
        asyncio.run(_run())
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, (SQLiteLogHandler, IdentityQueueHandler)):
                root.removeHandler(handler)

    assert seen.get("files") == [expected]
    assert faulthandler.is_enabled()


def _run_real_lifespan(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, inside: Callable[[], None]
) -> None:
    from fastapi import FastAPI

    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
        _write_minimal_config,
    )

    data_dir.mkdir()
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    _write_minimal_config(str(data_dir))
    lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())

    async def _run() -> None:
        async with lifespan_fn(FastAPI()):
            inside()

    root = logging.getLogger()
    try:
        asyncio.run(_run())
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, (SQLiteLogHandler, IdentityQueueHandler)):
                root.removeHandler(handler)


def test_second_lifespan_after_first_dir_deleted_warns_nothing_and_sweeps_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_faulthandler: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_dir = tmp_path / "first-app"
    _run_real_lifespan(first_dir, monkeypatch, lambda: None)
    shutil.rmtree(first_dir)

    second_logs = tmp_path / "second-app" / "logs"
    real_scandir = os.scandir
    scan_threads: List[int] = []
    loop_threads: List[int] = []

    def spy_scandir(path: Any = ".") -> Any:
        if Path(path) == second_logs:
            scan_threads.append(threading.get_ident())
        return real_scandir(path)

    real_ticks = crash_dump.read_start_ticks
    prepare_threads: List[int] = []

    def spy_ticks(pid: int) -> int:
        prepare_threads.append(threading.get_ident())
        return real_ticks(pid)

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(file).parent == second_logs:
            prepare_threads.append(threading.get_ident())
        return open(file, *args, **kwargs)

    monkeypatch.setattr(crash_dump, "read_start_ticks", spy_ticks)
    monkeypatch.setattr(crash_dump, "open", spy_open, raising=False)
    monkeypatch.setattr(os, "scandir", spy_scandir)
    with caplog.at_level("WARNING", logger=crash_dump.__name__):
        _run_real_lifespan(
            second_logs.parent,
            monkeypatch,
            lambda: loop_threads.append(threading.get_ident()),
        )

    assert [r for r in caplog.records if r.name == crash_dump.__name__] == []
    own_dump = f"faulthandler-{os.getpid()}-{read_start_ticks(os.getpid())}.log"
    assert (second_logs / own_dump).is_file()
    assert scan_threads, "the startup sweep must scan the app's own logs dir"
    assert loop_threads and loop_threads[0] not in scan_threads
    assert len(prepare_threads) == 2, "dump file prep reads /proc and opens once"
    assert loop_threads[0] not in prepare_threads
