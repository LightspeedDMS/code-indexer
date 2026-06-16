"""Story #1110 (S6 Chunk A): record_audit + snapshot() audit fields.

Tests:
  1. record_audit increments _audit_total on every call.
  2. record_audit increments _audit_top1_matches only when top1_match=True.
  3. record_audit adds to _audit_overlap_sum correctly.
  4. record_audit records OTEL Histogram (top10_overlap) with provider+mode attrs.
  5. record_audit adds to OTEL Counter ONLY when top1_match=True.
  6. record_audit is fail-open: instrument raising does NOT propagate.
  7. snapshot() includes audit_total, audit_top1_matches, audit_overlap_avg.
  8. snapshot() audit_overlap_avg is None when total==0.
  9. snapshot() audit_overlap_avg is correct average after multiple records.
  10. Audit metric names: cidx.cache.embedding.audit_top10_overlap (Histogram),
      cidx.cache.embedding.audit_top1_match (Counter).
  11. Both instruments exist in InMemoryMetricReader data after being used.
"""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROVIDER = "voyage-ai"
MODE_SHADOW = "shadow"
MODE_ON = "on"


def _make_metrics_with_spies():
    """Return (metrics, audit_hist_spy, audit_top1_counter_spy)."""
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    fake_meter = MagicMock()
    hits_c = MagicMock()
    miss_c = MagicMock()
    shadow_cosine_hist = MagicMock()
    audit_hist = MagicMock()
    audit_top1_counter = MagicMock()

    def _create_counter_side_effect(name, **kw):
        if "hits" in name:
            return hits_c
        if "misses" in name:
            return miss_c
        if "top1_match" in name:
            return audit_top1_counter
        return MagicMock()

    def _create_histogram_side_effect(name, **kw):
        if "shadow_cosine" in name:
            return shadow_cosine_hist
        if "audit_top10_overlap" in name:
            return audit_hist
        return MagicMock()

    fake_meter.create_counter.side_effect = _create_counter_side_effect
    fake_meter.create_observable_gauge.return_value = MagicMock()
    fake_meter.create_histogram.side_effect = _create_histogram_side_effect

    metrics = QueryEmbeddingCacheMetrics(fake_meter, total_entries_fn=lambda: 0)
    return metrics, audit_hist, audit_top1_counter


# ===========================================================================
# 1-3: In-process tally increments
# ===========================================================================


def test_record_audit_increments_audit_total():
    """Each call to record_audit must increment _audit_total by 1."""
    metrics, _, _ = _make_metrics_with_spies()

    assert metrics._audit_total == 0
    metrics.record_audit(
        top10_overlap=0.8, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )
    assert metrics._audit_total == 1
    metrics.record_audit(
        top10_overlap=0.6, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )
    assert metrics._audit_total == 2


def test_record_audit_increments_top1_matches_only_when_true():
    """_audit_top1_matches incremented ONLY when top1_match=True."""
    metrics, _, _ = _make_metrics_with_spies()

    metrics.record_audit(
        top10_overlap=0.9, top1_match=False, provider=PROVIDER, mode=MODE_ON
    )
    assert metrics._audit_top1_matches == 0

    metrics.record_audit(
        top10_overlap=0.9, top1_match=True, provider=PROVIDER, mode=MODE_ON
    )
    assert metrics._audit_top1_matches == 1

    metrics.record_audit(
        top10_overlap=0.7, top1_match=True, provider=PROVIDER, mode=MODE_ON
    )
    assert metrics._audit_top1_matches == 2


def test_record_audit_adds_to_overlap_sum():
    """_audit_overlap_sum accumulates the top10_overlap values."""
    metrics, _, _ = _make_metrics_with_spies()

    metrics.record_audit(
        top10_overlap=0.6, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )
    metrics.record_audit(
        top10_overlap=0.8, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )

    assert abs(metrics._audit_overlap_sum - 1.4) < 1e-9


# ===========================================================================
# 4-5: OTEL instrument calls
# ===========================================================================


