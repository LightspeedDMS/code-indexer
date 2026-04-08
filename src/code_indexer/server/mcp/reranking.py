"""Reranking pipeline helper for CIDX search tools (Epic #649, Story #653-654).

Provides:
  - _apply_reranking_sync  — apply cross-encoder reranking to a result list
  - calculate_overfetch_limit — compute retrieval limit when reranking is active

Story #654: _apply_reranking_sync returns Tuple[List[dict], dict] where the
second element is rerank_metadata with keys:
  reranker_used: bool, reranker_provider: Optional[str],
  rerank_time_ms: int, rerank_hint: Optional[str]
"""

import logging
import time
from typing import Any, Callable, List, Optional, Tuple, Type

from code_indexer.server.clients.reranker_clients import (
    CohereRerankerClient,
    VoyageRerankerClient,
)
from code_indexer.server.utils.config_manager import RerankConfig
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

logger = logging.getLogger(__name__)

MAX_CANDIDATE_LIMIT = 200  # Hard cap on candidates fetched for reranking

_DISABLED_HINT = (
    "Reranking requested but no reranker providers are configured. "
    "Configure voyage_reranker_model or cohere_reranker_model in "
    "Settings > Reranking."
)


def _build_metadata(
    used: bool, provider: Optional[str], ms: int, hint: Optional[str] = None
) -> dict:
    """Return standard rerank_metadata dict for Story #654 telemetry."""
    return {
        "reranker_used": used,
        "reranker_provider": provider,
        "rerank_time_ms": ms,
        "rerank_hint": hint,
    }


def _load_provider_models(config_service: Any) -> Tuple[str, str]:
    """Return (voyage_model, cohere_model) from config."""
    cfg = config_service.get_config()
    rc = cfg.rerank_config or RerankConfig()
    return rc.voyage_reranker_model, rc.cohere_reranker_model


def _attempt_provider_rerank(
    provider_name: str,
    health_key: str,
    client_cls: Type,
    query: str,
    documents: List[str],
    instruction: Optional[str],
    top_k: int,
    monitor: ProviderHealthMonitor,
) -> Optional[List[int]]:
    """Try one reranker provider; return reranked indices or None on failure/down."""
    health = monitor.get_health(health_key)
    status = health.get(health_key)
    if status is not None and status.status == "down":
        return None
    try:
        client = client_cls()
        rerank_results = client.rerank(
            query=query, documents=documents, instruction=instruction, top_k=top_k
        )
        return [r.index for r in rerank_results]
    except Exception as exc:
        logger.warning("%s reranker failed: %s", provider_name.capitalize(), exc)
        return None


def _run_provider_chain(
    voyage_model: str,
    cohere_model: str,
    query: str,
    documents: List[str],
    instruction: Optional[str],
    top_k: int,
) -> Tuple[Optional[List[int]], Optional[str]]:
    """Run Voyage->Cohere chain; return (indices, provider_name) or (None, None)."""
    monitor = ProviderHealthMonitor.get_instance()
    for name, hkey, cls, model in [
        ("Voyage", "voyage-reranker", VoyageRerankerClient, voyage_model),
        ("Cohere", "cohere-reranker", CohereRerankerClient, cohere_model),
    ]:
        if not model:
            continue
        indices = _attempt_provider_rerank(
            name, hkey, cls, query, documents, instruction, top_k, monitor
        )
        if indices is not None:
            return indices, name.lower()
    return None, None


def _apply_reranking_sync(
    results: List[dict],
    rerank_query: Optional[str],
    rerank_instruction: Optional[str],
    content_extractor: Callable[[dict], str],
    requested_limit: int,
    config_service: Any,
) -> Tuple[List[dict], dict]:  # noqa: E501
    """Apply reranking; return (results, rerank_metadata). No-op when rerank_query absent."""
    safe_limit = max(0, requested_limit)
    if not rerank_query:
        if rerank_instruction:
            logger.warning(
                "rerank_instruction provided without rerank_query has no effect"
            )
        return results, _build_metadata(False, None, 0)
    if not results:
        return results, _build_metadata(False, None, 0)
    voyage_model, cohere_model = _load_provider_models(config_service)
    if not voyage_model and not cohere_model:
        return results[:safe_limit], _build_metadata(False, "none", 0, _DISABLED_HINT)
    documents = [content_extractor(r) for r in results]
    top_k = min(safe_limit, len(results))
    if top_k <= 0:
        return results[:0], _build_metadata(False, "none", 0)
    t_start = time.monotonic()
    reranked_indices, active_provider = _run_provider_chain(
        voyage_model, cohere_model, rerank_query, documents, rerank_instruction, top_k
    )
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    if reranked_indices is None:
        logger.warning(
            "Reranking failed for all providers, returning results in original order"
        )
        return results[:safe_limit], _build_metadata(False, "none", elapsed_ms)
    valid_indices = [i for i in reranked_indices if 0 <= i < len(results)]
    if len(valid_indices) != len(reranked_indices):
        logger.warning(
            "Reranker returned %d out-of-range indices (dropped); results count: %d",
            len(reranked_indices) - len(valid_indices),
            len(results),
        )
    return [results[i] for i in valid_indices], _build_metadata(
        True, active_provider, elapsed_ms
    )


def calculate_overfetch_limit(
    requested_limit: int,
    overfetch_multiplier: int,
    access_filter_overfetch: int = 0,
) -> int:
    """Calculate effective limit for retrieval when reranking is active.

    Formula: max(requested_limit * overfetch_multiplier,
                 requested_limit + access_filter_overfetch)
    Capped at MAX_CANDIDATE_LIMIT candidates.

    Args:
        requested_limit: Number of results the caller wants after reranking.
        overfetch_multiplier: From RerankConfig.overfetch_multiplier (default 5).
        access_filter_overfetch: Extra results needed for access filtering (default 0).

    Returns:
        Effective retrieval limit, capped at MAX_CANDIDATE_LIMIT.
    """
    reranker_limit = requested_limit * overfetch_multiplier
    effective = max(reranker_limit, requested_limit + access_filter_overfetch)
    return min(MAX_CANDIDATE_LIMIT, effective)
