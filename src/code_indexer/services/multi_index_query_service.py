"""
Multi-Index Query Service.

Provides parallel multi-index query capability that:
1. Detects if a multimodal collection exists in .code-indexer/index/ (VoyageAI or Cohere)
2. Queries code and multimodal collections concurrently (if both exist)
3. Merges results from both collections with order-independent deduplication
4. Handles timeouts and partial results gracefully
5. Deduplicates by (file_path, chunk_offset), keeping highest score
6. Sorts by score descending
7. Applies limit to final merged results

This enables querying markdown files with embedded images via multimodal
embeddings while maintaining backward compatibility when multimodal collection
doesn't exist.

Architecture:
- Code collection: .code-indexer/index/{code-model}/ (e.g. voyage-code-3)
- Multimodal collection: .code-indexer/index/{multimodal-model}/ (same level)
  Supported multimodal providers: VoyageAI (voyage-multimodal-3), Cohere (embed-v4.0-multimodal)

CRITICAL: Query vectorization must use the SAME model as indexing.
Using mismatched models produces incorrect similarity scores.
"""

import logging
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from ..config import VOYAGE_MULTIMODAL_MODEL, COHERE_MULTIMODAL_MODEL, VoyageAIConfig

logger = logging.getLogger(__name__)

# All known multimodal model collection names (for provider-agnostic detection)
MULTIMODAL_MODELS = [VOYAGE_MULTIMODAL_MODEL, COHERE_MULTIMODAL_MODEL]

# Query timeout per index (seconds)
QUERY_TIMEOUT = 30


