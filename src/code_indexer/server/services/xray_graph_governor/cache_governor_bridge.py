"""Story #1787 AC14: composes multiple governor-attachable caches behind
ONE `governor.attach_cache()` slot.

`MemoryGovernor.attach_cache()` is single-slot -- `service_init.py`
already calls it once for the HNSW index cache. Wiring the new X-Ray
graph cache (`cache_proxy.XrayGraphCacheProxy`) via a second,
unguarded `attach_cache()` call would silently REPLACE that
registration and break HNSW's existing YELLOW eviction (test-pinned in
`test_memory_governor_yellow_sampler_wiring.py`). Rather than changing
`MemoryGovernor.attach_cache()`'s single-slot semantics (risking that
already-tested wiring, and arguably widening governor API surface),
`CompositeLRUCache` wraps N sub-caches behind the ONE `get_stats()`/
`evict_lru_entries()` pair `attach_cache()` already accepts (ADR-003
Decision 4).

Critically, `evict_lru_entries(n)` does NOT distribute `n` across the
sub-caches: `n` is computed by `evict_lru_to_floor()` as
`combined_size - floor_entries` against ONE floor, which would silently
change HNSW's existing floor semantics once a second cache is added
(HNSW previously floored to `floor_entries` alone; sharing that budget
with a second cache would let HNSW get evicted more aggressively purely
because unrelated entries are occupying "its" slots). Instead, `n` is
ignored and EACH sub-cache is floored to `floor_per_cache`
INDEPENDENTLY -- byte-for-byte the same behavior HNSW had as the sole
attached cache, now extended uniformly to every attached cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol


class HasCachedRepositories(Protocol):
    """The one field `evict_lru_to_floor()` actually reads off a
    `get_stats()` result -- satisfied structurally by
    `HNSWIndexCacheStats`, `XrayGraphCacheStats`, and `CompositeCacheStats`
    alike, without any of them inheriting from this Protocol.
    """

    cached_repositories: int


class GovernorAttachableCache(Protocol):
    """The minimal interface `MemoryGovernor.evict_lru_to_floor()`
    actually calls -- any real cache (HNSWIndexCache, XrayGraphCacheProxy)
    satisfies this structurally, without inheriting from it.
    """

    def get_stats(self) -> HasCachedRepositories: ...

    def evict_lru_entries(self, n: int) -> int: ...


@dataclass
class CompositeCacheStats:
    """Matches `HNSWIndexCacheStats`'s shape (`cached_repositories`) so
    `evict_lru_to_floor()`'s `stats.cached_repositories` read works
    unchanged against the composite too.
    """

    cached_repositories: int


class CompositeLRUCache:
    """Wraps `caches` (each already implementing `get_stats()` returning
    an object with `.cached_repositories`, and `evict_lru_entries(n) ->
    int`) behind the single interface `governor.attach_cache()` expects.
    """

    def __init__(
        self, caches: List[GovernorAttachableCache], *, floor_per_cache: int
    ) -> None:
        if not caches:
            raise ValueError("caches must not be empty")
        if floor_per_cache < 0:
            raise ValueError(
                f"floor_per_cache must be non-negative, got {floor_per_cache}"
            )
        self._caches = list(caches)
        self._floor_per_cache = floor_per_cache

    def get_stats(self) -> CompositeCacheStats:
        total = sum(cache.get_stats().cached_repositories for cache in self._caches)
        return CompositeCacheStats(cached_repositories=total)

    def evict_lru_entries(self, n: int) -> int:
        """`n` is deliberately IGNORED -- see module docstring. Each
        sub-cache is floored independently to `floor_per_cache`,
        matching what it would do as the governor's sole attached cache.
        """
        evicted_total = 0
        for cache in self._caches:
            size = cache.get_stats().cached_repositories
            to_evict = size - self._floor_per_cache
            if to_evict > 0:
                evicted_total += cache.evict_lru_entries(to_evict)
        return evicted_total
