"""Story #1148 — Hit/miss counted ONCE per key-resolution (metric level).

Explicit AC-level test coverage proving production already satisfies every AC.
Stories #1146, #1147, #1149 delivered the production behaviour; this file
provides the dedicated #1148 metric-counter assertions.

AC mapping:
  AC1  TestAC1OmniColdOneMiss          K concurrent same-key COLD -> 1 miss
  AC2  TestAC2OmniWarmHits             K concurrent same-key WARM -> K hits (one per
                                        requestor — on-mode HITs return before enqueue)
  AC3  TestAC3SingleRepoOneCount       single-repo query -> exactly 1 counter delta
  AC4  TestAC4TwoProviderConfigsTwoRec two config-digests -> 2 records
  AC5  TestAC5OverCapMissPlusLongKey   over-cap query -> 1 MISS + 1 long_key
  AC6  TestAC6ShadowOneCosinePerKey    shadow mode -> 1 cosine per key-resolution
  AC7  TestAC7DashboardMathUnchanged   snapshot shape + ratio formula unchanged
  AC8  TestAC8AuditAxisSeparate        audit counters on own axis, not in hit/miss
  AC9  TestAC9ClusterPerNode           cluster metrics per-node (shape/independence)

Design invariants:
  - Real EmbeddingCoalescer, real in-memory cache backend (dict, no DB), real
    QueryEmbeddingCacheMetrics with a no-op OTEL stub (not MagicMock).
  - Monkeypatching of governed_call.get_query_embedding_cache and
    get_query_embedding_cache_metrics is required because those are process-global
    accessors set by lifespan; there is no constructor injection path for them.
  - Governor-saturation harness and fake provider/backend imported from
    test_coalescer_cache_1147 to avoid duplication (Messi #4).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import pytest

from tests.unit.server.services.test_coalescer_cache_1147 import (
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
from code_indexer.server.services.query_embedding_cache_metrics import (
    QueryEmbeddingCacheMetrics,
)

# ---------------------------------------------------------------------------
# Named constants (no magic numbers in tests)
# ---------------------------------------------------------------------------

_K_CONCURRENT: int = 5  # requestor concurrency for omni-style tests
_JOIN_TIMEOUT: float = 10.0  # thread join timeout (seconds)
_ACCUMULATE_SECS: float = 0.2  # saturation harness accumulation window
_ACQUIRE_TIMEOUT: float = 5.0  # coalescer governor acquire timeout
_OVER_CAP_TOKENS: int = 260  # 260 single-char tokens -> normalised > 256 chars

# Second config-digest representing a different provider configuration.
_DIGEST_B: str = "digest_provider_config_B_1148"

# Over-cap text: normalised form exceeds the 256-char cap so build_key returns None.
_OVER_CAP_TEXT: str = " ".join(["x"] * _OVER_CAP_TOKENS)


# ---------------------------------------------------------------------------
# Lightweight no-op OTEL meter stub (real object; no MagicMock)
# ---------------------------------------------------------------------------


class _NoOpCounter:
    def add(self, amount: int, attrs: Optional[dict] = None) -> None:
        pass


class _NoOpHistogram:
    def record(self, value: float, attrs: Optional[dict] = None) -> None:
        pass


class _NoOpMeter:
    """Minimal stub satisfying QueryEmbeddingCacheMetrics._register().

    _register() calls create_counter / create_histogram / create_observable_gauge.
    All return no-op objects; the in-process tallies (_tallies, _cosine_buffer, …)
    still function normally — only OTEL SDK forwarding is suppressed.
    """

    def create_counter(self, *, name: str, description: str = "", unit: str = "1"):
        return _NoOpCounter()

    def create_histogram(self, *, name: str, description: str = "", unit: str = "1"):
        return _NoOpHistogram()

    def create_observable_gauge(
        self,
        *,
        name: str,
        description: str = "",
        unit: str = "1",
        callbacks: tuple = (),
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# Minimal provider for governed_call Path B tests (AC5)
# ---------------------------------------------------------------------------


class _MinimalProvider:
    """Bare provider satisfying governed_call.coalesced_query_embedding (Path B)."""

    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_current_model(self) -> str:
        return MODEL

    def get_model_info(self) -> dict:
        return {"dimensions": DIM}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _real_metrics() -> QueryEmbeddingCacheMetrics:
    """Real QueryEmbeddingCacheMetrics backed by the no-op OTEL stub."""
    return QueryEmbeddingCacheMetrics(_NoOpMeter(), total_entries_fn=lambda: 0)


def _make_harness(
    monkeypatch,
    mode: str,
    pre_seed_text: Optional[str] = None,
    config_digest: str = _TEST_DIGEST,
) -> tuple:
    """Wire (coalescer, provider, metrics, gov) via process-global accessors.

    Monkeypatching is required here: get_query_embedding_cache() and
    get_query_embedding_cache_metrics() are module-level singletons set only
    by lifespan startup — there is no constructor injection path.

    Returns (coalescer, provider, metrics, gov).
    Callers that use _run_saturated_submits MUST pass the returned gov to
    the saturation harness so that concurrent submits are held pending during
    the accumulation window (standard single-flight requires the coalescer and
    saturation harness to share the same ProviderConcurrencyGovernor).
    """
    from code_indexer.server.services import governed_call

    cache, _ = _make_real_cache(mode=mode, pre_seed_text=pre_seed_text)
    metrics = _real_metrics()
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    monkeypatch.setattr(
        governed_call, "get_query_embedding_cache_metrics", lambda: metrics
    )
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


def _path_b_metrics(monkeypatch) -> QueryEmbeddingCacheMetrics:
    """Wire governed_call for Path B (no coalescer) and return the metrics object."""
    from code_indexer.server.services import governed_call
    from code_indexer.server.services.coalescer_registry import clear_coalescer_registry

    clear_coalescer_registry()
    cache, _ = _make_real_cache(mode="on")
    metrics = _real_metrics()
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    monkeypatch.setattr(
        governed_call, "get_query_embedding_cache_metrics", lambda: metrics
    )
    monkeypatch.setattr(
        governed_call,
        "governed_query_embedding",
        lambda p, t, *, embedding_purpose=None, acquire_timeout=30.0: LIVE_VEC,
        raising=False,
    )
    return metrics


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
    governed_call.clear_query_embedding_cache_metrics()
    clear_coalescer_registry()
    reset_config_service()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
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
# AC2 — K concurrent same-key WARM -> on_hits increases by K (one per requestor)
# ===========================================================================


class TestAC2OmniWarmHits:
    """AC2: K concurrent on-mode WARM submits -> hits == K.

    On-mode HITs return before _enqueue() (lock-free pre-enqueue check in
    submit()).  Each requestor independently checks the cache and calls
    record_hit() once.  HITs are NOT coalesced — only MISSes coalesce via
    _dispatch.  Therefore K concurrent same-key warm submits produce K hit
    records (one per requestor).
    """

    def _run_k_warm(self, coalescer, text: str) -> int:
        """Submit the same pre-seeded text K times concurrently; return hit count."""
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
        """K concurrent WARM submits -> hits == K, misses == 0."""
        text = "AC2 omni warm same key"
        coalescer, provider, metrics, _ = _make_harness(
            monkeypatch, "on", pre_seed_text=text
        )

        done = self._run_k_warm(coalescer, text)
        assert done == _K_CONCURRENT

        snap = metrics.snapshot()["on"]
        assert snap["hits"] == _K_CONCURRENT, (
            f"AC2: {_K_CONCURRENT} concurrent WARM submits must each record 1 hit "
            f"(no coalescing of HITs), got hits={snap['hits']}"
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
        metrics = _real_metrics()
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        monkeypatch.setattr(
            governed_call, "get_query_embedding_cache_metrics", lambda: metrics
        )

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

        metrics = _real_metrics()
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        monkeypatch.setattr(
            governed_call, "get_query_embedding_cache_metrics", lambda: metrics
        )

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


class TestAC5OverCapMissPlusLongKey:
    """AC5: build_key returns None (over-cap) -> 1 MISS + 1 long_key in the shim.

    Handled in governed_call.coalesced_query_embedding Step-1 before the
    coalescer routing decision (Path B — no coalescer wired for these tests).
    """

    def test_over_cap_records_one_miss_and_one_long_key(self, monkeypatch):
        from code_indexer.server.services import governed_call

        metrics = _path_b_metrics(monkeypatch)
        governed_call.coalesced_query_embedding(_MinimalProvider(), _OVER_CAP_TEXT)

        snap = metrics.snapshot()
        assert snap["on"]["misses"] == 1, (
            f"AC5: over-cap must record 1 MISS, got misses={snap['on']['misses']}"
        )
        assert snap["long_key"] == 1, (
            f"AC5: over-cap must increment long_key by 1, got long_key={snap['long_key']}"
        )
        assert snap["on"]["hits"] == 0

    def test_over_cap_miss_counts_in_hit_ratio_denominator(self, monkeypatch):
        """Over-cap MISS contributes to hits/(hits+misses) denominator (ratio is honest)."""
        from code_indexer.server.services import governed_call

        metrics = _path_b_metrics(monkeypatch)
        governed_call.coalesced_query_embedding(_MinimalProvider(), _OVER_CAP_TEXT)

        on = metrics.snapshot()["on"]
        denominator = on["hits"] + on["misses"]
        assert denominator == 1, (
            f"AC5: over-cap miss must contribute 1 to denominator, got {denominator}"
        )


# ===========================================================================
# AC6 — Shadow mode records exactly ONE shadow cosine per key-resolution
# ===========================================================================


class TestAC6ShadowOneCosinePerKey:
    """AC6: record_shadow_cosine fires exactly once per unique key in _dispatch.

    K concurrent same-key shadow submits produce 1 shadow HIT record and 1
    cosine (the dispatch loop iterates over key_to_first_idx, not my_batch).
    """

    def test_single_shadow_hit_records_one_cosine(self, monkeypatch):
        text = "AC6 single shadow cosine"
        coalescer, _, metrics, _gov = _make_harness(
            monkeypatch, "shadow", pre_seed_text=text
        )
        coalescer.submit(text)

        snap = metrics.snapshot()
        assert snap["shadow_cosine_p50"] is not None, (
            "AC6: shadow HIT must record a cosine (p50 must not be None)"
        )

    def test_k_concurrent_same_key_shadow_records_one_cosine(self, monkeypatch):
        """K concurrent same-key shadow submits -> 1 shadow hit + 1 cosine."""
        text = "AC6 K concurrent shadow cosine"
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
        assert snap["shadow_cosine_p50"] is not None


# ===========================================================================
# AC7 — Dashboard math and snapshot() shape are unchanged
# ===========================================================================


class TestAC7DashboardMathUnchanged:
    """AC7: snapshot() shape and hits/(hits+misses) formula are unchanged by #1148."""

    _REQUIRED_KEYS = frozenset(
        {
            "shadow",
            "on",
            "shadow_cosine_p50",
            "audit_total",
            "audit_top1_matches",
            "audit_overlap_avg",
            "long_key",
        }
    )

    def test_snapshot_key_set_is_exactly_documented(self):
        snap = _real_metrics().snapshot()
        assert set(snap.keys()) == self._REQUIRED_KEYS, (
            f"AC7: snapshot() key set changed. "
            f"Extra: {set(snap.keys()) - self._REQUIRED_KEYS}, "
            f"Missing: {self._REQUIRED_KEYS - set(snap.keys())}"
        )

    def test_mode_dicts_contain_hits_and_misses(self):
        snap = _real_metrics().snapshot()
        for mode in ("on", "shadow"):
            d = snap[mode]
            assert isinstance(d, dict) and "hits" in d and "misses" in d
            assert isinstance(d["hits"], int) and isinstance(d["misses"], int)

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
    """AC8: record_audit() and record_hit/miss use completely separate counters."""

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

    def test_record_audit_does_not_touch_hit_miss_tallies(self):
        metrics = _real_metrics()
        snap_before = {
            k: dict(v) if isinstance(v, dict) else v
            for k, v in metrics.snapshot().items()
        }

        for _ in range(3):
            metrics.record_audit(
                top10_overlap=0.8,
                top1_match=True,
                provider=PROVIDER_NAME,
                mode="on",
            )

        snap = metrics.snapshot()
        assert snap["on"] == snap_before["on"], "AC8: record_audit must NOT modify 'on'"
        assert snap["shadow"] == snap_before["shadow"]
        assert snap["audit_total"] == 3

    def test_hit_miss_does_not_touch_audit_tallies(self, monkeypatch):
        coalescer, _, metrics, _gov = _make_harness(monkeypatch, "on")
        coalescer.submit("AC8 miss A")
        coalescer.submit("AC8 miss B")

        snap = metrics.snapshot()
        assert snap["on"]["misses"] == 2
        assert snap["audit_total"] == 0, (
            "AC8: hit/miss recording must NOT increment audit_total"
        )


