"""Story #1109 (S5): OTEL metrics for the query-embedding cache.

Instruments:
  cidx.cache.embedding.hits   (Counter)  — incremented on cache hits
  cidx.cache.embedding.misses (Counter)  — incremented on cache misses / errors
  cidx.cache.embedding.total_entries (ObservableGauge) — cheap memoized count
  cidx.cache.embedding.shadow_cosine (Histogram) — cos(cached, live) in shadow+hit

Design constraints (AC1–AC5 from Story #1109):
  - hit/miss counters ALWAYS carry {"mode": <mode>, "provider": <provider>} attrs.
  - ObservableGauge callback calls `total_entries_fn` (a cheap memo supplied by
    the wiring layer) — NEVER calls backend.total_entries() directly.
  - shadow_cosine is recorded ONLY when mode==shadow AND a prior cached blob exists
    (enforced by the caller, _serve_with_cache in governed_call.py).
  - All OTEL calls are fail-open: an exception inside any record_* method MUST
    never propagate to the caller — only a DEBUG log is emitted.
  - This module has zero import-time side-effects. When the `meter` argument is
    None (or OTEL SDK is absent) all methods are no-ops.

Namespace: meter name "cidx.cache" — same namespace family as the existing
    ApplicationMetrics in metrics_instrumentation.py ("cidx.application").
"""

from __future__ import annotations

import logging
import struct
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Maximum number of shadow-cosine values kept for p50 computation.
# Bounded to avoid unbounded memory growth in long-running servers.
_MAX_COSINE_BUFFER = 1000

# Metric names (match AC1/AC2 spec exactly)
_METRIC_HITS = "cidx.cache.embedding.hits"
_METRIC_MISSES = "cidx.cache.embedding.misses"
_METRIC_TOTAL_ENTRIES = "cidx.cache.embedding.total_entries"
_METRIC_SHADOW_COSINE = "cidx.cache.embedding.shadow_cosine"

# Story #1110 (S6): deep-fidelity audit metrics
_METRIC_AUDIT_OVERLAP = "cidx.cache.embedding.audit_top10_overlap"
_METRIC_AUDIT_TOP1 = "cidx.cache.embedding.audit_top1_match"


def _decode_f32le(blob: bytes) -> List[float]:
    """Decode float32 little-endian bytes to a Python float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _dot_product(a: List[float], b: List[float]) -> float:
    """Plain Python dot product (no numpy dep in this module)."""
    return sum(x * y for x, y in zip(a, b))


def _magnitude(v: List[float]) -> float:
    import math

    return math.sqrt(sum(x * x for x in v))


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two float vectors."""
    ma = _magnitude(a)
    mb = _magnitude(b)
    if ma == 0.0 or mb == 0.0:
        return 0.0
    return _dot_product(a, b) / (ma * mb)


