"""Reranking pipeline helper for CIDX search tools (Epic #649, Story #653-654).

Provides:
  - _apply_reranking_sync  — apply cross-encoder reranking to a result list
  - calculate_overfetch_limit — compute retrieval limit when reranking is active

Story #654: _apply_reranking_sync returns Tuple[List[dict], dict] where the
second element is rerank_metadata with keys:
  reranker_used: bool, reranker_provider: Optional[str],
  rerank_time_ms: int, rerank_hint: Optional[str]

Bug #679 Part 2 (AC4): rerank_metadata also contains 'reranker_status' nested dict:
  {
    "status": "success" | "failed" | "skipped" | "disabled",
    "provider": Optional[str],
    "rerank_time_ms": Optional[int],
    "hint": Optional[str],
  }
  - success:  named provider, rerank_time_ms >= 0, hint=None
  - failed:   provider=None, rerank_time_ms >= 0, hint contains "failed"/"error"
  - skipped:  provider=None, rerank_time_ms=None, hint contains "skipped"/"down"
  - disabled: provider=None, rerank_time_ms=None, hint=None
"""

import logging
import time
from typing import Any, Callable, List, Optional, Tuple, Type

from code_indexer.server.clients.reranker_clients import (
    CohereRerankerClient,
    RerankerSinbinnedException,
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


def _build_reranker_status(
    status: str,
    provider: Optional[str],
    rerank_time_ms: Optional[int],
    hint: Optional[str] = None,
) -> dict:
    """Build the reranker_status nested dict for query_metadata (AC4).

    Args:
        status: One of "success", "failed", "skipped", "disabled".
        provider: Named provider string on success; None otherwise.
        rerank_time_ms: Elapsed rerank time in ms (None for skipped/disabled).
        hint: Human-readable explanation for non-success states; None on success.

    Returns:
        Dict with keys: status, provider, rerank_time_ms, hint.
    """
    return {
        "status": status,
        "provider": provider,
        "rerank_time_ms": rerank_time_ms,
        "hint": hint,
    }


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
) -> Tuple[Optional[List[Tuple[int, float]]], Optional[str]]:
    """Try one reranker provider; return (scored_pairs, failure_reason).

    scored_pairs is a list of (original_index, relevance_score) tuples,
    ordered by score descending (as returned by the reranker client).

    Returns:
        (scored_pairs, None)  — success
        (None, "skipped")     — provider health=down, not attempted
        (None, "failed")      — provider raised an exception
    """
    health = monitor.get_health(health_key)
    status = health.get(health_key)
    if status is not None and status.status == "down":
        return None, "skipped"
    if monitor.is_sinbinned(health_key):
        return None, "skipped"
    try:
        client = client_cls()
        rerank_results = client.rerank(
            query=query, documents=documents, instruction=instruction, top_k=top_k
        )
        return [(r.index, r.relevance_score) for r in rerank_results], None
    except RerankerSinbinnedException:
        logger.info("%s reranker sin-binned, skipping", provider_name.capitalize())
        return None, "skipped"
    except Exception as exc:
        logger.warning("%s reranker failed: %s", provider_name.capitalize(), exc)
        return None, "failed"


def _run_provider_chain(
    voyage_model: str,
    cohere_model: str,
    query: str,
    documents: List[str],
    instruction: Optional[str],
    top_k: int,
) -> Tuple[Optional[List[Tuple[int, float]]], Optional[str], Optional[str], int]:
    """Run Voyage->Cohere chain; return (scored_pairs, provider_name, failure_reason, elapsed_ms).

    scored_pairs is a list of (original_index, relevance_score) tuples ordered
    by score descending. Carries scores so _apply_reranking_sync can attach them.

    Measures total chain elapsed time from first provider attempt to last.
    failure_reason is the worst-case reason across all providers:
      "failed" takes priority over "skipped" (actual error is more specific).
    Returns (scored_pairs, provider, None, elapsed_ms) on success.
    Returns (None, None, failure_reason, elapsed_ms) when all providers fail/skip.
    """
    monitor = ProviderHealthMonitor.get_instance()
    worst_failure: Optional[str] = None
    t_start = time.monotonic()
    for name, hkey, cls, model in [
        ("Voyage", "voyage-reranker", VoyageRerankerClient, voyage_model),
        ("Cohere", "cohere-reranker", CohereRerankerClient, cohere_model),
    ]:
        if not model:
            continue
        scored_pairs, failure_reason = _attempt_provider_rerank(
            name, hkey, cls, query, documents, instruction, top_k, monitor
        )
        if scored_pairs is not None:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            return scored_pairs, name.lower(), None, elapsed_ms
        # Track worst failure: "failed" > "skipped"
        if worst_failure != "failed":
            worst_failure = failure_reason
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return None, None, worst_failure, elapsed_ms


