"""Temporal fusion dispatch — shared query execution across all paths (Story #634).

Routes temporal queries through parallel multi-provider execution with
RRF fusion, timeout handling, and single-provider fallback.
Used by CLI, server (semantic_query_manager), multi_search_service, and daemon.
"""

import logging
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
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
from ..path_pattern_matcher import parse_exclude_patterns

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
    chunk_type: Optional[str] = None,
    # Story #1108: per-request cache bypass flag
    no_embedding_cache_shortcut: bool = False,
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
        chunk_type: Filter by chunk type (e.g. 'function', 'class', 'commit_diff')

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

    # Group shards by provider so same-provider shards are queried sequentially
    provider_groups = _group_collections_by_provider(collections)

    if len(provider_groups) == 1:
        # Single provider: query its shards sequentially, merge with RRF
        _, shards = provider_groups[0]
        return _query_provider_shards_sequentially(
            config,
            vector_store,
            shards,
            query_text,
            limit,
            time_range,
            file_path_filter,
            language=language,
            exclude_language=exclude_language,
            exclude_path=exclude_path,
            diff_types=diff_types,
            author=author,
            chunk_type=chunk_type,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )

    # Multiple providers: query each provider's shards sequentially (within provider),
    # providers themselves run in parallel for cross-provider RRF fusion.
    results_by_provider: Dict[str, list] = {}
    warnings_multi: List[str] = []
    failed_providers: List[str] = []

    def _query_one_provider_group(base_name: str, shards: List[str]) -> Any:
        return _query_provider_shards_sequentially(
            config,
            vector_store,
            shards,
            query_text,
            limit * TEMPORAL_OVERFETCH_MULTIPLIER,
            time_range,
            file_path_filter,
            language=language,
            exclude_language=exclude_language,
            exclude_path=exclude_path,
            diff_types=diff_types,
            author=author,
            chunk_type=chunk_type,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )

    executor = ThreadPoolExecutor(max_workers=len(provider_groups))
    try:
        from .temporal_collection_naming import collection_display_name

        future_to_base: Dict[Future, str] = {
            executor.submit(_query_one_provider_group, base_name, shards): base_name
            for base_name, shards in provider_groups
        }
        try:
            for future in as_completed(
                future_to_base, timeout=TEMPORAL_QUERY_TIMEOUT_SECONDS
            ):
                base_name = future_to_base[future]
                display = collection_display_name(base_name)
                try:
                    result = future.result()
                    if result.results:
                        results_by_provider[display] = result.results
                    record_temporal_success(base_name, _UNKNOWN_LATENCY_MS)
                except Exception as e:
                    record_temporal_failure(base_name, _UNKNOWN_LATENCY_MS)
                    failed_providers.append(base_name)
                    logger.warning("Temporal provider %s failed: %s", display, e)
                    warnings_multi.append(f"Provider {display} failed: {e}")
        except FuturesTimeoutError:
            for future, base_name in future_to_base.items():
                if not future.done():
                    future.cancel()
                    record_temporal_failure(base_name, _UNKNOWN_LATENCY_MS)
                    failed_providers.append(base_name)
                    display = collection_display_name(base_name)
                    logger.warning(
                        "Temporal provider %s timed out after %ss",
                        display,
                        TEMPORAL_QUERY_TIMEOUT_SECONDS,
                    )
                    warnings_multi.append(
                        f"Provider {display} timed out after "
                        f"{TEMPORAL_QUERY_TIMEOUT_SECONDS}s"
                    )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not results_by_provider:
        if failed_providers:
            warning_msg = (
                "; ".join(warnings_multi) if warnings_multi else "All providers failed"
            )
        else:
            warning_msg = None
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning=warning_msg,
        )

    fused = fuse_rrf_multi(
        results_by_provider=results_by_provider,
        dedup_key=make_temporal_dedup_key,
        limit=limit,
    )
    warning_str = "; ".join(warnings_multi) if warnings_multi else None
    return TemporalSearchResults(
        results=fused,
        query=query_text,
        filter_type="time_range" if time_range else "none",
        filter_value=time_range,
        total_found=len(fused),
        warning=warning_str,
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


def _group_collections_by_provider(
    collections: List[Tuple[str, Any]],
) -> List[Tuple[str, List[str]]]:
    """Group temporal collection names by provider base slug.

    Strips quarterly shard suffix (-YYYYQN) to identify the provider, then
    groups shards belonging to the same provider. Within each group, shards
    are sorted in ascending chronological order (lexicographic on YYYYQN suffix).
    Legacy monolithic collections (no quarter suffix) are sorted last within
    their provider group.

    Returns:
        List of (provider_base_name, [shard_names]) tuples, one per provider.
        provider_base_name is e.g. 'code-indexer-temporal-voyage_code_3'.
    """
    import re
    from collections import defaultdict

    provider_to_shards: Dict[str, List[str]] = defaultdict(list)

    for coll_name, _ in collections:
        if coll_name.startswith(TEMPORAL_COLLECTION_PREFIX):
            slug = coll_name[len(TEMPORAL_COLLECTION_PREFIX) :]
            base_slug = re.sub(r"-\d{4}Q[1-4]$", "", slug)
            base_name = f"{TEMPORAL_COLLECTION_PREFIX}{base_slug}"
        else:
            base_name = coll_name

        provider_to_shards[base_name].append(coll_name)

    result = []
    for base_name, shards in provider_to_shards.items():
        # Sort: quarterly shards lexicographically (YYYYQN = chronological), legacy last
        def _sort_key(name: str) -> tuple:
            import re as _re

            m = _re.search(r"-(\d{4})Q([1-4])$", name)
            if m:
                return (int(m.group(1)), int(m.group(2)))
            return (9999, 99)  # legacy/monolithic sorts last

        shards_sorted = sorted(shards, key=_sort_key)
        result.append((base_name, shards_sorted))

    return result


def _query_provider_shards_sequentially(
    config: Any,
    vector_store: Any,
    shard_names: List[str],
    query_text: str,
    limit: int,
    time_range: Optional[Tuple[str, str]],
    file_path_filter: Optional[str],
    language: Optional[str] = None,
    exclude_language: Optional[str] = None,
    exclude_path: Optional[str] = None,
    diff_types: Optional[List[str]] = None,
    author: Optional[str] = None,
    chunk_type: Optional[str] = None,
    no_embedding_cache_shortcut: bool = False,
) -> Any:
    """Query a single provider's shards sequentially and merge results with RRF.

    Shards are loaded one at a time (never in parallel) to bound peak RAM usage.
    Each shard's HNSW index is loaded, searched, and can be evicted before the
    next shard is opened.

    Args:
        shard_names: Collection names in ascending chronological order.

    Returns:
        TemporalSearchResults with RRF-fused results from all shards.
    """
    import time as _time
    from .temporal_search_service import TemporalSearchResults
    from .temporal_collection_naming import collection_display_name

    overfetch_limit = limit * TEMPORAL_OVERFETCH_MULTIPLIER
    results_by_shard: Dict[str, list] = {}
    warnings: List[str] = []

    for shard_name in shard_names:  # Sequential — NEVER parallel
        _t0 = _time.time()
        try:
            result = _query_single_provider(
                config,
                vector_store,
                shard_name,
                query_text,
                overfetch_limit,
                time_range,
                file_path_filter,
                language=language,
                exclude_language=exclude_language,
                exclude_path=exclude_path,
                diff_types=diff_types,
                author=author,
                chunk_type=chunk_type,
                no_embedding_cache_shortcut=no_embedding_cache_shortcut,
            )
            if result.results:
                results_by_shard[collection_display_name(shard_name)] = result.results
            record_temporal_success(shard_name, (_time.time() - _t0) * 1000)
        except Exception as e:
            record_temporal_failure(shard_name, (_time.time() - _t0) * 1000)
            logger.warning("Temporal shard query failed for %s: %s", shard_name, e)
            warnings.append(f"Shard {collection_display_name(shard_name)} failed: {e}")

    if not results_by_shard:
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning="; ".join(warnings) if warnings else None,
        )

    fused = fuse_rrf_multi(
        results_by_provider=results_by_shard,
        dedup_key=make_temporal_dedup_key,
        limit=limit,
    )
    return TemporalSearchResults(
        results=fused,
        query=query_text,
        filter_type="time_range" if time_range else "none",
        filter_value=time_range,
        total_found=len(fused),
        warning="; ".join(warnings) if warnings else None,
    )


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
    chunk_type: Optional[str] = None,
    # Story #1108: per-request cache bypass flag
    no_embedding_cache_shortcut: bool = False,
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
            exclude_path=parse_exclude_patterns(exclude_path) or None,
            diff_types=diff_types,
            author=author,
            chunk_type=chunk_type,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
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


