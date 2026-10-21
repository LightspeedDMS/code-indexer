"""DaemonWatchManager.stop_watch() must not hold the state lock while it
joins the watch thread: the worker's exit path takes that same lock, so a
join under it can only end by timing out. While that join runs the watch is
STOPPING: a second stop must not stop the handler again, stats must not
report it running, and a start must not slip in.

The watch threads here are real; only the watch handler they build is a
test double, so no filesystem observer or indexer is involved.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

from code_indexer.daemon.watch_manager import DaemonWatchManager

# Far below the 5 s join timeout, generous for a loaded gate.
_PROMPT_STOP_SECONDS = 2.0
_HANDLER_WAIT_SECONDS = 5
_POLL_SECONDS = 0.01


class _FakeHandler:
    def __init__(self, hold: Optional[threading.Event] = None) -> None:
        self._watching = threading.Event()
        self._hold = hold
        self.stop_calls = 0

    def start_watching(self) -> None:
        self._watching.set()
        if self._hold is not None:
            self._hold.wait(timeout=_HANDLER_WAIT_SECONDS)

    def stop_watching(self) -> None:
        self.stop_calls += 1
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
    deadline = time.monotonic() + _HANDLER_WAIT_SECONDS
    while time.monotonic() < deadline:
        if manager.watch_handler is handler and handler.is_watching():
            return
        time.sleep(_POLL_SECONDS)
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
    assert handler.stop_calls == 1
    assert "did not stop" not in caplog.text
    assert "Watch stopped for project: /repo/example" in caplog.text
    assert manager.watch_thread is None
    assert manager.watch_handler is None
    assert manager.project_path is None
    assert manager.start_time is None
    assert not manager.is_running()


class _TracingLock:
    """The manager's state lock, signalling when a named thread releases it;
    a gated thread signals its arrival and waits for its gate to acquire."""

    def __init__(
        self, thread_names: Tuple[str, ...], gated: Tuple[str, ...] = ()
    ) -> None:
        self._lock = threading.Lock()
        self.released = {name: threading.Event() for name in thread_names}
        self.arrived = {name: threading.Event() for name in gated}
        self.gates = {name: threading.Event() for name in gated}

    def __enter__(self) -> "_TracingLock":
        name = threading.current_thread().name
        if name in self.gates:
            self.arrived[name].set()
            self.gates[name].wait(timeout=_HANDLER_WAIT_SECONDS)
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()
        event = self.released.get(threading.current_thread().name)
        if event is not None:
            event.set()


class _HeldWorkerManager(DaemonWatchManager):
    """The held watch's worker stays inside its handler's start_watching()
    until the test releases it: the worker is alive, its handler still in
    the manager's state, so the first stop stays inside its join."""

    def __init__(self) -> None:
        super().__init__()
        self.handlers: List[_FakeHandler] = []
        self.release_worker = threading.Event()
        self.tracing = _TracingLock(
            ("first-stopper", "second-stopper"), gated=("late-stopper",)
        )
        self._lock = cast(Any, self.tracing)

    def _create_watch_handler(self, project_path, config, mutation_lock=None, **kw):
        hold = self.release_worker if project_path == "/repo/held" else None
        handler = _FakeHandler(hold)
        self.handlers.append(handler)
        return handler


def _handler_at(manager: _HeldWorkerManager, index: int) -> _FakeHandler:
    deadline = time.monotonic() + _HANDLER_WAIT_SECONDS
    while time.monotonic() < deadline:
        if len(manager.handlers) > index:
            return manager.handlers[index]
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"watch thread never built handler #{index}")


def _stop_on_thread(
    manager: DaemonWatchManager, name: str, results: Dict[str, Any]
) -> threading.Thread:
    def _stop() -> None:
        results[name] = manager.stop_watch()

    thread = threading.Thread(target=_stop, name=name, daemon=True)
    thread.start()
    return thread


def _first_stop_in_its_join(
    manager: _HeldWorkerManager, results: Dict[str, Any]
) -> threading.Thread:
    assert manager.start_watch("/repo/held", config=None)["status"] == "success"
    _wait_until_watching(manager, _handler_at(manager, 0))
    first = _stop_on_thread(manager, "first-stopper", results)
    assert manager.tracing.released["first-stopper"].wait(_HANDLER_WAIT_SECONDS)
    return first


