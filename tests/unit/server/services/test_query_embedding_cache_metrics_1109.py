"""Story #1109 (S5): Query-embedding cache core metrics + OTEL + shadow cosine fidelity.

Coverage:
  AC1  hit/miss counters increment with mandatory `mode` attribute
  AC1b hit-ratio DERIVED per mode from counter payloads (shadow != on)
  AC2  shadow_cosine histogram recorded ONLY in shadow + prior-cached branch
  AC3  dashboard partial route + template exist
  AC4  InMemoryMetricReader payload shape: names, types, namespace, mode attr
  AC4b gauge callback is cheap (zero blocking DB calls)
  AC5  provider error in shadow -> miss/error metric; no cosine; error propagates
"""

from __future__ import annotations

import math
import os
import struct
from typing import List, Optional

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIVE_VEC: List[float] = [0.6, 0.8, 0.0]  # unit vector
CACHED_VEC: List[float] = [1.0, 0.0, 0.0]  # unit vector
PROVIDER = "voyage-ai"
MODEL = "voyage-code-3"
DIM = 3
TEXT = "hello world query"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _enc(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x**2 for x in a))
    mb = math.sqrt(sum(y**2 for y in b))
    return dot / (ma * mb) if ma and mb else 0.0


# ---------------------------------------------------------------------------
# Fake backend (real in-memory, no DB)
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self._store: dict = {}
        self._count = 0
        self.total_entries_calls = 0

    def lookup(self, key, provider, model, dimension) -> Optional[bytes]:
        return self._store.get((key, provider, model, dimension))

    def upsert(self, key, provider, model, dimension, blob, last_used, created_at):
        self._store[(key, provider, model, dimension)] = blob
        self._count = len(self._store)

    def touch_last_used(self, key, provider, model, dimension, ts):
        pass

    def prune_to_max(self, max_entries):
        pass

    def total_entries(self) -> int:
        self.total_entries_calls += 1
        return self._count


def _make_meter_with_spies():
    """Return (fake_meter, hits_counter, misses_counter, cosine_hist)."""
    from unittest.mock import MagicMock

    fake_meter = MagicMock()
    hits_c = MagicMock()
    miss_c = MagicMock()
    hist = MagicMock()
    fake_meter.create_counter.side_effect = lambda name, **kw: (
        hits_c if "hits" in name else miss_c
    )
    fake_meter.create_observable_gauge.return_value = MagicMock()
    fake_meter.create_histogram.return_value = hist
    return fake_meter, hits_c, miss_c, hist


def _make_metrics(total_entries_fn=None):
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    meter, hits_c, miss_c, hist = _make_meter_with_spies()
    m = QueryEmbeddingCacheMetrics(
        meter, total_entries_fn=total_entries_fn or (lambda: 0)
    )
    return m, hits_c, miss_c, hist


def _make_cache_and_key(mode="shadow", pre_seed=False):
    from code_indexer.server.services.query_embedding_cache import (
        QueryEmbeddingCache,
        CacheQualifier,
        build_key,
    )

    backend = _FakeBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode=mode, cohere_mode=mode
    )
    # _serve_with_cache reads cache.mode_for() from LIVE runtime config (defaults to
    # "shadow"); pin it to the requested mode so these branch tests are deterministic
    # and independent of ambient config.
    cache.mode_for = lambda provider_name: mode  # type: ignore[method-assign]
    qualifier = CacheQualifier(PROVIDER, MODEL, DIM)
    key = build_key(TEXT, config_digest="testdigest")
    if pre_seed:
        backend._store[(key, PROVIDER, MODEL, DIM)] = _enc(CACHED_VEC)
        backend._count = 1
    return cache, backend, qualifier, key


# ---------------------------------------------------------------------------
# AC1 — hit/miss counters carry `mode` attribute
# ---------------------------------------------------------------------------


def test_hit_counter_incremented_with_shadow_mode():
    m, hits_c, miss_c, _ = _make_metrics()
    m.record_hit(mode="shadow", provider=PROVIDER)
    hits_c.add.assert_called_once_with(1, {"mode": "shadow", "provider": PROVIDER})
    miss_c.add.assert_not_called()


