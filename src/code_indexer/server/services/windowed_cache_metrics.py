"""WindowedCacheMetrics — durable, windowed, cluster-aggregated cache metrics
(Story #1294, Epic #1288).

Story #1293 made `search_embed_event` the phantom-free, durable source of
truth for every query-embedding decision (one row per NEEDED embed, real
correlation ids, no phantom hits). This module is the pure aggregation layer
on top of that table: given a set of rows for a selected time window, it
computes the exact per-(cache_mode, provider) metrics the operator dashboard
needs — hit rate, provider embed calls, coalescer batch/dedup stats, shadow
cosine distribution, and audit stats.

This module is intentionally DB-agnostic: `compute_aggregate()` and
`build_windowed_result()` are pure functions over plain row mappings (dicts).
The backend-specific pieces (SQLite / PostgreSQL row fetch for a
`[from_ts, to_ts)` window) live on `SearchEmbedEventSqliteBackend` /
`SearchEmbedEventPostgresBackend` in `search_embed_event_writer.py`, which
call into this module's pure functions after fetching rows. Keeping the
formula logic here (not duplicated per backend) is what makes the two
backends provably identical.

Algorithm (verbatim from Story #1294's issue body):

  hits   = COUNT rows WHERE outcome IN (hit, shadow_hit)      # joiners count as hits
  misses = COUNT rows WHERE outcome IN (miss, shadow_miss)
  hit_rate = hits / (hits + misses)          IF (hits+misses) > 0 ELSE 0

  provider_embed_calls = COUNT(DISTINCT live_batch_id)
                        + COUNT(*) WHERE role='direct' AND outcome IN ('miss','shadow_miss')

  batches         = COUNT(DISTINCT live_batch_id)
  texts_coalesced = COUNT(*) WHERE live_batch_id IS NOT NULL
  dedup           = texts_coalesced - SUM_over_batches( COUNT(DISTINCT embed_key) per batch )

  shadow_vals  = [shadow_cosine for rows WHERE shadow_cosine IS NOT NULL]
  shadow_p50   = percentile(shadow_vals, 50)
  shadow_p05   = percentile(shadow_vals, 5)
  shadow_min   = min(shadow_vals)
  shadow_hist  = histogram(shadow_vals)

  long_key_sum = SUM(long_key)                 # count of truthy long_key rows
  audit_count  = COUNT rows WHERE audit_sampled
  audit_sum    = SUM(audit_cosine WHERE audit_sampled)
  audit_avg    = audit_sum / audit_count       IF audit_count > 0 ELSE 0

Note on column naming: the issue's Algorithm section names the audit value
column "audit_top10_overlap"; the actual `search_embed_event` schema (Story
#1293, search_embed_event_writer.py) stores this value in the `audit_cosine`
column. This module reads the ACTUAL schema column (`audit_cosine`) per the
project's fact-verification standard — the formula (SUM/COUNT/AVG over
audit_sampled rows) is unchanged, only the column name differs from the
issue's prose.

FAIL-OPEN: every aggregate defaults to zero/empty rather than raising, so a
malformed row or backend error never breaks the dashboard (see
`empty_windowed_result()` and the backend's try/except wrapping).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Cosine histogram bucketing constants (mirrors the retiring in-memory
# QueryEmbeddingCacheMetrics._build_histogram bucket layout so the dashboard's
# existing bar-chart rendering code in routes.py needs no changes).
COSINE_HIST_MIN: float = -1.0
COSINE_HIST_MAX: float = 1.0
COSINE_HIST_BUCKET_WIDTH: float = 0.05
_COSINE_HIST_N_BUCKETS: int = round(
    (COSINE_HIST_MAX - COSINE_HIST_MIN) / COSINE_HIST_BUCKET_WIDTH
)

# Outcome-value groupings used by the algorithm above.
_HIT_OUTCOMES = ("hit", "shadow_hit")
_MISS_OUTCOMES = ("miss", "shadow_miss")


@dataclass
class CacheMetricsAggregate:
    """One aggregate result (either the whole window, one (cache_mode,
    provider) group, or one cache_mode-collapsed-across-providers group).
    """

    hits: int = 0
    misses: int = 0
    hit_rate: float = 0.0
    provider_embed_calls: int = 0
    batches: int = 0
    texts_coalesced: int = 0
    dedup: int = 0
    shadow_cosine_p50: Optional[float] = None
    shadow_cosine_p05: Optional[float] = None
    shadow_cosine_min: Optional[float] = None
    shadow_cosine_histogram: List[Tuple[float, float, int]] = field(
        default_factory=list
    )
    long_key: int = 0
    audit_count: int = 0
    audit_sum: float = 0.0
    audit_avg: float = 0.0


@dataclass
class WindowedCacheMetricsResult:
    """Result of aggregating a window of search_embed_event rows.

    overall:       aggregate over ALL rows in the window (no filtering) —
                   feeds cards that are not mode-specific (provider embed
                   calls, texts coalesced, batches, dedup, long_key, audit).
    by_group:      GROUP BY (cache_mode, provider) per the story's literal
                   Algorithm — keyed by (cache_mode, provider) tuple.
    by_cache_mode: GROUP BY cache_mode only (providers collapsed) — feeds
                   mode-specific cards (e.g. Shadow Hit Rate, Shadow Cosine).
    """

    overall: CacheMetricsAggregate
    by_group: Dict[Tuple[Optional[str], Optional[str]], CacheMetricsAggregate]
    by_cache_mode: Dict[Optional[str], CacheMetricsAggregate]


def build_cosine_histogram(cosines: List[float]) -> List[Tuple[float, float, int]]:
    """Build a 40-bucket cosine histogram over [-1.0, 1.0], bucket width 0.05.

    Always returns a 40-element list of (lo, hi, count) tuples, all-zero when
    `cosines` is empty. Bucketing rules mirror the retiring in-memory
    QueryEmbeddingCacheMetrics._build_histogram (Story #1152) exactly, so
    existing dashboard bar-rendering code (log10 scaling in routes.py) is
    unaffected by the data-source change.
    """
    n = _COSINE_HIST_N_BUCKETS
    w = COSINE_HIST_BUCKET_WIDTH
    lo_base = COSINE_HIST_MIN
    counts = [0] * n
    for val in cosines:
        idx = int((val - lo_base) / w)
        if idx >= n:
            idx = n - 1
        elif idx < 0:
            idx = 0
        else:
            if idx > 0 and abs(val - (lo_base + idx * w)) < 1e-12:
                idx -= 1
        counts[idx] += 1
    result = []
    for i in range(n):
        lo = round(lo_base + i * w, 10)
        hi = round(lo_base + (i + 1) * w, 10)
        result.append((lo, hi, counts[i]))
    return result


def _percentile_stats(
    cosines: List[float],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (p50, p05, min) over cosines, or (None, None, None) if empty."""
    if not cosines:
        return None, None, None
    sorted_vals = sorted(cosines)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        p50 = (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    else:
        p50 = sorted_vals[mid]
    p05_idx = min(int(0.05 * n), n - 1)
    p05 = sorted_vals[p05_idx]
    cmin = sorted_vals[0]
    return p50, p05, cmin


def compute_aggregate(rows: List[Mapping[str, Any]]) -> CacheMetricsAggregate:
    """Compute one CacheMetricsAggregate from a list of search_embed_event
    row mappings, per Story #1294's Algorithm section (verbatim, see module
    docstring). Pure function — no I/O, never raises on well-formed rows.
    """
    hits = sum(1 for r in rows if r.get("outcome") in _HIT_OUTCOMES)
    misses = sum(1 for r in rows if r.get("outcome") in _MISS_OUTCOMES)
    hit_rate = hits / (hits + misses) if (hits + misses) > 0 else 0.0

    batch_ids = {r.get("live_batch_id") for r in rows if r.get("live_batch_id")}
    batches = len(batch_ids)
    texts_coalesced = sum(1 for r in rows if r.get("live_batch_id"))
    direct_calls = sum(
        1
        for r in rows
        if r.get("role") == "direct" and r.get("outcome") in _MISS_OUTCOMES
    )
    provider_embed_calls = batches + direct_calls

    embed_keys_by_batch: Dict[Any, set] = {}
    for r in rows:
        lbid = r.get("live_batch_id")
        if lbid:
            embed_keys_by_batch.setdefault(lbid, set()).add(r.get("embed_key"))
    dedup = texts_coalesced - sum(len(s) for s in embed_keys_by_batch.values())

    shadow_vals = [
        r["shadow_cosine"] for r in rows if r.get("shadow_cosine") is not None
    ]
    p50, p05, cmin = _percentile_stats(shadow_vals)
    histogram = build_cosine_histogram(shadow_vals)

    long_key_sum = sum(1 for r in rows if r.get("long_key"))

    audit_rows = [r for r in rows if r.get("audit_sampled")]
    audit_count = len(audit_rows)
    audit_sum = sum(r.get("audit_cosine") or 0.0 for r in audit_rows)
    audit_avg = audit_sum / audit_count if audit_count > 0 else 0.0

    return CacheMetricsAggregate(
        hits=hits,
        misses=misses,
        hit_rate=hit_rate,
        provider_embed_calls=provider_embed_calls,
        batches=batches,
        texts_coalesced=texts_coalesced,
        dedup=dedup,
        shadow_cosine_p50=p50,
        shadow_cosine_p05=p05,
        shadow_cosine_min=cmin,
        shadow_cosine_histogram=histogram,
        long_key=long_key_sum,
        audit_count=audit_count,
        audit_sum=float(audit_sum),
        audit_avg=audit_avg,
    )


def build_windowed_result(rows: List[Mapping[str, Any]]) -> WindowedCacheMetricsResult:
    """Aggregate a window's rows into overall + by_group + by_cache_mode.

    Pure function — no I/O, never raises on well-formed rows. Backends wrap
    the DB fetch + this call in a try/except for fail-open behavior.
    """
    overall = compute_aggregate(rows)

    grouped: Dict[Tuple[Optional[str], Optional[str]], List[Mapping[str, Any]]] = {}
    by_mode_rows: Dict[Optional[str], List[Mapping[str, Any]]] = {}
    for r in rows:
        key = (r.get("cache_mode"), r.get("provider"))
        grouped.setdefault(key, []).append(r)
        by_mode_rows.setdefault(r.get("cache_mode"), []).append(r)

    by_group = {key: compute_aggregate(grp) for key, grp in grouped.items()}
    by_cache_mode = {
        mode: compute_aggregate(mode_rows) for mode, mode_rows in by_mode_rows.items()
    }

    return WindowedCacheMetricsResult(
        overall=overall, by_group=by_group, by_cache_mode=by_cache_mode
    )


def empty_windowed_result() -> WindowedCacheMetricsResult:
    """FAIL-OPEN default: never breaks the dashboard on a backend error."""
    return WindowedCacheMetricsResult(
        overall=compute_aggregate([]), by_group={}, by_cache_mode={}
    )
