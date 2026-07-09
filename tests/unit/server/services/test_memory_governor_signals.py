"""Signal math tests for MemoryGovernor §3.1.

Covers: cgroup v2, cgroup v1, host fallback.
All tests drive _compute_sample() synchronously (start_sampler=False).
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    CGROUP_LIMIT_LARGER_THAN_HOST,
    CGROUP_V1_UNLIMITED_SENTINEL,
    DEFAULT_HOST_TOTAL,
    DEFAULT_HOST_USED,
    FIFTY_PCT,
    GB,
    HOST_8GB,
    HOST_16GB,
    PCT_DIVISOR,
    PCT_TOLERANCE_EXACT,
    PCT_TOLERANCE_ROUNDED,
    SIXTY_PCT,
    THIRTY_PCT,
    TWENTY_FIVE_PCT,
    USED_2GB,
    USED_4GB,
    FakeMemoryReaders,
    make_gov,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


class TestCgroupV2Math:
    """§3.1 cgroup v2: memory.max / memory.current math."""

    def test_fifty_pct_basis_and_fields(self, MemoryGovernor):
        """50% usage reports cgroup_v2 basis with correct fields."""
        limit = CGROUP_LIMIT_4GB
        used = int(limit * FIFTY_PCT / PCT_DIVISOR)
        readers = FakeMemoryReaders(
            cgroup_v2_max=str(limit), cgroup_v2_current=str(used)
        )
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "cgroup_v2"
        assert abs(sample.used_pct - FIFTY_PCT) < PCT_TOLERANCE_EXACT
        assert sample.effective_limit == limit
        assert sample.effective_used == used

    def test_max_string_falls_through_to_host(self, MemoryGovernor):
        """'max' in memory.max means unlimited; basis falls to host."""
        readers = FakeMemoryReaders(
            cgroup_v2_max="max",
            cgroup_v2_current=str(GB),
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
        )
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "host"
        assert sample.effective_limit == DEFAULT_HOST_TOTAL

    def test_cgroup_larger_than_host_uses_host_limit(self, MemoryGovernor):
        """effective_limit = min(host_total, cgroup_limit)."""
        readers = FakeMemoryReaders(
            cgroup_v2_max=str(CGROUP_LIMIT_LARGER_THAN_HOST),
            cgroup_v2_current=str(USED_4GB),
            host_total=HOST_8GB,
            host_used=USED_4GB,
        )
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.effective_limit == HOST_8GB
        assert sample.basis == "cgroup_v2"


class TestCgroupV1Math:
    """§3.1 cgroup v1: memory.limit_in_bytes / memory.usage_in_bytes math."""

    def test_sixty_pct_basis_and_fields(self, MemoryGovernor):
        limit = HOST_8GB
        used = int(limit * SIXTY_PCT / PCT_DIVISOR)
        readers = FakeMemoryReaders(
            cgroup_v1_limit=str(limit), cgroup_v1_usage=str(used)
        )
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "cgroup_v1"
        assert abs(sample.used_pct - SIXTY_PCT) < PCT_TOLERANCE_ROUNDED

    def test_huge_sentinel_falls_through_to_host(self, MemoryGovernor):
        """Sentinel >= 2^62 means no cgroup limit; falls to host."""
        readers = FakeMemoryReaders(
            cgroup_v1_limit=str(CGROUP_V1_UNLIMITED_SENTINEL),
            cgroup_v1_usage=str(GB),
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
        )
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "host"
        assert sample.effective_limit == DEFAULT_HOST_TOTAL

    def test_not_present_falls_through_to_host(self, MemoryGovernor):
        readers = FakeMemoryReaders(host_total=HOST_16GB, host_used=USED_4GB)
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "host"
        assert abs(sample.used_pct - TWENTY_FIVE_PCT) < PCT_TOLERANCE_ROUNDED


class TestHostFallback:
    """§3.1 host (psutil) fallback when no cgroup limit is present."""

    def test_thirty_pct(self, MemoryGovernor):
        used = int(HOST_16GB * THIRTY_PCT / PCT_DIVISOR)
        readers = FakeMemoryReaders(host_total=HOST_16GB, host_used=used)
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.basis == "host"
        assert abs(sample.used_pct - THIRTY_PCT) < PCT_TOLERANCE_ROUNDED

    def test_effective_fields_match_input(self, MemoryGovernor):
        readers = FakeMemoryReaders(host_total=HOST_8GB, host_used=USED_2GB)
        sample = make_gov(readers, MemoryGovernor)._compute_sample()

        assert sample.effective_used == USED_2GB
        assert sample.effective_limit == HOST_8GB
