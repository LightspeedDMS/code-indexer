"""Temporal RRF fusion for multi-provider temporal search results (Story #633).

Implements Reciprocal Rank Fusion (RRF) for temporal search, using
temporal_chunk_id as the dedup key to correctly merge results from
different providers for the same commit+file+chunk.

Story #1290 (AC10) adds ``dedup_by_commit``: the per-commit coalesce/dedup
primitive used by the per-commit recall pipeline (retrieve -> coalesce ->
dedup-by-commit -> limit). Distinct from the RRF fusion above, which merges
per-shard/per-provider candidate lists — dedup_by_commit collapses ALL
chunk-level hits for the SAME commit_hash into a single representative
result (the max-scoring chunk), unioning `paths[]` from every retained
chunk of that commit so provenance is never lost by picking just one chunk.
"""

import logging
from typing import Any, Callable, Dict, List

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


def _commit_hash_of(result: Any) -> str:
    """Return the commit_hash for a TemporalSearchResult (empty string if absent)."""
    metadata = getattr(result, "metadata", None) or {}
    commit_hash = metadata.get("commit_hash")
    if not commit_hash:
        temporal_context = getattr(result, "temporal_context", None) or {}
        commit_hash = temporal_context.get("commit_hash")
    return str(commit_hash or "")


def dedup_by_commit(results: List[Any]) -> List[Any]:
    """Coalesce ALL chunk-level hits for the same commit into one result (AC10).

    For each commit_hash group, the max-scoring chunk becomes the group's
    representative ("top_chunk"). The representative's `metadata["paths"]`
    is replaced with the ORDER-PRESERVING UNION of `paths` across every
    chunk retained in that commit's group — so provenance from non-winning
    chunks is never silently dropped.

    This is deliberately called on the FULL (over-fetched, not yet
    limit-truncated) candidate list so a commit whose only matching chunk
    ranks low in the raw retrieval order still survives into the deduped
    output (coalesce-before-truncate).

    Args:
        results: Chunk-level TemporalSearchResult hits (any order).

    Returns:
        One representative TemporalSearchResult per distinct commit_hash,
        in no particular order (callers apply their own final sort).
    """
    if not results:
        return []

    groups: Dict[str, List[Any]] = {}
    for result in results:
        key = _commit_hash_of(result)
        groups.setdefault(key, []).append(result)

    representatives: List[Any] = []
    for _commit_hash, group in groups.items():
        best = max(group, key=lambda r: r.score)

        union_paths: List[str] = []
        seen = set()
        head_commit_message = ""
        for member in group:
            member_metadata = getattr(member, "metadata", None) or {}
            member_paths = member_metadata.get("paths") or []
            for path in member_paths:
                if path not in seen:
                    seen.add(path)
                    union_paths.append(path)
            if member_metadata.get("is_head"):
                head_commit_message = member_metadata.get("commit_message", "") or ""

        if best.metadata is not None:
            best.metadata["paths"] = union_paths
            # AC11: when the winning chunk is itself non-head, its OWN
            # commit_message field is "" (AC5) -- stash the group's
            # head-chunk short-capped message so query_temporal's degraded
            # fallback (git reconstruction failure) has something real to
            # surface instead of an empty string.
            if not best.metadata.get("is_head"):
                best.metadata["_head_commit_message"] = head_commit_message

        representatives.append(best)

    return representatives
