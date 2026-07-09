"""Story #1295 (Epic #1288 final): DB-backed OTEL re-source for
``cidx.cache.embedding.*`` (Step E of the ordered E -> gate -> F execution).

Replaces the retiring in-memory ``QueryEmbeddingCacheMetrics`` (Story #1109
S5, ``query_embedding_cache_metrics.py`` -- deleted in Step F of this story).
That class recorded hits/misses/shadow_cosine/audit samples as PUSH-based OTEL
instruments (Counter.add() / Histogram.record()) fed by per-node in-memory
tallies -- restart-volatile numbers that disagreed across cluster nodes.

Story #1293 made ``search_embed_event`` the durable, phantom-free source of
truth for every query-embedding decision. Story #1294 built the pure
aggregation layer (``windowed_cache_metrics.build_windowed_result``) on top of
it. This module is the OTEL export layer on top of THAT aggregation: every
instrument below is a PULL-based ``ObservableGauge`` whose callback re-fetches
``WindowedCacheMetrics`` for ``[now - window, now)`` on every OTEL export tick
-- there is no push path left, no per-node tally, one source of truth.

BREAKING CHANGE -- Counter -> Gauge semantic shift
---------------------------------------------------
``cidx.cache.embedding.hits`` and ``cidx.cache.embedding.misses`` were
monotonic Counters (``query_embedding_cache_metrics.py:144-149``,
incremented once per operation, cumulative since process start, reset only on
restart). They are now ``ObservableGauge`` instruments reporting the count of
hits/misses observed in the last ``window_seconds`` (default 24h), re-computed
fresh on every scrape. This is a BREAKING CHANGE for any downstream OTEL
consumer that expected a monotonic counter (e.g. a Prometheus ``rate()``/
``increase()`` query, or a dashboard computing deltas across scrapes) --
those consumers must switch to reading the gauge value directly (already a
rate over the window) instead of taking a derivative. ``cidx.cache.embedding.
shadow_cosine`` similarly moves from a push Histogram (raw per-sample
recording) to a set of pull Gauges reporting the window's percentiles
(p50/p05/min) and a 40-bucket histogram snapshot -- a consumer that expected
live per-request histogram buckets must switch to reading the periodic
snapshot instead.

Instrument enumeration (post-cutover source for each)
------------------------------------------------------
  cidx.cache.embedding.total_entries          -- UNCHANGED: total_entries_fn()
                                                  (a cheap query_embedding_cache
                                                  COUNT). NOT event-sourced --
                                                  cache state, not a decision
                                                  event.
  cidx.cache.embedding.hit_rate                -- WindowedCacheMetrics.overall.hit_rate
  cidx.cache.embedding.provider_calls           -- WindowedCacheMetrics.overall.provider_embed_calls
  cidx.cache.embedding.hits                     -- WindowedCacheMetrics.overall.hits   (was Counter)
  cidx.cache.embedding.misses                   -- WindowedCacheMetrics.overall.misses (was Counter)
  cidx.cache.embedding.long_key                 -- WindowedCacheMetrics.overall.long_key (SUM)
  cidx.cache.embedding.audit_top10_overlap      -- WindowedCacheMetrics.overall.audit_avg (was Histogram)
  cidx.cache.embedding.shadow_cosine_p50/_p05/_min
                                                 -- WindowedCacheMetrics.by_cache_mode["shadow"]
                                                    .shadow_cosine_{p50,p05,min} (was Histogram)
  cidx.cache.embedding.shadow_cosine_histogram  -- one Observation per 40-bucket
                                                    (lo, hi) range, value=count,
                                                    from by_cache_mode["shadow"]
                                                    .shadow_cosine_histogram (was Histogram)

Explicitly REMOVED (no DB source)
----------------------------------
  cidx.cache.embedding.audit_top1_match -- the retiring QueryEmbeddingCacheMetrics
      recorded a per-sample "did the top-1 result match" counter. The
      search_embed_event schema (Story #1293) stores only audit_sampled +
      audit_cosine per row -- there is no top1-match column to aggregate.
      Since the story's DoD mandates "any instrument with no DB source is
      explicitly removed", this instrument is NOT re-created here.

Fail-open (Messi #2/#13): every callback swallows exceptions with DEBUG
logging and yields NO observation (or falls back to the empty windowed
result) rather than raising -- OTEL export must never break the query path.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, cast

from code_indexer.server.services.windowed_cache_metrics import (
    WindowedCacheMetricsResult,
    empty_windowed_result,
)

logger = logging.getLogger(__name__)

# Metric names (cidx.cache.embedding.* namespace, matching the retiring
# module's naming convention exactly for the instruments that carry over).
_METRIC_TOTAL_ENTRIES = "cidx.cache.embedding.total_entries"
_METRIC_HIT_RATE = "cidx.cache.embedding.hit_rate"
_METRIC_PROVIDER_CALLS = "cidx.cache.embedding.provider_calls"
_METRIC_HITS = "cidx.cache.embedding.hits"
_METRIC_MISSES = "cidx.cache.embedding.misses"
_METRIC_LONG_KEY = "cidx.cache.embedding.long_key"
_METRIC_AUDIT_TOP10_OVERLAP = "cidx.cache.embedding.audit_top10_overlap"
_METRIC_SHADOW_COSINE_P50 = "cidx.cache.embedding.shadow_cosine_p50"
_METRIC_SHADOW_COSINE_P05 = "cidx.cache.embedding.shadow_cosine_p05"
_METRIC_SHADOW_COSINE_MIN = "cidx.cache.embedding.shadow_cosine_min"
_METRIC_SHADOW_COSINE_HISTOGRAM = "cidx.cache.embedding.shadow_cosine_histogram"

# Default rolling window (seconds) for the DB-backed gauges -- mirrors the
# dashboard's default cache_window (Story #1294, 86400 == 24h) so OTEL export
# and the operator dashboard agree by default.
DEFAULT_OTEL_CACHE_WINDOW_SECONDS = 86400


class EmbeddingCacheOtelMetrics:
    """DB-backed OTEL re-source for cidx.cache.embedding.* (Story #1295).

    Args:
        meter: An opentelemetry.metrics.Meter instance (or a fake/duck-typed
            test double). ``None`` is a documented no-op -- no instruments
            are created and every method is inert.
        total_entries_fn: Zero-arg callable returning the current
            query_embedding_cache entry COUNT. Cheap -- called synchronously
            on every ObservableGauge callback tick. NOT event-sourced.
        windowed_metrics_fn: ``(from_ts: float, to_ts: float) ->
            WindowedCacheMetricsResult`` -- typically
            ``SearchEmbedEventBackend.get_windowed_metrics``. Fail-open: any
            exception is caught HERE and treated as an empty result so a
            transient backend error never raises out of an OTEL callback.
        window_seconds: Rolling window width for every windowed gauge.
            Defaults to 24h (matches the dashboard default).
    """

    def __init__(
        self,
        meter: Any,
        *,
        total_entries_fn: Callable[[], int],
        windowed_metrics_fn: Callable[[float, float], WindowedCacheMetricsResult],
        window_seconds: float = DEFAULT_OTEL_CACHE_WINDOW_SECONDS,
    ) -> None:
        self._meter = meter
        self._total_entries_fn = total_entries_fn
        self._windowed_metrics_fn = windowed_metrics_fn
        self._window_seconds = window_seconds
        self._register()

    # ------------------------------------------------------------------
    # Internal: windowed-result fetch (fail-open)
    # ------------------------------------------------------------------

    def _fetch_windowed(self) -> WindowedCacheMetricsResult:
        to_ts = time.time()
        from_ts = to_ts - self._window_seconds
        try:
            return self._windowed_metrics_fn(from_ts, to_ts)
        except Exception as exc:  # noqa: BLE001 -- OTEL export must never raise
            logger.debug(
                "EmbeddingCacheOtelMetrics: windowed_metrics_fn failed: %s", exc
            )
            return empty_windowed_result()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        if self._meter is None:
            return
        try:
            from opentelemetry.metrics import Observation

            def _single_value_gauge(
                name: str,
                description: str,
                value_fn: Callable[[], Optional[float]],
            ) -> None:
                def _cb(options: Any) -> Any:
                    try:
                        value = value_fn()
                        if value is not None:
                            yield Observation(value=value)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "EmbeddingCacheOtelMetrics: %s callback error: %s",
                            name,
                            exc,
                        )

                self._meter.create_observable_gauge(
                    name=name,
                    description=description,
                    unit="1",
                    callbacks=[_cb],
                )

            # total_entries: UNCHANGED source (cheap cache COUNT), independent
            # of windowed_metrics_fn -- never triggers a DB aggregation fetch.
            _single_value_gauge(
                _METRIC_TOTAL_ENTRIES,
                "Current number of entries in the query-embedding cache",
                self._total_entries_fn,
            )

            _single_value_gauge(
                _METRIC_HIT_RATE,
                "Windowed query-embedding cache hit rate (search_embed_event)",
                lambda: self._fetch_windowed().overall.hit_rate,
            )
            _single_value_gauge(
                _METRIC_PROVIDER_CALLS,
                "Windowed count of provider embed HTTP calls (search_embed_event)",
                lambda: self._fetch_windowed().overall.provider_embed_calls,
            )
            _single_value_gauge(
                _METRIC_HITS,
                "Windowed count of query-embedding cache hits (was a Counter; "
                "see module docstring for the Counter->Gauge breaking change)",
                lambda: self._fetch_windowed().overall.hits,
            )
            _single_value_gauge(
                _METRIC_MISSES,
                "Windowed count of query-embedding cache misses (was a Counter; "
                "see module docstring for the Counter->Gauge breaking change)",
                lambda: self._fetch_windowed().overall.misses,
            )
            _single_value_gauge(
                _METRIC_LONG_KEY,
                "Windowed count of over-256-char normalized-query cache bypasses",
                lambda: self._fetch_windowed().overall.long_key,
            )
            _single_value_gauge(
                _METRIC_AUDIT_TOP10_OVERLAP,
                "Windowed average deep-fidelity audit top-10 overlap fraction "
                "(was a Histogram; see module docstring)",
                lambda: self._fetch_windowed().overall.audit_avg,
            )

            def _shadow_agg_value(attr: str) -> Callable[[], Optional[float]]:
                def _get() -> Optional[float]:
                    agg = self._fetch_windowed().by_cache_mode.get("shadow")
                    if agg is None:
                        return None
                    return cast(Optional[float], getattr(agg, attr))

                return _get

            _single_value_gauge(
                _METRIC_SHADOW_COSINE_P50,
                "Windowed median cosine similarity between cached and live "
                "embeddings in shadow mode",
                _shadow_agg_value("shadow_cosine_p50"),
            )
            _single_value_gauge(
                _METRIC_SHADOW_COSINE_P05,
                "Windowed 5th-percentile shadow-mode cosine similarity",
                _shadow_agg_value("shadow_cosine_p05"),
            )
            _single_value_gauge(
                _METRIC_SHADOW_COSINE_MIN,
                "Windowed minimum shadow-mode cosine similarity",
                _shadow_agg_value("shadow_cosine_min"),
            )

            def _histogram_cb(options: Any) -> Any:
                try:
                    agg = self._fetch_windowed().by_cache_mode.get("shadow")
                    if agg is None:
                        return
                    for lo, hi, count in agg.shadow_cosine_histogram:
                        yield Observation(
                            value=count,
                            attributes={"bucket_lo": lo, "bucket_hi": hi},
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "EmbeddingCacheOtelMetrics: shadow_cosine_histogram "
                        "callback error: %s",
                        exc,
                    )

            self._meter.create_observable_gauge(
                name=_METRIC_SHADOW_COSINE_HISTOGRAM,
                description="Windowed 40-bucket shadow-mode cosine similarity "
                "histogram, one Observation per bucket",
                unit="1",
                callbacks=[_histogram_cb],
            )

            # cidx.cache.embedding.audit_top1_match is EXPLICITLY NOT
            # registered here -- see module docstring "Explicitly REMOVED".
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "EmbeddingCacheOtelMetrics: failed to register instruments: %s",
                exc,
            )
