"""Minimal uvicorn worker app for the worker-stall watchdog tests (Story S12).

Its lifespan calls the SAME ``start_stall_watchdog`` / ``stop_stall_watchdog``
helpers that ``server/startup/lifespan.py`` calls, so a real
``uvicorn --workers N`` run exercises the production wiring of the watchdog in
real worker processes supervised by uvicorn's real health check.

Not collected by pytest (no ``test_`` prefix); launched by
``test_stall_watchdog_uvicorn.py`` as ``stall_watchdog_harness_app:app``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

from fastapi import FastAPI

from code_indexer.server.utils.stall_watchdog import (
    start_stall_watchdog,
    stop_stall_watchdog,
)

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s pid=%(process)d %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    start_stall_watchdog(app, os.environ["CIDX_SERVER_DATA_DIR"])
    yield
    await stop_stall_watchdog(app)


app = FastAPI(lifespan=lifespan)


def hold_gil_in_c_call(seconds: int) -> None:
    """Block inside a C call WITHOUT releasing the GIL.

    ``ctypes.PyDLL`` (unlike ``CDLL``) calls the foreign function while holding
    the GIL, so for ``seconds`` no other Python thread in this worker can run:
    not the event loop, not uvicorn's pong thread, not the watchdog heartbeat.
    """
    ctypes.PyDLL(None).sleep(seconds)


@app.get("/stall")
def stall(seconds: int = 6) -> Dict[str, int]:
    hold_gil_in_c_call(seconds)
    return {"pid": os.getpid()}


@app.get("/busy")
def busy(seconds: float = 1.0) -> Dict[str, int]:
    """Pure-Python CPU work: it keeps the GIL busy but yields it every
    switch interval, like any healthy worker under load."""
    deadline = time.monotonic() + seconds
    counter = 0
    while time.monotonic() < deadline:
        counter += 1
    return {"pid": os.getpid(), "iterations": counter}
