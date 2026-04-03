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


def fuse_multiply(
    primary_results: List[QueryResult],
    secondary_results: List[QueryResult],
    limit: int = 10,
) -> List[QueryResult]:
    """Multiply normalized scores. Missing provider uses 0.5 (neutral)."""
    primary_norm = _normalize_scores(list(primary_results))
    secondary_norm = _normalize_scores(list(secondary_results))

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

    all_keys = set(primary_norm.keys()) | set(secondary_norm.keys())
    scores: Dict[str, float] = {}
    for key in all_keys:
        p = primary_norm.get(key, 0.5)
        s = secondary_norm.get(key, 0.5)
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
    """Average normalized scores. Single-provider results use their own score."""
    primary_norm = _normalize_scores(list(primary_results))
    secondary_norm = _normalize_scores(list(secondary_results))

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

    all_keys = set(primary_norm.keys()) | set(secondary_norm.keys())
    scores: Dict[str, float] = {}
    for key in all_keys:
        p = primary_norm.get(key)
        s = secondary_norm.get(key)
        if p is not None and s is not None:
            scores[key] = (p + s) / 2
        elif p is not None:
            scores[key] = p
        else:
            scores[key] = s  # type: ignore[assignment]

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