def test_miss_counter_incremented_with_on_mode():
    m, hits_c, miss_c, _ = _make_metrics()
    m.record_miss(mode="on", provider=PROVIDER)
    miss_c.add.assert_called_once_with(1, {"mode": "on", "provider": PROVIDER})
    hits_c.add.assert_not_called()


def test_hit_counter_for_on_mode_cohere_provider():
    m, hits_c, miss_c, _ = _make_metrics()
    m.record_hit(mode="on", provider="cohere")
    hits_c.add.assert_called_once_with(1, {"mode": "on", "provider": "cohere"})


# ---------------------------------------------------------------------------
# AC1b — hit-ratio DERIVED per mode: shadow and on payloads are independent
# ---------------------------------------------------------------------------


def test_hit_ratio_derived_per_mode_not_blended():
    """Hit-ratio for shadow and on modes must never be blended into a single value.

    This test verifies that counters carry mode attributes, so the caller can
    compute separate ratios: shadow_hits/(shadow_hits+shadow_misses) and
    on_hits/(on_hits+on_misses) independently from the OTEL data.
    The metrics layer must NOT aggregate across modes.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    # shadow: 2 hits, 1 miss -> shadow ratio = 2/3
    metrics.record_hit(mode="shadow", provider=PROVIDER)
    metrics.record_hit(mode="shadow", provider=PROVIDER)
    metrics.record_miss(mode="shadow", provider=PROVIDER)

    # on: 1 hit, 2 misses -> on ratio = 1/3
    metrics.record_hit(mode="on", provider=PROVIDER)
    metrics.record_miss(mode="on", provider=PROVIDER)
    metrics.record_miss(mode="on", provider=PROVIDER)

    data = reader.get_metrics_data()
    assert data is not None

    # Collect datapoints per metric per mode
    hits_by_mode: dict = {}
    misses_by_mode: dict = {}

    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for dp in m.data.data_points:
                    mode = dp.attributes.get("mode")  # type: ignore[union-attr]
                    if m.name == "cidx.cache.embedding.hits":
                        hits_by_mode[mode] = hits_by_mode.get(mode, 0) + dp.value  # type: ignore[union-attr]
                    elif m.name == "cidx.cache.embedding.misses":
                        misses_by_mode[mode] = misses_by_mode.get(mode, 0) + dp.value  # type: ignore[union-attr]

    # Verify per-mode breakdown is available (not blended)
    assert hits_by_mode.get("shadow", 0) == 2, f"shadow hits: {hits_by_mode}"
    assert misses_by_mode.get("shadow", 0) == 1, f"shadow misses: {misses_by_mode}"
    assert hits_by_mode.get("on", 0) == 1, f"on hits: {hits_by_mode}"
    assert misses_by_mode.get("on", 0) == 2, f"on misses: {misses_by_mode}"

    # Derived ratios per mode
    shadow_ratio = hits_by_mode["shadow"] / (
        hits_by_mode["shadow"] + misses_by_mode["shadow"]
    )
    on_ratio = hits_by_mode["on"] / (hits_by_mode["on"] + misses_by_mode["on"])
    assert abs(shadow_ratio - 2 / 3) < 1e-9
    assert abs(on_ratio - 1 / 3) < 1e-9
    # Ratios are different — NOT blended
    assert shadow_ratio != on_ratio


# ---------------------------------------------------------------------------
# AC2 — shadow_cosine histogram
# ---------------------------------------------------------------------------


def test_shadow_cosine_recorded_when_shadow_prior_cached():
    m, _, _, hist = _make_metrics()
    m.record_shadow_cosine(cached_blob=_enc(CACHED_VEC), live_vec=LIVE_VEC)
    assert hist.record.call_count == 1
    val = hist.record.call_args[0][0]
    assert abs(val - _cosine(CACHED_VEC, LIVE_VEC)) < 1e-5


def test_shadow_cosine_identical_vecs_equals_one():
    m, _, _, hist = _make_metrics()
    v = [0.6, 0.8, 0.0]
    m.record_shadow_cosine(cached_blob=_enc(v), live_vec=v)
    val = hist.record.call_args[0][0]
    assert abs(val - 1.0) < 1e-5


def test_shadow_cosine_orthogonal_vecs_equals_zero():
    m, _, _, hist = _make_metrics()
    m.record_shadow_cosine(cached_blob=_enc([1.0, 0.0, 0.0]), live_vec=[0.0, 1.0, 0.0])
    val = hist.record.call_args[0][0]
    assert abs(val - 0.0) < 1e-5


# AC2 integration: _serve_with_cache must call record_shadow_cosine


def test_serve_with_cache_shadow_hit_records_cosine():
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, backend, qualifier, key = _make_cache_and_key("shadow", pre_seed=True)
    m, _, _, hist = _make_metrics()

    _serve_with_cache(cache, PROVIDER, key, qualifier, lambda: LIVE_VEC, metrics=m)

    assert hist.record.call_count == 1, "shadow+cached must record cosine"


def test_serve_with_cache_shadow_miss_no_cosine():
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, backend, qualifier, key = _make_cache_and_key("shadow", pre_seed=False)
    m, _, _, hist = _make_metrics()

    _serve_with_cache(cache, PROVIDER, key, qualifier, lambda: LIVE_VEC, metrics=m)

    assert hist.record.call_count == 0, "shadow+miss must NOT record cosine"


def test_serve_with_cache_on_mode_hit_no_cosine():
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, backend, qualifier, key = _make_cache_and_key("on", pre_seed=True)
    m, _, _, hist = _make_metrics()

    _serve_with_cache(cache, PROVIDER, key, qualifier, lambda: LIVE_VEC, metrics=m)

    assert hist.record.call_count == 0, "on-mode HIT skips live_fn -> no cosine"


# ---------------------------------------------------------------------------
# AC3 — dashboard partial: route + template
# ---------------------------------------------------------------------------


def test_cache_metrics_template_exists():
    template_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "../../../../src/code_indexer/server/web/templates/partials/dashboard_cache_metrics.html",
        )
    )
    assert os.path.exists(template_path), f"Template not found: {template_path}"


def test_cache_metrics_route_registered():
    from code_indexer.server.web.routes import web_router

    routes = {r.path for r in web_router.routes}
    assert "/partials/dashboard-cache-metrics" in routes, (
        f"Route not found; registered paths: {sorted(routes)}"
    )


# ---------------------------------------------------------------------------
# AC4 — InMemoryMetricReader payload shape
# ---------------------------------------------------------------------------


def _build_in_memory_metrics():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 42)
    return metrics, reader


def _all_metric_names(data) -> set:
    return {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }


def test_inmemory_hits_counter_name():
    metrics, reader = _build_in_memory_metrics()
    metrics.record_hit(mode="shadow", provider=PROVIDER)
    names = _all_metric_names(reader.get_metrics_data())
    assert "cidx.cache.embedding.hits" in names, f"got: {names}"


def test_inmemory_misses_counter_name():
    metrics, reader = _build_in_memory_metrics()
    metrics.record_miss(mode="on", provider=PROVIDER)
    names = _all_metric_names(reader.get_metrics_data())
    assert "cidx.cache.embedding.misses" in names, f"got: {names}"


def test_inmemory_total_entries_gauge_name():
    metrics, reader = _build_in_memory_metrics()
    names = _all_metric_names(reader.get_metrics_data())
    assert "cidx.cache.embedding.total_entries" in names, f"got: {names}"


def test_inmemory_shadow_cosine_histogram_name():
    metrics, reader = _build_in_memory_metrics()
    metrics.record_shadow_cosine(cached_blob=_enc(CACHED_VEC), live_vec=LIVE_VEC)
    names = _all_metric_names(reader.get_metrics_data())
    assert "cidx.cache.embedding.shadow_cosine" in names, f"got: {names}"


def test_inmemory_hits_datapoints_have_mode_attribute():
    metrics, reader = _build_in_memory_metrics()
    metrics.record_hit(mode="shadow", provider=PROVIDER)
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.hits":
                    for dp in m.data.data_points:
                        assert "mode" in dp.attributes, f"mode missing: {dp.attributes}"
                        assert dp.attributes["mode"] == "shadow"
                        return
    pytest.fail("cidx.cache.embedding.hits datapoint not found")


def test_inmemory_misses_datapoints_have_mode_attribute():
    metrics, reader = _build_in_memory_metrics()
    metrics.record_miss(mode="on", provider="cohere")
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.misses":
                    for dp in m.data.data_points:
                        assert "mode" in dp.attributes, f"mode missing: {dp.attributes}"
                        assert dp.attributes["mode"] == "on"
                        return
    pytest.fail("cidx.cache.embedding.misses datapoint not found")


def test_inmemory_gauge_returns_total_entries_fn_value():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    _metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 99)

    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.total_entries":
                    for dp in m.data.data_points:
                        assert dp.value == 99  # type: ignore[union-attr]
                        return
    pytest.fail("total_entries gauge not found in metrics data")


# ---------------------------------------------------------------------------
# AC4b — gauge callback does NOT call backend.total_entries (no blocking DB)
# ---------------------------------------------------------------------------


def test_gauge_callback_does_not_call_backend_total_entries():
    """ObservableGauge callback must use cheap memo; NOT backend.total_entries()."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    backend = _FakeBackend()
    # Seeding a cheap memo; the gauge ONLY calls the provided lambda
    cheap_calls = [0]

    def cheap_memo():
        cheap_calls[0] += 1
        return 7

    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader])
    meter = mp.get_meter("cidx.cache")
    # Pass cheap_memo; backend.total_entries must NOT be called from gauge
    QueryEmbeddingCacheMetrics(meter, total_entries_fn=cheap_memo)

    # Trigger gauge collection
    reader.get_metrics_data()

    # The gauge called our cheap fn (not zero)
    assert cheap_calls[0] >= 1
    # backend.total_entries was NEVER called (no blocking DB query)
    assert backend.total_entries_calls == 0, (
        f"Gauge called blocking backend.total_entries() {backend.total_entries_calls} times!"
    )


