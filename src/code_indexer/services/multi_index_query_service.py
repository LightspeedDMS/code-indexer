"""
Multi-Index Query Service.

Provides parallel multi-index query capability that:
1. Detects if multimodal_index exists in .code-indexer/multimodal_index/
2. Queries code_index and multimodal_index concurrently (if both exist)
3. Merges results from both indexes with order-independent deduplication
4. Handles timeouts and partial results gracefully
5. Deduplicates by (file_path, chunk_offset), keeping highest score
6. Sorts by score descending
7. Applies limit to final merged results

This enables querying markdown files with embedded images via multimodal
embeddings while maintaining backward compatibility when multimodal_index
doesn't exist.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

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
            embedding_provider: Embedding provider instance
        """
        self.project_root = project_root
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider

    def has_multimodal_index(self) -> bool:
        """
        Check if multimodal_index exists.

        Returns:
            True if .code-indexer/multimodal_index/ directory exists
        """
        multimodal_path = self.project_root / ".code-indexer" / "multimodal_index"
        return multimodal_path.exists() and multimodal_path.is_dir()

    def _query_code_index(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Query code_index.

        Args:
            query_text: Query string
            limit: Maximum number of results
            collection_name: Collection name
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            List of results from code_index
        """
        logger.debug(f"Querying code_index for: {query_text}")
        results, _ = self.vector_store.search(
            query=query_text,
            embedding_provider=self.embedding_provider,
            collection_name=collection_name,
            limit=limit * 2,  # Get more results for better merging
            filter_conditions=filter_conditions,
            subdirectory=None,  # Default code_index location
            return_timing=True,
            **kwargs
        )
        return results

    def _query_multimodal_index(
        self,
        query_text: str,
        limit: int,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Query multimodal_index.

        Args:
            query_text: Query string
            limit: Maximum number of results
            collection_name: Collection name
            filter_conditions: Optional filter conditions
            **kwargs: Additional query parameters

        Returns:
            List of results from multimodal_index
        """
        logger.debug(f"Querying multimodal_index for: {query_text}")
        results, _ = self.vector_store.search(
            query=query_text,
            embedding_provider=self.embedding_provider,
            collection_name=collection_name,
            limit=limit * 2,
            filter_conditions=filter_conditions,
            subdirectory="multimodal_index",  # Multimodal index subdirectory
            return_timing=True,
            **kwargs
        )
        return results

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
    ) -> List[Dict[str, Any]]:
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
            Merged and deduplicated list of results sorted by score descending
        """
        # Use ThreadPoolExecutor for parallel queries
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}

            # Submit code_index query (always)
            futures[executor.submit(
                self._query_code_index,
                query_text,
                limit,
                collection_name,
                filter_conditions,
                **kwargs
            )] = "code"

            # Submit multimodal_index query (if exists)
            if self.has_multimodal_index():
                futures[executor.submit(
                    self._query_multimodal_index,
                    query_text,
                    limit,
                    collection_name,
                    filter_conditions,
                    **kwargs
                )] = "multimodal"

            # Collect results as they complete
            results = {"code": [], "multimodal": []}
            for future in as_completed(futures, timeout=QUERY_TIMEOUT):
                index_type = futures[future]
                try:
                    results[index_type] = future.result()
                except TimeoutError:
                    logger.warning(f"{index_type}_index query timed out after {QUERY_TIMEOUT}s")
                except Exception as e:
                    logger.warning(f"{index_type}_index query failed: {e}")

            # Merge results from both indexes
            return self._merge_results(
                results["code"],
                results["multimodal"],
                limit
            )

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
