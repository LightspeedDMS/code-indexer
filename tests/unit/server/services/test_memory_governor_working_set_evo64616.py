"""EVO-64616: the memory governor measures the cgroup WORKING SET, not page
cache.

claude-code-indexer-5 on tools-test: memory.max 4 GiB, memory.current 3.87
GiB (97%), of which inactive_file 3.50 GiB -- page cache from index reads
over NFS that the kernel reclaims on demand -- and anon 317 MiB. Counting
memory.current as used kept the pod YELLOW/RED forever: LRU eviction and
malloc_trim every sample, the query caches thrown away, ~88k INFO lines a
day. Used is now usage minus inactive_file (the kubelet's working set), v2
and v1, floored at 0; the host fallback uses psutil's ``available``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.server.services import memory_governor as gov_module
from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor
from tests.unit.server.services.test_memory_governor_fixtures import (
    GB,
    FakeMemoryReaders,
    make_gov,
)

LIMIT = 4294967296
CURRENT = 4156919808
INACTIVE_FILE = 3760975872
ANON_HEAVY_CURRENT = int(LIMIT * 0.95)
GOVERNOR_LOGGER = "code_indexer.server.services.memory_governor"


def v2(current: int, inactive: Any = None) -> FakeMemoryReaders:
    return FakeMemoryReaders(
        cgroup_v2_max=str(LIMIT),
        cgroup_v2_current=str(current),
        cgroup_v2_inactive_file=inactive,
    )


def v1(usage: int, inactive: Any = None) -> FakeMemoryReaders:
    return FakeMemoryReaders(
        cgroup_v1_limit=str(LIMIT),
        cgroup_v1_usage=str(usage),
        cgroup_v1_inactive_file=inactive,
    )


def ticked(readers: FakeMemoryReaders) -> MemoryGovernor:
    gov: MemoryGovernor = make_gov(readers, MemoryGovernor)
    gov._tick()
    return gov


# -- the working set ---------------------------------------------------------------


@pytest.mark.parametrize("build", [v2, v1], ids=["cgroup_v2", "cgroup_v1"])
def test_page_cache_from_index_reads_is_not_used_memory(build: Any) -> None:
    gov = ticked(build(CURRENT, INACTIVE_FILE))
    assert gov.band == MemoryBand.GREEN
    assert gov.last_used_pct == pytest.approx(100.0 * (CURRENT - INACTIVE_FILE) / LIMIT)
    assert gov.last_used_pct < 10


@pytest.mark.parametrize("build", [v2, v1], ids=["cgroup_v2", "cgroup_v1"])
def test_real_anonymous_growth_still_goes_red(build: Any) -> None:
    gov = ticked(build(ANON_HEAVY_CURRENT, 20 * 1024 * 1024))
    assert gov.band == MemoryBand.RED


@pytest.mark.parametrize("build", [v2, v1], ids=["cgroup_v2", "cgroup_v1"])
def test_real_anonymous_growth_goes_yellow(build: Any) -> None:
    gov = ticked(build(int(LIMIT * 0.72), 0))
    assert gov.band == MemoryBand.YELLOW


def test_the_working_set_is_floored_at_zero() -> None:
    gov = ticked(v2(GB, 2 * GB))
    assert gov.last_used_pct == 0.0 and gov.band == MemoryBand.GREEN


@pytest.mark.parametrize("build", [v2, v1], ids=["cgroup_v2", "cgroup_v1"])
def test_without_memory_stat_the_page_cache_still_counts(build: Any) -> None:
    """No memory.stat: the old, conservative reading (all of usage)."""
    gov = ticked(build(CURRENT, None))
    assert gov.band == MemoryBand.RED


def test_the_host_fallback_uses_available() -> None:
    readers = FakeMemoryReaders(
        host_total=100 * GB, host_used=95 * GB, host_available=80 * GB
    )
    gov = ticked(readers)
    assert gov.last_used_pct == pytest.approx(20.0)
    assert gov.band == MemoryBand.GREEN


def test_an_unreadable_memory_stat_counts_the_cache_and_warns(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = v2(CURRENT, INACTIVE_FILE)

    def broken() -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(readers, "read_cgroup_v2_inactive_file", broken)
    with caplog.at_level(logging.WARNING, logger=GOVERNOR_LOGGER):
        gov = ticked(readers)
    assert gov.band == MemoryBand.RED
    assert any("page cache counted as used" in r.getMessage() for r in caplog.records)


def test_the_admission_gate_uses_the_working_set() -> None:
    cached = ticked(v2(CURRENT, INACTIVE_FILE))
    assert cached.admission_allowed(80.0) is True
    heavy = ticked(v2(int(LIMIT * 0.82), 0))
    assert heavy.admission_allowed(80.0) is False


# -- reading memory.stat --------------------------------------------------------------


STAT = """anon 332128256
file 3783188480
active_file 22212608
inactive_file 3760975872
total_inactive_file 1234
"""


def test_memory_stat_values_are_read_by_exact_key(tmp_path: Path) -> None:
    stat = tmp_path / "memory.stat"
    stat.write_text(STAT)
    assert gov_module._memory_stat_value(str(stat), "inactive_file") == INACTIVE_FILE
    assert gov_module._memory_stat_value(str(stat), "total_inactive_file") == 1234
    with pytest.raises(ValueError):
        gov_module._memory_stat_value(str(stat), "missing")


# -- logging ------------------------------------------------------------------------


class _Cache:
    def __init__(self, size: int) -> None:
        self.size = size

    def get_stats(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(cached_repositories=self.size)

    def evict_lru_entries(self, n: int) -> int:
        self.size -= n
        return n


def _gov_lines(caplog: pytest.LogCaptureFixture, level: int) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == level and r.getMessage().startswith("GOV-00")
    ]


def test_a_yellow_pass_that_evicts_nothing_logs_nothing_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gov = make_gov(v2(int(LIMIT * 0.72), 0), MemoryGovernor)
    gov.attach_cache(_Cache(size=0))
    with caplog.at_level(logging.DEBUG, logger=GOVERNOR_LOGGER):
        gov._tick()
        caplog.clear()
        gov._tick()  # steady YELLOW, nothing to evict
    assert gov.band == MemoryBand.YELLOW
    assert _gov_lines(caplog, logging.INFO) == []
    debug = _gov_lines(caplog, logging.DEBUG)
    assert any(line.startswith("GOV-003 lru_evict count=0") for line in debug)
    assert any(line.startswith("GOV-004 malloc_trim") for line in debug)


def test_an_eviction_that_frees_entries_is_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gov = make_gov(v2(int(LIMIT * 0.72), 0), MemoryGovernor)
    with caplog.at_level(logging.DEBUG, logger=GOVERNOR_LOGGER):
        gov._tick()
        gov.log_gov003_lru_evict(count=3, freed_mb=0.0)
    assert any(
        line.startswith("GOV-003 lru_evict count=3")
        for line in _gov_lines(caplog, logging.INFO)
    )


def test_band_transitions_stay_at_info(caplog: pytest.LogCaptureFixture) -> None:
    readers = v2(int(LIMIT * 0.72), 0)
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()
    readers.cgroup_v2_current = str(int(LIMIT * 0.20))
    with caplog.at_level(logging.INFO, logger=GOVERNOR_LOGGER):
        gov._tick()
    assert gov.band == MemoryBand.GREEN
    assert any(line.startswith("GOV-001") for line in _gov_lines(caplog, logging.INFO))
