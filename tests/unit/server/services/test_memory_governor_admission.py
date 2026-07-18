"""MemoryGovernor.admission_allowed() gate tests.

Drives the governor synchronously via _tick() (start_sampler=False) using the
injectable FakeMemoryReaders, so cgroup used% and the resulting band are
deterministic — no real /sys/fs/cgroup or psutil I/O.
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


def _gov_at_used_pct(used_pct: float, MemoryGovernor):
    """Build a governor and tick it once so its band reflects `used_pct`."""
    limit = CGROUP_LIMIT_4GB
    current = int(limit * used_pct / 100.0)
    readers = FakeMemoryReaders(
        cgroup_v2_max=str(limit), cgroup_v2_current=str(current)
    )
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()
    return gov


class TestAdmissionAllowed:
    """admission_allowed(max_used_pct) — reactive cgroup-aware gate."""

    def test_blocks_before_first_sample_failsafe(self, MemoryGovernor):
        """No sample yet (band starts RED, _first_tick True) → never admit."""
        readers = FakeMemoryReaders(
            cgroup_v2_max=str(CGROUP_LIMIT_4GB),
            cgroup_v2_current=str(CGROUP_LIMIT_4GB // 100),  # ~1%, plenty of room
        )
        gov = make_gov(readers, MemoryGovernor)  # not ticked yet
        assert gov._first_tick is True
        # Even with a very high watermark and tiny usage, fail-safe blocks.
        assert gov.admission_allowed(99.0) is False

    def test_admits_when_green_and_below_watermark(self, MemoryGovernor):
        """GREEN band, used% below watermark → admit."""
        gov = _gov_at_used_pct(25.0, MemoryGovernor)
        from code_indexer.server.services.memory_governor import MemoryBand

        assert gov.band == MemoryBand.GREEN
        assert gov.admission_allowed(80.0) is True

    def test_blocks_when_used_at_or_above_watermark(self, MemoryGovernor):
        """used% >= watermark → block even though band is not RED."""
        gov = _gov_at_used_pct(72.0, MemoryGovernor)
        from code_indexer.server.services.memory_governor import MemoryBand

        # 72% is YELLOW (>=70 <85) — not RED, so only the watermark blocks it.
        assert gov.band == MemoryBand.YELLOW
        assert gov.admission_allowed(70.0) is False
        assert gov.admission_allowed(80.0) is True

    def test_blocks_when_band_red_regardless_of_watermark(self, MemoryGovernor):
        """RED band (>= red_pct) → block even if watermark is above used%."""
        gov = _gov_at_used_pct(90.0, MemoryGovernor)
        from code_indexer.server.services.memory_governor import MemoryBand

        assert gov.band == MemoryBand.RED
        # Watermark above the 90% usage, but RED still blocks (fail-safe).
        assert gov.admission_allowed(95.0) is False

    def test_watermark_is_percentage_of_cgroup_limit_not_host(self, MemoryGovernor):
        """The gate uses the cgroup used% basis (self-tuning to memory.max)."""
        # 4Gi limit, 50% used → GREEN, admit at 80, block at 40.
        gov = _gov_at_used_pct(50.0, MemoryGovernor)
        assert gov.admission_allowed(80.0) is True
        assert gov.admission_allowed(40.0) is False
