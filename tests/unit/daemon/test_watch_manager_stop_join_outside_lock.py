"""DaemonWatchManager.stop_watch() must not hold the state lock while it
joins the watch thread: the worker's exit path takes that same lock, so a
join under it can only end by timing out.

The watch thread here is real; only the watch handler it builds is a
test double, so no filesystem observer or indexer is involved.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict

import pytest

from code_indexer.daemon.watch_manager import DaemonWatchManager

# Far below the 5 s join timeout, generous for a loaded gate.
_PROMPT_STOP_SECONDS = 2.0


class _FakeHandler:
    def __init__(self) -> None:
        self._watching = threading.Event()

    def start_watching(self) -> None:
        self._watching.set()

    def stop_watching(self) -> None:
        self._watching.clear()

    def is_watching(self) -> bool:
        return self._watching.is_set()

    def get_stats(self) -> Dict[str, Any]:
        return {"files_processed": 3}


class _ManagerWithFakeHandler(DaemonWatchManager):
    def __init__(self, handler: _FakeHandler) -> None:
        super().__init__()
        self._fake_handler = handler

    def _create_watch_handler(self, project_path, config, mutation_lock=None, **kw):
        return self._fake_handler


def _wait_until_watching(manager: DaemonWatchManager, handler: _FakeHandler) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if manager.watch_handler is handler and handler.is_watching():
            return
        time.sleep(0.01)
    pytest.fail("watch thread never installed and started its handler")


@pytest.mark.timeout(30)
def test_stop_watch_returns_promptly_once_worker_exits(caplog) -> None:
    handler = _FakeHandler()
    manager = _ManagerWithFakeHandler(handler)
    assert manager.start_watch("/repo/example", config=None)["status"] == "success"
    _wait_until_watching(manager, handler)
    worker = manager.watch_thread
    assert worker is not None

    caplog.set_level(logging.INFO, logger="code_indexer.daemon.watch_manager")
    started = time.monotonic()
    result = manager.stop_watch()
    elapsed = time.monotonic() - started

    assert result["status"] == "success", result
    assert result["stats"] == {"files_processed": 3}
    assert elapsed < _PROMPT_STOP_SECONDS, (
        f"stop_watch took {elapsed:.2f}s: it waited out the join timeout "
        "instead of letting the worker exit"
    )
    assert not worker.is_alive()
    assert "did not stop" not in caplog.text
    assert "Watch stopped for project: /repo/example" in caplog.text
    assert manager.watch_thread is None
    assert manager.watch_handler is None
    assert manager.project_path is None
    assert manager.start_time is None
    assert not manager.is_running()


class _RestartingManager(DaemonWatchManager):
    """Its first worker, once its own exit path has cleared the state,
    starts a second watch -- i.e. a watch started after the stopped worker
    exited but before stop_watch() takes the lock back."""

    def __init__(self) -> None:
        super().__init__()
        self.handlers: list = []

    def _create_watch_handler(self, project_path, config, mutation_lock=None, **kw):
        handler = _FakeHandler()
        self.handlers.append(handler)
        return handler

    def _watch_thread_worker(self, project_path, config, mutation_lock=None, **kw):
        super()._watch_thread_worker(project_path, config, mutation_lock, **kw)
        if project_path == "/repo/first":
            assert self.start_watch("/repo/second", config=None)["status"] == "success"


_HANDLER_WAIT_SECONDS = 5
_POLL_SECONDS = 0.01


def _handler_at(manager: _RestartingManager, index: int) -> _FakeHandler:
    deadline = time.monotonic() + _HANDLER_WAIT_SECONDS
    while time.monotonic() < deadline:
        if len(manager.handlers) > index:
            handler: _FakeHandler = manager.handlers[index]
            return handler
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"watch thread never built handler #{index}")


@pytest.mark.timeout(30)
def test_stop_watch_keeps_a_watch_started_while_it_was_joining() -> None:
    manager = _RestartingManager()
    assert manager.start_watch("/repo/first", config=None)["status"] == "success"
    _wait_until_watching(manager, _handler_at(manager, 0))
    first_worker = manager.watch_thread

    result = manager.stop_watch()

    assert result["status"] == "success", result
    assert first_worker is not None and not first_worker.is_alive()
    assert manager.project_path == "/repo/second"
    assert manager.watch_thread is not None
    assert manager.watch_thread is not first_worker
    _wait_until_watching(manager, _handler_at(manager, 1))
    assert manager.is_running()

    assert manager.stop_watch()["status"] == "success"
    assert not manager.is_running()
