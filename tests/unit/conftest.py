"""pytest fixtures for the cidx-curl.sh wrapper test suite (Story #929 Item #2a).

Placing fixtures here (conftest.py) allows pytest to discover them
automatically for all test files in tests/unit/, including the aggregator
module test_cidx_curl_wrapper.py that imports test classes from sibling files.

Note: _CFG.curl_timeout is not used here — it belongs in the _run/_run_no_curl
helper functions that actually invoke subprocesses.
"""

import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.unit.test_cidx_curl_wrapper_helpers import _CFG

# Bug #1800: bounded settle window for disposed non-daemon threads to exit.
# Bounded on purpose (Messi Rule #14) -- a guard against hangs must not hang.
_THREAD_EXIT_TIMEOUT_SECONDS = 10.0
_THREAD_EXIT_POLL_SECONDS = 0.02


@pytest.fixture
def isolated_config():
    """Yield (config_path, env) where CIDX_SERVER_DATA_DIR points to a temp dir.

    The temp dir is created fresh for each test and torn down afterwards,
    ensuring no config state leaks between tests.
    """
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        config_path = td_path / "config.json"
        env = os.environ.copy()
        env["CIDX_SERVER_DATA_DIR"] = str(td_path)
        yield config_path, env


@pytest.fixture
def fake_curl_env(tmp_path, isolated_config):
    """Yield (env_with_fake, config_path) with a fake curl intercept on PATH.

    The fake curl prints each argument on a separate line as 'ARG=<arg>',
    then exits 0. Tests that need to inspect curl invocation args use this
    fixture to intercept the exec'd curl without making real network calls.
    Uses _CFG.fake_curl_shebang for the script header so the shebang is
    configurable from the central WrapperTestConfig.
    PATH is built with os.pathsep and empty segments are omitted to avoid
    implicitly adding the current working directory to the lookup path.
    """
    config_path, env = isolated_config
    fake_curl_dir = tmp_path / "fake-bin"
    fake_curl_dir.mkdir()
    fake_curl = fake_curl_dir / "curl"
    fake_curl.write_text(
        f'{_CFG.fake_curl_shebang}\nfor arg in "$@"; do echo "ARG=$arg"; done\nexit 0\n'
    )
    fake_curl.chmod(0o755)
    env_with_fake = env.copy()
    env_with_fake["PATH"] = os.pathsep.join(
        p for p in (str(fake_curl_dir), env.get("PATH", "")) if p
    )
    yield env_with_fake, config_path


def _leaked_identity_queue_handlers(logger: logging.Logger, pre_existing: list) -> list:
    """Return IdentityQueueHandler instances on ``logger`` not in ``pre_existing``.

    Bug #1820: a test that pairs ``async_logging.install_queue_logging()``
    with a bare ``listener.stop()`` (instead of
    ``async_logging.shutdown_queue_logging()``) leaves an
    ``IdentityQueueHandler`` permanently attached to whatever logger it was
    installed on, pointed at a queue nothing drains anymore. Only handlers
    NEWLY added during the test (not already present before it ran) are the
    test's own responsibility to detach.
    """
    from code_indexer.server.services.async_logging import IdentityQueueHandler

    return [
        h
        for h in logger.handlers
        if isinstance(h, IdentityQueueHandler) and h not in pre_existing
    ]


