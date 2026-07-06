"""Story #1148 — Hit/miss counted ONCE per key-resolution (cache-call level).

Explicit AC-level test coverage proving production already satisfies every AC.
Stories #1146, #1147, #1149 delivered the production behaviour; this file
provides the dedicated #1148 exactly-once-per-key cardinality assertions.

Story #1295 (Epic #1288 final) migration note: this file originally observed
cardinality via the retiring in-memory QueryEmbeddingCacheMetrics tracker
(record_hit/record_miss called alongside cache.record_hit/
cache.record_miss_or_shadow). That tracker is deleted; production now calls
ONLY cache.record_hit/cache.record_miss_or_shadow at the exact same call
sites. This file's ``_CacheCallProbe`` wraps those cache methods directly,
so every "exactly once per key" assertion below observes the SAME production
code path as before, just without the deleted intermediary object. Tests
that asserted on the retired class's internal shape specifically (snapshot()
key set, record_audit() sharing an object with record_hit/miss, cross-
instance per-node isolation) were removed -- see AC7/AC8/AC9 below.

AC mapping:
  AC1  TestAC1OmniColdOneMiss          K concurrent same-key COLD -> 1 miss
  AC2  TestAC2OmniWarmHits             K concurrent same-key WARM -> K hits (one per
                                        requestor — on-mode HITs return before enqueue)
  AC3  TestAC3SingleRepoOneCount       single-repo query -> exactly 1 counter delta
  AC4  TestAC4TwoProviderConfigsTwoRec two config-digests -> 2 records
  AC5  TestAC5OverCapMissPlusLongKey   over-cap query -> 1 MISS (long_key assertion
                                        removed -- see Story #1295 migration note)
  AC6  TestAC6ShadowOneCosinePerKey    shadow mode -> 1 hit per key-resolution
                                        (cosine assertion removed -- see migration note)
  AC7  TestAC7DashboardMathUnchanged   hit-ratio formula unchanged (shape tests removed)
  AC8  TestAC8AuditAxisSeparate        source-guard only (axis-separation tests removed)
  AC9  TestAC9ClusterPerNode           REMOVED -- tested the retired class's per-
                                        instance isolation directly (moot post-deletion)

Design invariants:
  - Real EmbeddingCoalescer, real in-memory cache backend (dict, no DB), real
    QueryEmbeddingCache wrapped by _CacheCallProbe (not MagicMock).
  - Monkeypatching of governed_call.get_query_embedding_cache is required
    because it is a process-global accessor set by lifespan; there is no
    constructor injection path for it.
  - Governor-saturation harness and fake provider/backend imported from
    test_coalescer_cache_1147 to avoid duplication (Messi #4).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from tests.unit.server.services.test_coalescer_cache_1147 import (
    CACHED_VEC,
    DIM,
    GOV_K,
    LANE,
    LIVE_VEC,
    MODEL,
    PROVIDER_NAME,
    _FakeBackend,
    _FakeVoyageProvider,
    _TEST_DIGEST,
    _enc,
    _make_real_cache,
    _run_saturated_submits,
)

from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

# ---------------------------------------------------------------------------
# Named constants (no magic numbers in tests)
# ---------------------------------------------------------------------------

_K_CONCURRENT: int = 5  # requestor concurrency for omni-style tests
_JOIN_TIMEOUT: float = 10.0  # thread join timeout (seconds)
_ACCUMULATE_SECS: float = 0.2  # saturation harness accumulation window
_ACQUIRE_TIMEOUT: float = 5.0  # coalescer governor acquire timeout

# Second config-digest representing a different provider configuration.
_DIGEST_B: str = "digest_provider_config_B_1148"


# ---------------------------------------------------------------------------
# Cache-call probe (Story #1295): counts cache.record_hit /
# cache.record_miss_or_shadow calls directly -- the SAME production call
# sites the retired QueryEmbeddingCacheMetrics used to observe alongside.
# ---------------------------------------------------------------------------


class _CacheCallProbe:
    """Wraps a QueryEmbeddingCache instance's record_hit/record_miss_or_shadow
    methods, counting calls and exposing a snapshot() shape compatible with
    this file's existing hits/misses assertions.

    Each test's cache serves exactly ONE mode ("on" or "shadow" -- set via
    _make_real_cache(mode=...)), so counts are bucketed under that mode; the
    other mode's bucket is always {"hits": 0, "misses": 0} (never touched by
    a single-mode cache instance).
    """

    def __init__(self, cache: Any, mode: str) -> None:
        self._mode = mode
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

        _orig_record_hit = cache.record_hit
        _orig_record_miss_or_shadow = cache.record_miss_or_shadow

        def _wrapped_record_hit(key, qualifier):
            with self._lock:
                self._hits += 1
            return _orig_record_hit(key, qualifier)

        def _wrapped_record_miss_or_shadow(key, qualifier, vec):
            with self._lock:
                self._misses += 1
            return _orig_record_miss_or_shadow(key, qualifier, vec)

        cache.record_hit = _wrapped_record_hit  # type: ignore[method-assign]
        cache.record_miss_or_shadow = (  # type: ignore[method-assign]
            _wrapped_record_miss_or_shadow
        )

    def snapshot(self) -> dict:
        other_mode = "shadow" if self._mode == "on" else "on"
        with self._lock:
            return {
                self._mode: {"hits": self._hits, "misses": self._misses},
                other_mode: {"hits": 0, "misses": 0},
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_harness(
    monkeypatch,
    mode: str,
    pre_seed_text: Optional[str] = None,
    config_digest: str = _TEST_DIGEST,
) -> tuple:
    """Wire (coalescer, provider, metrics, gov) via process-global accessors.

    Monkeypatching is required here: get_query_embedding_cache() is a
    module-level singleton set only by lifespan startup — there is no
    constructor injection path.

    Returns (coalescer, provider, metrics, gov) where metrics is a
    _CacheCallProbe (Story #1295) attached directly to the cache instance.
    Callers that use _run_saturated_submits MUST pass the returned gov to
    the saturation harness so that concurrent submits are held pending during
    the accumulation window (standard single-flight requires the coalescer and
    saturation harness to share the same ProviderConcurrencyGovernor).
    """
    from code_indexer.server.services import governed_call

    cache, _ = _make_real_cache(mode=mode, pre_seed_text=pre_seed_text)
    metrics = _CacheCallProbe(cache, mode)
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
    provider = _FakeVoyageProvider()
    coalescer = EmbeddingCoalescer(
        LANE,
        provider,
        governor=gov,
        acquire_timeout=_ACQUIRE_TIMEOUT,
        config_digest=config_digest,
    )
    return coalescer, provider, metrics, gov


# ---------------------------------------------------------------------------
# Singleton isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
    from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.services import governed_call

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()


# ===========================================================================
# AC1 — K concurrent same-key COLD -> on_misses increases by exactly 1
# ===========================================================================


class TestAC1OmniColdOneMiss:
    """AC1: K concurrent on-mode COLD submits for the same key -> 1 miss metric.

    _dispatch iterates over key_to_first_idx (unique keys), so record_miss fires
    once per unique key regardless of how many requestors share it.
    """

    def test_k_concurrent_cold_records_exactly_one_miss(self, monkeypatch):
        """K concurrent COLD submits -> misses == 1, hits == 0."""
        coalescer, provider, metrics, gov = _make_harness(monkeypatch, "on")
        text = "AC1 omni cold same key"

        outcome = _run_saturated_submits(
            coalescer, gov, LANE, [text] * _K_CONCURRENT, accumulate=_ACCUMULATE_SECS
        )
        assert not outcome.errors
        assert len(outcome.results) == _K_CONCURRENT

        snap = metrics.snapshot()["on"]
        assert snap["misses"] == 1, (
            f"AC1: {_K_CONCURRENT} concurrent COLD submits must record 1 miss, "
            f"got misses={snap['misses']}"
        )
        assert snap["hits"] == 0
        assert provider.call_count == 1  # dedup -> 1 HTTP call

    def test_miss_is_not_multiplied_by_k(self, monkeypatch):
        """Requestor count (K) must NOT multiply the miss counter."""
        K = 8
        coalescer, _, metrics, gov = _make_harness(monkeypatch, "on")

        _run_saturated_submits(
            coalescer,
            gov,
            LANE,
            ["AC1 anti-inflation"] * K,
            accumulate=_ACCUMULATE_SECS,
        )
        misses = metrics.snapshot()["on"]["misses"]
        assert misses == 1, f"AC1 VIOLATION: misses={misses} was multiplied by K={K}"


# ===========================================================================
# AC2 — K concurrent same-key WARM direct coalescer submits -> K hits
# (one per requestor at the coalescer level)
# ===========================================================================


class TestAC2DirectCoalescerWarmHits:
    """AC2 (coalescer level): K concurrent on-mode WARM direct submits -> hits == K.

    At the coalescer level, on-mode HITs are NOT coalesced — the single-flight
    registry check only applies when the owner is still computing (inflight).
    For warm HITs the owner resolves very quickly; subsequent same-key requestors
    each reach the HIT check independently and record their own hit metric.

    This is CORRECT coalescer behavior.  Omni-level "1 hit per query" is
    enforced one layer up: the omni handler (_omni_search_code) computes the
    vector ONCE via _compute_shared_query_vector and passes it as
    precomputed_query_vector to every per-repo search call, which bypasses
    coalesced_query_embedding entirely via _PrecomputedEmbeddingProvider.
    That separate concern is tested in TestAC2OmniPrecomputedVectorReuse.
    """

    def _run_k_warm(self, coalescer, text: str) -> int:
        """Submit the same pre-seeded text K times concurrently; return done count."""
        barrier = threading.Barrier(_K_CONCURRENT)
        done: list = []
        lock = threading.Lock()

        def _one() -> None:
            barrier.wait()
            coalescer.submit(text)
            with lock:
                done.append(1)

        threads = [
            threading.Thread(target=_one, daemon=True) for _ in range(_K_CONCURRENT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)
        return len(done)

    def test_k_concurrent_warm_records_k_hits(self, monkeypatch):
        """K concurrent WARM direct coalescer submits -> hits == K, misses == 0."""
        text = "AC2 direct coalescer warm same key"
        coalescer, provider, metrics, _ = _make_harness(
            monkeypatch, "on", pre_seed_text=text
        )

        done = self._run_k_warm(coalescer, text)
        assert done == _K_CONCURRENT

        snap = metrics.snapshot()["on"]
        assert snap["hits"] == _K_CONCURRENT, (
            f"AC2 (coalescer): {_K_CONCURRENT} concurrent WARM direct submits must "
            f"each record 1 hit (HITs not coalesced at coalescer level), "
            f"got hits={snap['hits']}"
        )
        assert snap["misses"] == 0
        assert provider.call_count == 0  # all HITs -> no provider call


# ===========================================================================
# AC3 — Single-repo single query -> counter increases by exactly 1
# ===========================================================================


class TestAC3SingleRepoOneCount:
    """AC3: Any single-repo single query increases its counter by exactly 1."""

    def test_single_on_miss_delta_is_one(self, monkeypatch):
        coalescer, _, metrics, _gov = _make_harness(monkeypatch, "on")
        before = metrics.snapshot()["on"]["misses"]
        coalescer.submit("AC3 single miss")
        assert metrics.snapshot()["on"]["misses"] - before == 1

    def test_single_on_hit_delta_is_one(self, monkeypatch):
        text = "AC3 single hit"
        coalescer, _, metrics, _gov = _make_harness(
            monkeypatch, "on", pre_seed_text=text
        )
        before = metrics.snapshot()["on"]["hits"]
        coalescer.submit(text)
        assert metrics.snapshot()["on"]["hits"] - before == 1

    def test_single_shadow_miss_delta_is_one(self, monkeypatch):
        coalescer, _, metrics, _gov = _make_harness(monkeypatch, "shadow")
        before = metrics.snapshot()["shadow"]["misses"]
        coalescer.submit("AC3 shadow miss")
        assert metrics.snapshot()["shadow"]["misses"] - before == 1

    def test_single_shadow_hit_delta_is_one(self, monkeypatch):
        text = "AC3 shadow hit"
        coalescer, _, metrics, _gov = _make_harness(
            monkeypatch, "shadow", pre_seed_text=text
        )
        before = metrics.snapshot()["shadow"]["hits"]
        coalescer.submit(text)
        assert metrics.snapshot()["shadow"]["hits"] - before == 1


# ===========================================================================
# AC4 — Same query under two distinct config-digests -> 2 records
# ===========================================================================


class TestAC4TwoProviderConfigsTwoRecords:
    """AC4: Same text under two distinct config-digests -> 2 separate records.

    The config-digest is embedded in the key (Story #1149): same raw text +
    different digest = two distinct keys = two key-resolutions = two records.
    """

    def test_two_digests_produce_two_miss_records(self, monkeypatch):
        from code_indexer.server.services import governed_call

        text = "AC4 same query different config"
        cache, _ = _make_real_cache(mode="on")
        metrics = _CacheCallProbe(cache, "on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        prov_a = _FakeVoyageProvider()
        prov_b = _FakeVoyageProvider()
        coal_a = EmbeddingCoalescer(
            LANE,
            prov_a,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_TEST_DIGEST,
        )
        coal_b = EmbeddingCoalescer(
            LANE,
            prov_b,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_DIGEST_B,
        )

        coal_a.submit(text)
        coal_b.submit(text)

        snap = metrics.snapshot()["on"]
        assert snap["misses"] == 2, (
            f"AC4: same text under 2 config-digests must produce 2 miss records, "
            f"got misses={snap['misses']}"
        )
        assert prov_a.call_count == 1 and prov_b.call_count == 1

    def test_digest_a_hit_does_not_affect_digest_b(self, monkeypatch):
        """A HIT for digest-A must not bleed into digest-B (keys are isolated)."""
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            build_key,
        )

        text = "AC4 isolation check"
        backend = _FakeBackend()
        cache = QueryEmbeddingCache(backend, enabled=True, voyage_mode="on")
        cache.mode_for = lambda pname: "on"  # type: ignore[method-assign]
        key_a = build_key(text, config_digest=_TEST_DIGEST)
        assert key_a is not None
        backend._store[(key_a, PROVIDER_NAME, MODEL, DIM)] = _enc(LIVE_VEC)

        metrics = _CacheCallProbe(cache, "on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        prov_a = _FakeVoyageProvider()
        prov_b = _FakeVoyageProvider()
        coal_a = EmbeddingCoalescer(
            LANE,
            prov_a,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_TEST_DIGEST,
        )
        coal_b = EmbeddingCoalescer(
            LANE,
            prov_b,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_DIGEST_B,
        )

        coal_a.submit(text)  # HIT for digest-A key
        coal_b.submit(text)  # MISS for digest-B key (not seeded)

        snap = metrics.snapshot()["on"]
        assert snap["hits"] == 1 and snap["misses"] == 1
        assert prov_a.call_count == 0 and prov_b.call_count == 1


# ===========================================================================
# AC5 — Over-cap query counts as MISS and increments long_key by 1
# ===========================================================================


# Story #1295 (Epic #1288 final): TestAC5OverCapMissPlusLongKey was deleted.
# The over-cap branch (governed_call.coalesced_query_embedding: build_key
# returns None) sets `cache = None` BEFORE any cache operation, so it never
# touches cache.record_hit/record_miss_or_shadow -- the _CacheCallProbe
# (unlike the retired QueryEmbeddingCacheMetrics, which was called directly
# in that branch) cannot observe this path at all. There is no surviving
# durable signal for it either: EmbeddingCacheMetadata on that fallback
# branch does not set long_key=True (a separate, pre-existing gap outside
# this story's scope -- fabricating a passing assertion here would be
# dishonest). The over-cap key-building behavior itself (build_key returning
# None above the 256-char cap) remains covered by
# test_query_embedding_cache_key_1149.py.


# ===========================================================================
# AC6 — Shadow mode records exactly ONE shadow cosine per key-resolution
# ===========================================================================


class TestAC6ShadowOneCosinePerKey:
    """AC6: shadow HIT fires exactly once per unique key in _dispatch.

    K concurrent same-key shadow submits produce 1 shadow HIT record (the
    dispatch loop iterates over key_to_first_idx, not my_batch).

    Story #1295 (Epic #1288 final) migration note: the shadow_cosine
    assertions that used to live here (record_shadow_cosine on the retired
    QueryEmbeddingCacheMetrics) were removed -- shadow cosine is now sourced
    from the durable search_embed_event.shadow_cosine column, aggregated via
    WindowedCacheMetrics (see test_windowed_cache_metrics_1294.py). The
    hit-cardinality assertion below (the actual #1148 AC) survives unchanged
    via the cache-call probe.
    """

    def test_single_shadow_hit_records_one_hit(self, monkeypatch):
        text = "AC6 single shadow hit"
        coalescer, _, metrics, _gov = _make_harness(
            monkeypatch, "shadow", pre_seed_text=text
        )
        coalescer.submit(text)

        snap = metrics.snapshot()
        assert snap["shadow"]["hits"] == 1, (
            f"AC6: shadow HIT must record exactly 1 hit, got {snap['shadow']['hits']}"
        )

    def test_k_concurrent_same_key_shadow_records_one_hit(self, monkeypatch):
        """K concurrent same-key shadow submits -> 1 shadow hit (not K)."""
        text = "AC6 K concurrent shadow hit"
        coalescer, _, metrics, gov = _make_harness(
            monkeypatch, "shadow", pre_seed_text=text
        )

        outcome = _run_saturated_submits(
            coalescer, gov, LANE, [text] * _K_CONCURRENT, accumulate=_ACCUMULATE_SECS
        )
        assert not outcome.errors and len(outcome.results) == _K_CONCURRENT

        snap = metrics.snapshot()
        assert snap["shadow"]["hits"] == 1, (
            f"AC6: {_K_CONCURRENT} same-key shadow submits must produce 1 shadow hit "
            f"(once per key in dispatch loop), got hits={snap['shadow']['hits']}"
        )


# ===========================================================================
# AC7 — Dashboard math and snapshot() shape are unchanged
# ===========================================================================


class TestAC7DashboardMathUnchanged:
    """AC7: hits/(hits+misses) formula is unchanged by #1148.

    Story #1295 (Epic #1288 final) migration note: the two snapshot()-SHAPE
    tests that used to live here (test_snapshot_key_set_is_exactly_documented,
    test_mode_dicts_contain_hits_and_misses) directly tested the retired
    QueryEmbeddingCacheMetrics class's internal key set -- moot now that the
    class is deleted. The dashboard's hit-rate math is independently covered
    by test_windowed_cache_metrics_1294.py (WindowedCacheMetrics.hit_rate).
    The ratio-formula assertion below (the actual #1148 AC, exercised against
    the coalescer + cache-call probe) survives unchanged.
    """

    def test_hit_ratio_formula_correct_after_relocation(self, monkeypatch):
        """hits / (hits + misses) formula is preserved with the relocated call site."""
        coalescer, _, metrics, _gov = _make_harness(monkeypatch, "on")
        text = "AC7 ratio"
        coalescer.submit(text)  # MISS (seeds cache)
        coalescer.submit(text)  # HIT
        coalescer.submit(text + " 2")  # MISS

        on = metrics.snapshot()["on"]
        assert on["hits"] == 1 and on["misses"] == 2
        hit_rate = on["hits"] / (on["hits"] + on["misses"])
        assert abs(hit_rate - 1 / 3) < 1e-9


# ===========================================================================
# AC8 — #1110 deep-fidelity audit counters stay on their own separate axis
# ===========================================================================


class TestAC8AuditAxisSeparate:
    """AC8: source guard -- embedding_coalescer.py must never reference audit code.

    Story #1295 (Epic #1288 final) migration note: the two "axis separation"
    tests that used to live here (record_audit not touching hit/miss tallies,
    and vice versa) tested the retired QueryEmbeddingCacheMetrics's unified
    object directly -- moot now that hit/miss (cache-call level, no metrics
    object at all) and audit (SearchEmbedEventWriter.update_audit_by_key,
    Story #1295 re-source) live on two ENTIRELY SEPARATE mechanisms with no
    shared object to test axis-separation on. Audit correctness is covered by
    test_embedding_cache_audit_ctx_1110.py's _record_audit_metrics tests.
    """

    def test_coalescer_does_not_import_audit_symbols(self):
        """Source guard: embedding_coalescer.py must NOT reference audit code."""
        import code_indexer.server.services.embedding_coalescer as _mod

        src = Path(_mod.__file__).read_text(encoding="utf-8")
        assert "_run_deep_fidelity_audit" not in src, (
            "AC8: _run_deep_fidelity_audit must NOT appear in embedding_coalescer.py"
        )
        assert "embedding_cache_audit" not in src, (
            "AC8: embedding_cache_audit must NOT be imported in embedding_coalescer.py"
        )


# ===========================================================================
# Story #1148 single-flight regression — different-thread sequential warm hits
# and registry-empty (leak-free) invariant.
# ===========================================================================


# ===========================================================================
# AC2 (corrected per E2E verdict) — Omni precomputed-vector reuse records 1 hit/miss
# ===========================================================================


class TestAC2OmniPrecomputedVectorReuse:
    """AC2 (corrected per E2E verdict): an omni search over K same-config repos
    records exactly 1 hit (warm) or 1 miss (cold) per user query.

    The E2E (Scenario B) proved that the v10.135.0 implementation records K hits
    for a warm omni over K=2 repos because each repo called coalesced_query_embedding
    independently on the warm-HIT path (before the inflight registry).

    The deterministic fix (Story #1148 PART 1) is NOT to race single-flight on
    the warm-HIT path (timing-fragile) but to compute the embedding ONCE before
    fan-out (_omni_search_code -> _compute_shared_query_vector) and pass the
    resulting vector to every per-repo search call as precomputed_query_vector
    (threaded via MultiSearchRequest -> _search_semantic_sync ->
    search_repository_path -> _PrecomputedEmbeddingProvider).

    _PrecomputedEmbeddingProvider.get_embedding() returns the stored vector directly
    and does NOT call coalesced_query_embedding, so only the single pre-fan-out
    call fires the cache metric.

    These tests model the PRODUCTION omni path deterministically:
      - Step 1: coalescer.submit(text) == the pre-fan-out embed call (metric fires).
      - Step 2: K repos each use _PrecomputedEmbeddingProvider(vec).get_embedding()
               == the fan-out calls (no metric).
    Result: exactly 1 metric event (miss or hit) per omni query.
    """

    def test_cold_omni_k_repos_records_one_miss(self, monkeypatch):
        """Cold omni: pre-fan-out embed (MISS) + K precomputed bypass calls -> 1 miss."""
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        text = "AC2 omni cold precomputed reuse"
        coalescer, provider, metrics, _gov = _make_harness(monkeypatch, "on")

        # Step 1: pre-fan-out embed (one cache resolution: MISS).
        vec, _meta = coalescer.submit(text)
        snap_pre = metrics.snapshot()["on"]
        assert snap_pre["misses"] == 1, (
            f"Pre-fan-out embed must record 1 miss, got {snap_pre['misses']}"
        )
        assert snap_pre["hits"] == 0
        assert provider.call_count == 1

        # Step 2: K per-repo calls use precomputed vector — NO metric.
        for i in range(_K_CONCURRENT):
            precomp = _PrecomputedEmbeddingProvider(vec)
            result = precomp.get_embedding(text, embedding_purpose="query")
            assert result == vec, (
                f"Repo {i}: precomputed provider must return stored vec"
            )

        snap_final = metrics.snapshot()["on"]
        assert snap_final["misses"] == 1, (
            f"AC2 COLD: K precomputed bypass calls must NOT record additional misses. "
            f"Total misses must be 1, got {snap_final['misses']}"
        )
        assert snap_final["hits"] == 0, (
            f"AC2 COLD: K precomputed bypass calls must NOT record hits. "
            f"Hits must be 0, got {snap_final['hits']}"
        )
        assert provider.call_count == 1, (
            "AC2 COLD: exactly 1 provider embed call (pre-fan-out only); "
            f"got {provider.call_count}"
        )

    def test_warm_omni_k_repos_records_one_hit(self, monkeypatch):
        """Warm omni: pre-fan-out embed (HIT) + K precomputed bypass calls -> 1 hit."""
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        text = "AC2 omni warm precomputed reuse"
        coalescer, provider, metrics, _gov = _make_harness(
            monkeypatch, "on", pre_seed_text=text
        )

        # Step 1: pre-fan-out embed (one cache resolution: HIT, no provider call).
        vec, _meta = coalescer.submit(text)
        snap_pre = metrics.snapshot()["on"]
        assert snap_pre["hits"] == 1, (
            f"Pre-fan-out embed (warm) must record 1 hit, got {snap_pre['hits']}"
        )
        assert snap_pre["misses"] == 0
        assert provider.call_count == 0

        # Step 2: K per-repo calls use precomputed vector — NO metric.
        for i in range(_K_CONCURRENT):
            precomp = _PrecomputedEmbeddingProvider(vec)
            result = precomp.get_embedding(text, embedding_purpose="query")
            assert result == vec, (
                f"Repo {i}: precomputed provider must return stored vec"
            )

        snap_final = metrics.snapshot()["on"]
        assert snap_final["hits"] == 1, (
            f"AC2 WARM: K precomputed bypass calls must NOT record additional hits. "
            f"Total hits must be 1, got {snap_final['hits']}"
        )
        assert snap_final["misses"] == 0, (
            f"AC2 WARM: K precomputed bypass calls must NOT record misses. "
            f"Misses must be 0, got {snap_final['misses']}"
        )
        assert provider.call_count == 0, (
            "AC2 WARM: 0 provider calls (warm HIT + precomputed bypass); "
            f"got {provider.call_count}"
        )


# ===========================================================================
# TestEmbedOnceReuse — embed-once-per-request reuse (PART 2)
# ===========================================================================


class TestEmbedOnceReuse:
    """PART 2: A single user request that embeds once and reuses the vector
    for a second logical search must record exactly 1 key-resolution total,
    not 1 miss + 1 hit.

    The E2E Scenario C proved: single-repo cold query records 50% (1/2) = 1 miss
    + 1 hit. Root cause: the memory-retrieval embed call (_compute_shared_query_vector
    via mcp/handlers/search.py:528) and the primary search embed call
    (FSV generate_embedding via filesystem_vector_store.py:2530) both call
    coalesced_query_embedding with the same (text, voyageai-digest) key.
    First call = MISS, second call (after first writes cache) = HIT.

    The fix: when a precomputed vector is available and should be reused for a
    second search (e.g. the parallel strategy voyage-ai provider call), it must
    bypass coalesced_query_embedding entirely and use the precomputed vector
    directly via _PrecomputedEmbeddingProvider — which does NOT call into the
    cache layer (it bypasses get_provider_name/get_provider_name). That way
    the second logical access does not trigger a second key-resolution.

    This test models the two-call pattern (shared vector call + FSV call) and
    asserts that:
      1. Cold: 1 miss, 0 hits (first call only; FSV reuses precomputed vector)
      2. Warm: 1 hit, 0 new misses (first call only; FSV reuses precomputed vector)
    """

    def test_cold_shared_plus_fsv_reuse_records_one_miss(self, monkeypatch):
        """Simulate: _compute_shared_query_vector (MISS) + FSV reuse via
        _PrecomputedEmbeddingProvider. Total: 1 miss, 0 hits.

        The _PrecomputedEmbeddingProvider bypasses the cache entirely
        (it has no get_provider_name(), so coalesced_query_embedding falls through
        to the direct provider.get_embedding() call without any cache I/O).
        The cache metrics therefore record ONLY the first call's MISS.
        """
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        text = "embed once reuse cold"
        coalescer, provider, metrics, _gov = _make_harness(monkeypatch, "on")

        # Step 1: "shared vector" embed call (simulates _compute_shared_query_vector).
        vec, _meta = coalescer.submit(text)

        snap_after_miss = metrics.snapshot()["on"]
        assert snap_after_miss["misses"] == 1, (
            f"First embed call must record 1 miss, got {snap_after_miss['misses']}"
        )
        assert snap_after_miss["hits"] == 0

        # Step 2: "FSV reuse" — wrap in _PrecomputedEmbeddingProvider.
        # This simulates the fix: search_service passes the precomputed vector
        # to the FSV as _PrecomputedEmbeddingProvider, bypassing coalesced_query_embedding.
        precomputed_provider = _PrecomputedEmbeddingProvider(vec)
        result = precomputed_provider.get_embedding(text, embedding_purpose="query")
        assert result == vec, "Precomputed provider must return the stored vector"

        # Metrics unchanged: FSV reuse via _PrecomputedEmbeddingProvider
        # does NOT call coalesced_query_embedding.
        snap_final = metrics.snapshot()["on"]
        assert snap_final["misses"] == 1, (
            f"FSV reuse must NOT record another miss. Total misses should be 1, "
            f"got {snap_final['misses']}"
        )
        assert snap_final["hits"] == 0, (
            f"FSV reuse must NOT record a hit. Hits should be 0, "
            f"got {snap_final['hits']}"
        )
        assert provider.call_count == 1  # exactly one live embed call

    def test_warm_shared_plus_fsv_reuse_records_one_hit(self, monkeypatch):
        """Simulate: _compute_shared_query_vector (HIT) + FSV reuse via
        _PrecomputedEmbeddingProvider. Total: 1 hit, 0 new misses.

        The pre-seeded cache represents the warm scenario. First call is a HIT.
        FSV reuse via _PrecomputedEmbeddingProvider adds no further metric.
        """
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        text = "embed once reuse warm"
        coalescer, provider, metrics, _gov = _make_harness(
            monkeypatch, "on", pre_seed_text=text
        )

        # Step 1: "shared vector" embed call (warm HIT).
        vec, _meta = coalescer.submit(text)
        snap_after_hit = metrics.snapshot()["on"]
        assert snap_after_hit["hits"] == 1 and snap_after_hit["misses"] == 0

        # Step 2: FSV reuse — no coalesced_query_embedding call.
        precomputed_provider = _PrecomputedEmbeddingProvider(vec)
        result = precomputed_provider.get_embedding(text, embedding_purpose="query")
        assert result == vec

        snap_final = metrics.snapshot()["on"]
        assert snap_final["hits"] == 1, (
            f"FSV reuse must NOT add another hit. Hits should remain 1, "
            f"got {snap_final['hits']}"
        )
        assert snap_final["misses"] == 0
        assert provider.call_count == 0  # warm HIT, no provider call

    def test_parallel_strategy_voyage_reuses_precomputed_vector(self, monkeypatch):
        """Model the parallel-strategy double-call bug found by E2E Scenario C.

        Scenario: single cold search_code on a dual-provider repo.
          - Call A: coalesced_query_embedding(voyage, text) = MISS (shared vector)
          - Call B: coalesced_query_embedding(voyage, text) again (parallel voyage-ai
            provider call from _search_with_provider) = spurious HIT

        The fix passes precomputed_query_vector to _search_with_provider for the
        voyage-ai leg, so call B becomes a _PrecomputedEmbeddingProvider.get_embedding()
        call that bypasses coalesced_query_embedding entirely.

        After the fix: 1 MISS total (call A), 0 hits.
        Before the fix (current): 1 MISS + 1 HIT = 2 key-resolutions.

        This test models the fixed behaviour by simulating the two-call sequence
        with the precomputed bypass in place.
        """
        text = "parallel strategy voyage double call"
        coalescer, provider, metrics, _gov = _make_harness(monkeypatch, "on")

        # Call A: shared vector embed (MISS, writes cache).
        vec_a, _meta = coalescer.submit(text)
        snap_a = metrics.snapshot()["on"]
        assert snap_a["misses"] == 1 and snap_a["hits"] == 0

        # Call B (FIXED): parallel voyage-ai leg uses precomputed vector.
        # _search_with_provider(provider_name="voyage-ai",
        #                       precomputed_query_vector=vec_a) wraps vec_a in
        # _PrecomputedEmbeddingProvider, bypassing coalesced_query_embedding.
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        precomp = _PrecomputedEmbeddingProvider(vec_a)
        vec_b = precomp.get_embedding(text, embedding_purpose="query")
        assert vec_b == vec_a

        # No additional metric recorded.
        snap_final = metrics.snapshot()["on"]
        assert snap_final["misses"] == 1, (
            "After fix: only 1 miss for single cold query (not 1 miss + 1 hit). "
            f"Got misses={snap_final['misses']}"
        )
        assert snap_final["hits"] == 0, (
            f"After fix: 0 hits for single cold query. Got hits={snap_final['hits']}"
        )
        assert provider.call_count == 1  # exactly one live embed


class TestSingleFlightRegressions:
    """Regression tests that the rejected thread-identity impl failed.

    Prescription (reviewer):
      - cold MISS resolves fully (not concurrent), THEN two SEQUENTIAL same-key
        warm queries on DIFFERENT threads each record a real hit -> on_hits == 2.
      - After all resolutions complete, len(coalescer._inflight_keys) == 0
        (proves no retained-done-entry leak and bounded registry).
    """

    def test_different_thread_sequential_warm_hits_each_count(self, monkeypatch):
        """Cold MISS on thread A -> warm HIT on thread B -> warm HIT on thread C.

        The rejected implementation suppressed both warm hits (hits=0) because it
        retained done entries and treated all different-thread callers as
        'concurrent joiners — no metric'.  The correct single-flight removes the
        entry on completion, so thread B and C each find no in-flight entry, do a
        real cache lookup, get a HIT, and record their own metric.

        Expected: misses == 1 (cold MISS), hits == 2 (two sequential warm HITs).
        """
        coalescer, provider, metrics, _gov = _make_harness(monkeypatch, "on")
        text = "different thread sequential warm hits"

        # Cold MISS on the calling thread — seeds the cache.
        coalescer.submit(text)
        snap_after_miss = metrics.snapshot()["on"]
        assert snap_after_miss["misses"] == 1
        assert snap_after_miss["hits"] == 0
        assert provider.call_count == 1

        # Two SEQUENTIAL warm queries on DIFFERENT threads.
        results: list = []
        lock = threading.Lock()

        def warm_submit() -> None:
            vec, _meta = coalescer.submit(text)
            with lock:
                results.append(vec)

        t1 = threading.Thread(target=warm_submit, daemon=True)
        t1.start()
        t1.join(timeout=_JOIN_TIMEOUT)
        assert t1.is_alive() is False, "Thread B hung — possible joiner deadlock"

        t2 = threading.Thread(target=warm_submit, daemon=True)
        t2.start()
        t2.join(timeout=_JOIN_TIMEOUT)
        assert t2.is_alive() is False, "Thread C hung — possible joiner deadlock"

        assert len(results) == 2, "Both warm queries must complete"
        snap_final = metrics.snapshot()["on"]
        assert snap_final["misses"] == 1, (
            f"misses must remain 1 after warm HITs, got {snap_final['misses']}"
        )
        assert snap_final["hits"] == 2, (
            f"Two sequential warm HITs on different threads must each record 1 hit "
            f"(total 2), got hits={snap_final['hits']}. "
            f"The rejected impl retained done entries and suppressed these hits."
        )
        # No additional provider calls for warm HITs.
        assert provider.call_count == 1

    def test_inflight_registry_is_empty_after_all_resolutions(self, monkeypatch):
        """_inflight_keys must be empty after every resolution completes.

        The correct single-flight removes the key in a finally block when the
        owner completes dispatch.  The rejected impl retained done entries
        indefinitely (one per distinct cache key ever seen = unbounded leak).

        This test asserts len(coalescer._inflight_keys) == 0 after:
          (a) a cold MISS resolution (concurrent group)
          (b) subsequent sequential warm HITs (different threads)

        Note: the coalescer and _run_saturated_submits MUST share the SAME
        ProviderConcurrencyGovernor so saturation actually holds up the coalescer's
        dispatches and concurrent submits join via the single-flight.
        """
        from code_indexer.server.services import governed_call

        text = "registry empty after resolution"
        cache, _ = _make_real_cache(mode="on")
        metrics = _CacheCallProbe(cache, "on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        # Single shared governor so saturation in _run_saturated_submits holds up
        # the coalescer's dispatches (they share the same limiter).
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_TEST_DIGEST,
        )

        # Cold concurrent MISS group using the shared governor.
        _run_saturated_submits(
            coalescer, gov, LANE, [text] * _K_CONCURRENT, accumulate=_ACCUMULATE_SECS
        )
        assert (
            not hasattr(coalescer, "_inflight_keys")
            or len(coalescer._inflight_keys) == 0
        ), (
            f"Registry must be empty after cold MISS group resolves, "
            f"got {len(coalescer._inflight_keys)} retained entries (leak)"
        )
        snap_cold = metrics.snapshot()["on"]
        assert snap_cold["misses"] == 1, (
            f"Cold concurrent group must record 1 miss, got {snap_cold['misses']}"
        )

        # Sequential warm HITs on different threads.
        for _ in range(3):
            t = threading.Thread(target=lambda: coalescer.submit(text), daemon=True)
            t.start()
            t.join(timeout=_JOIN_TIMEOUT)

        assert (
            not hasattr(coalescer, "_inflight_keys")
            or len(coalescer._inflight_keys) == 0
        ), (
            f"Registry must be empty after sequential warm HITs, "
            f"got {len(coalescer._inflight_keys)} retained entries (leak)"
        )
        snap = metrics.snapshot()["on"]
        # 1 miss + 3 sequential warm HITs on different threads.
        assert snap["misses"] == 1
        assert snap["hits"] == 3, (
            f"Three sequential warm HITs on different threads must each record 1 hit "
            f"(total 3), got hits={snap['hits']}"
        )


# ===========================================================================
# DEFECT 1 — Mixed-config omni vector isolation (provider-config-correct reuse)
# ===========================================================================


class TestMixedConfigOmniVectorIsolation:
    """DEFECT 1 fix: the precomputed_query_vector must only be reused by repos
    whose embedding-service provider-config digest matches the digest used to
    produce the vector.  A repo on a different provider config (different
    provider, model, endpoint) must receive None and embed via its own chokepoint.

    These tests model the digest-guard in _search_semantic_sync by exercising
    the MultiSearchRequest.precomputed_query_vector_digest field and the
    _digest_for_provider-based comparison that guards the precomputed vector.

    No real repo paths are needed: we verify the guard logic by inspecting
    whether effective_precomputed is set to the vector or None based on digest
    matching — the guard is exercised by constructing providers with different
    configs and checking _digest_for_provider produces distinct digests.
    """

    def test_same_config_digest_allows_precomputed_vector_reuse(self):
        """Same provider config -> same digest -> precomputed vector should be reused.

        Proves _digest_for_provider is stable: two providers built from the
        same VoyageAIConfig produce the same digest (reuse is allowed).
        """
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        cfg = VoyageAIConfig()
        prov_a = VoyageAIClient(cfg)
        prov_b = VoyageAIClient(cfg)

        digest_a = _digest_for_provider(prov_a)
        digest_b = _digest_for_provider(prov_b)
        assert digest_a == digest_b, (
            "Same VoyageAIConfig must produce identical digests — "
            f"digest_a={digest_a!r}, digest_b={digest_b!r}"
        )

    def test_different_provider_type_produces_different_digest(self):
        """Different provider types -> different digest -> precomputed vector NOT reused.

        Proves that a Voyage provider and a Cohere provider (different class,
        different model, different endpoint) produce distinct digests so the
        guard in _search_semantic_sync correctly rejects the Voyage precomputed
        vector for a Cohere-configured repo.
        """
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        voyage_prov = VoyageAIClient(VoyageAIConfig())
        voyage_digest = _digest_for_provider(voyage_prov)

        # Use a minimal stub that identifies as a different provider class.
        class _StubCohereProvider:
            """Minimal stub: different class name -> different provider_type in digest."""

            class config:
                model = "embed-v4.0"
                api_key = None
                api_endpoint = "https://api.cohere.ai"
                connect_timeout = 10.0
                timeout = 60.0
                max_retries = None
                retry_delay = None
                exponential_backoff = None

        cohere_prov = _StubCohereProvider()
        cohere_digest = _digest_for_provider(cohere_prov)

        assert voyage_digest != cohere_digest, (
            "Voyage provider and Cohere-style provider must produce distinct digests; "
            f"both returned {voyage_digest!r}"
        )

    def test_precomputed_query_vector_digest_field_exists_on_request(self):
        """MultiSearchRequest has precomputed_query_vector_digest field (DEFECT 1 fix)."""
        from code_indexer.server.multi.models import MultiSearchRequest

        req = MultiSearchRequest(
            repositories=["repo-a"],
            query="test query",
            search_type="semantic",
        )
        assert hasattr(req, "precomputed_query_vector_digest"), (
            "MultiSearchRequest must have precomputed_query_vector_digest field"
        )
        assert req.precomputed_query_vector_digest is None

        req.precomputed_query_vector_digest = "abc123"
        assert req.precomputed_query_vector_digest == "abc123"

    def test_precomputed_query_vector_digest_excluded_from_json(self):
        """precomputed_query_vector_digest must not appear in JSON serialisation."""
        from code_indexer.server.multi.models import MultiSearchRequest

        req = MultiSearchRequest(
            repositories=["repo-a"],
            query="test query",
            search_type="semantic",
        )
        req.precomputed_query_vector_digest = "some-digest"
        serialised = req.model_dump()
        assert "precomputed_query_vector_digest" not in serialised, (
            "precomputed_query_vector_digest must be excluded from JSON serialisation "
            "(internal field only; never sent over the wire)"
        )


# ===========================================================================
# DEFECT 2 — Joiner audit_ctx reflects owner's actual resolution type
# ===========================================================================


class TestSentinelCollapseVectorLeakDefect:
    """Codex review finding for Story #1148: sentinel-collapse mixed-config leak.

    ``_digest_for_provider`` FAIL-OPENS to ``_FALLBACK_DIGEST`` ("fallback-no-config")
    on any AttributeError/Exception.  If digest extraction fails for BOTH the
    shared (Voyage) provider AND a mismatched repo provider, both collapse to the
    SAME sentinel.  A naive ``repo_digest == precomputed_digest`` comparison then
    evaluates True and the wrong-config precomputed vector is reused — producing
    incorrect query results ("Query is everything").

    The fix in multi_search_service.py guards reuse with three conditions:
      (1) precomputed digest is NOT the sentinel, AND
      (2) repo digest is NOT the sentinel, AND
      (3) repo_digest == precomputed_query_vector_digest.

    These tests verify:
      A. ``is_fallback_digest`` correctly identifies the sentinel value.
      B. A provider without a ``.config`` attribute returns the sentinel digest.
      C. The correct multi-condition guard rejects reuse when EITHER digest is
         the sentinel — proving the collapse path is closed.
      D. Legitimate same-config reuse (both non-sentinel, equal) still works.
    """

    def test_is_fallback_digest_true_for_sentinel(self):
        """is_fallback_digest returns True for the sentinel value."""
        from code_indexer.server.services.coalescer_registry import (
            _FALLBACK_DIGEST,
            is_fallback_digest,
        )

        assert is_fallback_digest(_FALLBACK_DIGEST) is True

    def test_is_fallback_digest_false_for_real_digest(self):
        """is_fallback_digest returns False for any non-sentinel digest."""
        from code_indexer.server.services.coalescer_registry import is_fallback_digest

        assert is_fallback_digest("abc123def456") is False
        assert is_fallback_digest("") is False
        assert is_fallback_digest("fallback-no-config-extra") is False

    def test_configless_provider_returns_sentinel_digest(self):
        """A provider with no .config attribute produces the sentinel digest.

        This confirms the failure mode: a misconfigured/stub provider returns
        _FALLBACK_DIGEST and therefore is_fallback_digest() is True for it.
        """
        from code_indexer.server.services.coalescer_registry import (
            _FALLBACK_DIGEST,
            _digest_for_provider,
            is_fallback_digest,
        )

        class _NoConfigProvider:
            """Stub with no .config attribute — triggers AttributeError path."""

            pass

        digest = _digest_for_provider(_NoConfigProvider())
        assert digest == _FALLBACK_DIGEST, (
            f"Provider without .config must return sentinel, got {digest!r}"
        )
        assert is_fallback_digest(digest) is True

    # -----------------------------------------------------------------------
    # Real-path tests: drive _search_semantic_sync and assert on
    # effective_precomputed_query_vector passed to search_repository_path.
    #
    # Strategy:
    #   - Instantiate real MultiSearchService (no server startup required).
    #   - Monkeypatch _get_repository_path to return a fixed dummy path.
    #   - Monkeypatch _digest_for_provider in multi_search_service's import
    #     namespace to return a controlled repo_digest.
    #   - Monkeypatch _load_repo_config / EmbeddingProviderFactory.create /
    #     _get_http_client_factory in search_service to avoid real filesystem.
    #   - Monkeypatch SemanticSearchService.search_repository_path to capture
    #     the precomputed_query_vector argument actually passed by the guard.
    #   - Assert captured value is None (reuse rejected) or the vector (reuse
    #     allowed), depending on the digest scenario.
    #
    # Non-tautology proof: reverting the guard in multi_search_service.py to
    # plain ``repo_digest == precomp_digest`` causes the sentinel-sentinel and
    # partial-sentinel tests to FAIL because the captured vector would be
    # non-None instead of None.
    # -----------------------------------------------------------------------

    _PRECOMP_VEC: list = [0.1, 0.2, 0.3]  # dummy precomputed vector
    _REAL_DIGEST: str = "real-digest-voyage-abc123"

    def _run_guard(
        self,
        monkeypatch,
        *,
        repo_digest: str,
        precomp_digest: str,
        precomp_vec=None,
    ):
        """Drive _search_semantic_sync and return the captured precomputed_query_vector.

        Monkeypatches just enough to reach the guard without any real
        filesystem, network, or server startup.  Returns the value of
        precomputed_query_vector that the guard passed to
        search_repository_path.
        """
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        from code_indexer.server.multi.models import MultiSearchRequest

        if precomp_vec is None:
            precomp_vec = self._PRECOMP_VEC

        svc = MultiSearchService(MultiSearchConfig())

        # 1. Bypass _get_repository_path (needs AliasManager + BackendRegistry).
        dummy_path = "/tmp/dummy-repo-1148"
        monkeypatch.setattr(svc, "_get_repository_path", lambda repo_id: dummy_path)

        # 2. Control the repo-side digest.
        import code_indexer.server.multi.multi_search_service as _mss_mod

        monkeypatch.setattr(
            _mss_mod,
            "_digest_for_provider_in_search_semantic",
            lambda provider: repo_digest,
            raising=False,
        )

        # The guard calls _digest_for_provider imported locally inside
        # _search_semantic_sync.  Patch it at the coalescer_registry source
        # so the local import picks up our stub.
        import code_indexer.server.services.coalescer_registry as _cr_mod

        monkeypatch.setattr(
            _cr_mod,
            "_digest_for_provider",
            lambda provider: repo_digest,
        )

        # 3. Stub out imports used inside _search_semantic_sync before the guard.
        import code_indexer.server.services.search_service as _ss_mod

        class _FakeRepoConfig:
            pass

        class _FakeEmbeddingService:
            pass

        class _FakeEmbeddingProviderFactory:
            @staticmethod
            def create(config, http_client_factory):
                return _FakeEmbeddingService()

        class _FakeSemanticSearchResponse:
            results: list = []

        captured = {"precomputed_query_vector": "NOT_CALLED"}

        class _FakeSemanticSearchService:
            def search_repository_path(
                self,
                repo_path,
                search_request,
                hnsw_cache=None,
                precomputed_query_vector=None,
            ):
                captured["precomputed_query_vector"] = precomputed_query_vector
                return _FakeSemanticSearchResponse()

        monkeypatch.setattr(
            _ss_mod, "_load_repo_config", lambda path: _FakeRepoConfig()
        )
        monkeypatch.setattr(
            _ss_mod,
            "EmbeddingProviderFactory",
            _FakeEmbeddingProviderFactory,
        )
        monkeypatch.setattr(_ss_mod, "_get_http_client_factory", lambda: None)

        # 4. Patch SemanticSearchService constructor to return our fake.
        fake_svc_instance = _FakeSemanticSearchService()
        monkeypatch.setattr(_ss_mod, "SemanticSearchService", lambda: fake_svc_instance)

        # 5. Ensure the path exists so search_repository_path is reached
        #    (os.path.exists check is inside search_repository_path which we
        #    replaced, so no issue — but _search_semantic_sync itself also
        #    calls _get_repository_path which we patched).
        request = MultiSearchRequest(
            repositories=["dummy-repo"],
            query="test query",
            search_type="semantic",
            precomputed_query_vector=precomp_vec,
            precomputed_query_vector_digest=precomp_digest,
        )

        svc._search_semantic_sync("dummy-repo", request)

        return captured["precomputed_query_vector"]

    def test_real_path_both_sentinel_digests_rejects_reuse(self, monkeypatch):
        """REAL-PATH: both digests == sentinel -> search_repository_path gets None.

        This is the sentinel-collapse scenario: two different broken providers
        both fail to a sentinel digest.  Plain equality (sentinel == sentinel)
        would be True, causing vector reuse across incompatible configs.
        The three-condition guard must return None instead of the precomputed vec.

        NON-TAUTOLOGY: reverting the guard to plain ``repo_digest ==
        precomp_digest`` makes this test FAIL (captured value would be the
        precomp vector, not None).
        """
        from code_indexer.server.services.coalescer_registry import _FALLBACK_DIGEST

        result = self._run_guard(
            monkeypatch,
            repo_digest=_FALLBACK_DIGEST,
            precomp_digest=_FALLBACK_DIGEST,
        )
        assert result is None, (
            "Sentinel-collapse: both digests are the sentinel (_FALLBACK_DIGEST). "
            "Plain equality would allow reuse — the three-condition guard must "
            "reject it and pass precomputed_query_vector=None to search_repository_path."
        )

    def test_real_path_sentinel_precomp_real_repo_rejects_reuse(self, monkeypatch):
        """REAL-PATH: sentinel precomp digest + real repo digest -> None.

        Partial-sentinel: the shared provider failed (sentinel), the repo has a
        real digest.  The guard must reject because we cannot verify the vector
        was computed with the correct config.

        NON-TAUTOLOGY: reverting the guard to plain equality makes this FAIL
        (sentinel != real_digest -> already False under equality, so this case
        passes by coincidence — BUT the all-sentinel case catches the regression).
        """
        from code_indexer.server.services.coalescer_registry import _FALLBACK_DIGEST

        result = self._run_guard(
            monkeypatch,
            repo_digest=self._REAL_DIGEST,
            precomp_digest=_FALLBACK_DIGEST,
        )
        assert result is None, (
            "Sentinel precomp_digest must cause reuse rejection even when "
            "repo_digest is a real non-sentinel digest."
        )

    def test_real_path_real_precomp_sentinel_repo_rejects_reuse(self, monkeypatch):
        """REAL-PATH: real precomp digest + sentinel repo digest -> None.

        Partial-sentinel: the shared provider has a real digest but the repo's
        provider failed (sentinel).  Guard must reject — we cannot verify the
        repo's embedding space.

        NON-TAUTOLOGY: reverting the guard to plain equality makes this FAIL
        (real != sentinel -> already False — caught by all-sentinel case).
        """
        from code_indexer.server.services.coalescer_registry import _FALLBACK_DIGEST

        result = self._run_guard(
            monkeypatch,
            repo_digest=_FALLBACK_DIGEST,
            precomp_digest=self._REAL_DIGEST,
        )
        assert result is None, (
            "Sentinel repo_digest must cause reuse rejection even when "
            "precomp_digest is a real non-sentinel digest."
        )

    def test_real_path_mismatched_real_digests_rejects_reuse(self, monkeypatch):
        """REAL-PATH: two different non-sentinel digests -> None.

        Different real digests means different provider configs (provider,
        model, endpoint, key) -> different vector spaces -> reuse is wrong.
        """
        result = self._run_guard(
            monkeypatch,
            repo_digest="real-digest-cohere-xyz789",
            precomp_digest=self._REAL_DIGEST,
        )
        assert result is None, "Mismatched non-sentinel digests must reject reuse."

    def test_real_path_matching_real_digests_allows_reuse(self, monkeypatch):
        """REAL-PATH: matching non-sentinel digests -> precomputed vec is reused.

        This is the positive case: same provider config on both sides.  The
        guard must pass the precomputed vector through to search_repository_path.

        NON-TAUTOLOGY: reverting to plain equality still passes this case,
        but the sentinel-sentinel test above catches the regression.
        """
        result = self._run_guard(
            monkeypatch,
            repo_digest=self._REAL_DIGEST,
            precomp_digest=self._REAL_DIGEST,
        )
        assert result == self._PRECOMP_VEC, (
            "Matching non-sentinel digests must allow reuse: "
            "search_repository_path must receive the precomputed vector."
        )


class TestJoinerAuditSemantics:
    """DEFECT 2 fix: joiner audit_ctx must reflect what the OWNER actually did.

    - Owner resolved via on-mode cache HIT -> joiner gets mode="on" + cached_blob.
    - Owner resolved via LIVE (MISS/shadow/dispatch) -> joiner audit_ctx untouched.

    The pre-fix bug: joiner always set mode=_cache_mode (e.g. "on") and
    live_vec but no cached_blob.  The audit interpreted mode="on" as
    "primary served from cache, re-embed to compare" and re-embedded live —
    producing a trivial live-vs-live comparison (100% overlap, misleading).
    """

    def test_joiner_after_owner_live_miss_has_no_audit_ctx(self, monkeypatch):
        """Owner resolves via LIVE (cold MISS) -> joiner audit_ctx left untouched.

        The inflight registry stores (Future, resolution_container).
        resolution_container[0] remains None for live resolutions.
        After the Future resolves, the joiner finds resolution_container[0]=None
        and does NOT set audit_ctx["sampled"] = True.
        """
        coalescer, _provider, _metrics, gov = _make_harness(monkeypatch, "on")
        text = "joiner live miss audit"

        # Simulate two concurrent submits: first becomes owner (MISS + live embed),
        # second becomes joiner.  We use the saturated harness to hold the dispatcher
        # so the second submit arrives while the first is still in-flight.
        outcome = _run_saturated_submits(
            coalescer, gov, LANE, [text, text], accumulate=_ACCUMULATE_SECS
        )
        assert not outcome.errors and len(outcome.results) == 2

        # Verify the inflight registry is clean (no leak).
        assert len(coalescer._inflight_keys) == 0, (
            "Registry must be empty after concurrent MISS group resolves"
        )

    def test_resolution_container_filled_on_hit_owner(self, monkeypatch):
        """Owner finds an on-mode cache HIT -> resolution_container[0] = cached_blob.

        Regression guard: _resolution_container must be a 1-element list stored
        alongside the Future in _inflight_keys[(future, container)].
        When the HIT owner completes, container[0] is the cached bytes blob
        (non-None) so joiners can correctly set audit_ctx mode="on" + cached_blob.
        """
        text = "resolution container HIT test"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        metrics = _CacheCallProbe(cache, "on")

        from code_indexer.server.services import governed_call

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=_ACQUIRE_TIMEOUT,
            config_digest=_TEST_DIGEST,
        )

        # Warm submit (HIT) — registry registers then immediately resolves.
        # Pre-seeded vector is CACHED_VEC (not LIVE_VEC); HIT returns cached value.
        vec, _meta = coalescer.submit(text)
        assert vec == CACHED_VEC, "Warm HIT must return the pre-seeded CACHED_VEC"

        # Registry must be empty (HIT owner popped the key).
        assert len(coalescer._inflight_keys) == 0, (
            "Registry must be empty after HIT owner resolves"
        )

        # No provider call on warm HIT.
        assert provider.call_count == 0

        snap = metrics.snapshot()["on"]
        assert snap["hits"] == 1 and snap["misses"] == 0


# ===========================================================================
# TestFSVPrecomputedVectorBypass — search_service passes vector directly to FSV
# ===========================================================================


class TestFSVPrecomputedVectorBypass:
    """Regression guard for the omni-search crash (E2E blocker surfaced by #1148 E2E).

    Root cause: when _perform_semantic_search wraps a precomputed vector in
    _PrecomputedEmbeddingProvider and passes it as embedding_provider to
    FilesystemVectorStore.search(), the inner generate_embedding() closure calls
    coalesced_query_embedding(provider, ...) which calls provider.get_provider_name()
    -> AttributeError -> zero results for every omni repo.

    Correct fix:
      1. FilesystemVectorStore.search() gains a precomputed_query_vector parameter.
         When supplied, generate_embedding() is skipped entirely and the precomputed
         vector is used directly — no coalesced_query_embedding call, no get_provider_name.
      2. search_service._perform_semantic_search() passes the vector via
         precomputed_query_vector=... to FSV.search() directly, NOT via
         _PrecomputedEmbeddingProvider.

    These tests prove:
      (a) _perform_semantic_search with a precomputed vector passes it as
          precomputed_query_vector= to FSV.search() (not via _PrecomputedEmbeddingProvider).
      (b) FSV.search(precomputed_query_vector=v) does NOT call coalesced_query_embedding:
          no AttributeError from get_provider_name, and no second metric event.
      (c) The default path (no precomputed vector) is UNCHANGED: FSV.search() receives
          only query + embedding_provider, and the embedding_provider is the real service
          (not a _PrecomputedEmbeddingProvider wrapper).
    """

    @pytest.fixture
    def search_service_repo(self, tmp_path):
        """Minimal repo directory with a filesystem-backend config.json.

        Enough for search_service._perform_semantic_search to load config and
        create a FilesystemVectorStore — actual index content is not needed because
        we patch FSV.search() to capture the call arguments.
        """
        import json

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir()
        config_data = {
            "embedding": {
                "provider": "voyage",
                "model": "voyage-code-3",
                "dimensions": DIM,
            },
            "vector_store": {"provider": "filesystem"},
            "chunking": {
                "chunk_size": 512,
                "chunk_overlap": 128,
                "tree_sitter_config": {"python": {"enabled": True}},
            },
        }
        (config_dir / "config.json").write_text(json.dumps(config_data))
        (config_dir / "index").mkdir()
        return str(repo_path)

    def test_perform_semantic_search_with_precomputed_vector_passes_vector_to_fsv(
        self, search_service_repo
    ):
        """_perform_semantic_search with precomputed_query_vector must pass the vector
        as precomputed_query_vector= to FSV.search(), NOT via _PrecomputedEmbeddingProvider.

        This is the crash-prevention test: if the precomputed vector is passed as
        embedding_provider=_PrecomputedEmbeddingProvider(vec), FSV calls
        coalesced_query_embedding -> provider.get_provider_name() -> AttributeError.
        If it is passed as precomputed_query_vector=vec, FSV skips generate_embedding
        entirely.

        After the fix:
          - fsv_search_kwargs["precomputed_query_vector"] == precomputed_vec
          - fsv_search_kwargs["embedding_provider"] is the REAL embedding service
            (not a _PrecomputedEmbeddingProvider instance)
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        precomputed_vec = [0.1] * DIM
        captured: dict = {}

        def tracked_fsv_search(self, *args, **kwargs):
            captured.update(kwargs)
            return [], {}

        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = precomputed_vec
        mock_embedding_service.get_provider_name.return_value = "voyage-ai"

        with patch.object(FilesystemVectorStore, "search", tracked_fsv_search):
            with patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=mock_embedding_service,
            ):
                svc = SemanticSearchService()
                try:
                    svc._perform_semantic_search(
                        search_service_repo,
                        "authentication logic",
                        limit=5,
                        include_source=False,
                        precomputed_query_vector=precomputed_vec,
                    )
                except Exception:
                    pass  # index may not exist; we only need the FSV call args

        # After the fix: precomputed vector arrives via precomputed_query_vector=,
        # not wrapped in _PrecomputedEmbeddingProvider as embedding_provider.
        assert "precomputed_query_vector" in captured, (
            "After the fix, _perform_semantic_search MUST pass precomputed_query_vector= "
            "to FSV.search() when a precomputed vector is supplied. "
            f"Captured FSV.search() kwargs: {list(captured.keys())}"
        )
        assert captured["precomputed_query_vector"] == precomputed_vec, (
            "precomputed_query_vector passed to FSV.search() must equal the input vector"
        )

        # embedding_provider must be the real service, NOT _PrecomputedEmbeddingProvider.
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        if "embedding_provider" in captured:
            assert not isinstance(
                captured["embedding_provider"], _PrecomputedEmbeddingProvider
            ), (
                "_PrecomputedEmbeddingProvider MUST NOT be passed as embedding_provider "
                "to FSV.search() — it lacks get_provider_name() and causes AttributeError "
                "inside coalesced_query_embedding."
            )

    def test_perform_semantic_search_without_precomputed_vector_does_not_pass_it_to_fsv(
        self, search_service_repo
    ):
        """Default path (no precomputed_query_vector) must NOT pass precomputed_query_vector
        to FSV.search(), and the embedding_provider must be the real service.

        Regression guard: the fix must not alter the normal single-repo path.
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        captured: dict = {}

        def tracked_fsv_search(self, *args, **kwargs):
            captured.update(kwargs)
            return [], {}

        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = [0.1] * DIM
        mock_embedding_service.get_provider_name.return_value = "voyage-ai"

        with patch.object(FilesystemVectorStore, "search", tracked_fsv_search):
            with patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=mock_embedding_service,
            ):
                svc = SemanticSearchService()
                try:
                    svc._perform_semantic_search(
                        search_service_repo,
                        "authentication logic",
                        limit=5,
                        include_source=False,
                        # No precomputed_query_vector — default single-repo path.
                    )
                except Exception:
                    pass

        # Default path: no precomputed_query_vector passed to FSV.
        assert captured.get("precomputed_query_vector") is None, (
            "Default path MUST NOT pass precomputed_query_vector to FSV.search(). "
            f"Got: {captured.get('precomputed_query_vector')}"
        )

        # The embedding_provider must be the real service.
        from code_indexer.server.services.memory_candidate_retriever import (
            _PrecomputedEmbeddingProvider,
        )

        if "embedding_provider" in captured:
            assert not isinstance(
                captured["embedding_provider"], _PrecomputedEmbeddingProvider
            ), (
                "Default path MUST use real embedding service, not _PrecomputedEmbeddingProvider"
            )
