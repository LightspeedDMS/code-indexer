"""
Query strategy router and score fusion algorithms (Story #488).

Strategies:
- primary_only: Use primary provider only (default, no failover)
- failover: Switch to secondary on primary failure
- parallel: Query both, fuse scores
- specific: Use explicitly named provider

Score fusion methods (for parallel strategy):
- rrf: Reciprocal Rank Fusion (default)
- multiply: Normalized score multiplication
- average: Normalized score averaging
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class QueryStrategy(Enum):
    """Query routing strategy."""

    PRIMARY_ONLY = "primary_only"
    FAILOVER = "failover"
    PARALLEL = "parallel"
    SPECIFIC = "specific"


class ScoreFusion(Enum):
    """Score fusion method for parallel queries."""

    RRF = "rrf"
    MULTIPLY = "multiply"
    AVERAGE = "average"


# Failover defaults (pre-Story #491)
DEFAULT_FAILOVER_TIMEOUT = 10.0  # seconds
FAILOVER_HTTP_CODES = {500, 502, 503, 504}

# RRF constant
RRF_K = 60

# Story #638: Dual-Provider Fusion Quality constants
PARALLEL_FETCH_MULTIPLIER = 2
MAX_PARALLEL_FETCH = 40
SCORE_GATE_RATIO = 0.80
SCORE_GATE_FLOOR = 0.70
PARALLEL_TIMEOUT_SECONDS = 20

# Neutral score used for a missing/single provider in fuse_multiply and
# fuse_average (midpoint of the [0, 1] normalized range -- neither rewards
# nor penalizes a document that only one provider returned).
NEUTRAL_FUSION_SCORE = 0.5


@dataclass
class QueryResult:
    """Single search result with provider tracking."""

    file_path: str
    score: float
    content: str = ""
    chunk_id: str = ""
    repository_alias: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_provider: str = ""
    fusion_score: Optional[float] = None
    contributing_providers: Optional[List[str]] = None


def apply_score_gate(
    primary_results: List[Any],
    secondary_results: List[Any],
    score_attr: str = "score",
) -> tuple:
    """Symmetric score-gated filtering of the weaker provider (Story #638).

    Determines stronger/weaker by actual max scores. If weaker_max is
    below stronger_max * SCORE_GATE_RATIO, filters weaker results below
    stronger_max * SCORE_GATE_FLOOR. Returns (primary_results, secondary_results)
    in original order with weaker set potentially culled.

    Args:
        primary_results: Results from primary provider.
        secondary_results: Results from secondary provider.
        score_attr: Attribute name to read score from. Use "score" for
            StrategyQueryResult, "similarity_score" for semantic QueryResult.

    If either list is empty, returns inputs unchanged (no gating possible).
    """
    if not primary_results or not secondary_results:
        return primary_results, secondary_results

    primary_max = max(getattr(r, score_attr) for r in primary_results)
    secondary_max = max(getattr(r, score_attr) for r in secondary_results)

    if secondary_max < primary_max * SCORE_GATE_RATIO:
        # Primary is stronger — filter secondary
        floor = primary_max * SCORE_GATE_FLOOR
        logger.debug(
            "Score gate triggered: primary_max=%.4f secondary_max=%.4f floor=%.4f — filtering secondary",
            primary_max,
            secondary_max,
            floor,
        )
        filtered_secondary = [
            r for r in secondary_results if getattr(r, score_attr) >= floor
        ]
        return primary_results, filtered_secondary
    elif primary_max < secondary_max * SCORE_GATE_RATIO:
        # Secondary is stronger — filter primary
        floor = secondary_max * SCORE_GATE_FLOOR
        logger.debug(
            "Score gate triggered: secondary_max=%.4f primary_max=%.4f floor=%.4f — filtering primary",
            secondary_max,
            primary_max,
            floor,
        )
        filtered_primary = [
            r for r in primary_results if getattr(r, score_attr) >= floor
        ]
        return filtered_primary, secondary_results
    else:
        # Providers are close — no gating
        return primary_results, secondary_results


def fuse_rrf(
    primary_results: List[QueryResult],
    secondary_results: List[QueryResult],
    limit: int = 10,
) -> List[QueryResult]:
    """Reciprocal Rank Fusion.

    score(doc) = sum(1 / (k + rank_in_provider_i))
    where k=60 (standard constant).
    """
    scores: Dict[str, float] = {}
    result_map: Dict[str, QueryResult] = {}
    contributing: Dict[str, set] = defaultdict(set)

    for rank, r in enumerate(primary_results):
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        scores[key] = scores.get(key, 0) + 1.0 / (RRF_K + rank + 1)
        contributing[key].add(r.source_provider)
        if key not in result_map:
            result_map[key] = r

    for rank, r in enumerate(secondary_results):
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        scores[key] = scores.get(key, 0) + 1.0 / (RRF_K + rank + 1)
        contributing[key].add(r.source_provider)
        if key not in result_map:
            result_map[key] = r

    # Sort by fused score descending
    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    fused = []
    for key in sorted_keys[:limit]:
        fused.append(
            replace(
                result_map[key],
                score=scores[key],
                source_provider="fused",
                fusion_score=scores[key],
                contributing_providers=sorted(contributing[key]),
            )
        )

    return fused


def _normalize_scores(results: List[QueryResult]) -> Dict[str, float]:
    """Min-max normalize scores to [0, 1], returned as dict keyed by dedup key."""
    if not results:
        return {}
    scores_list = [r.score for r in results]
    min_s = min(scores_list)
    max_s = max(scores_list)
    normalized: Dict[str, float] = {}
    for r in results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        if max_s == min_s:
            normalized[key] = 1.0
        else:
            normalized[key] = (r.score - min_s) / (max_s - min_s)
    return normalized


def _normalize_scores_global(all_results: List[QueryResult]) -> Dict[str, float]:
    """Min-max normalize scores across the COMBINED pool (not per-provider).

    Story #638: Global normalization preserves relative score gaps between
    documents when providers have different score scales.
    """
    if not all_results:
        return {}
    scores_list = [r.score for r in all_results]
    min_s = min(scores_list)
    max_s = max(scores_list)
    normalized: Dict[str, float] = {}
    for r in all_results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        if max_s == min_s:
            normalized[key] = 1.0
        else:
            normalized[key] = (r.score - min_s) / (max_s - min_s)
    return normalized


def _normalize_scores_global_per_provider(
    primary_results: List[QueryResult],
    secondary_results: List[QueryResult],
) -> "tuple[Dict[str, float], Dict[str, float]]":
    """Min-max normalize primary and secondary results INDEPENDENTLY, using a
    SHARED global min/max computed across the combined pool.

    Bug #1712: `_normalize_scores_global` returns a single dict keyed by
    document. When the same document appears in BOTH primary_results and
    secondary_results (the consensus case), both results map to the same
    key and the second write silently discards the first provider's
    normalized value -- only one provider's contribution survives even
    though both raw scores were used to compute the global min/max.

    This helper preserves the global min/max computation (so relative score
    gaps between documents are still preserved regardless of per-provider
    scale, per Story #638) but keeps each provider's normalized value in its
    own dict, so a consensus document has two independently-computed
    normalized scores available to combine.
    """
    all_results = list(primary_results) + list(secondary_results)
    if not all_results:
        return {}, {}
    scores_list = [r.score for r in all_results]
    min_s = min(scores_list)
    max_s = max(scores_list)

    def _normalize(results: List[QueryResult]) -> Dict[str, float]:
        normalized: Dict[str, float] = {}
        for r in results:
            key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
            if max_s == min_s:
                normalized[key] = 1.0
            else:
                normalized[key] = (r.score - min_s) / (max_s - min_s)
        return normalized

    return _normalize(primary_results), _normalize(secondary_results)


def fuse_multiply(
    primary_results: List[QueryResult],
    secondary_results: List[QueryResult],
    limit: int = 10,
) -> List[QueryResult]:
    """Multiply globally-normalized scores. Missing provider uses 0.5 (neutral).

    Story #638: Uses global normalization across the combined pool so that
    score gaps between documents are preserved regardless of per-provider scale.

    Bug #1712 fix: primary and secondary are normalized into SEPARATE dicts
    (sharing the same global min/max) so a consensus document (present in
    both providers) genuinely multiplies each provider's OWN normalized
    score, instead of squaring whichever value happened to be written last
    into a single collapsed dict.
    """
    primary_norm, secondary_norm = _normalize_scores_global_per_provider(
        primary_results, secondary_results
    )

    result_map: Dict[str, QueryResult] = {}
    contributing: Dict[str, set] = defaultdict(set)

    for r in primary_results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        result_map[key] = r
        contributing[key].add(r.source_provider)

    for r in secondary_results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        if key not in result_map:
            result_map[key] = r
        contributing[key].add(r.source_provider)

    primary_keys = {
        f"{r.repository_alias}:{r.file_path}:{r.chunk_id}" for r in primary_results
    }
    secondary_keys = {
        f"{r.repository_alias}:{r.file_path}:{r.chunk_id}" for r in secondary_results
    }
    all_keys = primary_keys | secondary_keys
    scores: Dict[str, float] = {}
    for key in all_keys:
        p = (
            primary_norm.get(key, NEUTRAL_FUSION_SCORE)
            if key in primary_keys
            else NEUTRAL_FUSION_SCORE
        )
        s = (
            secondary_norm.get(key, NEUTRAL_FUSION_SCORE)
            if key in secondary_keys
            else NEUTRAL_FUSION_SCORE
        )
        scores[key] = p * s

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    fused = []
    for key in sorted_keys[:limit]:
        fused.append(
            replace(
                result_map[key],
                score=scores[key],
                source_provider="fused",
                fusion_score=scores[key],
                contributing_providers=sorted(contributing[key]),
            )
        )

    return fused


def fuse_average(
    primary_results: List[QueryResult],
    secondary_results: List[QueryResult],
    limit: int = 10,
) -> List[QueryResult]:
    """Average globally-normalized scores. Single-provider uses (norm + 0.5) / 2.

    Story #638: Uses global normalization across the combined pool.
    - Consensus (both providers): score = average of each provider's OWN
      independently-normalized score
    - Single-provider: score = (global_norm[key] + 0.5) / 2  (consensus bias)

    Bug #1712 fix: primary and secondary are normalized into SEPARATE dicts
    (sharing the same global min/max) so a consensus document (present in
    both providers) genuinely averages each provider's OWN normalized score,
    instead of reading whichever value happened to be written last into a
    single collapsed dict.
    """
    primary_norm, secondary_norm = _normalize_scores_global_per_provider(
        primary_results, secondary_results
    )

    result_map: Dict[str, QueryResult] = {}
    contributing: Dict[str, set] = defaultdict(set)

    for r in primary_results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        result_map[key] = r
        contributing[key].add(r.source_provider)

    for r in secondary_results:
        key = f"{r.repository_alias}:{r.file_path}:{r.chunk_id}"
        if key not in result_map:
            result_map[key] = r
        contributing[key].add(r.source_provider)

    primary_keys = {
        f"{r.repository_alias}:{r.file_path}:{r.chunk_id}" for r in primary_results
    }
    secondary_keys = {
        f"{r.repository_alias}:{r.file_path}:{r.chunk_id}" for r in secondary_results
    }
    all_keys = primary_keys | secondary_keys
    scores: Dict[str, float] = {}
    for key in all_keys:
        in_primary = key in primary_keys
        in_secondary = key in secondary_keys
        if in_primary and in_secondary:
            # Consensus result: average both providers' own normalized scores
            scores[key] = (primary_norm[key] + secondary_norm[key]) / 2
        elif in_primary:
            # Single-provider (primary only): blend with neutral score
            scores[key] = (primary_norm[key] + NEUTRAL_FUSION_SCORE) / 2
        else:
            # Single-provider (secondary only): blend with neutral score
            scores[key] = (secondary_norm[key] + NEUTRAL_FUSION_SCORE) / 2

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    fused = []
    for key in sorted_keys[:limit]:
        fused.append(
            replace(
                result_map[key],
                score=scores[key],
                source_provider="fused",
                fusion_score=scores[key],
                contributing_providers=sorted(contributing[key]),
            )
        )

    return fused


FUSION_METHODS = {
    ScoreFusion.RRF: fuse_rrf,
    ScoreFusion.MULTIPLY: fuse_multiply,
    ScoreFusion.AVERAGE: fuse_average,
}


def execute_parallel_query(
    primary_query_fn: Callable[[], List[QueryResult]],
    secondary_query_fn: Callable[[], List[QueryResult]],
    fusion: ScoreFusion = ScoreFusion.RRF,
    limit: int = 10,
) -> List[QueryResult]:
    """Execute parallel query on both providers and fuse results."""
    primary_results: List[QueryResult] = []
    secondary_results: List[QueryResult] = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(primary_query_fn): "primary",
            executor.submit(secondary_query_fn): "secondary",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results = future.result()
                if name == "primary":
                    primary_results = results
                else:
                    secondary_results = results
            except Exception as e:
                logger.warning("Parallel query %s failed: %s", name, e)

    if not primary_results and not secondary_results:
        return []
    if not secondary_results:
        return primary_results[:limit]
    if not primary_results:
        return secondary_results[:limit]

    fuse_fn = FUSION_METHODS.get(fusion, fuse_rrf)
    return fuse_fn(primary_results, secondary_results, limit)


def execute_failover_query(
    primary_query_fn: Callable[[], List[QueryResult]],
    secondary_query_fn: Callable[[], List[QueryResult]],
    limit: int = 10,
) -> List[QueryResult]:
    """Execute primary query, failover to secondary on error."""
    try:
        results = primary_query_fn()
        return results[:limit]
    except Exception as e:
        logger.warning("Primary query failed, failing over to secondary: %s", e)
        return secondary_query_fn()[:limit]