@pytest.fixture(autouse=True)
def _guard_no_leaked_identity_queue_handler_1820():
    """Bug #1820 guard: fail loudly (and self-heal) on a leaked async-logging
    handler on the REAL process root logger.

    Mechanism (confirmed via py-spy + empirical reproduction, see
    tests/unit/test_conftest_leaked_identity_queue_handler_guard_1820.py):
    ``async_logging.install_queue_logging()`` attaches an
    ``IdentityQueueHandler`` to a logger and hands its real handlers to a
    ``QueueListener`` thread. A test that installs one on the process root
    logger and never detaches it (e.g. calling ``listener.stop()`` directly
    instead of ``async_logging.shutdown_queue_logging()``) leaves that
    handler permanently attached once the listener stops draining its queue.
    Every subsequent ``logger.x()`` call anywhere in the SAME pytest process
    that propagates to the root logger (the default) then enqueues into a
    queue nothing drains anymore. Once it fills to
    ``async_logging.DEFAULT_QUEUE_MAXSIZE`` (10,000), every later
    ERROR/CRITICAL log pays the full
    ``async_logging._HIGH_SEVERITY_QUEUE_TIMEOUT_S`` (2s) bounded blocking
    put -- turning the rest of the suite into a near-stall (Bug #1820: ~50
    minutes wall for 266 CPU-seconds).

    Placed in tests/unit/conftest.py (autouse, applies to the whole
    tests/unit/ tree, which covers both directories named in Bug #1820:
    tests/unit/server/services/ and tests/unit/services/) so a leak from
    EITHER directory is caught regardless of which one introduces it.

    Self-heals (removes the leaked handler) so one offending test cannot
    cascade into a 50-minute stall for the rest of the suite, but still
    fails that SPECIFIC test loudly -- the leak itself must still be fixed
    at its source, not silently tolerated.
    """
    root = logging.getLogger()
    pre_existing = list(root.handlers)
    yield
    leaked = _leaked_identity_queue_handlers(root, pre_existing)
    if leaked:
        for handler in leaked:
            root.removeHandler(handler)
        raise AssertionError(
            f"Bug #1820 guard: this test leaked {len(leaked)} "
            "IdentityQueueHandler(s) onto the process root logger without "
            "detaching them on teardown (e.g. via "
            "async_logging.install_queue_logging() + a bare "
            "listener.stop() instead of "
            "async_logging.shutdown_queue_logging()). Removed them to "
            "protect the rest of the suite from a cascading queue-full "
            "stall, but the leak must be fixed at its source."
        )


def _surviving_non_daemon_threads(baseline_ids: set) -> list:
    """Return alive non-daemon threads started during the session (Bug #1800).

    Interpreter exit joins every one of these before the process may exit, so
    each survivor is time the process must wait out at teardown. Remediation is
    to give the owning component an explicit shutdown and call it.
    """
    main = threading.main_thread()
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and not t.daemon and t is not main and id(t) not in baseline_ids
    ]


def _dispose_process_wide_pools() -> None:
    """Dispose process-wide pools this session created but never shut down.

    The server disposes these from its lifespan shutdown; a test session has no
    lifespan, so it is the owner here and must do the same, through the same
    production entry point. Looked up in ``sys.modules`` rather than imported,
    so a session that never touched a module does not pay to import it here.
    """
    routes = sys.modules.get("code_indexer.server.web.routes")
    if routes is not None:
        routes.shutdown_discovery_branch_fetch_executor()

    fvs = sys.modules.get("code_indexer.storage.filesystem_vector_store")
    if fvs is not None:
        fvs.shutdown_deep_fidelity_audit_executor()

    # Already correct in production -- lifespan calls this same reset on
    # shutdown. Only the test session, which runs no lifespan, was leaving the
    # global query-dispatch pool's workers alive.
    pqe = sys.modules.get("code_indexer.server.query.parallel_query_executor")
    if pqe is not None:
        pqe.reset_global_parallel_query_executor()


def _settle_non_daemon_threads(baseline_ids: set) -> list:
    """Wait, bounded, for disposed threads to exit; return whatever survives."""
    deadline = time.monotonic() + _THREAD_EXIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        survivors = _surviving_non_daemon_threads(baseline_ids)
        if not survivors:
            return []
        time.sleep(_THREAD_EXIT_POLL_SECONDS)
    return _surviving_non_daemon_threads(baseline_ids)


