"""Pytest configuration for the cidx-xray engine tests.

The autouse fixture repairs the main thread's event loop after every test
(Bug #1817): the engine's sync-over-async helper falls back to
``asyncio.run()``, which clears the loop and would otherwise break whatever
runs next in the same process. Shared with the core's X-Ray integration tests
through ``cidx_xray.testing``.
"""

from typing import Iterator

import pytest

from cidx_xray.testing import reset_main_thread_event_loop


@pytest.fixture(scope="function", autouse=True)
def cleanup_event_loop() -> Iterator[None]:
    """Leave the main thread a fresh, usable event loop after each test."""
    yield
    reset_main_thread_event_loop()
