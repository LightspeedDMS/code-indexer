"""Story #1109 (S5) regression guard: lifespan wires QueryEmbeddingCacheMetrics.

Mirrors test_lifespan_query_embedding_cache_wiring_1105.py.

Guards:
  G1  set_query_embedding_cache_metrics present in lifespan startup (source text)
  G2  clear_query_embedding_cache_metrics present in lifespan shutdown (source text)
  G3  set_before_yield / clear_after_yield ordering invariant
  G4  accessor is None when telemetry disabled (functional)
  G5  accessor is set when telemetry enabled (functional, InMemory reader)
  G5b lifespan source constructs QueryEmbeddingCacheMetrics inside telemetry-enabled block
  G5c lifespan source does NOT set metrics when telemetry is disabled
  G6  dashboard handler reads REAL snapshot() data (not hard-coded zeros)
  G7  gauge callback uses cheap memo — never calls backend.total_entries()
  G8  snapshot() per-mode tallies are correct
  G9  snapshot() is thread-safe under concurrent writes
  G10 coalesced_query_embedding passes wired metrics into _serve_with_cache
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List
import struct

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


# ---------------------------------------------------------------------------
# G1-G3 source-text guards
# ---------------------------------------------------------------------------


class TestLifespanMetricsWiringSourceText:
    def test_set_present_in_startup(self):
        source = _LIFESPAN_PATH.read_text()
        assert "set_query_embedding_cache_metrics" in source, (
            "lifespan.py must install cache metrics via "
            "set_query_embedding_cache_metrics(...)"
        )

    def test_clear_present_in_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert "clear_query_embedding_cache_metrics" in source, (
            "lifespan.py must clear cache metrics on shutdown via "
            "clear_query_embedding_cache_metrics()"
        )

    def test_set_before_yield_and_clear_after_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        set_pos = source.find("set_query_embedding_cache_metrics")
        clear_pos = source.find("clear_query_embedding_cache_metrics")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert set_pos != -1, "set_query_embedding_cache_metrics not found"
        assert clear_pos != -1, "clear_query_embedding_cache_metrics not found"
        assert set_pos < yield_pos, (
            "set_query_embedding_cache_metrics must run during STARTUP (before yield)"
        )
        assert clear_pos > yield_pos, (
            "clear_query_embedding_cache_metrics must run during SHUTDOWN (after yield)"
        )

    def test_query_embedding_cache_metrics_class_imported_in_lifespan(self):
        source = _LIFESPAN_PATH.read_text()
        assert "QueryEmbeddingCacheMetrics" in source, (
            "lifespan.py must construct a QueryEmbeddingCacheMetrics instance"
        )

    def test_metrics_built_inside_telemetry_enabled_block(self):
        """QueryEmbeddingCacheMetrics must be instantiated inside the telemetry-enabled
        branch (after 'telemetry_manager.get_meter' or under 'is_initialized' gate)."""
        source = _LIFESPAN_PATH.read_text()
        # Both the QueryEmbeddingCacheMetrics construction and get_meter call must exist
        assert "QueryEmbeddingCacheMetrics" in source, (
            "QueryEmbeddingCacheMetrics construction must appear in lifespan.py"
        )
        # The telemetry-enabled path uses get_meter — must appear somewhere near the metrics build
        assert "get_meter" in source, (
            "lifespan.py must call telemetry_manager.get_meter() to obtain the meter "
            "for QueryEmbeddingCacheMetrics construction"
        )

    def test_set_metrics_inside_telemetry_enabled_scope(self):
        """set_query_embedding_cache_metrics must appear AFTER the telemetry_manager
        initialization block (i.e. it must be in the enabled branch)."""
        source = _LIFESPAN_PATH.read_text()
        # Telemetry init starts after 'Startup: Initialize TelemetryManager'
        telemetry_init_pos = source.find("Initialize TelemetryManager")
        set_metrics_pos = source.find("set_query_embedding_cache_metrics")
        # set must appear after telemetry init
        assert telemetry_init_pos != -1
        assert set_metrics_pos > telemetry_init_pos, (
            "set_query_embedding_cache_metrics must appear after telemetry initialization"
        )


# ---------------------------------------------------------------------------
# G4 — accessor stays None when telemetry disabled
# ---------------------------------------------------------------------------


class TestAccessorNoneWhenTelemetryDisabled:
    def setup_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def teardown_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def test_accessor_returns_none_before_set(self):
        from code_indexer.server.services.governed_call import (
            get_query_embedding_cache_metrics,
        )

        assert get_query_embedding_cache_metrics() is None

    def test_accessor_returns_none_after_clear(self):
        from code_indexer.server.services.governed_call import (
            get_query_embedding_cache_metrics,
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )

        set_query_embedding_cache_metrics(object())
        clear_query_embedding_cache_metrics()
        assert get_query_embedding_cache_metrics() is None


# ---------------------------------------------------------------------------
# G5 — accessor is set when telemetry enabled (functional, InMemory reader)
# ---------------------------------------------------------------------------


class TestAccessorSetWhenTelemetryEnabled:
    def setup_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def teardown_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def test_set_and_get_roundtrip(self):
        from code_indexer.server.services.governed_call import (
            get_query_embedding_cache_metrics,
            set_query_embedding_cache_metrics,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )
        from unittest.mock import MagicMock

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        m = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)
        set_query_embedding_cache_metrics(m)
        assert get_query_embedding_cache_metrics() is m

    def test_real_otel_meter_produces_valid_metrics_object(self):
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from code_indexer.server.services.governed_call import (
            get_query_embedding_cache_metrics,
            set_query_embedding_cache_metrics,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        meter = provider.get_meter("cidx.cache")
        m = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 5)
        set_query_embedding_cache_metrics(m)

        retrieved = get_query_embedding_cache_metrics()
        assert retrieved is m
        retrieved.record_hit(mode="shadow", provider="voyage-ai")
        data = reader.get_metrics_data()
        assert data is not None
        names = {
            metric.name
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
        }
        assert "cidx.cache.embedding.hits" in names


# ---------------------------------------------------------------------------
# G6 — dashboard reads real snapshot() data (not hard-coded zeros)
# ---------------------------------------------------------------------------


class TestDashboardReadsRealSnapshot:
    def setup_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def teardown_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache_metrics()

    def test_snapshot_mode_tallies_reach_dashboard_handler(self):
        """When metrics has recorded hits/misses, snapshot() returns non-zero values."""
        from unittest.mock import MagicMock
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache_metrics,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        m = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 10)
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_miss(mode="on", provider="voyage-ai")
        set_query_embedding_cache_metrics(m)

        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 2
        assert snap["shadow"]["misses"] == 0
        assert snap["on"]["misses"] == 1
        assert snap["on"]["hits"] == 0

    def test_snapshot_returns_zeros_when_no_records(self):
        from unittest.mock import MagicMock
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        m = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 0
        assert snap["shadow"]["misses"] == 0
        assert snap["on"]["hits"] == 0
        assert snap["on"]["misses"] == 0
        assert snap["shadow_cosine_p50"] is None

    def test_dashboard_handler_uses_snapshot_not_hardcoded_zeros(self):
        """The dashboard handler source must not hardcode zeros for hits.

        Story #1294: shadow_hits/shadow_misses are now derived from
        get_windowed_metrics() (search_embed_event), not
        QueryEmbeddingCacheMetrics.snapshot() — this assertion was updated to
        match the new real data source instead of the retired one.
        """
        import inspect
        from code_indexer.server.web import routes as routes_module

        source = inspect.getsource(routes_module.dashboard_cache_metrics_partial)
        assert "shadow_hits=0," not in source, (
            "dashboard handler must not hardcode shadow_hits=0 — use get_windowed_metrics()"
        )
        assert "on_hits=0," not in source, (
            "dashboard handler must not hardcode on_hits=0 — use get_hit_rate_counts()"
        )
        assert "get_windowed_metrics" in source, (
            "dashboard handler must use get_windowed_metrics() (Story #1294)"
        )


# ---------------------------------------------------------------------------
# G7 — gauge callback uses cheap memo, never calls backend.total_entries()
# ---------------------------------------------------------------------------


class TestGaugeCheapMemo:
    def test_cached_total_entries_exists_on_query_embedding_cache(self):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        assert hasattr(QueryEmbeddingCache, "cached_total_entries"), (
            "QueryEmbeddingCache must have cached_total_entries() method"
        )

    def test_cached_total_entries_is_cheap_no_backend_call(self):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        class _TrackingBackend:
            def __init__(self):
                self._store: dict = {}
                self._count = 0
                self.total_entries_calls = 0

            def lookup(self, key, provider, model, dimension):
                return self._store.get((key, provider, model, dimension))

            def upsert(
                self, key, provider, model, dimension, blob, last_used, created_at
            ):
                self._store[(key, provider, model, dimension)] = blob
                self._count += 1

            def touch_last_used(self, key, provider, model, dimension, ts):
                pass

            def prune_to_max(self, max_entries):
                pass

            def total_entries(self) -> int:
                self.total_entries_calls += 1
                return self._count  # type: ignore[no-any-return]

        backend = _TrackingBackend()
        cache = QueryEmbeddingCache(backend, enabled=True)
        _ = cache.cached_total_entries()
        assert backend.total_entries_calls == 0, (
            f"cached_total_entries() called backend.total_entries() "
            f"{backend.total_entries_calls} times — must use cheap memo"
        )

    def test_cached_total_entries_updates_after_record_miss_or_shadow(self):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            CacheQualifier,
            build_key,
        )

        class _SimpleBackend:
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

            def total_entries(self) -> int:
                return len(self._store)

        backend = _SimpleBackend()
        cache = QueryEmbeddingCache(backend, enabled=True, voyage_mode="shadow")
        assert cache.cached_total_entries() == 0

        key = build_key("hello world", config_digest="testdigest")
        q = CacheQualifier("voyage-ai", "voyage-code-3", 3)
        cache.record_miss_or_shadow(key, q, [0.1, 0.2, 0.3])
        assert cache.cached_total_entries() == 1

        key2 = build_key("another query text", config_digest="testdigest")
        cache.record_miss_or_shadow(key2, q, [0.4, 0.5, 0.6])
        assert cache.cached_total_entries() == 2


# ---------------------------------------------------------------------------
# G8 — snapshot() per-mode tallies correct
# ---------------------------------------------------------------------------


class TestSnapshotTallies:
    def _make_metrics(self):
        from unittest.mock import MagicMock
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()
        return QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    def test_hit_increments_mode_hits_tally(self):
        m = self._make_metrics()
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="shadow", provider="cohere")
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 2
        assert snap["shadow"]["misses"] == 0

    def test_miss_increments_mode_misses_tally(self):
        m = self._make_metrics()
        m.record_miss(mode="on", provider="voyage-ai")
        snap = m.snapshot()
        assert snap["on"]["misses"] == 1
        assert snap["on"]["hits"] == 0

    def test_shadow_and_on_tallies_are_independent(self):
        m = self._make_metrics()
        m.record_hit(mode="shadow", provider="voyage-ai")
        m.record_miss(mode="shadow", provider="voyage-ai")
        m.record_hit(mode="on", provider="voyage-ai")
        m.record_hit(mode="on", provider="voyage-ai")
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 1
        assert snap["shadow"]["misses"] == 1
        assert snap["on"]["hits"] == 2
        assert snap["on"]["misses"] == 0

    def test_cosine_p50_none_when_no_cosines_recorded(self):
        m = self._make_metrics()
        snap = m.snapshot()
        assert snap["shadow_cosine_p50"] is None

    def test_cosine_p50_present_after_shadow_cosine_recorded(self):
        m = self._make_metrics()
        cached_blob = struct.pack("<3f", 1.0, 0.0, 0.0)
        m.record_shadow_cosine(cached_blob=cached_blob, live_vec=[0.6, 0.8, 0.0])
        cached_blob2 = struct.pack("<3f", 0.0, 1.0, 0.0)
        m.record_shadow_cosine(cached_blob=cached_blob2, live_vec=[0.0, 1.0, 0.0])
        snap = m.snapshot()
        assert snap["shadow_cosine_p50"] is not None
        assert 0.0 <= snap["shadow_cosine_p50"] <= 1.0


# ---------------------------------------------------------------------------
# G9 — snapshot() is thread-safe
# ---------------------------------------------------------------------------


class TestSnapshotThreadSafety:
    def test_concurrent_record_hit_miss_no_race(self):
        from unittest.mock import MagicMock
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()
        m = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

        errors: List[Exception] = []

        def worker_hits():
            for _ in range(50):
                try:
                    m.record_hit(mode="shadow", provider="voyage-ai")
                except Exception as e:
                    errors.append(e)

        def worker_misses():
            for _ in range(50):
                try:
                    m.record_miss(mode="on", provider="cohere")
                except Exception as e:
                    errors.append(e)

        def worker_snapshot():
            for _ in range(50):
                try:
                    m.snapshot()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=worker_hits),
            threading.Thread(target=worker_hits),
            threading.Thread(target=worker_misses),
            threading.Thread(target=worker_snapshot),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        snap = m.snapshot()
        assert snap["shadow"]["hits"] == 100
        assert snap["on"]["misses"] == 50


# ---------------------------------------------------------------------------
# G10 — coalesced_query_embedding passes wired metrics into _serve_with_cache
# ---------------------------------------------------------------------------


class TestCoalescedEmbeddingPassesMetrics:
    """Verify that coalesced_query_embedding forwards the wired metrics accessor
    into _serve_with_cache when the cache and metrics are both set."""

    def setup_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache,
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache()
        clear_query_embedding_cache_metrics()

    def teardown_method(self):
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache,
            clear_query_embedding_cache_metrics,
        )

        clear_query_embedding_cache()
        clear_query_embedding_cache_metrics()

    def test_metrics_kwarg_passed_to_serve_with_cache(self):
        """_serve_with_cache must be called with the wired metrics= argument."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            set_query_embedding_cache_metrics,
            coalesced_query_embedding,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        # Build a fake cache that says "enabled + shadow mode"
        fake_cache = MagicMock()
        fake_cache.enabled_for.return_value = True
        fake_cache.mode_for.return_value = "shadow"
        fake_cache.build_key_for_provider.return_value = "aabbccdd"
        fake_cache.qualifier.return_value = MagicMock()

        # Build a fake metrics object
        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()
        fake_metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

        set_query_embedding_cache(fake_cache)
        set_query_embedding_cache_metrics(fake_metrics)

        # Build a fake provider
        fake_provider = MagicMock()
        fake_provider.get_provider_name.return_value = "voyage-ai"

        captured_metrics: List = []

        def fake_serve_with_cache(
            cache,
            provider_name,
            cache_key,
            qualifier,
            live_fn,
            *,
            metrics=None,
            audit_ctx=None,
        ):
            captured_metrics.append(metrics)
            return [0.1, 0.2, 0.3]

        with patch(
            "code_indexer.server.services.governed_call._serve_with_cache",
            side_effect=fake_serve_with_cache,
        ):
            result = coalesced_query_embedding(fake_provider, "test query text")

        assert len(captured_metrics) == 1, "_serve_with_cache was not called"
        assert captured_metrics[0] is fake_metrics, (
            f"Expected metrics={fake_metrics!r} to be passed to _serve_with_cache, "
            f"got {captured_metrics[0]!r}"
        )
        assert result == [0.1, 0.2, 0.3]

    def test_metrics_none_when_accessor_not_set(self):
        """When no metrics are wired, _serve_with_cache receives metrics=None."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            coalesced_query_embedding,
        )

        fake_cache = MagicMock()
        fake_cache.enabled_for.return_value = True
        fake_cache.mode_for.return_value = "on"
        fake_cache.build_key_for_provider.return_value = "deadbeef"
        fake_cache.qualifier.return_value = MagicMock()
        set_query_embedding_cache(fake_cache)

        fake_provider = MagicMock()
        fake_provider.get_provider_name.return_value = "voyage-ai"

        captured_metrics: List = []

        def fake_serve_with_cache(
            cache,
            provider_name,
            cache_key,
            qualifier,
            live_fn,
            *,
            metrics=None,
            audit_ctx=None,
        ):
            captured_metrics.append(metrics)
            return [0.5, 0.5]

        with patch(
            "code_indexer.server.services.governed_call._serve_with_cache",
            side_effect=fake_serve_with_cache,
        ):
            _ = coalesced_query_embedding(fake_provider, "test query")

        assert captured_metrics[0] is None, (
            "When no metrics are wired, metrics=None must be passed"
        )
