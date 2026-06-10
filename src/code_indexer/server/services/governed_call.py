"""Shared governed embedding call helper (Bug #1078).

Consolidates the duplicated
    execute_with_backoff(lambda: governor.execute(budget, lambda: provider.get_embedding(...)))
pattern used by all 4 query-embedding serving sites:
  - search_service.py  (non-FilesystemVectorStore path)
  - handlers/search.py (_compute_memory_query_vector)
  - services/temporal/temporal_search_service.py
  - storage/filesystem_vector_store.py (generate_embedding inner fn)

Reranking (reranking.py _attempt_provider_rerank) is intentionally NOT here —
it calls client.rerank(), a different operation on a different client type.

The test at tests/integration/test_provider_governor_real_concurrency_1078.py
uses this function directly to exercise the real Voyage HTTP path under a 20-
thread simultaneous burst and assert the governor caps in-flight to K=8.
"""

import logging
from typing import Any, List, Optional, cast

from code_indexer.server.services.coalescer_registry import get_coalescer_registry
from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

# Seconds to wait for a governor slot — shared across all 4 embedding sites.
# 30 s is well within the 60 s caller timeout and absorbs momentary bursts.
_GOVERNOR_ACQUIRE_TIMEOUT_SECS: float = 30.0


def _get_embedding_budget(provider: Any) -> str:
    """Map an embedding provider instance to its governor EMBED-lane key.

    Story #1079 Phase B+C: the governor is now 4-lane. Embedding calls route to
    the ``:embed`` lane of the provider:
      - "cohere:embed" for CohereEmbeddingProvider instances,
      - "voyage:embed" for everything else (VoyageAIClient and any future
        Voyage-backed providers).
    Rerank calls use the ``:rerank`` lanes (see mcp/reranking.py).
    """
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    return (
        "cohere:embed"
        if isinstance(provider, CohereEmbeddingProvider)
        else "voyage:embed"
    )


def governed_query_embedding(
    provider: Any,
    text: str,
    *,
    embedding_purpose: Optional[str] = "query",
    acquire_timeout: float = _GOVERNOR_ACQUIRE_TIMEOUT_SECS,
) -> List[float]:
    """Gate one query-embedding HTTP call through the concurrency governor.

    Wraps the canonical serving-path call:
        execute_with_backoff(
            lambda: governor.execute(
                budget,
                lambda: provider.get_embedding(text, embedding_purpose=...),
                acquire_timeout=...,
            )
        )

    The governor semaphore caps concurrent in-flight calls at K per budget.
    The execute_with_backoff wrapper handles HTTP 429 retries OUTSIDE the
    held slot so other callers can use the freed slot during backoff sleep.

    Args:
        provider: Any EmbeddingProvider (VoyageAIClient, CohereEmbeddingProvider, …).
        text: Query text to embed.
        embedding_purpose: Passed through to provider.get_embedding().
            Default "query" for all serving-path callers.
        acquire_timeout: Seconds to wait for a governor slot.

    Returns:
        List[float] embedding vector.

    Raises:
        GovernorBusyError: acquire_timeout elapsed with no slot available.
        ProviderSinbinnedError: provider budget is sinbinned.
        ProviderRateLimitedError: all retry attempts exhausted (429).
        Any other exception raised by provider.get_embedding().
    """
    from code_indexer.server.services.provider_concurrency_governor import (
        ProviderConcurrencyGovernor,
    )
    from code_indexer.services.provider_backoff import execute_with_backoff

    budget = _get_embedding_budget(provider)
    governor = ProviderConcurrencyGovernor.get_instance()

    return execute_with_backoff(  # type: ignore[no-any-return]
        lambda: governor.execute(
            budget,
            lambda: provider.get_embedding(text, embedding_purpose=embedding_purpose),
            acquire_timeout=acquire_timeout,
        )
    )


def coalesced_query_embedding(
    provider: Any,
    text: str,
    *,
    embedding_purpose: Optional[str] = "query",
    acquire_timeout: float = _GOVERNOR_ACQUIRE_TIMEOUT_SECS,
) -> List[float]:
    """Server-gated entry point for a single query embedding (Story #1079 Phase E).

    The 4 query sites call this (swapping only the function name). ALL gating
    lives here, so call sites are identical on CLI and server. Args mirror
    ``governed_query_embedding``. The chosen path is debug-logged; each branch is
    explicit (Messi #2 anti-fallback). See the inline path comments below.
    """

    def _direct() -> List[float]:
        return governed_query_embedding(
            provider,
            text,
            embedding_purpose=embedding_purpose,
            acquire_timeout=acquire_timeout,
        )

    registry = get_coalescer_registry()
    if registry is None:
        # Path 1: CLI/solo — no registry was ever built. Direct governed call.
        # No coalescer constructed, no accumulation window — CLI path untouched.
        logger.debug("coalesced_query_embedding: no registry -> direct governed call")
        return _direct()

    # Read the kill switch LIVE so Web UI toggles hot-reload without a restart
    # (mirrors the memory_retrieval_enabled pattern). Defensive: if config is
    # unreadable, fail toward the simpler always-correct direct governed call.
    try:
        coalesce_enabled = bool(get_config_service().get_config().coalesce_enabled)
    except Exception as exc:  # noqa: BLE001 — config read is best-effort here
        logger.warning(
            "coalesced_query_embedding: could not read coalesce_enabled (%s); "
            "delegating to direct governed call",
            exc,
        )
        coalesce_enabled = False

    if not coalesce_enabled:
        # Path 2: kill switch — governor + AIMD still apply, no batching.
        logger.debug("coalesced_query_embedding: coalesce disabled -> direct call")
        return _direct()

    lane = _get_embedding_budget(provider)  # "cohere:embed" or "voyage:embed"
    coalescer = registry.get(lane)
    if coalescer is None:
        # Path 4: lane not configured (provider key absent) — direct governed call.
        logger.debug(
            "coalesced_query_embedding: no coalescer for lane=%s -> direct call",
            lane,
        )
        return _direct()

    # Path 3: coalesce. submit() blocks until this text's vector is ready.
    logger.debug("coalesced_query_embedding: coalescing on lane=%s", lane)
    return cast(List[float], coalescer.submit(text))
