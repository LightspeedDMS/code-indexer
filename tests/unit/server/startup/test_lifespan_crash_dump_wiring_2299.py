from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import signal
from pathlib import Path
from typing import Dict, Iterator, List

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
_CALL = 'enable_crash_dumps(Path(server_data_dir) / "logs")'


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
