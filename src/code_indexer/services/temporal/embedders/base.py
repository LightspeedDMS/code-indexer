"""Pluggable TemporalEmbedder adapter interface (Story #1290 / Epic #1289).

A TemporalEmbedder wraps ONE embedding-provider-specific strategy for turning
a per-commit aggregated document's ordered text chunks into vectors, and for
embedding a query string for temporal recall. Adding a new embedder requires
implementing this interface and registering it via
``code_indexer.services.temporal.embedders.registry`` -- zero core
indexer/recall change (Epic #1289 primary objective).
"""

from abc import ABC, abstractmethod
from typing import List


class TemporalEmbedder(ABC):
    """Abstract adapter for a temporal embedding strategy.

    Attributes:
        name: Human/config-facing embedder identifier, e.g. "voyage-context-4".
            This is the value stored in ``TemporalConfig.embedders`` /
            ``TemporalConfig.active_embedder`` and used to look up the adapter
            in the registry.
        model_slug: Filesystem/collection-safe slug derived from ``name``
            (e.g. "voyage_context_4"), used to build the quarterly shard
            collection name and the v2 structure marker's "model" field.
        dimensions: Vector dimensionality produced by this embedder.
        overlap_percentage: Fractional overlap (0.0-1.0) applied when chunking
            the aggregated document for this embedder. Contextual embedders use
            0% overlap; standard (non-contextual) embedders may use 15%.
    """

    name: str
    model_slug: str
    dimensions: int
    overlap_percentage: float

    @abstractmethod
    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        """Embed the ordered text chunks of ONE commit's aggregated document.

        Args:
            chunks: Ordered list of chunk texts produced by chunking the
                commit's aggregated document (message-once head + diffs).

        Returns:
            One embedding vector per input chunk, in the same order.
        """
        raise NotImplementedError

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """Embed a query string for temporal recall (embedding_purpose='query')."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """Return True iff this embedder can actually embed right now.

        Story #1291 (AC4): checked by the indexer BEFORE scheduling work for
        an embedder. A NON-ACTIVE embedder returning False is skipped with a
        WARNING (the other configured embedders still index normally); the
        ACTIVE embedder returning False FAILS the job (never reports green).

        Concrete adapters override this to do a cheap, non-network
        credential-presence check (e.g. "is an API key configured?") --
        never a live network probe. Default True preserves prior adapters
        (e.g. ContextualTemporalEmbedder) that assume availability once
        construction has already succeeded.
        """
        return True