def test_record_audit_records_otel_histogram_with_attrs():
    """record_audit records top10_overlap on the audit Histogram with provider+mode attrs."""
    metrics, audit_hist, _ = _make_metrics_with_spies()

    metrics.record_audit(
        top10_overlap=0.75, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )

    audit_hist.record.assert_called_once()
    call_args = audit_hist.record.call_args
    value = call_args[0][0]
    attrs = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]

    assert abs(value - 0.75) < 1e-9, f"Expected 0.75 overlap, got {value}"
    if isinstance(attrs, dict):
        assert attrs.get("provider") == PROVIDER
        assert attrs.get("mode") == MODE_SHADOW


def test_record_audit_records_histogram_attrs_via_kwargs():
    """Histogram record must pass attributes dict as second positional or keyword."""
    metrics, audit_hist, _ = _make_metrics_with_spies()

    metrics.record_audit(
        top10_overlap=0.5, top1_match=True, provider="cohere", mode=MODE_ON
    )

    assert audit_hist.record.call_count == 1
    call = audit_hist.record.call_args
    # Check value
    assert abs(call[0][0] - 0.5) < 1e-9


def test_record_audit_adds_top1_counter_only_when_match():
    """Counter.add called ONLY when top1_match=True."""
    metrics, _, audit_top1_counter = _make_metrics_with_spies()

    # Not a match: counter should NOT be called
    metrics.record_audit(
        top10_overlap=0.3, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )
    audit_top1_counter.add.assert_not_called()

    # Match: counter should be called once
    metrics.record_audit(
        top10_overlap=0.9, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )
    assert audit_top1_counter.add.call_count == 1
    call_args = audit_top1_counter.add.call_args[0]
    assert call_args[0] == 1


# ===========================================================================
# 6: Fail-open
# ===========================================================================


def test_record_audit_fail_open_when_histogram_raises():
    """An exception in audit_hist.record must NOT propagate to the caller."""
    metrics, audit_hist, _ = _make_metrics_with_spies()
    audit_hist.record.side_effect = RuntimeError("OTEL failure")

    # Must NOT raise
    metrics.record_audit(
        top10_overlap=0.5, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )

    # In-process tally still incremented
    assert metrics._audit_total == 1


def test_record_audit_fail_open_when_counter_raises():
    """An exception in audit_top1_counter.add must NOT propagate."""
    metrics, _, audit_top1_counter = _make_metrics_with_spies()
    audit_top1_counter.add.side_effect = RuntimeError("OTEL counter failure")

    # Must NOT raise
    metrics.record_audit(
        top10_overlap=0.8, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )

    # Tally still incremented
    assert metrics._audit_top1_matches == 1


# ===========================================================================
# 7-9: snapshot() audit fields
# ===========================================================================


def test_snapshot_includes_audit_fields_initial():
    """snapshot() must include audit_total, audit_top1_matches, audit_overlap_avg."""
    metrics, _, _ = _make_metrics_with_spies()
    snap = metrics.snapshot()

    assert "audit_total" in snap, f"Missing audit_total in snapshot: {snap.keys()}"
    assert "audit_top1_matches" in snap, (
        f"Missing audit_top1_matches in snapshot: {snap.keys()}"
    )
    assert "audit_overlap_avg" in snap, (
        f"Missing audit_overlap_avg in snapshot: {snap.keys()}"
    )


def test_snapshot_audit_overlap_avg_none_when_zero():
    """audit_overlap_avg must be None when no audits have been recorded."""
    metrics, _, _ = _make_metrics_with_spies()
    snap = metrics.snapshot()

    assert snap["audit_total"] == 0
    assert snap["audit_top1_matches"] == 0
    assert snap["audit_overlap_avg"] is None


