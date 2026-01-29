"""
Multi-Index Query Service.

Provides parallel multi-index query capability that:
1. Detects if voyage-multimodal-3 collection exists in .code-indexer/index/
2. Queries voyage-code-3 and voyage-multimodal-3 collections concurrently (if both exist)
3. Merges results from both collections with order-independent deduplication
4. Handles timeouts and partial results gracefully
5. Deduplicates by (file_path, chunk_offset), keeping highest score
6. Sorts by score descending
7. Applies limit to final merged results

This enables querying markdown files with embedded images via multimodal
embeddings while maintaining backward compatibility when multimodal collection
doesn't exist.

Architecture:
- voyage-code-3 collection: .code-indexer/index/voyage-code-3/
- voyage-multimodal-3 collection: .code-indexer/index/voyage-multimodal-3/ (same level)

CRITICAL: Query vectorization must use the SAME model as indexing:
- Code collection queries: Use voyage-code-3 embedding provider
- Multimodal collection queries: Use voyage-multimodal-3 embedding provider
Using mismatched models produces incorrect similarity scores.
"""

import logging
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from ..config import VOYAGE_MULTIMODAL_MODEL, VoyageAIConfig

logger = logging.getLogger(__name__)

# Query timeout per index (seconds)
QUERY_TIMEOUT = 30


class MultiIndexQueryService:
    """Service for querying multiple indexes sequentially and merging results."""

    def __init__(
        self,
        project_root: Path,
        vector_store,
        embedding_provider
    ):
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

        Returns:
            VoyageMultimodalClient instance for multimodal query embedding
        """
        if self._multimodal_provider is None:
            from .voyage_multimodal import VoyageMultimodalClient
            # Create config for multimodal model
            multimodal_config = VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL)
            self._multimodal_provider = VoyageMultimodalClient(multimodal_config)
            logger.debug(f"Initialized multimodal embedding provider: {VOYAGE_MULTIMODAL_MODEL}")
        return self._multimodal_provider

    def has_multimodal_index(self) -> bool:
        """
        Check if multimodal collection exists.

        Checks for voyage-multimodal-3 collection in .code-indexer/index/
        (NEW architecture) or multimodal_index/ subdirectory (LEGACY fallback).

        Returns:
            True if voyage-multimodal-3 collection OR legacy multimodal_index/ exists
        """
        # NEW: Check for voyage-multimodal-3 collection (primary approach)
        multimodal_collection = self.project_root / ".code-indexer" / "index" / VOYAGE_MULTIMODAL_MODEL
        if multimodal_collection.exists() and multimodal_collection.is_dir():
            return True

        # LEGACY: Check for old multimodal_index/ subdirectory (backward compatibility)
        legacy_multimodal_path = self.project_root / ".code-indexer" / "multimodal_index"
        return legacy_multimodal_path.exists() and legacy_multimodal_path.is_dir()

    def _query_code_index(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs
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
            **kwargs
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
        **kwargs
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Query voyage-multimodal-3 collection.

        CRITICAL: Uses VoyageMultimodalClient (voyage-multimodal-3) for query embedding
        to match the model used during indexing. Using voyage-code-3 would produce
        incorrect similarity scores due to different vector spaces.

        Args:
            query_text: Query string
            limit: Maximum number of results
            collection_name: Collection name (used for legacy path only; new path uses VOYAGE_MULTIMODAL_MODEL)
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            Tuple of (results, timing_dict) from voyage-multimodal-3 collection
        """
        logger.debug(f"Querying {VOYAGE_MULTIMODAL_MODEL} collection for: {query_text}")

        # Get multimodal embedding provider (voyage-multimodal-3)
        multimodal_provider = self._get_multimodal_provider()

        # Measure wall-clock time for this index query
        query_start = time.time()

        # Check if legacy subdirectory exists, use it for backward compatibility
        legacy_multimodal_path = self.project_root / ".code-indexer" / "multimodal_index"
        if legacy_multimodal_path.exists() and legacy_multimodal_path.is_dir():
            # LEGACY: Use subdirectory approach
            logger.debug("Using legacy multimodal_index subdirectory")
            results, timing = self.vector_store.search(
                query=query_text,
                embedding_provider=multimodal_provider,
                collection_name=collection_name,
                limit=limit * 2,
                filter_conditions=filter_conditions,
                subdirectory="multimodal_index",
                return_timing=True,
                **kwargs
            )
        else:
            # NEW: Query voyage-multimodal-3 collection directly
            logger.debug(f"Querying {VOYAGE_MULTIMODAL_MODEL} collection directly")
            results, timing = self.vector_store.search(
                query=query_text,
                embedding_provider=multimodal_provider,
                collection_name=VOYAGE_MULTIMODAL_MODEL,  # Use multimodal collection name
                limit=limit * 2,
                filter_conditions=filter_conditions,
                subdirectory=None,  # No subdirectory - same level as voyage-code-3
                return_timing=True,
                **kwargs
            )
        # Add actual wall-clock elapsed time (this is what we display)
        timing["elapsed_ms"] = (time.time() - query_start) * 1000
        return results, timing

    def _merge_results(
        self,
        code_results: List[Dict[str, Any]],
        multimodal_results: List[Dict[str, Any]],
        limit: int
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
            deduplicated,
            key=lambda x: x.get("score", 0.0),
            reverse=True
        )

        # Apply limit to final results
        return sorted_results[:limit]

    def query(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs
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
        has_multimodal = self.has_multimodal_index()

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
                **kwargs
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
                    **kwargs
                )
                futures[multimodal_future] = "multimodal"

            # Collect results and timing as they complete
            results = {"code": [], "multimodal": []}

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
                    logger.warning(f"{index_type}_index query timed out after {QUERY_TIMEOUT}s")
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
            results["code"],
            results["multimodal"],
            limit
        )
        merge_end = time.time()

        if has_multimodal:
            timing_dict["merge_deduplicate_ms"] = (merge_end - merge_start) * 1000

        return merged_results, timing_dict

    def _deduplicate_results(
        self,
        results: List[Dict[str, Any]]
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
