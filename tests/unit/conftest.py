"""pytest fixtures for the cidx-curl.sh wrapper test suite (Story #929 Item #2a).

Placing fixtures here (conftest.py) allows pytest to discover them
automatically for all test files in tests/unit/, including the aggregator
module test_cidx_curl_wrapper.py that imports test classes from sibling files.

Note: _CFG.curl_timeout is not used here — it belongs in the _run/_run_no_curl
helper functions that actually invoke subprocesses.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest

from tests.unit.test_cidx_curl_wrapper_helpers import _CFG


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
