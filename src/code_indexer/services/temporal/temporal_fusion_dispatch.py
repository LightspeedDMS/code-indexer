"""Temporal fusion dispatch — shared query execution across all paths (Story #634).

Routes temporal queries through parallel multi-provider execution with
RRF fusion, timeout handling, and single-provider fallback.
Used by CLI, server (semantic_query_manager), multi_search_service, and daemon.
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .temporal_fusion import (
    TEMPORAL_OVERFETCH_MULTIPLIER,
    fuse_rrf_multi,
    make_temporal_dedup_key,
)
from .temporal_collection_naming import get_temporal_collections

logger = logging.getLogger(__name__)

TEMPORAL_QUERY_TIMEOUT_SECONDS = 15


def execute_temporal_query_with_fusion(
    config: Any,
    index_path: Path,
    vector_store: Any,
    query_text: str,
    limit: int,
    time_range: Optional[Tuple[str, str]] = None,
    file_path_filter: Optional[str] = None,
    show_evolution: bool = False,
    provider_filter: Optional[str] = None,
) -> Any:
    """Execute temporal query with multi-provider fusion.

    Args:
        config: CIDX Config object
        index_path: Path to .code-indexer/index/
        vector_store: FilesystemVectorStore instance
        query_text: Search query text
        limit: Max results to return
        time_range: Optional time range filter as (start_date, end_date) tuple
        file_path_filter: Optional file path filter
        show_evolution: Whether to show file evolution timeline
        provider_filter: Optional specific provider to query (bypasses fusion)

    Returns:
        TemporalSearchResults with fused results
    """
    from .temporal_search_service import TemporalSearchResults

    collections = _discover_queryable_collections(config, index_path, provider_filter)

    if not collections:
        logger.warning("No temporal indexes available for query")
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning=(
                "No temporal indexes available. "
                "Run cidx index --index-commits to create temporal indexes."
            ),
        )

    if len(collections) == 1:
        coll_name, _ = collections[0]
        return _query_single_provider(
            config,
            vector_store,
            coll_name,
            query_text,
            limit,
            time_range,
            file_path_filter,
        )

    return _query_multi_provider_fusion(
        config,
        vector_store,
        collections,
        query_text,
        limit,
        time_range,
        file_path_filter,
    )


def _discover_queryable_collections(
    config: Any,
    index_path: Path,
    provider_filter: Optional[str] = None,
) -> List[Tuple[str, Any]]:
    """Find temporal collections available for querying.

    Returns list of (collection_name, path) tuples.
    """
    index_path = Path(index_path)
    raw = get_temporal_collections(config, index_path)

    result: List[Tuple[str, Any]] = [(name, path) for name, path in raw]

    if provider_filter:
        result = [(name, path) for name, path in result if provider_filter in name]

    return result


def _query_single_provider(
    config: Any,
    vector_store: Any,
    coll_name: str,
    query_text: str,
    limit: int,
    time_range: Optional[Tuple[str, str]],
    file_path_filter: Optional[str],
) -> Any:
    """Query a single temporal provider directly (no fusion)."""
    from .temporal_search_service import TemporalSearchService
    from .temporal_search_service import ALL_TIME_RANGE

    embedding_provider = _create_embedding_provider(config)

    service = TemporalSearchService(
        config_manager=_make_config_manager(config),
        project_root=vector_store.project_root,
        vector_store_client=vector_store,
        embedding_provider=embedding_provider,
        collection_name=coll_name,
    )

    resolved_range = time_range if time_range is not None else ALL_TIME_RANGE

    path_filter = [file_path_filter] if file_path_filter else None

    results = service.query_temporal(
        query=query_text,
        time_range=resolved_range,
        limit=limit,
        path_filter=path_filter,
    )

    for r in results.results:
        r.source_provider = coll_name
        r.contributing_providers = [coll_name]
        r.fusion_score = r.score

    return results


def _query_multi_provider_fusion(
    config: Any,
    vector_store: Any,
    collections: List[Tuple[str, Any]],
    query_text: str,
    limit: int,
    time_range: Optional[Tuple[str, str]],
    file_path_filter: Optional[str],
) -> Any:
    """Query multiple providers in parallel and fuse results."""
    from .temporal_search_service import TemporalSearchResults, ALL_TIME_RANGE

    overfetch_limit = limit * TEMPORAL_OVERFETCH_MULTIPLIER
    results_by_provider: Dict[str, list] = {}
    warnings: List[str] = []

    resolved_range = time_range if time_range is not None else ALL_TIME_RANGE
    path_filter = [file_path_filter] if file_path_filter else None

    def query_provider(coll_name: str) -> Any:
        provider = _create_embedding_provider(config)
        service = TemporalSearchService(
            config_manager=_make_config_manager(config),
            project_root=vector_store.project_root,
            vector_store_client=vector_store,
            embedding_provider=provider,
            collection_name=coll_name,
        )
        return service.query_temporal(
            query=query_text,
            time_range=resolved_range,
            limit=overfetch_limit,
            path_filter=path_filter,
        )

    from .temporal_search_service import TemporalSearchService

    with ThreadPoolExecutor(max_workers=len(collections)) as executor:
        future_to_coll: Dict[Future, str] = {}
        for coll_name, _ in collections:
            future = executor.submit(query_provider, coll_name)
            future_to_coll[future] = coll_name

        for future in as_completed(
            future_to_coll, timeout=TEMPORAL_QUERY_TIMEOUT_SECONDS
        ):
            coll_name = future_to_coll[future]
            try:
                result = future.result()
                if result.results:
                    results_by_provider[coll_name] = result.results
            except Exception as e:
                logger.warning("Temporal query failed for %s: %s", coll_name, e)
                warnings.append(f"Provider {coll_name} failed: {e}")

    for future, coll_name in future_to_coll.items():
        if not future.done():
            future.cancel()
            warnings.append(
                f"Provider {coll_name} timed out after {TEMPORAL_QUERY_TIMEOUT_SECONDS}s"
            )

    if not results_by_provider:
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning="; ".join(warnings) if warnings else "All providers failed",
        )

    fused = fuse_rrf_multi(
        results_by_provider=results_by_provider,
        dedup_key=make_temporal_dedup_key,
        limit=limit,
    )

    warning_str = "; ".join(warnings) if warnings else None
    return TemporalSearchResults(
        results=fused,
        query=query_text,
        filter_type="time_range" if time_range else "none",
        filter_value=time_range,
        total_found=len(fused),
        warning=warning_str,
    )


def _create_embedding_provider(config: Any) -> Any:
    """Create embedding provider from config."""
    from ..embedding_factory import EmbeddingProviderFactory

    provider_name = getattr(config, "embedding_provider", "voyage-ai")
    return EmbeddingProviderFactory.create(config, provider_name=provider_name)


def _make_config_manager(config: Any) -> Any:
    """Create a minimal config manager wrapper for TemporalSearchService."""

    class _ConfigManagerShim:
        def get_config(self) -> Any:
            return config

    return _ConfigManagerShim()