def test_snapshot_audit_overlap_avg_correct_after_records():
    """audit_overlap_avg = sum(overlaps) / total after multiple record_audit calls."""
    metrics, _, _ = _make_metrics_with_spies()

    metrics.record_audit(
        top10_overlap=0.4, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )
    metrics.record_audit(
        top10_overlap=0.6, top1_match=True, provider=PROVIDER, mode=MODE_ON
    )
    metrics.record_audit(
        top10_overlap=0.8, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )

    snap = metrics.snapshot()

    assert snap["audit_total"] == 3
    assert snap["audit_top1_matches"] == 2
    expected_avg = (0.4 + 0.6 + 0.8) / 3
    assert snap["audit_overlap_avg"] is not None
    assert abs(snap["audit_overlap_avg"] - expected_avg) < 1e-9, (
        f"Expected avg {expected_avg}, got {snap['audit_overlap_avg']}"
    )


def test_snapshot_audit_total_matches_record_count():
    """audit_total in snapshot equals the number of record_audit calls."""
    metrics, _, _ = _make_metrics_with_spies()

    for i in range(5):
        metrics.record_audit(
            top10_overlap=float(i) / 10,
            top1_match=(i % 2 == 0),
            provider=PROVIDER,
            mode=MODE_SHADOW,
        )

    snap = metrics.snapshot()
    assert snap["audit_total"] == 5
    assert snap["audit_top1_matches"] == 3  # i=0,2,4 are even


def test_snapshot_is_thread_safe_under_lock():
    """snapshot() under concurrent modifications must not crash."""
    import threading

    metrics, _, _ = _make_metrics_with_spies()
    errors: List[Exception] = []

    def writer():
        try:
            for _ in range(20):
                metrics.record_audit(
                    top10_overlap=0.5,
                    top1_match=True,
                    provider=PROVIDER,
                    mode=MODE_SHADOW,
                )
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(20):
                metrics.snapshot()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread safety errors: {errors}"


# ===========================================================================
# 10-11: Metric names + InMemoryMetricReader
# ===========================================================================


def test_audit_metric_names_registered():
    """Both audit instruments must be registered with correct names."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    # Record something to force them into the metric data
    metrics.record_audit(
        top10_overlap=0.7, top1_match=True, provider=PROVIDER, mode=MODE_SHADOW
    )

    data = reader.get_metrics_data()
    assert data is not None
    names = {
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }

    assert "cidx.cache.embedding.audit_top10_overlap" in names, (
        f"Histogram name missing. Got: {names}"
    )
    assert "cidx.cache.embedding.audit_top1_match" in names, (
        f"Counter name missing. Got: {names}"
    )


def test_audit_top10_overlap_histogram_in_inmemory_reader():
    """InMemoryMetricReader captures the audit_top10_overlap histogram datapoint."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    metrics.record_audit(
        top10_overlap=0.85, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )

    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.audit_top10_overlap":
                    found = True
                    # Check datapoints exist
                    assert len(m.data.data_points) > 0, (
                        "No datapoints for audit_top10_overlap"
                    )
    assert found, "cidx.cache.embedding.audit_top10_overlap not found in metrics data"


def test_audit_top1_counter_in_inmemory_reader():
    """InMemoryMetricReader captures the audit_top1_match counter when top1_match=True."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    metrics.record_audit(
        top10_overlap=0.9, top1_match=True, provider=PROVIDER, mode=MODE_ON
    )

    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.audit_top1_match":
                    found = True
                    # Counter must have positive value
                    for dp in m.data.data_points:
                        assert dp.value >= 1, f"Counter value={dp.value}, expected >=1"  # type: ignore[union-attr]
    assert found, "cidx.cache.embedding.audit_top1_match not found in metrics data"


def test_audit_top1_counter_not_recorded_when_no_match():
    """audit_top1_match counter must NOT appear if top1_match is never True."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from code_indexer.server.services.query_embedding_cache_metrics import (
        QueryEmbeddingCacheMetrics,
    )

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("cidx.cache")
    metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

    # Only record non-matches
    metrics.record_audit(
        top10_overlap=0.4, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )
    metrics.record_audit(
        top10_overlap=0.5, top1_match=False, provider=PROVIDER, mode=MODE_SHADOW
    )

    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "cidx.cache.embedding.audit_top1_match":
                    # If it appears, all datapoints must have value 0
                    for dp in m.data.data_points:
                        assert dp.value == 0, f"Expected 0 top1 count, got {dp.value}"  # type: ignore[union-attr]
