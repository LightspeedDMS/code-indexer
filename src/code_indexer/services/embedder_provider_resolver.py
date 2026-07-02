"""Embedder provider resolver (Story #904 of Epic #689).

Resolves the primary and secondary EmbeddingProvider instances for the CLI
query path by inspecting environment variables.

Truth table:

  VOYAGE_API_KEY  CO_API_KEY   Returns
  --------------- ------------ -------------------------------------------
  set             set          (VoyageAIClient, CohereEmbeddingProvider)
  set             unset        (VoyageAIClient, None)
  unset           set          (CohereEmbeddingProvider, None)
  unset           unset        (None, None)  -- caller surfaces "no provider" error

Mirrors how the reranker shim (Story #692) resolves rerank vendors but for
embedders.  Uses VOYAGE_API_KEY for Voyage and CO_API_KEY for Cohere (the
embedding provider's actual key var, matching EmbeddingProviderFactory.resolve_api_key).
"""

import os
from typing import Optional, Tuple

from code_indexer.services.embedding_provider import EmbeddingProvider


def _resolve_embedder_providers() -> Tuple[
    Optional[EmbeddingProvider], Optional[EmbeddingProvider]
]:
    """Return (primary, secondary) EmbeddingProvider instances based on env-vars.

    Provider instances are constructed with default configs (model names from
    VoyageAIConfig / CohereConfig defaults).  Callers must not mutate the
    returned instances.

    Returns:
        (primary, secondary) where each element is an EmbeddingProvider instance
        or None when the corresponding API key is absent.

    Raises:
        Nothing -- key absence is signalled by a None return value so callers
        can choose their own error-surfacing strategy (Messi Rule 02 Anti-Fallback:
        callers decide what to do with None, this function does not hide the gap).
    """
    voyage_key: Optional[str] = os.environ.get("VOYAGE_API_KEY") or None
    cohere_key: Optional[str] = os.environ.get("CO_API_KEY") or None

    primary: Optional[EmbeddingProvider] = None
    secondary: Optional[EmbeddingProvider] = None

    if voyage_key is not None:
        # Lazy import: keeps startup path clean (avoids importing heavy provider
        # modules at module-load time; mirrors the FTS lazy-import pattern).
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        primary = VoyageAIClient(VoyageAIConfig(), None)

        if cohere_key is not None:
            from code_indexer.config import CohereConfig
            from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

            secondary = CohereEmbeddingProvider(CohereConfig(), None)

    elif cohere_key is not None:
        from code_indexer.config import CohereConfig
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        primary = CohereEmbeddingProvider(CohereConfig(), None)
        # secondary stays None (only one provider available)

    return primary, secondary