def _finish(manager: _HeldWorkerManager, *threads: threading.Thread) -> None:
    manager.release_worker.set()
    for thread in threads:
        thread.join(timeout=_HANDLER_WAIT_SECONDS * 2)
        assert not thread.is_alive(), f"{thread.name} never returned"


@pytest.mark.timeout(30)
def test_concurrent_stops_stop_the_handler_once() -> None:
    manager = _HeldWorkerManager()
    results: Dict[str, Any] = {}
    first = _first_stop_in_its_join(manager, results)

    second = _stop_on_thread(manager, "second-stopper", results)
    assert manager.tracing.released["second-stopper"].wait(_HANDLER_WAIT_SECONDS)
    stop_calls_while_joining = manager.handlers[0].stop_calls
    _finish(manager, first, second)

    assert stop_calls_while_joining == 1
    assert manager.handlers[0].stop_calls == 1
    assert results["first-stopper"]["status"] == "success", results
    assert results["second-stopper"] == results["first-stopper"]
    assert not manager.is_running()


@pytest.mark.timeout(30)
def test_stats_during_a_stop_report_stopping() -> None:
    manager = _HeldWorkerManager()
    results: Dict[str, Any] = {}
    first = _first_stop_in_its_join(manager, results)

    stats = manager.get_stats()
    _finish(manager, first)

    assert stats["status"] == "stopping", stats
    assert stats["project_path"] == "/repo/held"
    assert results["first-stopper"]["status"] == "success", results
    assert manager.get_stats()["status"] == "idle"


@pytest.mark.timeout(30)
def test_start_during_a_stop_is_refused() -> None:
    manager = _HeldWorkerManager()
    results: Dict[str, Any] = {}
    first = _first_stop_in_its_join(manager, results)

    started = manager.start_watch("/repo/other", config=None)
    _finish(manager, first)

    assert started["status"] == "error", started
    assert "stopping" in started["message"]
    assert len(manager.handlers) == 1
    assert results["first-stopper"]["status"] == "success", results
    assert not manager.is_running()
    assert manager.start_watch("/repo/other", config=None)["status"] == "success"
    _wait_until_watching(manager, _handler_at(manager, 1))
    assert manager.stop_watch()["status"] == "success"


@pytest.mark.timeout(30)
def test_stop_reaching_the_lock_after_the_first_completed_gets_its_result() -> None:
    manager = _HeldWorkerManager()
    results: Dict[str, Any] = {}
    first = _first_stop_in_its_join(manager, results)

    late = _stop_on_thread(manager, "late-stopper", results)
    assert manager.tracing.arrived["late-stopper"].wait(_HANDLER_WAIT_SECONDS)
    _finish(manager, first)
    manager.tracing.gates["late-stopper"].set()
    late.join(timeout=_HANDLER_WAIT_SECONDS)
    assert not late.is_alive()

    assert results["first-stopper"]["status"] == "success", results
    assert results["late-stopper"] == results["first-stopper"]
    assert manager.handlers[0].stop_calls == 1
    sequential = manager.stop_watch()
    assert sequential == {"status": "error", "message": "Watch not running"}


@pytest.mark.timeout(30)
def test_status_rpc_during_a_stop_reports_stopping() -> None:
    from code_indexer.daemon.service import CIDXDaemonService

    service = CIDXDaemonService()
    try:
        manager = _HeldWorkerManager()
        service.watch_manager = manager
        results: Dict[str, Any] = {}
        first = _first_stop_in_its_join(manager, results)

        status = service.exposed_watch_status()
        _finish(manager, first)

        assert status["running"] is False, status
        assert status["stopping"] is True
        assert status["project_path"] == "/repo/held"
        assert results["first-stopper"]["status"] == "success", results
        assert service.exposed_watch_status() == {
            "running": False,
            "project_path": None,
        }
    finally:
        service.eviction_thread.stop()
        service.eviction_thread.join(timeout=1)