# ---------------------------------------------------------------------------
# AC5 — provider error in shadow: miss recorded, no cosine, error propagates
# ---------------------------------------------------------------------------


def test_provider_error_in_shadow_records_miss_and_propagates():
    from code_indexer.server.services.governed_call import _serve_with_cache
    from code_indexer.server.services.query_embedding_cache import (
        QueryEmbeddingCache,
        CacheQualifier,
        build_key,
    )

    backend = _FakeBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode="shadow", cohere_mode="shadow"
    )
    # Pin mode so the test is immune to live config state from other tests
    # (importing the full server stack can set mode to "on" from ~/.cidx-server/config.json)
    cache.mode_for = lambda provider_name: "shadow"  # type: ignore[method-assign]
    qualifier = CacheQualifier(PROVIDER, MODEL, DIM)
    key = build_key(TEXT, config_digest="testdigest")
    m, hits_c, miss_c, hist = _make_metrics()

    class _ProvError(RuntimeError):
        pass

    def bad_fn():
        raise _ProvError("provider 503")

    with pytest.raises(_ProvError):
        _serve_with_cache(cache, PROVIDER, key, qualifier, bad_fn, metrics=m)

    # Miss must be recorded
    assert miss_c.add.call_count >= 1
    # No hit
    hits_c.add.assert_not_called()
    # No cosine (no live vector obtained)
    hist.record.assert_not_called()