@pytest.fixture(scope="session", autouse=True)
def _guard_no_leaked_non_daemon_threads_1800():
    """Bug #1800 guard: fail if a non-daemon thread outlives the test session.

    Such a leak is invisible per-test -- every test passes and only the
    interpreter refuses to die. Cannot self-heal, since a running thread cannot
    be killed from outside. Remediation: give the owning component an explicit
    shutdown and call it.
    """
    baseline_ids = {id(t) for t in threading.enumerate()}
    yield
    _dispose_process_wide_pools()
    leaked = _settle_non_daemon_threads(baseline_ids)
    if leaked:
        described = ", ".join(f"{t.name!r} (class={type(t).__name__})" for t in leaked)
        raise AssertionError(
            f"Bug #1800 guard: {len(leaked)} non-daemon thread(s) outlived the "
            f"test session: {described}. Interpreter exit must join every one "
            "of these, so the process blocks at exit for as long as they run "
            "-- forever if they never return."
        )


def _leaked_temporal_watch_polling_threads(pre_existing_ids: set) -> list:
    """Return alive threads matching TemporalWatchHandler's polling-thread
    name that were NOT already running before the test.

    Bug #1825: TemporalWatchHandler._start_polling_thread() used to run
    `while True: time.sleep(5); ...subprocess.run(["git", "rev-parse",
    "HEAD"], ...)...` with no stop mechanism anywhere in the class. Any
    test that triggered the polling fallback without calling the (now
    added) stop() method leaked this thread for the rest of the pytest
    process's life -- it periodically spawns a real `git rev-parse HEAD`
    subprocess call that can land inside an UNRELATED test's exact
    subprocess-call-count assertion elsewhere in the suite (this was the
    actual root cause of the intermittent +1/+2 failures in
    tests/unit/services/test_reconcile_batch_content_id_1505.py).
    """
    from code_indexer.cli_temporal_watch_handler import _POLLING_THREAD_NAME

    return [
        t
        for t in threading.enumerate()
        if t.is_alive()
        and t.name.startswith(_POLLING_THREAD_NAME)
        and id(t) not in pre_existing_ids
    ]


@pytest.fixture(autouse=True)
def _guard_no_leaked_temporal_watch_polling_thread_1825():
    """Bug #1825 guard: fail loudly if a test leaks a real
    TemporalWatchHandler polling thread without stopping it.

    Placed in tests/unit/conftest.py (autouse, applies to the whole
    tests/unit/ tree) so a leak from ANY test constructing a
    TemporalWatchHandler is caught regardless of which file introduces it
    -- the original bug's leak came from four different tests inside a
    single file, each incidentally missing a matching `.git/refs/heads/`
    setup.

    Unlike the #1820 IdentityQueueHandler guard above, this CANNOT
    self-heal: a running Python thread cannot be force-killed from the
    outside, only the handler's own stop_event (which this guard has no
    reference to) can signal it to exit cleanly. The thread is
    `daemon=True` so it will never block process exit, but it WILL keep
    firing a real subprocess call every poll interval for the rest of this
    pytest session unless the offending test is fixed. Failing loudly at
    the exact test that introduced the leak is still a major improvement
    over the alternative -- silent cross-test pollution discovered only
    much later, in an unrelated test's exact-call-count assertion, which is
    exactly how Bug #1825 itself was found.
    """
    pre_existing_ids = {id(t) for t in threading.enumerate()}
    yield
    leaked = _leaked_temporal_watch_polling_threads(pre_existing_ids)
    if leaked:
        raise AssertionError(
            f"Bug #1825 guard: this test leaked {len(leaked)} "
            "TemporalWatchHandler polling thread(s) without calling "
            "stop() on the handler that owns it (or, if the test isn't "
            "actually testing polling behavior, without giving the "
            "handler a matching .git/refs/heads/<branch> file so it never "
            "enters the polling fallback at all). This thread will keep "
            "spawning a real `git rev-parse HEAD` subprocess call every "
            "poll interval for the rest of this pytest session, which can "
            "corrupt exact subprocess-call-count assertions in unrelated "
            "tests elsewhere in the suite."
        )
