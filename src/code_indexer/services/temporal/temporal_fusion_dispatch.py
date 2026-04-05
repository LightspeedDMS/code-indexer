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
from .temporal_collection_naming import (
    get_temporal_collections,
    TEMPORAL_COLLECTION_PREFIX,
    sanitize_model_name,
    get_model_name_for_provider,
)
from .temporal_health import (
    filter_healthy_temporal_providers,
    record_temporal_success,
    record_temporal_failure,
)

logger = logging.getLogger(__name__)

TEMPORAL_QUERY_TIMEOUT_SECONDS = 15

# Latency placeholder when per-future timing is not available
_UNKNOWN_LATENCY_MS = 0.0


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
    # Server params (Story #640, audit fix B2)
    at_commit: Optional[str] = None,
    include_removed: Optional[bool] = None,
    language: Optional[str] = None,
    exclude_language: Optional[str] = None,
    evolution_limit: Optional[int] = None,
    exclude_path: Optional[str] = None,
    diff_types: Optional[List[str]] = None,
    author: Optional[str] = None,
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
        at_commit: Query at specific commit (server param, resolved by caller)
        include_removed: Include removed files (server param, converted to diff_types by caller)
        language: Filter by language (server param)
        exclude_language: Exclude language (server param)
        evolution_limit: Limit evolution entries (server param, applied by caller)
        exclude_path: Exclude path pattern (server param)

    Returns:
        TemporalSearchResults with fused results
    """
    from .temporal_search_service import TemporalSearchResults

    # Auto-migrate legacy collection if present (Story #629, wired by audit fix F2)
    from .temporal_migration import migrate_legacy_temporal_collection

    migrate_legacy_temporal_collection(index_path, config)

    collections = _discover_queryable_collections(config, index_path, provider_filter)

    # Health-gate: filter to healthy providers only
    collections, _skipped = filter_healthy_temporal_providers(collections)

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
            language=language,
            exclude_language=exclude_language,
            exclude_path=exclude_path,
            diff_types=diff_types,
            author=author,
        )

    return _query_multi_provider_fusion(
        config,
        vector_store,
        collections,
        query_text,
        limit,
        time_range,
        file_path_filter,
        language=language,
        exclude_language=exclude_language,
        exclude_path=exclude_path,
        diff_types=diff_types,
        author=author,
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
    language: Optional[str] = None,
    exclude_language: Optional[str] = None,
    exclude_path: Optional[str] = None,
    diff_types: Optional[List[str]] = None,
    author: Optional[str] = None,
) -> Any:
    """Query a single temporal provider directly (no fusion)."""
    import time as _time
    from .temporal_search_service import TemporalSearchService
    from .temporal_search_service import ALL_TIME_RANGE

    embedding_provider = _create_embedding_provider_for_collection(config, coll_name)

    service = TemporalSearchService(
        config_manager=_make_config_manager(config),
        project_root=vector_store.project_root,
        vector_store_client=vector_store,
        embedding_provider=embedding_provider,
        collection_name=coll_name,
    )

    resolved_range = time_range if time_range is not None else ALL_TIME_RANGE
    path_filter = [file_path_filter] if file_path_filter else None

    _t0 = _time.time()
    try:
        results = service.query_temporal(
            query=query_text,
            time_range=resolved_range,
            limit=limit,
            path_filter=path_filter,
            language=[language] if language else None,
            exclude_language=[exclude_language] if exclude_language else None,
            exclude_path=[exclude_path] if exclude_path else None,
            diff_types=diff_types,
            author=author,
        )
        record_temporal_success(coll_name, (_time.time() - _t0) * 1000)
    except Exception:
        record_temporal_failure(coll_name, (_time.time() - _t0) * 1000)
        raise

    from .temporal_collection_naming import collection_display_name

    _display = collection_display_name(coll_name)
    for r in results.results:
        r.source_provider = _display
        r.contributing_providers = [_display]
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
    language: Optional[str] = None,
    exclude_language: Optional[str] = None,
    exclude_path: Optional[str] = None,
    diff_types: Optional[List[str]] = None,
    author: Optional[str] = None,
) -> Any:
    """Query multiple providers in parallel and fuse results."""
    from .temporal_search_service import TemporalSearchResults, ALL_TIME_RANGE

    overfetch_limit = limit * TEMPORAL_OVERFETCH_MULTIPLIER
    results_by_provider: Dict[str, list] = {}
    warnings: List[str] = []

    resolved_range = time_range if time_range is not None else ALL_TIME_RANGE
    path_filter = [file_path_filter] if file_path_filter else None

    def query_provider(coll_name: str) -> Any:
        provider = _create_embedding_provider_for_collection(config, coll_name)
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
            language=[language] if language else None,
            exclude_language=[exclude_language] if exclude_language else None,
            exclude_path=[exclude_path] if exclude_path else None,
            diff_types=diff_types,
            author=author,
        )

    from .temporal_search_service import TemporalSearchService
    from .temporal_collection_naming import collection_display_name

    with ThreadPoolExecutor(max_workers=len(collections)) as executor:
        future_to_coll: Dict[Future, str] = {}
        for coll_name, _ in collections:
            future = executor.submit(query_provider, coll_name)
            future_to_coll[future] = coll_name

        for future in as_completed(
            future_to_coll, timeout=TEMPORAL_QUERY_TIMEOUT_SECONDS
        ):
            coll_name = future_to_coll[future]
            _display = collection_display_name(coll_name)
            try:
                result = future.result()
                if result.results:
                    results_by_provider[_display] = result.results
                record_temporal_success(coll_name, _UNKNOWN_LATENCY_MS)
            except Exception as e:
                record_temporal_failure(coll_name, _UNKNOWN_LATENCY_MS)
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


def _create_embedding_provider_for_collection(config: Any, collection_name: str) -> Any:
    """Create the correct embedding provider for a temporal collection.

    Reverse-maps the collection name to the provider name by matching the
    model slug against each configured provider. Falls back to the primary
    provider for legacy collections or unknown slugs.

    Args:
        config: CIDX Config object
        collection_name: Temporal collection name, e.g. 'code-indexer-temporal-voyage_code_3'

    Returns:
        Configured EmbeddingProvider instance for the matching provider
    """
    from ..embedding_factory import EmbeddingProviderFactory

    slug = ""
    if collection_name.startswith(TEMPORAL_COLLECTION_PREFIX):
        slug = collection_name[len(TEMPORAL_COLLECTION_PREFIX) :]

    configured = EmbeddingProviderFactory.get_configured_providers(config)
    for provider_name in configured:
        try:
            model_name = get_model_name_for_provider(provider_name, config)
            model_slug = sanitize_model_name(model_name)
            if model_slug == slug:
                return EmbeddingProviderFactory.create(
                    config, provider_name=provider_name
                )
        except (KeyError, ValueError) as e:
            logger.debug(
                "Skipping provider %s while resolving collection: %s", provider_name, e
            )
            continue

    logger.warning(
        "Could not determine provider for collection '%s', using primary provider",
        collection_name,
    )
    return EmbeddingProviderFactory.create(config)


def _make_config_manager(config: Any) -> Any:
    """Create a minimal config manager wrapper for TemporalSearchService."""

    class _ConfigManagerShim:
        def get_config(self) -> Any:
            return config

    return _ConfigManagerShim()