class MultiIndexQueryService:
    """Service for querying multiple indexes sequentially and merging results."""

    def __init__(self, project_root: Path, vector_store, embedding_provider):
        """
        Initialize MultiIndexQueryService.

        Args:
            project_root: Project root directory path
            vector_store: Vector store client instance
            embedding_provider: Embedding provider instance (voyage-code-3)
        """
        self.project_root = project_root
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        # Lazy-initialized multimodal embedding provider
        self._multimodal_provider = None

    def _get_multimodal_provider(self):
        """Get or create the multimodal embedding provider (lazy initialization).

        Detects provider by checking which multimodal collection exists on disk.
        Cohere takes precedence when its collection is found; falls back to VoyageAI.
        """
        if self._multimodal_provider is not None:
            return self._multimodal_provider

        # Check for Cohere multimodal collection first
        cohere_path = (
            self.project_root / ".code-indexer" / "index" / COHERE_MULTIMODAL_MODEL
        )
        if cohere_path.exists() and cohere_path.is_dir():
            from ..config import CohereConfig
            from .cohere_multimodal import CohereMultimodalClient

            cohere_config = CohereConfig(model="embed-v4.0")
            self._multimodal_provider = CohereMultimodalClient(cohere_config)  # type: ignore[assignment]
            logger.debug(
                "Initialized Cohere multimodal embedding provider: %s",
                COHERE_MULTIMODAL_MODEL,
            )
            return self._multimodal_provider

        # Default to VoyageAI multimodal
        from .voyage_multimodal import VoyageMultimodalClient

        multimodal_config = VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL)
        self._multimodal_provider = VoyageMultimodalClient(multimodal_config)  # type: ignore[assignment]
        logger.debug(
            "Initialized VoyageAI multimodal embedding provider: %s",
            VOYAGE_MULTIMODAL_MODEL,
        )
        return self._multimodal_provider

    def will_query_multimodal(self) -> bool:
        """Check if multimodal index will actually be queried.

        Returns True when a multimodal collection (VoyageAI or Cohere) exists.
        """
        return self.has_multimodal_index()

    def has_multimodal_index(self) -> bool:
        """Check if any multimodal collection exists.

        Checks for known multimodal collections in .code-indexer/index/
        and legacy multimodal_index/ subdirectory.
        """
        for model_name in MULTIMODAL_MODELS:
            collection_path = self.project_root / ".code-indexer" / "index" / model_name
            if collection_path.exists() and collection_path.is_dir():
                return True

        # LEGACY: Check for old multimodal_index/ subdirectory
        legacy_multimodal_path = (
            self.project_root / ".code-indexer" / "multimodal_index"
        )
        return legacy_multimodal_path.exists() and legacy_multimodal_path.is_dir()

    def _query_code_index(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Query code_index.

        Args:
            query_text: Query string
            limit: Maximum number of results
            collection_name: Collection name
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            Tuple of (results, timing_dict) from code_index
        """
        logger.debug(f"Querying code_index for: {query_text}")
        # Measure wall-clock time for this index query
        query_start = time.time()
        results, timing = self.vector_store.search(
            query=query_text,
            embedding_provider=self.embedding_provider,
            collection_name=collection_name,
            limit=limit * 2,  # Get more results for better merging
            filter_conditions=filter_conditions,
            subdirectory=None,  # Default code_index location
            return_timing=True,
            **kwargs,
        )
        # Add actual wall-clock elapsed time (this is what we display)
        timing["elapsed_ms"] = (time.time() - query_start) * 1000
        return results, timing

    def _query_multimodal_index(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Query multimodal collection (VoyageAI or Cohere).

        CRITICAL: Uses the matching multimodal provider for query embedding
        to match the model used during indexing. Mismatched models produce
        incorrect similarity scores due to different vector spaces.

        Args:
            query_text: Query string
            limit: Maximum number of results
            collection_name: Collection name (used for legacy path only)
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            Tuple of (results, timing_dict) from multimodal collection
        """
        logger.debug("Querying multimodal collection for: %s", query_text)

        # Get multimodal embedding provider
        multimodal_provider = self._get_multimodal_provider()

        # Find which multimodal collection exists
        actual_collection = None
        for model_name in MULTIMODAL_MODELS:
            coll_path = self.project_root / ".code-indexer" / "index" / model_name
            if coll_path.exists() and coll_path.is_dir():
                actual_collection = model_name
                break

        query_start = time.time()

        # Check if legacy subdirectory exists, use it for backward compatibility
        legacy_multimodal_path = (
            self.project_root / ".code-indexer" / "multimodal_index"
        )
        if (
            actual_collection is None
            and legacy_multimodal_path.exists()
            and legacy_multimodal_path.is_dir()
        ):
            logger.debug("Using legacy multimodal_index subdirectory")
            results, timing = self.vector_store.search(
                query=query_text,
                embedding_provider=multimodal_provider,
                collection_name=collection_name,
                limit=limit * 2,
                filter_conditions=filter_conditions,
                subdirectory="multimodal_index",
                return_timing=True,
                **kwargs,
            )
        elif actual_collection is not None:
            logger.debug("Querying %s collection directly", actual_collection)
            results, timing = self.vector_store.search(
                query=query_text,
                embedding_provider=multimodal_provider,
                collection_name=actual_collection,
                limit=limit * 2,
                filter_conditions=filter_conditions,
                subdirectory=None,
                return_timing=True,
                **kwargs,
            )
        else:
            logger.debug("No multimodal collection found, returning empty results")
            return [], {"elapsed_ms": 0}

        timing["elapsed_ms"] = (time.time() - query_start) * 1000
        return results, timing

    def _merge_results(
        self,
        code_results: List[Dict[str, Any]],
        multimodal_results: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """
        Merge and deduplicate results from both indexes.

        Merges results in an order-independent way:
        1. Combines results from both indexes
        2. Deduplicates by (file_path, chunk_offset), keeping highest score
        3. Sorts by score descending
        4. Applies limit

        Args:
            code_results: Results from code_index
            multimodal_results: Results from multimodal_index
            limit: Maximum number of results to return

        Returns:
            Merged, deduplicated, and sorted list of results
        """
        # Combine all results
        all_results = code_results + multimodal_results

        # Deduplicate by (file_path, chunk_offset), keeping highest score
        deduplicated = self._deduplicate_results(all_results)

        # Sort by score descending
        sorted_results = sorted(
            deduplicated, key=lambda x: x.get("score", 0.0), reverse=True
        )

        # Apply limit to final results
        return sorted_results[:limit]

    def query(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Execute parallel multi-index query.

        Queries code_index and multimodal_index (if exists) concurrently.
        Merges results in an order-independent way, deduplicates by
        (file_path, chunk_offset), sorts by score descending, and applies limit.

        Handles timeouts gracefully by returning partial results from
        successful queries.

        Args:
            query_text: Query string
            limit: Maximum number of results to return
            collection_name: Collection name (typically "code_index")
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            Tuple of (results, timing_dict) where:
            - results: Merged and deduplicated list of results sorted by score descending
            - timing_dict: Dictionary with timing information and flags
        """
        has_multimodal = self.will_query_multimodal()

        # Initialize timing dict
        timing_dict: Dict[str, Any] = {
            "has_multimodal": has_multimodal,
            "code_timed_out": False,
            "code_index_ms": 0,
        }

        if has_multimodal:
            timing_dict["multimodal_timed_out"] = False
            timing_dict["multimodal_index_ms"] = 0

        # Track parallel execution start time
        parallel_start = time.time()

        # Use ThreadPoolExecutor for parallel queries
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}

            # Submit code_index query (always)
            code_future = executor.submit(
                self._query_code_index,
                query_text,
                limit,
                collection_name,
                filter_conditions,
                **kwargs,
            )
            futures[code_future] = "code"

            # Submit multimodal_index query (if exists)
            if has_multimodal:
                multimodal_future = executor.submit(
                    self._query_multimodal_index,
                    query_text,
                    limit,
                    collection_name,
                    filter_conditions,
                    **kwargs,
                )
                futures[multimodal_future] = "multimodal"

            # Collect results and timing as they complete
            results: Dict[str, List[Any]] = {"code": [], "multimodal": []}

            for future in as_completed(futures, timeout=QUERY_TIMEOUT):
                index_type = futures[future]
                try:
                    result_list, result_timing = future.result()
                    results[index_type] = result_list

                    # Use the wall-clock elapsed_ms from the query method
                    # This is the actual time the query took, not sum of internal breakdowns
                    elapsed_time = result_timing.get("elapsed_ms", 0)

                    if index_type == "code":
                        timing_dict["code_index_ms"] = elapsed_time
                    else:
                        timing_dict["multimodal_index_ms"] = elapsed_time

                except TimeoutError:
                    logger.warning(
                        f"{index_type}_index query timed out after {QUERY_TIMEOUT}s"
                    )
                    if index_type == "code":
                        timing_dict["code_timed_out"] = True
                    else:
                        timing_dict["multimodal_timed_out"] = True
                except Exception as e:
                    logger.warning(f"{index_type}_index query failed: {e}")
                    # Treat exceptions as timeouts for timing purposes
                    if index_type == "code":
                        timing_dict["code_timed_out"] = True
                    else:
                        timing_dict["multimodal_timed_out"] = True

        # Calculate parallel wall-clock time (max of both, not sum)
        parallel_end = time.time()
        parallel_ms = (parallel_end - parallel_start) * 1000

        if has_multimodal:
            timing_dict["parallel_multi_index_ms"] = parallel_ms

        # Merge results from both indexes with timing
        merge_start = time.time()
        merged_results = self._merge_results(
            results["code"], results["multimodal"], limit
        )
        merge_end = time.time()

        if has_multimodal:
            timing_dict["merge_deduplicate_ms"] = (merge_end - merge_start) * 1000

        return merged_results, timing_dict

    def _deduplicate_results(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Deduplicate results by (file_path, chunk_offset), keeping highest score.

        Args:
            results: List of search results

        Returns:
            Deduplicated list of results
        """
        # Use dict to track best result for each (path, offset) pair
        best_results: Dict[tuple, Dict[str, Any]] = {}

        for result in results:
            payload = result.get("payload", {})
            path = payload.get("path", "")
            chunk_offset = payload.get("chunk_offset", 0)

            key = (path, chunk_offset)
            current_score = result.get("score", 0.0)

            # Keep result with highest score for this key
            if key not in best_results:
                best_results[key] = result
            else:
                existing_score = best_results[key].get("score", 0.0)
                if current_score > existing_score:
                    best_results[key] = result

        return list(best_results.values())
