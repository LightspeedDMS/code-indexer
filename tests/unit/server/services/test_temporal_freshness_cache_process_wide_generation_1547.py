"""Bug #1547 round-2 hardening FIX 1 (RED): the generation counter must be
unique across the whole PROCESS, not per TemporalFreshnessSignalCache
instance.

TemporalFreshnessSignalCache._generation_counter starts fresh (at 0) for
EVERY new instance. If a cache instance is replaced while the (separate)
TemporalDedupCache singleton survives, the new instance's first recompute
pass replays generation 1 -- identical to the OLD instance's first pass --
so a degraded signal computed under the new instance compares byte-equal
(via canonical_signature) to a degraded signal computed under the old one,
and a pre-refresh dedup entry can be silently rejoined. Latent in
production today (both singletons share the process's full lifetime), but
a real trap: any future code path that resets one singleton independently
of the other silently reopens Bug #1547's original staleness hole.

Written BEFORE the fix: TemporalFreshnessSignalCache._generation_counter
is per-instance today, so two SEPARATE instances' first compute() calls
both observe generation=1 -- this test must genuinely fail against the
unmodified code.
"""

from typing import List

from code_indexer.server.services.temporal_freshness_cache import (
    TemporalFreshnessSignalCache,
)


class TestGenerationCounterIsProcessWide:
    def test_two_separate_cache_instances_never_share_a_generation(self):
        cache1 = TemporalFreshnessSignalCache()
        cache2 = TemporalFreshnessSignalCache()
        observed_generations: List[int] = []

        def compute(generation: int) -> List[int]:
            observed_generations.append(generation)
            return [generation]

        result1 = cache1.get_or_compute("key-a", compute)
        result2 = cache2.get_or_compute("key-a", compute)

        assert len(observed_generations) == 2
        assert observed_generations[0] != observed_generations[1], (
            "Bug #1547 FIX 1: a degraded compute() on a SEPARATE cache "
            "instance must never replay the same generation as another "
            "instance's compute() -- both instances produced generation "
            f"{observed_generations[0]!r}"
        )
        assert result1 != result2
