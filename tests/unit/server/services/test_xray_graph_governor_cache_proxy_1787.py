"""Story #1787 AC9/AC14: the Python-side X-Ray graph-cache proxy.

Real temp files, real mmap, real MemoryGovernor (via the shared
FakeMemoryReaders/make_gov fixtures) -- no mocking of the class under
test, matching this project's established convention. The `proxy`
fixture's teardown evicts (and therefore closes) every remaining entry
regardless of whether the test body passes or raises, so no real mmap
is ever left open past a single test.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.xray_graph_governor.cache_proxy import (
    GraphWireFileHandle,
    XrayGraphCacheProxy,
)
from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)

# Large enough to always drain every entry a test could have inserted,
# never large enough to matter as an actual bound (evict_lru_entries
# stops the moment the registry is empty).
_DRAIN_ALL = 10**6


@pytest.fixture()
def wire_file_paths(tmp_path):
    paths = []
    for i in range(5):
        path = tmp_path / f"wire_{i}.bin"
        path.write_bytes(b"x" * 64)
        paths.append(str(path))
    return paths


@pytest.fixture()
def proxy():
    p = XrayGraphCacheProxy()
    yield p
    p.evict_lru_entries(_DRAIN_ALL)  # closes every real mmap, pass or fail


def test_graph_wire_file_handle_requires_an_absolute_path():
    with pytest.raises(ValueError):
        GraphWireFileHandle("relative/path.bin")


def test_graph_wire_file_handle_rejects_path_traversal_components():
    with pytest.raises(ValueError):
        GraphWireFileHandle("/tmp/../etc/passwd")


def test_graph_wire_file_handle_mmaps_a_real_file_and_closes_cleanly(wire_file_paths):
    handle = GraphWireFileHandle(wire_file_paths[0])
    try:
        assert handle._mmap is not None
        assert handle._mmap[:1] == b"x"
    finally:
        handle.close()
    assert handle._mmap is None
    handle.close()  # idempotent, must not raise


def test_proxy_get_returns_none_on_miss_and_the_handle_on_hit(proxy, wire_file_paths):
    assert proxy.get("missing") is None

    handle = GraphWireFileHandle(wire_file_paths[0])
    proxy.put("repo-a", handle)
    assert proxy.get("repo-a") is handle


def test_proxy_get_stats_reports_the_real_entry_count(proxy, wire_file_paths):
    for i, path in enumerate(wire_file_paths[:3]):
        proxy.put(f"repo-{i}", GraphWireFileHandle(path))

    assert proxy.get_stats().cached_repositories == 3


def test_proxy_evict_lru_entries_evicts_oldest_first_and_closes_the_mmap(
    proxy, wire_file_paths
):
    handles = [GraphWireFileHandle(path) for path in wire_file_paths[:3]]
    for i, handle in enumerate(handles):
        proxy.put(f"repo-{i}", handle)

    evicted = proxy.evict_lru_entries(1)

    assert evicted == 1
    assert proxy.get_stats().cached_repositories == 2
    assert handles[0]._mmap is None, (
        "the LRU (first-inserted, never touched) entry must be the one evicted"
    )
    assert proxy.get("repo-0") is None


def test_proxy_put_replacing_an_existing_key_closes_the_displaced_handle(
    proxy, wire_file_paths
):
    old_handle = GraphWireFileHandle(wire_file_paths[0])
    proxy.put("repo-a", old_handle)

    new_handle = GraphWireFileHandle(wire_file_paths[1])
    proxy.put("repo-a", new_handle)

    assert old_handle._mmap is None, (
        "replacing an entry must close the handle it displaces"
    )
    assert proxy.get("repo-a") is new_handle
    assert proxy.get_stats().cached_repositories == 1


def _gov_at_used_pct(used_pct: float):
    from code_indexer.server.services.memory_governor import MemoryGovernor

    limit = CGROUP_LIMIT_4GB
    current = int(limit * used_pct / 100.0)
    readers = FakeMemoryReaders(
        cgroup_v2_max=str(limit), cgroup_v2_current=str(current)
    )
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()
    return gov


def test_proxy_attaches_to_a_real_governor_and_is_evicted_to_floor_on_yellow_tick(
    proxy, wire_file_paths
):
    """THE AC14 integration proof: a real MemoryGovernor, real
    attach_cache(), real evict_lru_to_floor() against a real
    XrayGraphCacheProxy -- not a mock standing in for either side.
    """
    gov = _gov_at_used_pct(72.0)  # inside [yellow=70, red_exit=75) -> YELLOW
    assert gov.band.value == "YELLOW"

    for i, path in enumerate(wire_file_paths):
        proxy.put(f"repo-{i}", GraphWireFileHandle(path))
    assert proxy.get_stats().cached_repositories == 5

    gov.attach_cache(proxy)
    gov.evict_lru_to_floor(proxy, floor_entries=1)

    assert proxy.get_stats().cached_repositories == 1
    assert gov.counters.lru_evictions >= 4