def _collect_provider_results(
    future_to_coll: Dict[Future, str],
    results_by_provider: Dict[str, list],
    warnings: List[str],
    failed_providers: List[str],
) -> None:
    """Drain completed futures from as_completed, collecting results and warnings.

    Catches FuturesTimeoutError at the loop level (Bug #669). On timeout,
    all unfinished futures are cancelled and record_temporal_failure is called
    for each timed-out collection so the health monitor can gate them out.

    Args:
        future_to_coll: Mapping of Future -> collection name (all futures already submitted).
        results_by_provider: Mutable dict populated with provider display-name -> result list.
        warnings: Mutable list populated with failure/timeout warning strings.
        failed_providers: Mutable list of collection names that failed or timed out.
    """
    from .temporal_collection_naming import collection_display_name

    try:
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
                failed_providers.append(coll_name)
                logger.warning("Temporal query failed for %s: %s", coll_name, e)
                warnings.append(
                    f"Provider {collection_display_name(coll_name)} failed: {e}"
                )
    except FuturesTimeoutError:
        # Cancel unfinished futures. record_temporal_failure is called for each
        # so the health monitor can gate them out on future queries.
        for future, coll_name in future_to_coll.items():
            if not future.done():
                future.cancel()
                record_temporal_failure(coll_name, _UNKNOWN_LATENCY_MS)
                failed_providers.append(coll_name)
                _display = collection_display_name(coll_name)
                logger.warning(
                    "Temporal provider %s timed out after %ss",
                    _display,
                    TEMPORAL_QUERY_TIMEOUT_SECONDS,
                )
                warnings.append(
                    f"Provider {_display} timed out after "
                    f"{TEMPORAL_QUERY_TIMEOUT_SECONDS}s"
                )


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
    chunk_type: Optional[str] = None,
    # Story #1108: per-request cache bypass flag
    no_embedding_cache_shortcut: bool = False,
) -> Any:
    """Query multiple providers in parallel and fuse results.

    Delegates parallel execution and timeout handling to _collect_provider_results.
    Returns partial results with a warning if providers time out (Bug #669).
    Never raises an exception to the caller.
    """
    from .temporal_search_service import TemporalSearchResults, ALL_TIME_RANGE
    from .temporal_search_service import TemporalSearchService

    overfetch_limit = limit * TEMPORAL_OVERFETCH_MULTIPLIER
    results_by_provider: Dict[str, list] = {}
    warnings: List[str] = []
    failed_providers: List[str] = []

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
            exclude_path=parse_exclude_patterns(exclude_path) or None,
            diff_types=diff_types,
            author=author,
            chunk_type=chunk_type,
            no_embedding_cache_shortcut=no_embedding_cache_shortcut,
        )

    # Use explicit lifecycle instead of context manager so shutdown(wait=False)
    # prevents blocking on in-flight embedding API calls after a timeout (Bug #669).
    executor = ThreadPoolExecutor(max_workers=len(collections))
    try:
        future_to_coll: Dict[Future, str] = {
            executor.submit(query_provider, coll_name): coll_name
            for coll_name, _ in collections
        }
        _collect_provider_results(
            future_to_coll, results_by_provider, warnings, failed_providers
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not results_by_provider:
        # Only warn "All providers failed" when providers actually failed or timed out.
        # Empty results from healthy providers are not a failure.
        if failed_providers:
            warning_msg = "; ".join(warnings) if warnings else "All providers failed"
        else:
            warning_msg = None
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning=warning_msg,
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

    import re as _re

    slug = ""
    if collection_name.startswith(TEMPORAL_COLLECTION_PREFIX):
        slug = collection_name[len(TEMPORAL_COLLECTION_PREFIX) :]
        # Strip quarterly shard suffix -YYYYQN before matching provider slug
        slug = _re.sub(r"-\d{4}Q[1-4]$", "", slug)

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
