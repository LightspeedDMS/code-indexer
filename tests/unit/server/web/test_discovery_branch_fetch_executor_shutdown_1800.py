"""Bug #1800: the discovery branch-fetch pool must have a real shutdown.

``_get_discovery_branch_fetch_executor()`` hands out a process-wide
``ThreadPoolExecutor`` that was documented as "never shut down, because it
lives exactly as long as the process". That premise is wrong: its workers are
non-daemon threads registered in
``concurrent.futures.thread._threads_queues``, and ``_python_exit`` -- which
runs INSIDE ``threading._shutdown()`` because it is registered via
``threading._register_atexit`` -- ``join()``s every one of them before the
interpreter may exit. Process exit therefore waits for whatever the pool is
running, with no timeout anywhere.

That is how ``server-fast-automation.sh`` chunk 6 printed its full pass summary
and then wedged: the tests were over, but the interpreter could not die.
"""

import ast
import inspect
import threading
import time
from concurrent.futures import thread as cf_thread

import pytest

from code_indexer.server.web import routes as routes_module

# Bounded settle window (Messi Rule #14): workers exit promptly on the shutdown
# sentinel, but not synchronously. Never wait unbounded for a thread to die.
_WORKER_EXIT_TIMEOUT_SECONDS = 10.0
_WORKER_EXIT_POLL_SECONDS = 0.02

_SHUTDOWN_FUNCTION_NAME = "shutdown_discovery_branch_fetch_executor"


def _pool_worker_threads() -> list:
    """Return live threads named for the discovery branch-fetch pool."""
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and t.name.startswith("discovery-branch-fetch")
    ]


def _wait_for_pool_workers_to_exit() -> list:
    """Poll until the pool's workers are gone, or the bounded window expires."""
    deadline = time.monotonic() + _WORKER_EXIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        survivors = _pool_worker_threads()
        if not survivors:
            return []
        time.sleep(_WORKER_EXIT_POLL_SECONDS)
    return _pool_worker_threads()


def _saturate_pool() -> None:
    """Force every worker thread in the pool to be created."""
    executor = routes_module._get_discovery_branch_fetch_executor()
    width = routes_module._DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY
    barrier = threading.Barrier(width + 1)
    for _ in range(width):
        executor.submit(barrier.wait, _WORKER_EXIT_TIMEOUT_SECONDS)
    barrier.wait(_WORKER_EXIT_TIMEOUT_SECONDS)


def _retire_pool(when: str) -> None:
    """Dispose the pool and fail loudly if any worker refuses to exit."""
    routes_module.shutdown_discovery_branch_fetch_executor()
    survivors = _wait_for_pool_workers_to_exit()
    assert not survivors, (
        f"{[t.name for t in survivors]} still alive at {when}; a surviving "
        "worker contaminates later tests and blocks interpreter exit"
    )


@pytest.fixture(autouse=True)
def _fresh_pool():
    """Give every test a disposed pool, and dispose it again afterwards.

    The pool is process-wide state, so without this a failing assertion would
    leak worker threads into the rest of the session -- precisely the condition
    under test.
    """
    _retire_pool("test setup")
    yield
    _retire_pool("test teardown")


class TestPoolWorkersGateInterpreterExit:
    """Document the hazard the shutdown function exists to remove."""

    def test_every_pool_worker_is_non_daemon_and_joined_at_interpreter_exit(self):
        _saturate_pool()
        workers = _pool_worker_threads()
        assert workers, "pool created no worker threads to inspect"

        daemonic = [t.name for t in workers if t.daemon]
        assert not daemonic, (
            f"expected every pool worker to be non-daemon, but {daemonic} were "
            "not -- the whole hazard is that interpreter exit must join them"
        )

        registered = set(cf_thread._threads_queues)
        unregistered = [t.name for t in workers if t not in registered]
        assert not unregistered, (
            f"{unregistered} are absent from "
            "concurrent.futures.thread._threads_queues; the premise of "
            "Bug #1800 no longer holds and this test needs revisiting"
        )


class TestShutdownRetiresThePool:
    """The fix: disposal actually retires the threads and drops the global."""

    def test_shutdown_retires_every_worker_thread(self):
        _saturate_pool()
        assert _pool_worker_threads(), "precondition: pool has live workers"

        routes_module.shutdown_discovery_branch_fetch_executor()

        survivors = _wait_for_pool_workers_to_exit()
        assert not survivors, (
            f"shutdown left {[t.name for t in survivors]} alive; interpreter "
            "exit would still have to join them"
        )

    def test_shutdown_clears_the_module_global(self):
        _saturate_pool()
        assert routes_module._DISCOVERY_BRANCH_FETCH_EXECUTOR is not None

        routes_module.shutdown_discovery_branch_fetch_executor()

        assert routes_module._DISCOVERY_BRANCH_FETCH_EXECUTOR is None, (
            "a shut-down executor must not stay bound to the global -- a later "
            "submit() would raise RuntimeError on a dead pool"
        )


class TestShutdownIdempotenceAndReuse:
    """Disposal must be safe to repeat and must not disable the pool forever."""

    def test_shutdown_is_idempotent_and_safe_when_never_created(self):
        assert routes_module._DISCOVERY_BRANCH_FETCH_EXECUTOR is None, (
            "precondition: no pool exists"
        )

        routes_module.shutdown_discovery_branch_fetch_executor()
        routes_module.shutdown_discovery_branch_fetch_executor()

        assert routes_module._DISCOVERY_BRANCH_FETCH_EXECUTOR is None

    def test_pool_is_usable_again_after_shutdown(self):
        _saturate_pool()
        routes_module.shutdown_discovery_branch_fetch_executor()

        revived = routes_module._get_discovery_branch_fetch_executor()

        result = revived.submit(lambda: "ok").result(
            timeout=_WORKER_EXIT_TIMEOUT_SECONDS
        )
        assert result == "ok", "shutdown must not permanently disable the pool"


def _lifespan_shutdown_calls() -> list:
    """Return line numbers of real calls to the pool shutdown after the yield.

    Parses ``lifespan.py`` rather than matching text, so a comment, a docstring
    or a stray mention cannot satisfy the assertion -- only an actual ``Call``
    node placed on the post-``yield`` shutdown half of ``lifespan``.
    """
    from code_indexer.server.startup import lifespan as lifespan_module

    tree = ast.parse(inspect.getsource(lifespan_module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "lifespan":
            continue
        yields = [y.lineno for y in ast.walk(node) if isinstance(y, ast.Yield)]
        if not yields:
            continue
        shutdown_begins = max(yields)
        return [
            call.lineno
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", getattr(call.func, "attr", None))
            == _SHUTDOWN_FUNCTION_NAME
            and call.lineno > shutdown_begins
        ]
    return []


class TestShutdownIsWiredIntoLifespan:
    """Bug #1800: an unwired shutdown would leave production restarts stalling."""

    def test_lifespan_shutdown_path_calls_the_pool_shutdown(self):
        assert _lifespan_shutdown_calls(), (
            "lifespan's shutdown path never calls "
            f"{_SHUTDOWN_FUNCTION_NAME}(), so a server shutdown would block "
            "joining the pool's workers -- the same defect as Bug #1800, in "
            "production instead of the test gate"
        )