# ===========================================================================
# AC9 — Cluster metrics remain per-node (independence + shape)
# ===========================================================================


class TestAC9ClusterPerNode:
    """AC9: Metrics are per-node; no cross-node aggregation in snapshot()."""

    def test_two_objects_accumulate_independently(self):
        """Two metrics objects ('two nodes') keep completely separate tallies."""
        node1 = _real_metrics()
        node2 = _real_metrics()

        for _ in range(3):
            node1.record_hit(mode="on", provider=PROVIDER_NAME)
        for _ in range(5):
            node2.record_miss(mode="on", provider=PROVIDER_NAME)

        s1 = node1.snapshot()["on"]
        s2 = node2.snapshot()["on"]
        assert s1["hits"] == 3 and s1["misses"] == 0
        assert s2["hits"] == 0 and s2["misses"] == 5

    def test_snapshot_has_no_cross_node_keys(self):
        snap = _real_metrics().snapshot()
        for bad_key in ("cluster_hits", "cluster_misses", "node_id", "nodes"):
            assert bad_key not in snap, (
                f"AC9: snapshot() must NOT contain cross-node key '{bad_key}'"
            )


# ===========================================================================
# Story #1148 single-flight regression — different-thread sequential warm hits
# and registry-empty (leak-free) invariant.
# ===========================================================================


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
            vec = coalescer.submit(text)
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
        metrics = _real_metrics()
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        monkeypatch.setattr(
            governed_call, "get_query_embedding_cache_metrics", lambda: metrics
        )
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
