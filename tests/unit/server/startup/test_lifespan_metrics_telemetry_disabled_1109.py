"""Regression tests: QueryEmbeddingCacheMetrics is wired even when telemetry is disabled.

Bug: the lifespan.py wiring was gated on `telemetry_manager is not None`, so the dashboard
cache-metrics cards stayed permanently blank in the production/staging default (no OTEL).

Fix: build QueryEmbeddingCacheMetrics whenever the cache is wired, passing meter=None when
telemetry is disabled. The in-process tallies must update unconditionally; only the OTEL
.add()/.record() calls are skipped when instruments are None.

Guards:
  D1  lifespan source does NOT gate set_query_embedding_cache_metrics exclusively on telemetry
  D2  lifespan source passes meter=None (or conditional meter) to constructor
  D3  meter=None construction succeeds, instruments are None
  D4  record_hit with meter=None updates in-process tally
  D5  record_miss with meter=None updates in-process tally
  D6  record_shadow_cosine with meter=None updates cosine buffer
  D7  record_audit with meter=None updates audit tallies
  D8  snapshot() reflects all tallies when meter=None (full dashboard data)
  D9  shadow hit via _serve_with_cache with meter=None metrics increments tally
"""

from __future__ import annotations

import struct
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


# ---------------------------------------------------------------------------
# D1 / D2 — source-text guards: wiring must not be exclusively telemetry-gated
# ---------------------------------------------------------------------------


class TestLifespanWiresMetricsWithoutTelemetry:
    def test_set_metrics_not_gated_exclusively_on_telemetry_manager(self):
        """lifespan.py must NOT contain `if telemetry_manager is not None and _wired_cache`.

        The old condition prevented metrics from being built when telemetry was off.
        After the fix the cache-only guard `if _wired_cache is not None:` must be used
        (telemetry enables the OTEL meter, not the metrics object itself).
        """
        source = _LIFESPAN_PATH.read_text()
        # The old broken guard — must not appear
        assert (
            "telemetry_manager is not None and _wired_cache is not None:" not in source
        ), (
            "lifespan.py still gates QueryEmbeddingCacheMetrics on telemetry_manager. "
            "The fix must build metrics whenever the cache is wired (meter=None when "
            "telemetry is disabled)."
        )

    def test_lifespan_passes_none_meter_when_telemetry_disabled(self):
        """lifespan.py must pass meter=None (or a conditional meter expression) to
        QueryEmbeddingCacheMetrics when telemetry is not available.

        After the fix, the source must contain a pattern like:
            _cache_meter = telemetry_manager.get_meter(...) if telemetry_manager is not None else None
        or equivalent conditional.
        """
        source = _LIFESPAN_PATH.read_text()
        # The fix must have some form of conditional meter — either ternary or if/else
        # We check that meter=None is a possible code path (the else None or similar)
        has_meter_none = (
            "else None" in source  # ternary: ... if condition else None
            or "meter=None" in source  # direct kwarg
            or "_cache_meter = None" in source  # explicit None assignment
        )
        assert has_meter_none, (
            "lifespan.py must produce meter=None when telemetry is disabled. "
            "Expected a ternary 'else None', 'meter=None', or '_cache_meter = None' pattern."
        )

    def test_cache_only_guard_present_in_source(self):
        """After the fix, the metrics block must be guarded by cache presence only."""
        source = _LIFESPAN_PATH.read_text()
        # The cache-only guard must exist
        assert "_wired_cache is not None" in source, (
            "lifespan.py must check _wired_cache is not None (cache-only guard)"
        )


# ---------------------------------------------------------------------------
# D3 — meter=None construction
# ---------------------------------------------------------------------------


class TestMeterNoneConstruction:
    def test_construction_with_meter_none_succeeds(self):
        """QueryEmbeddingCacheMetrics(meter=None, ...) must not raise."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        assert m is not None

    def test_otel_instruments_are_none_when_meter_is_none(self):
        """All OTEL instruments must be None when meter=None."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        assert m._hits_counter is None
        assert m._misses_counter is None
        assert m._shadow_cosine_hist is None
        assert m._audit_top10_overlap_hist is None
        assert m._audit_top1_counter is None


# ---------------------------------------------------------------------------
# D4 / D5 — in-process tallies work without OTEL
# ---------------------------------------------------------------------------


class TestTalliesWithMeterNone:
    def _make(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        return QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)

    def test_record_hit_updates_shadow_tally(self):
        m = self._make()
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="shadow", provider="voyage-ai")
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 2, (
            f"Expected 2 shadow hits, got {snap['shadow']['hits']} "
            "(meter=None tallies not updating)"
        )

    def test_record_hit_updates_on_tally(self):
        m = self._make()
        m.record_hit(mode="on", provider="cohere")
        snap = m.snapshot()
        assert snap["on"]["hits"] == 1

    def test_record_miss_updates_shadow_tally(self):
        m = self._make()
        m.record_miss(mode="shadow", provider="voyage-ai")
        snap = m.snapshot()
        assert snap["shadow"]["misses"] == 1

    def test_record_miss_updates_on_tally(self):
        m = self._make()
        m.record_miss(mode="on", provider="cohere")
        snap = m.snapshot()
        assert snap["on"]["misses"] == 1

    def test_shadow_and_on_tallies_independent(self):
        m = self._make()
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_miss(mode="on", provider="voyage-ai")
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 2
        assert snap["shadow"]["misses"] == 0
        assert snap["on"]["hits"] == 0
        assert snap["on"]["misses"] == 1


