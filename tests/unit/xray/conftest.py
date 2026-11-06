"""Pytest configuration for tests/unit/xray/ -- event loop cleanup.

These are the core's X-Ray integration tests (X-Ray through the server's
wiring); the engine's own tests live with the feature, in
``packages/features/cidx-xray/tests``. Both trees share their helpers through
``cidx_xray.testing``.

Bug #1817: several xray tests drive an async call through
``XRaySearchEngine``'s ``_run_async_in_sync`` helper
(``cidx_xray.search_engine``). When no event loop is already running on the
calling (pytest main) thread, that helper falls back to ``asyncio.run(coro)``,
which clears the calling thread's event loop when it returns. Left uncleaned,
that state poisons whatever runs next in the same pytest process -- exactly
what happened to 16 tests under ``tests/unit/server/mcp/`` in the combined
selection ``pytest tests/unit/xray/ tests/unit/server/mcp/``. The autouse
fixture below repairs it after every test, as ``tests/unit/remote/conftest.py``
does for the same class of bug.
"""

from typing import Iterator

import pytest

from cidx_xray.testing import (
    placement_host_installed,
    require_xray_cli_binary as require_xray_cli_binary,
    reset_main_thread_event_loop,
)


@pytest.fixture(scope="function", autouse=True)
def cleanup_event_loop() -> Iterator[None]:
    """Leave the main thread a fresh, usable event loop after each test."""
    yield
    reset_main_thread_event_loop()


@pytest.fixture(scope="function", autouse=True)
def xray_placement_host() -> Iterator[None]:
    """The placement host the started xray feature installs: these tests
    drive X-Ray through the server's wiring without starting the feature."""
    with placement_host_installed():
        yield
