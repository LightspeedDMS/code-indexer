"""Pytest configuration for tests/unit/xray/ -- event loop cleanup.

Bug #1817: several xray tests drive an async call through
``XRaySearchEngine``'s ``_run_async_in_sync`` helper
(``src/code_indexer/xray/search_engine.py``). When no event loop is already
running on the calling (pytest main) thread, that helper falls back to
``asyncio.run(coro)``. CPython's ``asyncio.run()`` unconditionally clears the
calling thread's event loop in its own ``finally`` block once the coroutine
completes -- regardless of what the loop looked like before it was called.

Left uncleaned, that state survives past the end of ``tests/unit/xray/``'s
own test run and poisons whatever runs next in the same pytest process: the
deprecated ``asyncio.get_event_loop()`` API only auto-creates a fresh loop
the very first time it is ever called on a thread, so once any earlier
``asyncio.run()`` call has cleared the loop, later callers of
``asyncio.get_event_loop()`` raise ``RuntimeError: There is no current event
loop in thread 'MainThread'.`` -- exactly what happened to 16 tests under
``tests/unit/server/mcp/`` in the combined selection
``pytest tests/unit/xray/ tests/unit/server/mcp/``.

This mirrors the project's existing precedent for this exact class of bug:
``tests/unit/remote/conftest.py``'s ``cleanup_event_loop`` fixture.
"""

import asyncio

import pytest


@pytest.fixture(scope="function", autouse=True)
def cleanup_event_loop():
    """Ensure the main thread has a fresh, usable event loop after each xray
    test, regardless of whether the test (or code it called) ran
    asyncio.run() and left the loop cleared (Bug #1817)."""
    yield

    # Close any lingering event loop left behind by the test.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
    except RuntimeError:
        # No event loop exists (e.g. asyncio.run() already cleared it) --
        # this is precisely the state we are here to repair.
        pass

    # Always leave a fresh, open event loop set for whatever runs next.
    asyncio.set_event_loop(asyncio.new_event_loop())