def _apply_reranking_sync(
    results: List[dict],
    rerank_query: Optional[str],
    rerank_instruction: Optional[str],
    content_extractor: Callable[[dict], str],
    requested_limit: int,
    config_service: Any,
) -> Tuple[List[dict], dict]:  # noqa: E501
    """Apply reranking; return (results, rerank_metadata). No-op when rerank_query absent.

    rerank_metadata contains legacy Story #654 fields plus 'reranker_status' (AC4).
    """
    safe_limit = max(0, requested_limit)
    if not rerank_query:
        if rerank_instruction:
            logger.warning(
                "rerank_instruction provided without rerank_query has no effect"
            )
        meta = _build_metadata(False, None, 0)
        meta["reranker_status"] = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        return results, meta
    if not results:
        meta = _build_metadata(False, None, 0)
        meta["reranker_status"] = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        return results, meta
    voyage_model, cohere_model = _load_provider_models(config_service)
    if not voyage_model and not cohere_model:
        meta = _build_metadata(False, "none", 0, _DISABLED_HINT)
        meta["reranker_status"] = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        return results[:safe_limit], meta
    documents = [content_extractor(r) for r in results]
    top_k = min(safe_limit, len(results))
    if top_k <= 0:
        meta = _build_metadata(False, "none", 0)
        meta["reranker_status"] = _build_reranker_status(
            status="disabled", provider=None, rerank_time_ms=None
        )
        return results[:0], meta
    reranked_pairs, active_provider, failure_reason, elapsed_ms = _run_provider_chain(
        voyage_model, cohere_model, rerank_query, documents, rerank_instruction, top_k
    )
    if reranked_pairs is None:
        logger.warning(
            "Reranking failed for all providers, returning results in original order"
        )
        if failure_reason == "skipped":
            reranker_status = _build_reranker_status(
                status="skipped",
                provider=None,
                rerank_time_ms=None,
                hint="Provider skipped: reranker is down (circuit-breaker active)",
            )
        else:
            reranker_status = _build_reranker_status(
                status="failed",
                provider=None,
                rerank_time_ms=elapsed_ms,
                hint="All reranker providers failed with errors",
            )
        meta = _build_metadata(False, "none", elapsed_ms)
        meta["reranker_status"] = reranker_status
        return results[:safe_limit], meta
    valid_pairs = [(i, score) for i, score in reranked_pairs if 0 <= i < len(results)]
    if len(valid_pairs) != len(reranked_pairs):
        logger.warning(
            "Reranker returned %d out-of-range indices (dropped); results count: %d",
            len(reranked_pairs) - len(valid_pairs),
            len(results),
        )
    reordered = []
    for idx, score in valid_pairs:
        result = results[idx]
        result["rerank_score"] = score
        reordered.append(result)
    meta = _build_metadata(True, active_provider, elapsed_ms)
    meta["reranker_status"] = _build_reranker_status(
        status="success",
        provider=active_provider,
        rerank_time_ms=elapsed_ms,
    )
    return reordered, meta


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


# ---------------------------------------------------------------------------
# Story #883 Phase D: Tagged pool helpers
# ---------------------------------------------------------------------------


def _tag_and_pool(code_results: list, memory_candidates: list) -> list:
    """Merge code results and memory candidates into a single pooled list.

    Code items are tagged with ``_source_tag="code"`` and left otherwise
    unchanged (shallow copy to avoid mutating the caller's list).

    Memory items are tagged with ``_source_tag="memory"`` and augmented with
    fields extracted from the MemoryCandidate dataclass:
      - ``memory_id``  — the HNSW point id
      - ``memory_path`` — disk path from the payload (for Stage 8 hydration)
      - ``hnsw_score`` — Voyage cosine similarity score (Scenario 16 ordering)
      - ``title``      — memory headline (from frontmatter pre-load)
      - ``summary``    — one-sentence gist (from frontmatter pre-load)

    Code items appear first in the returned list, followed by memory items.

    Args:
        code_results: List of code-search result dicts.
        memory_candidates: List of MemoryCandidate objects (with title/summary
            pre-populated by the handler's frontmatter pre-load loop).

    Returns:
        Combined list with _source_tag (and memory fields) injected on each item.
    """
    pool = []
    for item in code_results:
        tagged = dict(item)
        tagged["_source_tag"] = "code"
        pool.append(tagged)
    for candidate in memory_candidates:
        pool.append(
            {
                "_source_tag": "memory",
                "memory_id": candidate.memory_id,
                "memory_path": candidate.memory_path,
                "hnsw_score": candidate.hnsw_score,
                "title": candidate.title,
                "summary": candidate.summary,
            }
        )
    return pool


def _tagged_content_extractor(item: dict) -> str:
    """Extract rerank-query-relevant text from a pooled item by its _source_tag.

    For ``_source_tag="memory"``: returns ``title + ': ' + summary`` so the
    Cohere / Voyage reranker can assess relevance against the query using the
    memory's headline text.  Returns empty string when both fields are absent.

    For ``_source_tag="code"`` (or unrecognised): returns ``item["content"]``
    or falls back to ``item["code_snippet"]`` (mirrors existing extractor
    lambdas in _apply_rerank_and_filter).

    Args:
        item: A dict from the pool built by _tag_and_pool.

    Returns:
        String used as the document text for reranking.
    """
    if item.get("_source_tag") == "memory":
        title = item.get("title", "")
        summary = item.get("summary", "")
        if not title and not summary:
            return ""
        return f"{title}: {summary}" if title and summary else (title or summary)
    return item.get("content", "") or item.get("code_snippet", "")
