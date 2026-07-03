"""ContextualTemporalEmbedder: first-class voyage-context-4 adapter (Story #1290).

Wraps VoyageAIClient.get_contextualized_embeddings for the per-commit
contextual temporal embedder: 0% overlap chunking (chunking itself happens
in contextual_chunker.py before this adapter is called), 1024 output
dimensions, POST /v1/contextualizedembeddings.

Token preflight (AC23): contextual_chunker.py already produced fixed
CHARACTER-size chunks (aggregation_chunk_chars), but pathological
token-dense content (a long no-whitespace diff, a huge commit message) can
still exceed the provider's per-chunk token cap. embed_commit_chunks()
preflights each chunk and splits an oversized one DETERMINISTICALLY before
the API call. Splitting must never change the 1:1 contract between
requested chunks and returned embeddings (each AggregatedChunk needs exactly
one embedding for its point/payload) -- sub-piece embeddings for one
oversized original chunk are mean-pooled back into a single vector. This
keeps chunk_index/paths/point_id (computed once, before embedding) valid
regardless of any preflight split.
"""

from typing import Any, List

from ....config import VoyageAIConfig
from ...voyage_ai import VoyageAIClient
from ..token_preflight import preflight_split_chunk
from .base import TemporalEmbedder
from .registry import register_embedder


def _mean_pool(vectors: List[List[float]]) -> List[float]:
    """Mean-pool one or more equal-length vectors into a single vector."""
    if len(vectors) == 1:
        return vectors[0]
    dim = len(vectors[0])
    count = len(vectors)
    return [sum(v[d] for v in vectors) / count for d in range(dim)]


class ContextualTemporalEmbedder(TemporalEmbedder):
    """voyage-context-4 contextual embedder adapter."""

    name = "voyage-context-4"
    model_slug = "voyage_context_4"
    dimensions = 1024
    overlap_percentage = 0.0

    def __init__(self, config: Any):
        """Build the adapter, constructing a VoyageAIClient pinned to voyage-context-4.

        Args:
            config: Full Config (or None/anything) -- HTTP tuning fields
                (timeout, retries) are copied from `config.voyage_ai` when
                present; the model is always pinned to "voyage-context-4"
                regardless of the caller's primary embedding_provider/model
                (the temporal active_embedder is independent of the regular
                semantic-search provider).
        """
        base_voyage_config = getattr(config, "voyage_ai", None)
        if base_voyage_config is not None:
            voyage_config = base_voyage_config.model_copy(update={"model": self.name})
        else:
            voyage_config = VoyageAIConfig(model=self.name)
        self._client = VoyageAIClient(voyage_config)
        # Per-chunk token preflight cap (AC23). Defaults to the model's
        # request-level token limit -- in practice a 4096-char chunk is
        # ~1024 tokens, so this only ever fires on pathological token-dense
        # content. Test-overridable via `embedder._max_tokens_per_chunk`.
        self._max_tokens_per_chunk = self._client._get_model_token_limit()

    def _count_tokens(self, text: str) -> int:
        return int(self._client._count_tokens_accurately(text))

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        if not chunks:
            return []

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

        result = self._client.get_contextualized_embeddings(
            [sent_pieces],
            input_type="document",
            output_dimension=self.dimensions,
            model=self.name,
        )
        flat_embeddings = result[0]

        pooled: List[List[float]] = []
        cursor = 0
        for count in piece_counts:
            pooled.append(_mean_pool(flat_embeddings[cursor : cursor + count]))
            cursor += count
        return pooled

    def embed_query(self, text: str) -> List[float]:
        result = self._client.get_contextualized_embeddings(
            [[text]],
            input_type="query",
            output_dimension=self.dimensions,
            model=self.name,
        )
        return result[0][0]


register_embedder("voyage-context-4", lambda config: ContextualTemporalEmbedder(config))
