"""Fail-safe RED tests for MemoryGovernor §3.2 (Anti-Fallback rule).

FAIL-SAFE CONTRACT: band == RED before first sample and on any reader exception.
NEVER default to GREEN on error — that is the silent-failure anti-pattern.
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    DEFAULT_HOST_TOTAL,
    DEFAULT_HOST_USED,
    FakeMemoryReaders,
    make_gov,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def MemoryBand():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryBand as _MB

    return _MB


class _BrokenReaders:
    """All reader methods raise RuntimeError — simulates total I/O failure."""

    def read_cgroup_v2_max(self) -> str:
        raise RuntimeError("disk error")

    def read_cgroup_v1_limit(self) -> int:
        raise RuntimeError("disk error")

    def read_host_memory(self) -> object:
        raise RuntimeError("psutil failure")

    def read_pswpin(self) -> int:
        return 0


class TestFailSafeRed:
    """§3.2 Anti-Fallback — band is RED on any error or before first sample."""

    def test_pre_first_sample_is_red(self, MemoryGovernor, MemoryBand):
        """Band must be RED immediately after construction, before any _tick()."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
        )
        gov = MemoryGovernor(readers=readers, enabled=True, start_sampler=False)
        assert gov.band == MemoryBand.RED

    def test_reader_exception_keeps_red(self, MemoryGovernor, MemoryBand):
        """Broken reader on first _tick() must leave band at RED."""
        gov = MemoryGovernor(
            readers=_BrokenReaders(),
            enabled=True,
            start_sampler=False,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

    def test_reader_exception_after_green_reverts_to_red(
        self, MemoryGovernor, MemoryBand
    ):
        """Reader works once (GREEN), then breaks — band must revert to RED."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
        )
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        # Break the host reader in-place
        def _broken() -> object:
            raise RuntimeError("psutil crash")

        readers.read_host_memory = _broken  # type: ignore[method-assign]
        gov._tick()
        assert gov.band == MemoryBand.RED

    def test_disabled_governor_always_evicts(self, MemoryGovernor, MemoryBand):
        """enabled=False => should_evict_after_shard() always True (safe default)."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
        )
        gov = MemoryGovernor(readers=readers, enabled=False, start_sampler=False)
        gov._tick()
        assert gov.should_evict_after_shard() is True
