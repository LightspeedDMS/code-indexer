"""Sampler thread lifecycle and process-level singleton tests for MemoryGovernor.

Tests: thread starts/stops cleanly, double-start idempotent,
       get/set/clear singleton returns correct values.
"""

from __future__ import annotations

import time

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    DEFAULT_HOST_TOTAL,
    DEFAULT_HOST_USED,
    FAST_SAMPLE_INTERVAL,
    FAST_SAMPLE_WAIT,
    TEST_STOP_TIMEOUT,
    FakeMemoryReaders,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture(autouse=True)
def _clear_singleton():
    """Reset the process-level singleton before and after every test."""
    try:
        from code_indexer.server.services.memory_governor import clear_memory_governor

        clear_memory_governor()
    except ImportError:
        pass
    yield
    try:
        from code_indexer.server.services.memory_governor import clear_memory_governor

        clear_memory_governor()
    except ImportError:
        pass


def _default_readers() -> FakeMemoryReaders:
    return FakeMemoryReaders(
        host_total=DEFAULT_HOST_TOTAL,
        host_used=DEFAULT_HOST_USED,
    )


class TestSamplerThreadLifecycle:
    """Sampler thread must start, sample, and stop cleanly without leaking."""

    def test_starts_and_stops(self, MemoryGovernor):
        gov = MemoryGovernor(
            readers=_default_readers(),
            enabled=True,
            start_sampler=True,
            sample_interval_seconds=FAST_SAMPLE_INTERVAL,
        )
        gov.start()
        assert gov.is_running()

        time.sleep(FAST_SAMPLE_WAIT)
        gov.stop(timeout=TEST_STOP_TIMEOUT)

        assert not gov.is_running()
        assert gov._sampler_thread is None or not gov._sampler_thread.is_alive()

    def test_stop_without_start_is_safe(self, MemoryGovernor):
        """stop() before start() must not raise."""
        gov = MemoryGovernor(
            readers=_default_readers(),
            enabled=True,
            start_sampler=False,
        )
        gov.stop(timeout=TEST_STOP_TIMEOUT)  # must not raise

    def test_double_start_is_idempotent(self, MemoryGovernor):
        """Calling start() twice must not spawn an extra thread."""
        gov = MemoryGovernor(
            readers=_default_readers(),
            enabled=True,
            start_sampler=True,
            sample_interval_seconds=FAST_SAMPLE_INTERVAL,
        )
        gov.start()
        thread_before = gov._sampler_thread
        gov.start()
        thread_after = gov._sampler_thread

        assert thread_before is thread_after
        gov.stop(timeout=TEST_STOP_TIMEOUT)


class TestGovernorSingleton:
    """Process-level get/set/clear singleton — None before set, set after set."""

    def test_returns_none_before_set(self):
        from code_indexer.server.services.memory_governor import get_memory_governor

        assert get_memory_governor() is None

    def test_returns_governor_after_set(self, MemoryGovernor):
        from code_indexer.server.services.memory_governor import (
            get_memory_governor,
            set_memory_governor,
        )

        gov = MemoryGovernor(
            readers=_default_readers(),
            enabled=True,
            start_sampler=False,
        )
        set_memory_governor(gov)
        assert get_memory_governor() is gov

    def test_clear_sets_to_none(self, MemoryGovernor):
        from code_indexer.server.services.memory_governor import (
            clear_memory_governor,
            get_memory_governor,
            set_memory_governor,
        )

        gov = MemoryGovernor(
            readers=_default_readers(),
            enabled=True,
            start_sampler=False,
        )
        set_memory_governor(gov)
        clear_memory_governor()
        assert get_memory_governor() is None
