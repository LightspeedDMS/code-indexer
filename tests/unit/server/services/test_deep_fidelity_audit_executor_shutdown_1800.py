"""Bug #1800: the deep-fidelity audit pool must have a real shutdown.

Second pool of the same defect class as the discovery branch-fetch pool: a
module-level ``ThreadPoolExecutor`` created with no disposal anywhere in the
codebase. Its workers are non-daemon, so once anything has been submitted they
are joined by ``concurrent.futures.thread._python_exit`` inside
``threading._shutdown()`` before the interpreter may exit -- process exit waits
on whatever the pool is running.

This one is sharper than the discovery pool because it lives in ``storage/``,
on the CLI/solo path as well as the server's, so it gates ``cidx`` process exit
too and not only the server test gate.
"""

import ast
import inspect
import threading
import time

import pytest

from code_indexer.storage import filesystem_vector_store as fvs

# Bounded settle window (Messi Rule #14): a test about hangs must not hang.
_WORKER_EXIT_TIMEOUT_SECONDS = 10.0
_WORKER_EXIT_POLL_SECONDS = 0.02

_SHUTDOWN_FUNCTION_NAME = "shutdown_deep_fidelity_audit_executor"
_THREAD_NAME_PREFIX = "cidx-deep-audit"


def _audit_worker_threads() -> list:
    """Return live threads named for the deep-fidelity audit pool."""
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and t.name.startswith(_THREAD_NAME_PREFIX)
    ]


def _wait_for_audit_workers_to_exit() -> list:
    """Poll until the pool's workers are gone, or the bounded window expires."""
    deadline = time.monotonic() + _WORKER_EXIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        survivors = _audit_worker_threads()
        if not survivors:
            return []
        time.sleep(_WORKER_EXIT_POLL_SECONDS)
    return _audit_worker_threads()


def _spawn_audit_workers() -> None:
    """Force the pool to actually create its worker threads."""
    width = fvs._DEEP_FIDELITY_AUDIT_EXECUTOR_MAX_WORKERS
    barrier = threading.Barrier(width + 1)
    for _ in range(width):
        fvs._deep_fidelity_audit_executor.submit(
            barrier.wait, _WORKER_EXIT_TIMEOUT_SECONDS
        )
    barrier.wait(_WORKER_EXIT_TIMEOUT_SECONDS)


def _retire_audit_pool(when: str) -> None:
    """Dispose the pool and fail loudly if any worker refuses to exit."""
    fvs.shutdown_deep_fidelity_audit_executor()
    survivors = _wait_for_audit_workers_to_exit()
    assert not survivors, (
        f"{[t.name for t in survivors]} still alive at {when}; a surviving "
        "audit worker contaminates later tests and blocks interpreter exit"
    )


@pytest.fixture(autouse=True)
def _fresh_audit_pool():
    """Retire the process-wide audit pool before and after every test."""
    _retire_audit_pool("test setup")
    yield
    _retire_audit_pool("test teardown")


class TestAuditWorkersGateInterpreterExit:
    """Document the hazard the shutdown function exists to remove."""

    def test_every_audit_worker_is_non_daemon(self):
        _spawn_audit_workers()
        workers = _audit_worker_threads()
        assert workers, "pool created no worker threads to inspect"

        daemonic = [t.name for t in workers if t.daemon]
        assert not daemonic, (
            f"expected every audit worker to be non-daemon, but {daemonic} "
            "were not -- the hazard is that interpreter exit must join them"
        )


class TestShutdownRetiresTheAuditPool:
    """The fix: disposal actually retires the threads, and leaves it usable."""

    def test_shutdown_retires_every_worker_thread(self):
        _spawn_audit_workers()
        assert _audit_worker_threads(), "precondition: pool has live workers"

        fvs.shutdown_deep_fidelity_audit_executor()

        survivors = _wait_for_audit_workers_to_exit()
        assert not survivors, (
            f"shutdown left {[t.name for t in survivors]} alive; interpreter "
            "exit would still have to join them"
        )

    def test_module_attribute_still_holds_a_usable_executor(self):
        _spawn_audit_workers()

        fvs.shutdown_deep_fidelity_audit_executor()

        result = fvs._deep_fidelity_audit_executor.submit(lambda: "ok").result(
            timeout=_WORKER_EXIT_TIMEOUT_SECONDS
        )
        assert result == "ok", (
            "the module attribute must keep pointing at a live executor -- the "
            "Story #1822 tests monkeypatch it by name and the audit submit "
            "site reads it directly"
        )


class TestShutdownIsIdempotent:
    """Disposal must be safe to repeat, including before any submit."""

    def test_repeated_shutdown_without_any_submit_is_safe(self):
        fvs.shutdown_deep_fidelity_audit_executor()
        fvs.shutdown_deep_fidelity_audit_executor()

        assert not _audit_worker_threads()


def _lifespan_shutdown_calls() -> list:
    """Return line numbers of real post-yield calls to the audit shutdown.

    Parses ``lifespan.py`` rather than matching text, so a comment or docstring
    cannot satisfy the assertion -- only a genuine ``Call`` node on the
    post-``yield`` shutdown half of ``lifespan``.
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
    """An unwired shutdown would leave production restarts stalling."""

    def test_lifespan_shutdown_path_calls_the_audit_pool_shutdown(self):
        assert _lifespan_shutdown_calls(), (
            "lifespan's shutdown path never calls "
            f"{_SHUTDOWN_FUNCTION_NAME}(), so a server shutdown would block "
            "joining the audit pool's workers"
        )
