"""The tree-wide teardown stops service threads a test leaves running.

``create_app()`` starts a ``memory-governor-sampler`` and a
``DependencyLatencyTracker-writer`` thread; only the app's lifespan shutdown
stops them. Tests that build an app without running its lifespan (or start
these services directly) used to leave both threads alive for the rest of
the pytest process -- hundreds per gate worker. The autouse teardown in
``tests/unit/server/conftest.py`` stops every such thread's owner through
its real stop API (except the installed process singletons, which later
code may still read) and fails the test when one it started survives.

These tests drive the teardown generator directly, like
``test_background_job_manager_universal_teardown_1635.py`` does.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from code_indexer.server.services import memory_governor as governor_module
from code_indexer.server.services.dependency_latency_tracker import (
    DependencyLatencyTracker,
)
from code_indexer.server.services.memory_governor import MemoryGovernor
from code_indexer.server.storage.dependency_latency_backend import (
    DependencyLatencyBackend,
)
from tests.unit.server.conftest import (
    GOVERNOR_THREAD,
    TRACKER_THREAD,
    _stop_leaked_service_threads_impl,
)


def _run_teardown_around(body: Any) -> None:
    gen = _stop_leaked_service_threads_impl()
    next(gen)
    body()
    with pytest.raises(StopIteration):
        next(gen)


def _governor() -> MemoryGovernor:
    return MemoryGovernor(sample_interval_seconds=60.0)


@pytest.fixture
def restore_governor_singleton() -> Iterator[None]:
    previous = governor_module.get_memory_governor()
    yield
    current = governor_module.get_memory_governor()
    if current is not None and current is not previous:
        current.stop()
    if previous is None:
        governor_module.clear_memory_governor()
    else:
        governor_module.set_memory_governor(previous)


def test_a_started_governor_is_stopped(restore_governor_singleton: None) -> None:
    governor = _governor()
    _run_teardown_around(governor.start)
    assert not governor.is_running()


def test_a_started_latency_tracker_is_shut_down(tmp_path: Path) -> None:
    backend = DependencyLatencyBackend(str(tmp_path / "latency.db"))
    tracker = DependencyLatencyTracker(backend=backend, node_id="node-a")
    _run_teardown_around(tracker.start)
    names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert tracker._writer_thread is not None
    assert not tracker._writer_thread.is_alive(), names


def test_the_installed_singleton_governor_keeps_running(
    restore_governor_singleton: None,
) -> None:
    governor = _governor()

    def install() -> None:
        governor_module.set_memory_governor(governor)
        governor.start()

    _run_teardown_around(install)
    assert governor.is_running()


def test_a_guarded_thread_that_cannot_be_stopped_fails_the_test() -> None:
    release = threading.Event()
    gen = _stop_leaked_service_threads_impl()
    next(gen)
    stray = threading.Thread(
        target=release.wait, args=(30,), name=GOVERNOR_THREAD, daemon=True
    )
    stray.start()
    try:
        with pytest.raises(pytest.fail.Exception, match=GOVERNOR_THREAD):
            next(gen)
    finally:
        release.set()
        stray.join(timeout=10)


def test_guarded_names_match_the_production_threads() -> None:
    assert GOVERNOR_THREAD == "memory-governor-sampler"
    assert TRACKER_THREAD == "DependencyLatencyTracker-writer"