# ---------------------------------------------------------------------------
# D6 — shadow cosine buffer works without OTEL
# ---------------------------------------------------------------------------


class TestShadowCosineWithMeterNone:
    def test_record_shadow_cosine_updates_buffer(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        cached_blob = struct.pack("<3f", 1.0, 0.0, 0.0)
        m.record_shadow_cosine(cached_blob=cached_blob, live_vec=[1.0, 0.0, 0.0])
        snap = m.snapshot()
        assert snap["shadow_cosine_p50"] is not None, (
            "shadow_cosine_p50 must not be None after recording a cosine (meter=None path)"
        )
        assert abs(snap["shadow_cosine_p50"] - 1.0) < 1e-5

    def test_cosine_p50_none_before_any_records(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        snap = m.snapshot()
        assert snap["shadow_cosine_p50"] is None


# ---------------------------------------------------------------------------
# D7 — audit tallies work without OTEL
# ---------------------------------------------------------------------------


class TestAuditTalliesWithMeterNone:
    def test_record_audit_increments_total(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        m.record_audit(
            top10_overlap=0.8, top1_match=True, provider="voyage-ai", mode="shadow"
        )
        snap = m.snapshot()
        assert snap["audit_total"] == 1

    def test_record_audit_top1_match_counted(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        m.record_audit(
            top10_overlap=1.0, top1_match=True, provider="voyage-ai", mode="on"
        )
        m.record_audit(
            top10_overlap=0.5, top1_match=False, provider="voyage-ai", mode="on"
        )
        snap = m.snapshot()
        assert snap["audit_total"] == 2
        assert snap["audit_top1_matches"] == 1

    def test_record_audit_overlap_avg(self):
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        m.record_audit(
            top10_overlap=0.8, top1_match=False, provider="cohere", mode="shadow"
        )
        m.record_audit(
            top10_overlap=0.6, top1_match=False, provider="cohere", mode="shadow"
        )
        snap = m.snapshot()
        assert snap["audit_overlap_avg"] is not None
        assert abs(snap["audit_overlap_avg"] - 0.7) < 1e-9


# ---------------------------------------------------------------------------
# D8 — full snapshot() with meter=None matches dashboard expectations
# ---------------------------------------------------------------------------


class TestFullSnapshotWithMeterNone:
    def test_full_snapshot_reflects_all_tallies(self):
        """All dashboard-relevant fields must be populated with meter=None."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        m = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_miss(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="on", provider="cohere")
        m.record_miss(mode="on", provider="cohere")
        m.record_miss(mode="on", provider="cohere")

        cached_blob = struct.pack("<2f", 0.6, 0.8)
        m.record_shadow_cosine(cached_blob=cached_blob, live_vec=[0.6, 0.8])

        m.record_audit(
            top10_overlap=0.9, top1_match=True, provider="voyage-ai", mode="shadow"
        )

        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 2
        assert snap["shadow"]["misses"] == 1
        assert snap["on"]["hits"] == 1
        assert snap["on"]["misses"] == 2
        assert snap["shadow_cosine_p50"] is not None
        assert snap["audit_total"] == 1
        assert snap["audit_top1_matches"] == 1
        assert snap["audit_overlap_avg"] is not None


# ---------------------------------------------------------------------------
# D9 — _serve_with_cache with meter=None metrics increments tally (shadow hit)
# ---------------------------------------------------------------------------


class TestServeWithCacheMeterNoneMetrics:
    def test_shadow_hit_increments_tally_via_serve_with_cache(self):
        """_serve_with_cache with meter=None metrics must still increment shadow hit tally."""
        import struct as _struct
        from code_indexer.server.services.governed_call import _serve_with_cache
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            CacheQualifier,
            build_key,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        # Build a real in-memory cache pre-seeded with a cached blob
        class _Backend:
            def __init__(self):
                self._store: dict = {}

            def lookup(self, key, provider, model, dimension):
                return self._store.get((key, provider, model, dimension))

            def upsert(
                self, key, provider, model, dimension, blob, last_used, created_at
            ):
                self._store[(key, provider, model, dimension)] = blob

            def touch_last_used(self, key, provider, model, dimension, ts):
                pass

            def prune_to_max(self, max_entries):
                pass

            def total_entries(self):
                return len(self._store)

        backend = _Backend()
        cache = QueryEmbeddingCache(backend, enabled=True, voyage_mode="shadow")
        cache.mode_for = lambda pn: "shadow"  # type: ignore[method-assign]

        provider = "voyage-ai"
        model = "voyage-code-3"
        dim = 3
        text = "test query for shadow hit"
        key = build_key(text)
        qualifier = CacheQualifier(provider, model, dim)

        # Pre-seed a cached embedding
        cached_vec = [1.0, 0.0, 0.0]
        cached_blob = _struct.pack("<3f", *cached_vec)
        backend._store[(key, provider, model, dim)] = cached_blob

        live_vec = [0.6, 0.8, 0.0]
        metrics = QueryEmbeddingCacheMetrics(None, total_entries_fn=lambda: 0)

        _serve_with_cache(
            cache, provider, key, qualifier, lambda: live_vec, metrics=metrics
        )

        snap = metrics.snapshot()
        assert snap["shadow"]["hits"] == 1, (
            f"Expected shadow hit tally=1, got {snap['shadow']['hits']} "
            "(meter=None metrics not recording via _serve_with_cache)"
        )
