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

Request seal (AC23): after per-chunk preflight, the ordered flat piece list
for ONE commit is packed into minimal document groups
(``pack_chunks_into_documents``) and those documents are grouped into
request-level batches (``enforce_request_seal``) respecting the
contextualized-embeddings endpoint's per-request caps (max documents / max
total chunks / max total tokens). A normal commit (comfortably under every
cap) still yields exactly ONE document in ONE HTTP request, unchanged from
before; only a pathologically large commit is split across MULTIPLE
requests. Results are flattened back in original order before mean-pooling,
so the 1:1 chunk<->vector contract holds regardless of how many HTTP
requests were needed.
"""

from typing import Any, List

from ....config import VoyageAIConfig
from ...voyage_ai import VoyageAIClient
from ..token_preflight import (
    MAX_CHUNKS_PER_REQUEST,
    MAX_DOCUMENTS_PER_REQUEST,
    MAX_TOKENS_PER_REQUEST,
    enforce_request_seal,
    pack_chunks_into_documents,
    preflight_split_chunk,
)
from .base import TemporalEmbedder
from .registry import register_embedder

# Safety margin applied to provider-spec token limits before treating them as
# a hard seal boundary -- mirrors the embedding request coalescer's own
# margin (Story #1079's `_resolve_token_limit`, 0.9 fallback when the
# provider spec declares no explicit safety_margin_percentage, which is the
# case for every Voyage model today).
_TOKEN_SAFETY_MARGIN = 0.9


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
        # Per-chunk token preflight cap (AC23). Derived from the model's own
        # per-TEXT context length (NOT the per-request token_limit, which is
        # a ~10-100x larger batch budget and made this cap a near-no-op) --
        # in practice a 4096-char chunk is ~1024 tokens, so this only ever
        # fires on pathological token-dense content. Test-overridable via
        # `embedder._max_tokens_per_chunk`.
        self._max_tokens_per_chunk = int(
            self._client._get_model_context_length() * _TOKEN_SAFETY_MARGIN
        )
        # Request-level seal caps (AC23): max documents / max total chunks /
        # max total tokens per contextualized-embeddings HTTP request. All
        # three are test-overridable (`embedder._max_*_per_request`).
        self._max_documents_per_request = MAX_DOCUMENTS_PER_REQUEST
        self._max_chunks_per_request = MAX_CHUNKS_PER_REQUEST
        self._max_tokens_per_request = int(
            MAX_TOKENS_PER_REQUEST * _TOKEN_SAFETY_MARGIN
        )

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

        # AC23: pack the flat piece list into document groups, then seal
        # those documents into request-level batches. A commit comfortably
        # under every cap yields exactly one document in one batch --
        # BYTE-IDENTICAL to the pre-#1290-review-fix call shape.
        documents = pack_chunks_into_documents(
            sent_pieces,
            self._count_tokens,
            max_chunks_per_document=self._max_chunks_per_request,
            max_tokens_per_document=self._max_tokens_per_request,
        )
        request_batches = enforce_request_seal(
            documents,
            self._count_tokens,
            max_documents=self._max_documents_per_request,
            max_chunks=self._max_chunks_per_request,
            max_tokens=self._max_tokens_per_request,
        )

        flat_embeddings: List[List[float]] = []
        for batch in request_batches:
            batch_result = self._client.get_contextualized_embeddings(
                batch,
                input_type="document",
                output_dimension=self.dimensions,
                model=self.name,
            )
            for doc_embeddings in batch_result:
                flat_embeddings.extend(doc_embeddings)

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
