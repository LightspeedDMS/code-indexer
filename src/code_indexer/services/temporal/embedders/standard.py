"""StandardTemporalEmbedder: Cohere embed-v4.0 adapter (Story #1291).

The second, coexisting first-class TemporalEmbedder adapter (alongside
ContextualTemporalEmbedder / voyage-context-4). Chunks the SAME shared
aggregated per-commit document with 15% overlap (versus voyage's 0%),
embeds via CohereEmbeddingProvider.get_embeddings_batch() at 1536 native
dims, and self-registers as "embed-v4.0" in the TemporalEmbedder registry --
zero core indexer/recall change beyond what Story #1290 already built.

Token preflight (AC3, mirrors ContextualTemporalEmbedder's AC23 handling):
get_embeddings_batch() already enforces BOTH the <=96-texts-per-request cap
AND the provider token/request cap internally (dual-constraint batching), so
this adapter does not need to re-implement request-level sealing. What it
DOES need is a PER-CHUNK preflight: a single chunk whose own estimated
tokens exceed the provider's per-chunk cap must be split deterministically
before ever reaching get_embeddings_batch (otherwise a single oversized text
could be sent alone, exceeding the real API limit). Splitting must never
break the 1:1 contract between input chunks and returned embeddings -- an
oversized chunk's sub-piece embeddings are mean-pooled back into ONE vector.
"""

import os
from typing import Any, List, Optional

from ....config import CohereConfig
from ...cohere_embedding import CohereEmbeddingProvider
from ..token_preflight import preflight_split_chunk
from .base import TemporalEmbedder
from .registry import register_embedder

# Safety margin applied to the provider's per-model token limit before
# treating it as the per-CHUNK preflight cap -- mirrors
# ContextualTemporalEmbedder's _TOKEN_SAFETY_MARGIN (Story #1290 AC23).
_TOKEN_SAFETY_MARGIN = 0.9


def _mean_pool(vectors: List[List[float]]) -> List[float]:
    """Mean-pool one or more equal-length vectors into a single vector."""
    if len(vectors) == 1:
        return vectors[0]
    dim = len(vectors[0])
    count = len(vectors)
    return [sum(v[d] for v in vectors) / count for d in range(dim)]


class StandardTemporalEmbedder(TemporalEmbedder):
    """Cohere embed-v4.0 standard (non-contextual) embedder adapter."""

    name = "embed-v4.0"
    model_slug = "embed_v4_0"
    dimensions = 1536
    overlap_percentage = 0.15

    def __init__(self, config: Any):
        """Build the adapter, constructing a CohereEmbeddingProvider pinned to embed-v4.0.

        Args:
            config: Full Config (or None/anything) -- HTTP tuning fields
                (timeout, retries) are copied from `config.cohere` when
                present; the model is always pinned to "embed-v4.0"
                regardless of the caller's primary embedding_provider/model
                (the temporal active_embedder is independent of the regular
                semantic-search provider).

        Availability (AC4): if no Cohere API key is configured (neither
        config.cohere.api_key nor the CO_API_KEY env var), the client is
        NOT constructed and is_available() returns False -- construction
        never raises just because the key is absent, so a NON-ACTIVE
        embedder can be skipped with a warning rather than crashing the
        whole indexing job.
        """
        base_cohere_config = getattr(config, "cohere", None)
        if base_cohere_config is not None:
            cohere_config = base_cohere_config.model_copy(update={"model": self.name})
        else:
            cohere_config = CohereConfig(model=self.name)

        has_key = bool(cohere_config.api_key or os.getenv("CO_API_KEY", ""))
        self._available = has_key
        self._client: Optional[CohereEmbeddingProvider] = None
        if has_key:
            self._client = CohereEmbeddingProvider(cohere_config)
            # Per-chunk token preflight cap (AC3), derived from the model's
            # own token limit -- test-overridable via
            # `embedder._max_tokens_per_chunk`.
            self._max_tokens_per_chunk = int(
                self._client._get_model_token_limit() * _TOKEN_SAFETY_MARGIN
            )
        else:
            self._max_tokens_per_chunk = 0

    def _count_tokens(self, text: str) -> int:
        assert self._client is not None
        return int(self._client._count_tokens(text))

    def is_available(self) -> bool:
        return self._available

    def _require_client(self) -> CohereEmbeddingProvider:
        if self._client is None:
            raise RuntimeError(
                f"Temporal embedder '{self.name}' is not available -- no "
                f"Cohere API key configured (config.cohere.api_key or "
                f"CO_API_KEY env var)."
            )
        return self._client

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        if not chunks:
            return []

        client = self._require_client()

        sent_pieces: List[str] = []
        piece_counts: List[int] = []
        for i, chunk in enumerate(chunks):
            pieces = preflight_split_chunk(
                chunk,
                self._count_tokens,
                self._max_tokens_per_chunk,
                context_label=f"chunk index {i}",
            )
            if not pieces:
                # Defensive: an empty chunk still needs exactly one embedding
                # slot to preserve the 1:1 contract with the caller.
                pieces = [chunk]
            sent_pieces.extend(pieces)
            piece_counts.append(len(pieces))

        # get_embeddings_batch() already enforces the <=96 texts/request AND
        # provider token/request caps internally (dual-constraint batching) --
        # no additional request-level sealing needed here.
        flat_embeddings = client.get_embeddings_batch(
            sent_pieces, embedding_purpose="document"
        )

        pooled: List[List[float]] = []
        cursor = 0
        for count in piece_counts:
            pooled.append(_mean_pool(flat_embeddings[cursor : cursor + count]))
            cursor += count
        return pooled

    def embed_query(self, text: str) -> List[float]:
        client = self._require_client()
        return client.get_embedding(text, embedding_purpose="query")


register_embedder("embed-v4.0", lambda config: StandardTemporalEmbedder(config))