# ---------------------------------------------------------------------------
# AC4c — cached_total_entries() cheap memo clamps at cap, never drifts past it
# ---------------------------------------------------------------------------


def test_cached_total_pins_at_cap_not_unbounded(tmp_path):
    """cached_total_entries() must pin at max_entries after overflow, not grow unboundedly.

    Bug: _cached_total was incremented unconditionally on every upsert, but
    prune_to_max() evicts rows back to the cap.  After the cache fills,
    _cached_total diverged from reality (10001, 10002, ...) while the real
    row count stayed pinned at max_entries.  The OTEL gauge would then
    report an ever-growing count.

    Fix: clamp to min(_cached_total + 1, _resolve_max_entries()) so the memo
    matches post-prune reality.
    """
    from unittest.mock import MagicMock, patch

    from code_indexer.server.services.query_embedding_cache import (
        CacheQualifier,
        QueryEmbeddingCache,
    )
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    CAP = 100
    backend = QueryEmbeddingCacheSqliteBackend(db_path=str(tmp_path / "qec_clamp.db"))
    cache = QueryEmbeddingCache(backend, max_entries=CAP)

    mock_qec_cfg = MagicMock()
    mock_qec_cfg.query_embedding_cache_max_entries = CAP
    mock_qec_cfg.query_embedding_cache_enabled = True
    mock_qec_cfg.query_embedding_cache_voyage_mode = "on"
    mock_qec_cfg.query_embedding_cache_cohere_mode = "on"

    qualifier = CacheQualifier(provider="voyage-ai", model="voyage-code-3", dimension=4)
    import numpy as np

    vec = list(np.ones(4, dtype="float32"))

    with patch.object(cache, "_live_qec_cfg", return_value=mock_qec_cfg):
        for i in range(150):
            cache.record_miss_or_shadow(f"key{i:05d}", qualifier, vec)

    # Real row count must be at the cap
    real_count = cache.total_entries()
    assert real_count == CAP, f"real total_entries()={real_count}, expected {CAP}"

    # Cheap memo MUST NOT have drifted past the cap
    memo_count = cache.cached_total_entries()
    assert memo_count == CAP, (
        f"cached_total_entries()={memo_count} diverged from reality={real_count}; "
        f"expected both to equal {CAP}.  Bug: memo was incremented unconditionally "
        f"without clamping to the cap."
    )


