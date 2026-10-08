"""Invariant: every log handler in the server process masks credentials --
handlers behind the queue listener, the HTTP server's own console handlers,
and handlers attached to named loggers before startup -- while access and
error lines keep their normal format.
"""

import inspect
import io
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterator

import pytest

from code_indexer.server.logging_utils import REDACTING_LOG_FILTER
from code_indexer.server.services import async_logging
from code_indexer.server.services.async_logging import (
    attach_redacting_filter_to_all_handlers,
)

SECRET = "Wq4unboundHandlerSecret77"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def restore_uvicorn_loggers() -> Iterator[None]:
    saved: Dict[str, tuple] = {}
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        saved[name] = (list(lg.handlers), lg.propagate, lg.level)
    yield
    for name, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(name)
        for handler in lg.handlers:
            if handler not in handlers:
                handler.removeFilter(REDACTING_LOG_FILTER)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def _wait_for(predicate: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def test_uvicorn_error_and_access_output_masks_secret(
    restore_uvicorn_loggers: None,
) -> None:
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"upstream rejected token={SECRET}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # Config construction configures uvicorn's own console handlers.
    config = uvicorn.Config(app, lifespan="off", log_level="info")
    server = uvicorn.Server(config)

    error_out, access_out = io.StringIO(), io.StringIO()
    for handler in logging.getLogger("uvicorn").handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(error_out)
    for handler in logging.getLogger("uvicorn.access").handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(access_out)

    attach_redacting_filter_to_all_handlers()

    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        assert _wait_for(lambda: server.started)
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/boom", timeout=10)
        assert raised.value.code == 500
        assert _wait_for(
            lambda: "Exception in ASGI application" in error_out.getvalue()
        )
        assert _wait_for(lambda: "500" in access_out.getvalue())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()

    assert SECRET not in error_out.getvalue()
    assert SECRET not in access_out.getvalue()
    assert "GET /boom HTTP/1.1" in access_out.getvalue()


def test_handler_attached_before_startup_masks_secret() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    named = logging.getLogger("example.audit.pre_startup")
    named.addHandler(handler)
    named.propagate = False
    named.setLevel(logging.INFO)
    try:
        attach_redacting_filter_to_all_handlers()
        assert REDACTING_LOG_FILTER in handler.filters
        named.info("login with token=%s rejected", SECRET)
        named.info("bare value %s", "kept-readable")
    finally:
        named.removeHandler(handler)
        handler.removeFilter(REDACTING_LOG_FILTER)
    assert SECRET not in stream.getvalue()
    assert "bare value kept-readable" in stream.getvalue()


def test_attach_is_idempotent_and_skips_active_queue_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = logging.StreamHandler(io.StringIO())
    queue_handler = logging.StreamHandler(io.StringIO())
    named = logging.getLogger("example.audit.idempotent")
    named.addHandler(handler)
    named.addHandler(queue_handler)
    monkeypatch.setattr(async_logging, "_active_queue_handler", queue_handler)
    try:
        attach_redacting_filter_to_all_handlers()
        attach_redacting_filter_to_all_handlers()
        assert handler.filters.count(REDACTING_LOG_FILTER) == 1
        assert REDACTING_LOG_FILTER not in queue_handler.filters
    finally:
        named.removeHandler(handler)
        named.removeHandler(queue_handler)
        handler.removeFilter(REDACTING_LOG_FILTER)


def test_server_startup_attaches_filter_to_all_handlers() -> None:
    from code_indexer.server.startup import lifespan

    assert "attach_redacting_filter_to_all_handlers()" in inspect.getsource(lifespan)
