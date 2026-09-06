"""Story #1787 AC14: CompositeLRUCache -- composes multiple
governor-attachable caches behind ONE `governor.attach_cache()` slot,
flooring each sub-cache INDEPENDENTLY at the same floor
`evict_lru_to_floor()` already uses.

Uses real `XrayGraphCacheProxy` instances as stand-ins for "HNSW" and
"the X-Ray graph cache" (both satisfy the identical get_stats()/
evict_lru_entries() contract), and a real MemoryGovernor via the shared
FakeMemoryReaders/make_gov fixtures -- no mocking of the classes under
test.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.xray_graph_governor.cache_governor_bridge import (
    CompositeLRUCache,
)
from code_indexer.server.services.xray_graph_governor.cache_proxy import (
    GraphWireFileHandle,
    XrayGraphCacheProxy,
)
from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)

DRAIN_ALL_EVICT_COUNT = (
    10**6
)  # large enough to always empty a proxy in fixture teardown
WIRE_FILE_COUNT = 6
FLOOR_ENTRIES = 1
YELLOW_USED_PCT = 72.0  # inside [yellow=70, red_exit=75) -> YELLOW
CACHE_A_ENTRY_COUNT = 3
CACHE_A_ENTRY_COUNT_LARGE = 4
CACHE_B_ENTRY_COUNT_LARGE = 2


@pytest.fixture()
def wire_file_paths(tmp_path):
    paths = []
    for i in range(WIRE_FILE_COUNT):
        path = tmp_path / f"wire_{i}.bin"
        path.write_bytes(b"x")
        paths.append(str(path))
    return paths


@pytest.fixture()
def cache_a():
    c = XrayGraphCacheProxy()
    yield c
    c.evict_lru_entries(DRAIN_ALL_EVICT_COUNT)


@pytest.fixture()
def cache_b():
    c = XrayGraphCacheProxy()
    yield c
    c.evict_lru_entries(DRAIN_ALL_EVICT_COUNT)


def test_yellow_lru_floor_public_constant_matches_governor_internal_floor():
    """H4/H6 remediation: service_init.py must build the composite cache's
    floor_per_cache from the SAME constant memory_governor.py's own YELLOW
    tick uses internally -- never a second, independently-duplicated magic
    number that could silently drift from the governor's real floor.
    """
    from code_indexer.server.services.memory_governor import YELLOW_LRU_FLOOR

    assert YELLOW_LRU_FLOOR == FLOOR_ENTRIES


def test_get_stats_sums_both_sub_caches(cache_a, cache_b, wire_file_paths):
    cache_a.put("repo-a1", GraphWireFileHandle(wire_file_paths[0]))
    cache_a.put("repo-a2", GraphWireFileHandle(wire_file_paths[1]))
    cache_b.put("repo-b1", GraphWireFileHandle(wire_file_paths[2]))

    composite = CompositeLRUCache([cache_a, cache_b], floor_per_cache=FLOOR_ENTRIES)

    assert composite.get_stats().cached_repositories == 3


def test_empty_caches_list_raises_value_error():
    with pytest.raises(ValueError):
        CompositeLRUCache([], floor_per_cache=FLOOR_ENTRIES)


def test_evict_lru_entries_floors_each_sub_cache_independently(
    cache_a, cache_b, wire_file_paths
):
    for i in range(CACHE_A_ENTRY_COUNT):
        cache_a.put(f"repo-a{i}", GraphWireFileHandle(wire_file_paths[i]))
    cache_b.put(
        "repo-b0", GraphWireFileHandle(wire_file_paths[CACHE_A_ENTRY_COUNT])
    )  # already at floor

    composite = CompositeLRUCache([cache_a, cache_b], floor_per_cache=FLOOR_ENTRIES)
    # The `n` argument is deliberately ignored by CompositeLRUCache -- see
    # its module docstring -- so an arbitrarily large value proves that.
    evicted = composite.evict_lru_entries(DRAIN_ALL_EVICT_COUNT)

    assert cache_a.get_stats().cached_repositories == FLOOR_ENTRIES, (
        "cache_a (3 entries) must be floored to 1"
    )
    assert cache_b.get_stats().cached_repositories == FLOOR_ENTRIES, (
        "cache_b (already at floor) must be untouched"
    )
    assert evicted == CACHE_A_ENTRY_COUNT - FLOOR_ENTRIES


def _gov_at_used_pct(used_pct: float):
    if not 0.0 <= used_pct <= 100.0:
        raise ValueError(f"used_pct must be within [0, 100], got {used_pct}")
    from code_indexer.server.services.memory_governor import MemoryGovernor

    limit = CGROUP_LIMIT_4GB
    current = int(limit * used_pct / 100.0)
    readers = FakeMemoryReaders(
        cgroup_v2_max=str(limit), cgroup_v2_current=str(current)
    )
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()
    return gov


def test_composite_attaches_to_a_real_governor_and_floors_both_caches_on_yellow_tick(
    cache_a, cache_b, wire_file_paths
):
    """THE AC14 end-to-end proof: a real MemoryGovernor, real
    attach_cache(composite), real evict_lru_to_floor() -- both sub-caches
    end up independently floored, exactly as if each had been the
    governor's sole attached cache (HNSW's pre-existing behavior is
    unchanged when composed alongside the new X-Ray graph cache).
    """
    gov = _gov_at_used_pct(YELLOW_USED_PCT)
    assert gov.band.value == "YELLOW"

    for i in range(CACHE_A_ENTRY_COUNT_LARGE):
        cache_a.put(f"repo-a{i}", GraphWireFileHandle(wire_file_paths[i]))
    for i in range(CACHE_B_ENTRY_COUNT_LARGE):
        cache_b.put(
            f"repo-b{i}",
            GraphWireFileHandle(wire_file_paths[CACHE_A_ENTRY_COUNT_LARGE + i]),
        )

    composite = CompositeLRUCache([cache_a, cache_b], floor_per_cache=FLOOR_ENTRIES)
    gov.attach_cache(composite)
    gov.evict_lru_to_floor(composite, floor_entries=FLOOR_ENTRIES)

    assert cache_a.get_stats().cached_repositories == FLOOR_ENTRIES
    assert cache_b.get_stats().cached_repositories == FLOOR_ENTRIES
