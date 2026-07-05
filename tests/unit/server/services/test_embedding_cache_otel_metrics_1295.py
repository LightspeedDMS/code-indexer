"""Story #1295 (Epic #1288 final): DB-backed OTEL re-source for cidx.cache.embedding.*.

Step E of the ordered execution: every ``cidx.cache.embedding.*`` instrument is
re-sourced as a DB-backed ObservableGauge callback reading WindowedCacheMetrics
(search_embed_event, Story #1293/#1294) instead of the retiring in-memory
QueryEmbeddingCacheMetrics tallies.

Step (gate): this file's parity tests ARE the required gate -- each
ObservableGauge callback value must equal the WindowedCacheMetrics aggregate
computed over the SAME rows for the SAME window before Step F (delete) may
proceed.

Coverage:
  AC1a  total_entries stays sourced from the cheap total_entries_fn (a
        query_embedding_cache COUNT) -- NOT event-sourced. Unchanged behavior.
  AC1b  hit_rate / provider_calls / hits / misses / long_key /
        audit_top10_overlap gauges equal the overall WindowedCacheMetrics
        aggregate for the same rows (the parity gate).
  AC1c  shadow_cosine_p50 / _p05 / _min gauges equal the by_cache_mode["shadow"]
        aggregate.
  AC1d  shadow_cosine_histogram gauge yields one Observation per (lo, hi)
        bucket with the aggregate's bucket count as the value.
  AC1e  Counter -> Gauge breaking change: NO push-based create_counter() or
        create_histogram() instrument is ever created by this module -- every
        instrument is a pull-based ObservableGauge.
  AC1f  audit_top1_match has no DB source (schema has no top1 column) and is
        explicitly NEVER registered.
  AC2   fail-open: meter=None is a documented no-op (no instruments created,
        constructor does not raise); windowed_metrics_fn raising falls back
        to the empty windowed result (zero/empty values, never raises).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.windowed_cache_metrics import (
    build_windowed_result,
)


# ---------------------------------------------------------------------------
# Fake meter capturing every create_observable_gauge / create_counter /
# create_histogram call so tests can invoke the registered callback directly
# and assert on what instrument KINDS were created.
# ---------------------------------------------------------------------------


class _FakeMeter:
    def __init__(self) -> None:
        self.gauges: Dict[str, Callable[[Any], Any]] = {}
        self.counter_calls: List[str] = []
        self.histogram_calls: List[str] = []

    def create_observable_gauge(self, name: str, callbacks, **kwargs) -> MagicMock:
        assert len(callbacks) == 1
        self.gauges[name] = callbacks[0]
        return MagicMock()

    def create_counter(self, name: str, **kwargs) -> MagicMock:
        self.counter_calls.append(name)
        return MagicMock()

    def create_histogram(self, name: str, **kwargs) -> MagicMock:
        self.histogram_calls.append(name)
        return MagicMock()


def _observe(meter: _FakeMeter, metric_name: str) -> List[Any]:
    """Invoke the registered callback and materialize its Observation generator."""
    cb = meter.gauges[metric_name]
    return list(cb(None))


def _rows_fixture() -> List[Dict[str, Any]]:
    """A small, hand-computable set of search_embed_event-shaped rows."""
    return [
        {
            "cache_mode": "on",
            "provider": "voyage-ai",
            "outcome": "hit",
            "role": "warm_hit",
            "live_batch_id": None,
            "embed_key": "s:d:q1",
            "shadow_cosine": None,
            "long_key": False,
            "audit_sampled": False,
            "audit_cosine": None,
        },
        {
            "cache_mode": "on",
            "provider": "voyage-ai",
            "outcome": "miss",
            "role": "direct",
            "live_batch_id": None,
            "embed_key": "s:d:q2",
            "shadow_cosine": None,
            "long_key": True,
            "audit_sampled": False,
            "audit_cosine": None,
        },
        {
            "cache_mode": "shadow",
            "provider": "cohere",
            "outcome": "shadow_hit",
            "role": "owner",
            "live_batch_id": "b1",
            "embed_key": "s:d:q3",
            "shadow_cosine": 0.91,
            "long_key": False,
            "audit_sampled": True,
            "audit_cosine": 0.8,
        },
        {
            "cache_mode": "shadow",
            "provider": "cohere",
            "outcome": "shadow_miss",
            "role": "direct",
            "live_batch_id": None,
            "embed_key": "s:d:q4",
            "shadow_cosine": None,
            "long_key": False,
            "audit_sampled": False,
            "audit_cosine": None,
        },
    ]


def _make_metrics(*, meter, total_entries_fn=None, windowed_metrics_fn=None):
    from code_indexer.server.services.embedding_cache_otel_metrics import (
        EmbeddingCacheOtelMetrics,
    )

    return EmbeddingCacheOtelMetrics(
        meter,
        total_entries_fn=total_entries_fn or (lambda: 0),
        windowed_metrics_fn=windowed_metrics_fn
        or (lambda f, t: build_windowed_result([])),
    )


# ---------------------------------------------------------------------------
# AC1a: total_entries stays sourced from total_entries_fn (unchanged, NOT
# event-sourced).
# ---------------------------------------------------------------------------


class TestTotalEntriesUnchangedSource:
    def test_total_entries_gauge_reads_total_entries_fn(self):
        meter = _FakeMeter()
        calls = {"n": 0}

        def _total_entries_fn() -> int:
            calls["n"] += 1
            return 42

        _make_metrics(meter=meter, total_entries_fn=_total_entries_fn)

        observations = _observe(meter, "cidx.cache.embedding.total_entries")
        assert len(observations) == 1
        assert observations[0].value == 42
        assert calls["n"] == 1

    def test_total_entries_gauge_does_not_call_windowed_metrics_fn(self):
        meter = _FakeMeter()
        windowed_calls = {"n": 0}

        def _windowed_metrics_fn(from_ts, to_ts):
            windowed_calls["n"] += 1
            return build_windowed_result([])

        _make_metrics(
            meter=meter,
            total_entries_fn=lambda: 7,
            windowed_metrics_fn=_windowed_metrics_fn,
        )
        _observe(meter, "cidx.cache.embedding.total_entries")
        assert windowed_calls["n"] == 0


# ---------------------------------------------------------------------------
# AC1b (the parity GATE): hit_rate / provider_calls / hits / misses /
# long_key / audit_top10_overlap gauges equal the overall aggregate.
# ---------------------------------------------------------------------------


class TestOverallParityGate:
    def _expected_overall(self):
        return build_windowed_result(_rows_fixture()).overall

    def _windowed_metrics_fn(self, from_ts, to_ts):
        return build_windowed_result(_rows_fixture())

    def test_hit_rate_gauge_equals_windowed_overall_hit_rate(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.hit_rate")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().hit_rate

    def test_provider_calls_gauge_equals_windowed_overall_provider_embed_calls(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.provider_calls")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().provider_embed_calls

    def test_hits_gauge_equals_windowed_overall_hits(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.hits")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().hits

    def test_misses_gauge_equals_windowed_overall_misses(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.misses")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().misses

    def test_long_key_gauge_equals_windowed_overall_long_key(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.long_key")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().long_key

    def test_audit_top10_overlap_gauge_equals_windowed_overall_audit_avg(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.audit_top10_overlap")
        assert len(observations) == 1
        assert observations[0].value == self._expected_overall().audit_avg


# ---------------------------------------------------------------------------
# AC1c: shadow_cosine percentile gauges equal the by_cache_mode["shadow"]
# aggregate.
# ---------------------------------------------------------------------------


class TestShadowCosinePercentileParity:
    def _expected_shadow(self):
        return build_windowed_result(_rows_fixture()).by_cache_mode["shadow"]

    def _windowed_metrics_fn(self, from_ts, to_ts):
        return build_windowed_result(_rows_fixture())

    def test_shadow_cosine_p50_gauge_equals_windowed_shadow_p50(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.shadow_cosine_p50")
        assert len(observations) == 1
        assert observations[0].value == self._expected_shadow().shadow_cosine_p50

    def test_shadow_cosine_p05_gauge_equals_windowed_shadow_p05(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.shadow_cosine_p05")
        assert len(observations) == 1
        assert observations[0].value == self._expected_shadow().shadow_cosine_p05

    def test_shadow_cosine_min_gauge_equals_windowed_shadow_min(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter, windowed_metrics_fn=self._windowed_metrics_fn)
        observations = _observe(meter, "cidx.cache.embedding.shadow_cosine_min")
        assert len(observations) == 1
        assert observations[0].value == self._expected_shadow().shadow_cosine_min

    def test_shadow_cosine_percentile_gauges_absent_when_no_shadow_rows(self):
        """When the window has NO shadow-mode rows, by_cache_mode has no
        "shadow" key -- the percentile gauges must yield NO observation
        (never raise, never fabricate a fake 0.0)."""
        meter = _FakeMeter()
        _make_metrics(
            meter=meter,
            windowed_metrics_fn=lambda f, t: build_windowed_result([]),
        )
        assert _observe(meter, "cidx.cache.embedding.shadow_cosine_p50") == []
        assert _observe(meter, "cidx.cache.embedding.shadow_cosine_p05") == []
        assert _observe(meter, "cidx.cache.embedding.shadow_cosine_min") == []


# ---------------------------------------------------------------------------
# AC1d: shadow_cosine_histogram yields one Observation per bucket.
# ---------------------------------------------------------------------------


class TestShadowCosineHistogramParity:
    def test_histogram_gauge_yields_one_observation_per_bucket(self):
        meter = _FakeMeter()
        rows = _rows_fixture()
        _make_metrics(
            meter=meter, windowed_metrics_fn=lambda f, t: build_windowed_result(rows)
        )
        expected = (
            build_windowed_result(rows).by_cache_mode["shadow"].shadow_cosine_histogram
        )
        observations = _observe(meter, "cidx.cache.embedding.shadow_cosine_histogram")
        assert len(observations) == len(expected)
        for obs, (lo, hi, count) in zip(observations, expected):
            assert obs.value == count
            assert obs.attributes["bucket_lo"] == lo
            assert obs.attributes["bucket_hi"] == hi


# ---------------------------------------------------------------------------
# AC1e: Counter -> Gauge breaking change -- NO push-based instrument exists.
# ---------------------------------------------------------------------------


class TestNoPushBasedInstruments:
    def test_no_counters_or_histograms_are_created(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter)
        assert meter.counter_calls == []
        assert meter.histogram_calls == []

    def test_every_registered_instrument_is_an_observable_gauge(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter)
        # At minimum the enumerated instruments from the story body.
        expected_names = {
            "cidx.cache.embedding.total_entries",
            "cidx.cache.embedding.hit_rate",
            "cidx.cache.embedding.provider_calls",
            "cidx.cache.embedding.hits",
            "cidx.cache.embedding.misses",
            "cidx.cache.embedding.long_key",
            "cidx.cache.embedding.audit_top10_overlap",
            "cidx.cache.embedding.shadow_cosine_p50",
            "cidx.cache.embedding.shadow_cosine_p05",
            "cidx.cache.embedding.shadow_cosine_min",
            "cidx.cache.embedding.shadow_cosine_histogram",
        }
        assert expected_names.issubset(set(meter.gauges.keys()))


# ---------------------------------------------------------------------------
# AC1f: audit_top1_match has no DB source -- explicitly NEVER registered.
# ---------------------------------------------------------------------------


class TestAuditTop1MatchExplicitlyRemoved:
    def test_audit_top1_match_instrument_is_never_registered(self):
        meter = _FakeMeter()
        _make_metrics(meter=meter)
        assert "cidx.cache.embedding.audit_top1_match" not in meter.gauges
        assert not any(name.endswith("top1_match") for name in meter.gauges)


# ---------------------------------------------------------------------------
# AC2: fail-open behavior.
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_meter_none_is_a_documented_noop(self):
        from code_indexer.server.services.embedding_cache_otel_metrics import (
            EmbeddingCacheOtelMetrics,
        )

        # Must not raise.
        instance = EmbeddingCacheOtelMetrics(
            None,
            total_entries_fn=lambda: 1,
            windowed_metrics_fn=lambda f, t: build_windowed_result([]),
        )
        assert instance is not None

    def test_windowed_metrics_fn_raising_falls_back_to_empty_result(self):
        meter = _FakeMeter()

        def _raising(from_ts, to_ts):
            raise RuntimeError("backend unavailable")

        _make_metrics(meter=meter, windowed_metrics_fn=_raising)

        # hit_rate on an empty result is 0.0 (per compute_aggregate's default).
        observations = _observe(meter, "cidx.cache.embedding.hit_rate")
        assert len(observations) == 1
        assert observations[0].value == 0.0

        # Shadow percentile gauges: no shadow group in an empty result -> no observation.
        assert _observe(meter, "cidx.cache.embedding.shadow_cosine_p50") == []

    def test_total_entries_fn_raising_never_propagates(self):
        meter = _FakeMeter()

        def _raising_total_entries():
            raise RuntimeError("db down")

        _make_metrics(meter=meter, total_entries_fn=_raising_total_entries)

        # Must not raise; documented fail-open yields no observation.
        observations = _observe(meter, "cidx.cache.embedding.total_entries")
        assert observations == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