class QueryEmbeddingCacheMetrics:
    """OTEL metrics facade for the query-embedding cache.

    Args:
        meter: An opentelemetry.metrics.Meter instance (or MagicMock in tests).
               Pass a real SDK Meter for production; any duck-typed object works.
        total_entries_fn: Zero-arg callable returning current total cache entries.
               MUST be cheap (no DB query) — used as the ObservableGauge callback.

    All record_* methods are fail-open: exceptions are swallowed with DEBUG logging.
    """

    def __init__(
        self,
        meter: Any,
        *,
        total_entries_fn: Callable[[], int],
    ) -> None:
        self._meter = meter
        self._total_entries_fn = total_entries_fn

        self._hits_counter: Optional[Any] = None
        self._misses_counter: Optional[Any] = None
        self._total_entries_gauge: Optional[Any] = None
        self._shadow_cosine_hist: Optional[Any] = None
        # Story #1110 (S6): deep-fidelity audit instruments
        self._audit_top10_overlap_hist: Optional[Any] = None
        self._audit_top1_counter: Optional[Any] = None

        # In-process readable tallies (GAP 2 / Story #1109 dashboard fix).
        # These are incremented alongside every OTEL .add() call so the
        # dashboard can derive real hit-ratios without an OTEL exporter.
        self._lock = threading.Lock()
        # _tallies[mode]["hits"] and _tallies[mode]["misses"]
        self._tallies: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"hits": 0, "misses": 0}
        )
        # Bounded ring buffer of shadow cosine values for p50 computation.
        self._cosine_buffer: List[float] = []

        # Story #1110 (S6): in-process audit tallies (guarded by _lock).
        self._audit_total: int = 0
        self._audit_top1_matches: int = 0
        self._audit_overlap_sum: float = 0.0

        # Story #1149: long_key counter — incremented when build_key returns
        # None (normalized-query part exceeds 256-char cap). Surfaced in
        # snapshot() so the dashboard and REST front door can expose it.
        self._long_key: int = 0

        self._register()

    def _register(self) -> None:
        """Create OTEL instruments on the meter. Fail-open."""
        try:
            self._hits_counter = self._meter.create_counter(
                name=_METRIC_HITS,
                description="Number of query-embedding cache hits",
                unit="1",
            )
            self._misses_counter = self._meter.create_counter(
                name=_METRIC_MISSES,
                description="Number of query-embedding cache misses / errors",
                unit="1",
            )
            # ObservableGauge: callback calls the cheap total_entries_fn.
            # The closure captures self._total_entries_fn to avoid closure-over-mutable.
            _fn = self._total_entries_fn

            def _gauge_callback(options: Any) -> Any:
                try:
                    from opentelemetry.metrics import Observation

                    yield Observation(value=_fn())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("cache metrics: gauge callback error: %s", exc)

            self._total_entries_gauge = self._meter.create_observable_gauge(
                name=_METRIC_TOTAL_ENTRIES,
                description="Current number of entries in the query-embedding cache",
                unit="1",
                callbacks=[_gauge_callback],
            )
            self._shadow_cosine_hist = self._meter.create_histogram(
                name=_METRIC_SHADOW_COSINE,
                description="Cosine similarity between cached and live embedding in shadow mode",
                unit="1",
            )
            # Story #1110 (S6): audit instruments (fail-open; registered after S5 instruments)
            self._audit_top10_overlap_hist = self._meter.create_histogram(
                name=_METRIC_AUDIT_OVERLAP,
                description="Top-10 overlap fraction between cached and live HNSW results in audit",
                unit="1",
            )
            self._audit_top1_counter = self._meter.create_counter(
                name=_METRIC_AUDIT_TOP1,
                description="Number of audit samples where top-1 result matches cached result",
                unit="1",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: failed to register instruments: %s", exc)

    # ------------------------------------------------------------------
    # Public record methods (all fail-open)
    # ------------------------------------------------------------------

    def record_hit(self, *, mode: str, provider: str) -> None:
        """Increment the hits counter and the in-process tally.

        Args:
            mode: Cache mode — "on" or "shadow".
            provider: Provider name — e.g. "voyage-ai" or "cohere".
        """
        # Increment in-process tally (thread-safe, always, even if OTEL fails).
        try:
            with self._lock:
                self._tallies[mode]["hits"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_hit tally error: %s", exc)

        if self._hits_counter is None:
            return
        try:
            self._hits_counter.add(1, {"mode": mode, "provider": provider})
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_hit OTEL error: %s", exc)

    def record_miss(self, *, mode: str, provider: str) -> None:
        """Increment the misses counter and the in-process tally.

        Args:
            mode: Cache mode — "on" or "shadow".
            provider: Provider name — e.g. "voyage-ai" or "cohere".
        """
        # Increment in-process tally (thread-safe, always, even if OTEL fails).
        try:
            with self._lock:
                self._tallies[mode]["misses"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_miss tally error: %s", exc)

        if self._misses_counter is None:
            return
        try:
            self._misses_counter.add(1, {"mode": mode, "provider": provider})
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_miss OTEL error: %s", exc)

    def record_shadow_cosine(
        self,
        *,
        cached_blob: bytes,
        live_vec: List[float],
    ) -> None:
        """Record the cosine similarity between a cached blob and the live vector.

        Must only be called when mode==shadow AND a prior cached blob exists.
        The cosine is computed here from the float32-LE blob and the live vector.
        Also appends the value to the bounded in-process buffer for p50.

        Args:
            cached_blob: Raw float32 LE bytes from the cache backend.
            live_vec: Live embedding vector (list of floats).
        """
        try:
            cached_vec = _decode_f32le(cached_blob)
            cosine = _cosine_similarity(cached_vec, live_vec)
            # Append to bounded in-process buffer (thread-safe).
            with self._lock:
                if len(self._cosine_buffer) >= _MAX_COSINE_BUFFER:
                    # Evict oldest value (simple ring: remove head).
                    self._cosine_buffer.pop(0)
                self._cosine_buffer.append(cosine)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_shadow_cosine buffer error: %s", exc)
            return

        if self._shadow_cosine_hist is None:
            return
        try:
            self._shadow_cosine_hist.record(cosine)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_shadow_cosine OTEL error: %s", exc)

    def record_long_key(self, *, provider: str) -> None:
        """Increment the long_key counter (Story #1149).

        Called when build_key returns None because the normalized-query part
        exceeds the 256-char cap.  The over-cap query is NOT cached; the call
        site also records a MISS (separate counter).  This counter is a pure
        diagnostic: how many queries were skipped due to exceeding the cap.

        Args:
            provider: Provider name — e.g. "voyage-ai" or "cohere".
        """
        try:
            with self._lock:
                self._long_key += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_long_key error: %s", exc)

    def record_audit(
        self,
        *,
        top10_overlap: float,
        top1_match: bool,
        provider: str,
        mode: str,
    ) -> None:
        """Record a deep-fidelity audit sample (Story #1110 S6).

        Called by the search layer (Chunk B) after comparing cached-vector HNSW
        results against live-vector HNSW results.

        Args:
            top10_overlap: Fraction of top-10 results that overlap between
                cached and live HNSW searches (0.0 to 1.0).
            top1_match: True when the top-1 result is identical in both searches.
            provider: Provider name — e.g. "voyage-ai" or "cohere".
            mode: Cache mode at time of sampling — "on" or "shadow".

        Fail-open: any exception is swallowed with DEBUG logging; the caller
        must never be impacted by metrics recording failures.
        """
        # Increment in-process tallies under lock (always, even if OTEL fails).
        try:
            with self._lock:
                self._audit_total += 1
                self._audit_overlap_sum += top10_overlap
                if top1_match:
                    self._audit_top1_matches += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: record_audit tally error: %s", exc)

        # Record OTEL Histogram (top10_overlap value).
        if self._audit_top10_overlap_hist is not None:
            try:
                self._audit_top10_overlap_hist.record(
                    top10_overlap, {"provider": provider, "mode": mode}
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("cache metrics: record_audit histogram error: %s", exc)

        # Record OTEL Counter ONLY when top1_match=True.
        if top1_match and self._audit_top1_counter is not None:
            try:
                self._audit_top1_counter.add(1, {"provider": provider, "mode": mode})
            except Exception as exc:  # noqa: BLE001
                logger.debug("cache metrics: record_audit top1 counter error: %s", exc)

    # ------------------------------------------------------------------
    # Readable in-process snapshot (GAP 2)
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time snapshot of in-process tallies.

        Returns a dict with the following structure:
            {
                "shadow": {"hits": int, "misses": int},
                "on":     {"hits": int, "misses": int},
                "shadow_cosine_p50": float | None,
            }

        Hit-ratio is DERIVED per mode in the caller (never blended).
        shadow_cosine_p50 is the median of the bounded cosine buffer, or None
        when no cosines have been recorded.

        Thread-safe: acquires the internal lock for a brief copy.
        """
        try:
            with self._lock:
                shadow = dict(self._tallies.get("shadow", {"hits": 0, "misses": 0}))
                on = dict(self._tallies.get("on", {"hits": 0, "misses": 0}))
                cosines = list(self._cosine_buffer)
                # Story #1110 (S6): snapshot audit tallies under same lock.
                audit_total = self._audit_total
                audit_top1_matches = self._audit_top1_matches
                audit_overlap_sum = self._audit_overlap_sum
                # Story #1149: long_key counter.
                long_key = self._long_key

            p50: Optional[float] = None
            if cosines:
                sorted_cosines = sorted(cosines)
                mid = len(sorted_cosines) // 2
                if len(sorted_cosines) % 2 == 0:
                    p50 = (sorted_cosines[mid - 1] + sorted_cosines[mid]) / 2.0
                else:
                    p50 = sorted_cosines[mid]

            audit_overlap_avg: Optional[float] = (
                audit_overlap_sum / audit_total if audit_total > 0 else None
            )

            return {
                "shadow": shadow,
                "on": on,
                "shadow_cosine_p50": p50,
                # Story #1110 (S6): audit fields
                "audit_total": audit_total,
                "audit_top1_matches": audit_top1_matches,
                "audit_overlap_avg": audit_overlap_avg,
                # Story #1149: long_key counter
                "long_key": long_key,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("cache metrics: snapshot error: %s", exc)
            return {
                "shadow": {"hits": 0, "misses": 0},
                "on": {"hits": 0, "misses": 0},
                "shadow_cosine_p50": None,
                "audit_total": 0,
                "audit_top1_matches": 0,
                "audit_overlap_avg": None,
                "long_key": 0,
            }
