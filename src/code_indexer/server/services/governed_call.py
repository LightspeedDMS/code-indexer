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

from typing import Any, List, Optional

# Seconds to wait for a governor slot — shared across all 4 embedding sites.
# 30 s is well within the 60 s caller timeout and absorbs momentary bursts.
_GOVERNOR_ACQUIRE_TIMEOUT_SECS: float = 30.0


def _get_embedding_budget(provider: Any) -> str:
    """Map an embedding provider instance to its governor budget key.

    Returns "cohere" for CohereEmbeddingProvider instances and "voyage" for
    everything else (VoyageAIClient and any future Voyage-backed providers).
    """
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    return "cohere" if isinstance(provider, CohereEmbeddingProvider) else "voyage"


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
