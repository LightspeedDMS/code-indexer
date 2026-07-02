"""MemoryCandidateRetriever — Story #883 Component 2.

Queries the HNSW index for `cidx-meta/memories` using a pre-computed Voyage
embedding vector, converting raw FilesystemVectorStore results into typed
MemoryCandidate objects.

Design notes:
- Accepts a pre-computed query vector to avoid a duplicate Voyage API call
  (the vector is shared with the parallel code-search branch in Stage 2).
- Uses a lightweight _PrecomputedEmbeddingProvider adaptor so the existing
  FilesystemVectorStore.search() path is exercised without modification.
- Missing index (no collection on disk): returns [] and logs INFO exactly once
  per process via a class-level flag guarded by a threading.Lock (Scenario 13).
- retrieve() is called from a ThreadPoolExecutor worker thread; all paths are
  thread-safe.
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

logger = logging.getLogger(__name__)

_MEMORY_COLLECTION = "memories"


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class MemoryCandidate:
    """A single memory candidate returned by HNSW retrieval.

    Attributes:
        memory_id: The HNSW point id (matches the memory file's base name).
        hnsw_score: Cosine similarity score from the HNSW index (0-1).
        memory_path: Disk path to the memory file from the HNSW payload's 'path'
            key.  Empty string when the payload does not contain a path (e.g.
            older indexes that were built before the path was indexed).
    """

    memory_id: str
    hnsw_score: float
    memory_path: str = ""
    title: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# Internal embedding adaptor
# ---------------------------------------------------------------------------


class _PrecomputedEmbeddingProvider:
    """Adaptor that satisfies FilesystemVectorStore's embedding_provider
    protocol by returning a pre-computed vector instead of calling an API.

    FilesystemVectorStore.search() requires an embedding_provider with a
    get_embedding(text, embedding_purpose) method.  This adaptor ignores the
    text argument and returns the pre-computed vector directly, avoiding a
    second Voyage API call.
    """

    def __init__(self, vector: List[float]) -> None:
        self._vector = vector

    def get_embedding(
        self,
        text: str,
        embedding_purpose: str = "query",  # noqa: ARG002
    ) -> List[float]:
        return self._vector


# ---------------------------------------------------------------------------
# Retriever service
# ---------------------------------------------------------------------------


class MemoryCandidateRetriever:
    """Retrieves memory candidates from the HNSW index using a pre-computed vector.

    Class attributes:
        _missing_logged: Once-per-process guard.  Set to True after the first
            INFO log about a missing memory index so subsequent calls are silent.
        _missing_lock: Protects _missing_logged against concurrent mutation from
            multiple ThreadPoolExecutor workers.
    """

    _missing_logged: bool = False
    _missing_lock: threading.Lock = threading.Lock()

    def __init__(self, store_base_path: str) -> None:
        """Initialise the retriever.

        Args:
            store_base_path: Base path for the FilesystemVectorStore that
                contains the `memories` collection (typically the cidx-meta
                golden-repo directory).
        """
        self._store = FilesystemVectorStore(base_path=Path(store_base_path))

    def retrieve(
        self,
        query_vector: Optional[List[float]],
        user_id: Optional[str],
        k: int,
    ) -> List[MemoryCandidate]:
        """Query the memories HNSW index with a pre-computed embedding vector.

        Args:
            query_vector: Pre-computed Voyage embedding (shared with code search).
            user_id: Caller's user identifier (validated; reserved for future
                per-user partitioning).
            k: Maximum number of candidates to retrieve (must be a positive int,
               bool values are rejected).

        Returns:
            List of MemoryCandidate objects ordered by descending HNSW score.
            Returns [] when the collection / HNSW index does not exist.

        Raises:
            ValueError: If query_vector is None or empty, user_id is None or
                empty string, or k is not a positive integer (bool rejected).
        """
        if not query_vector:
            raise ValueError("query_vector must be a non-empty list of floats")
        if not user_id:
            raise ValueError("user_id must be a non-empty string")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k must be a positive integer")

        provider = _PrecomputedEmbeddingProvider(query_vector)
        raw_results = self._store.search(
            query="memory-retrieval",  # text ignored — provider returns precomputed vector
            embedding_provider=provider,
            collection_name=_MEMORY_COLLECTION,
            limit=k,
        )

        if not raw_results:
            with MemoryCandidateRetriever._missing_lock:
                if not MemoryCandidateRetriever._missing_logged:
                    logger.info(
                        "Memory HNSW index not found or empty for collection '%s'. "
                        "Memory retrieval returns no candidates. "
                        "(This message is logged only once per process.)",
                        _MEMORY_COLLECTION,
                    )
                    MemoryCandidateRetriever._missing_logged = True
            return []

        return [
            MemoryCandidate(
                memory_id=r["id"],
                hnsw_score=float(r["score"]),
                memory_path=r["payload"].get("path", ""),
            )
            for r in raw_results
        ]