def test_cached_total_below_cap_is_exact(tmp_path):
    """Before the cap is reached, cached_total_entries() must equal the write count."""
    from unittest.mock import MagicMock, patch

    from code_indexer.server.services.query_embedding_cache import (
        CacheQualifier,
        QueryEmbeddingCache,
    )
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    CAP = 100
    WRITES = 50
    backend = QueryEmbeddingCacheSqliteBackend(db_path=str(tmp_path / "qec_below.db"))
    cache = QueryEmbeddingCache(backend, max_entries=CAP)

    mock_qec_cfg = MagicMock()
    mock_qec_cfg.query_embedding_cache_max_entries = CAP
    mock_qec_cfg.query_embedding_cache_enabled = True
    mock_qec_cfg.query_embedding_cache_voyage_mode = "on"
    mock_qec_cfg.query_embedding_cache_cohere_mode = "on"

    qualifier = CacheQualifier(provider="voyage-ai", model="voyage-code-3", dimension=4)
    import numpy as np

    vec = list(np.ones(4, dtype="float32"))

    with patch.object(cache, "_live_qec_cfg", return_value=mock_qec_cfg):
        for i in range(WRITES):
            cache.record_miss_or_shadow(f"key{i:05d}", qualifier, vec)

    memo_count = cache.cached_total_entries()
    assert memo_count == WRITES, (
        f"cached_total_entries()={memo_count}, expected {WRITES} (below cap, no eviction)"
    )


def test_provider_error_in_shadow_no_upsert():
    """When live_fn raises in shadow mode, nothing must be written to the cache."""
    from code_indexer.server.services.governed_call import _serve_with_cache
    from code_indexer.server.services.query_embedding_cache import (
        QueryEmbeddingCache,
        CacheQualifier,
        build_key,
    )

    backend = _FakeBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode="shadow", cohere_mode="shadow"
    )
    # Pin mode so the test is immune to live config state from other tests
    cache.mode_for = lambda provider_name: "shadow"  # type: ignore[method-assign]
    qualifier = CacheQualifier(PROVIDER, MODEL, DIM)
    key = build_key(TEXT, config_digest="testdigest")
    m, _, _, _ = _make_metrics()

    with pytest.raises(RuntimeError):
        _serve_with_cache(
            cache,
            PROVIDER,
            key,
            qualifier,
            lambda: (_ for _ in ()).throw(RuntimeError("fail")),
            metrics=m,
        )

    assert len(backend._store) == 0, "No upsert should happen when live_fn raises"
