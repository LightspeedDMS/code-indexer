"""Unified CLI search funnel (Story #693 -- Epic #689).

Provides a single post-retrieval stage that applies the server reranker pipeline
to any CLI result list and truncates to the user-requested limit.

Public API:
  _apply_cli_rerank_and_filter  -- apply rerank and truncate a result list
  calculate_cli_overfetch_limit -- compute over-fetch limit from CLI config

Intermediate dict shape (consumed by _apply_reranking_sync):
  The server reranking orchestrator expects a flat list of dicts and a
  content_extractor callable.  This module detects the CLI result shape
  (semantic or FTS) and supplies the correct extractor:

    Semantic shape  {"score": float, "payload": {"content": str, ...}}
      -> extractor: result["payload"].get("content", "")

    FTS shape       {"path": str, "snippet": str, "match_text": str, ...}
      -> extractor: result.get("snippet", "") or result.get("match_text", "")

  All fields are passed through unchanged -- the reranker only reads the text
  returned by the extractor; it does not mutate or replace any other field.
  Round-trip fidelity is therefore guaranteed by the server orchestrator itself.

Design constraints (Story #693 Must NOT):
  - No import of server internals beyond _apply_reranking_sync and
    calculate_overfetch_limit.
  - No reimplementation of sin-bin, failover, or provider-chain logic.
  - No modification of display function signatures.
  - No hybrid fusion (RRF); hybrid mode is handled by callers calling this
    function twice (once per sublist).
"""

import logging
from typing import Any, Callable, Dict, List, Optional, cast

from code_indexer.server.mcp.reranking import (
    _apply_reranking_sync,
    calculate_overfetch_limit,
)
from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content extractor detection
# ---------------------------------------------------------------------------


def _is_semantic_result(result: Dict[str, Any]) -> bool:
    """Return True when result carries the semantic shape (has a 'payload' key)."""
    return "payload" in result


def _semantic_content_extractor(result: Dict[str, Any]) -> str:
    """Extract rerank-relevant text from a semantic search result."""
    payload = result.get("payload", {})
    return payload.get("content", "") or ""


def _fts_content_extractor(result: Dict[str, Any]) -> str:
    """Extract rerank-relevant text from an FTS / regex search result."""
    return result.get("snippet", "") or result.get("match_text", "") or ""


def _detect_content_extractor(
    results: List[Dict[str, Any]],
) -> Callable[[Dict[str, Any]], str]:
    """Choose content extractor based on first result shape.

    Defaults to FTS extractor when list is empty (caller truncates to 0 anyway).
    """
    if results and _is_semantic_result(results[0]):
        return _semantic_content_extractor
    return _fts_content_extractor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_cli_overfetch_limit(
    user_limit: int,
    config: CliRerankConfigService,
) -> int:
    """Compute over-fetch retrieval limit from CLI rerank config.

    Wraps the server's calculate_overfetch_limit so CLI callers have a single
    import point.

    Args:
        user_limit: Number of results the user wants after reranking.
        config: CLI rerank config shim providing overfetch_multiplier.

    Returns:
        Effective retrieval limit, capped at MAX_CANDIDATE_LIMIT (200).
    """
    multiplier = config.get_config().rerank_config.overfetch_multiplier
    # cast: calculate_overfetch_limit is annotated -> int in reranking.py but mypy
    # infers Any at this call site due to the cross-module import boundary.
    return cast(
        int,
        calculate_overfetch_limit(
            requested_limit=user_limit,
            overfetch_multiplier=multiplier,
        ),
    )


def _apply_cli_rerank_and_filter(
    *,
    results: List[Dict[str, Any]],
    rerank_query: Optional[str],
    rerank_instruction: Optional[str],
    config: CliRerankConfigService,
    user_limit: int,
    health_monitor: ProviderHealthMonitor,
) -> List[Dict[str, Any]]:
    """Apply rerank stage to a normalized result list and truncate to user_limit.

    Shape-agnostic: works with both semantic results (have a 'payload' key) and
    FTS results (flat dict with 'snippet'/'match_text').  The correct content
    extractor is selected automatically based on the first result in the list.

    Short-circuit semantics (mirrors server/mcp/reranking.py):
      - rerank_query is None or empty string  ->  skip rerank, return truncated input.
      - results is empty                      ->  return empty list immediately.

    Graceful degradation (Epic #689 Success Criterion 6):
      - Both providers down / sin-binned      ->  return truncated input, no exception.
      - Both providers raise HTTP errors      ->  return truncated input, no exception.
      - No API keys configured               ->  return truncated input, no exception.

    The health_monitor parameter is accepted for API symmetry and future use.
    The server reranker clients resolve the active singleton via
    ProviderHealthMonitor.get_instance(); the caller is responsible for wiring
    the desired monitor instance as the singleton before invoking this function.

    Args:
        results: Result list from any CLI retrieval path (semantic, FTS, or regex).
        rerank_query: Text query for the reranker.  None or "" skips reranking.
        rerank_instruction: Optional instruction prepended to rerank_query.
        config: CLI rerank config shim (CliRerankConfigService from Story #692).
        user_limit: Maximum number of results to return.
        health_monitor: ProviderHealthMonitor instance (used by caller for sinbin
            state control; reranker clients resolve their own singleton internally).

    Returns:
        Result list reranked (if rerank_query is set and a provider succeeds) and
        truncated to user_limit.  Dicts are the same objects from the input list
        (with an optional 'rerank_score' key added by the server orchestrator on
        successful rerank).
    """
    safe_limit = max(0, user_limit)

    if not results:
        return []

    if not rerank_query:
        return results[:safe_limit]

    content_extractor = _detect_content_extractor(results)

    try:
        reranked, _meta = _apply_reranking_sync(
            results=results,
            rerank_query=rerank_query,
            rerank_instruction=rerank_instruction,
            content_extractor=content_extractor,
            requested_limit=safe_limit,
            config_service=config,
        )
        # cast: _apply_reranking_sync returns Tuple[List[dict], dict] where List[dict]
        # uses the unparameterised dict; mypy widens the element type to Any and flags
        # the return.  The runtime type is correct: every element is a Dict[str, Any].
        return cast(List[Dict[str, Any]], reranked)
    except Exception as exc:
        logger.warning(
            "CLI reranker raised an unexpected exception; returning original order: %s",
            exc,
        )
        return results[:safe_limit]
