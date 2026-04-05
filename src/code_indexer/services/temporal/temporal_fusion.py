"""Temporal RRF fusion for multi-provider temporal search results (Story #633).

Implements Reciprocal Rank Fusion (RRF) for temporal search, using
temporal_chunk_id as the dedup key to correctly merge results from
different providers for the same commit+file+chunk.
"""

import logging
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

# Over-fetch multiplier: each provider queried for limit * this value
TEMPORAL_OVERFETCH_MULTIPLIER = 3

# Default RRF constant (standard value = 60)
DEFAULT_RRF_K = 60


def make_temporal_dedup_key(result) -> str:
    """Build temporal dedup key from result fields.

    Format: {commit_hash}:{file_path}:{chunk_index}
    """
    commit_hash = result.temporal_context.get("commit_hash", "")
    return f"{commit_hash}:{result.file_path}:{result.chunk_index}"


def fuse_rrf_multi(
    results_by_provider: Dict[str, list],
    dedup_key: Callable,
    limit: int,
    k: int = DEFAULT_RRF_K,
) -> list:
    """Fuse temporal search results from multiple providers using RRF.

    Args:
        results_by_provider: Dict mapping provider_name -> list of TemporalSearchResult
        dedup_key: Function that extracts dedup key from a result
        limit: Maximum results to return
        k: RRF constant (default 60)

    Returns:
        Fused list of TemporalSearchResult sorted by descending fusion_score
    """
    if not results_by_provider:
        return []

    # Single provider pass-through (no fusion needed)
    providers = list(results_by_provider.keys())
    if len(providers) == 1:
        provider = providers[0]
        results = results_by_provider[provider]
        for r in results:
            r.source_provider = provider
            r.contributing_providers = [provider]
            r.fusion_score = r.score
        return results[:limit]

    # Multi-provider RRF fusion
    # Track: dedup_key -> {rrf_score, best_result, best_score, best_provider, contributors}
    fused: Dict[str, dict] = {}

    for provider_name, results in results_by_provider.items():
        for rank, result in enumerate(results):
            key = dedup_key(result)
            rrf_contribution = 1.0 / (k + rank + 1)  # rank is 0-based, RRF uses 1-based

            if key not in fused:
                fused[key] = {
                    "rrf_score": 0.0,
                    "best_result": result,
                    "best_score": result.score,
                    "best_provider": provider_name,
                    "contributors": [],
                }

            entry = fused[key]
            entry["rrf_score"] += rrf_contribution
            entry["contributors"].append(provider_name)

            # Track highest individual score for source_provider attribution
            if result.score > entry["best_score"]:
                entry["best_score"] = result.score
                entry["best_result"] = result
                entry["best_provider"] = provider_name

    # Build output list
    output: List = []
    for key, entry in fused.items():
        result = entry["best_result"]
        result.fusion_score = entry["rrf_score"]
        result.source_provider = entry["best_provider"]
        result.contributing_providers = entry["contributors"]
        result.temporal_chunk_id = key
        output.append(result)

    # Sort by fusion_score descending
    output.sort(key=lambda r: r.fusion_score or 0.0, reverse=True)

    return output[:limit]
