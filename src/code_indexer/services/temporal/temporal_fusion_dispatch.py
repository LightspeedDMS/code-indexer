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

    # C1/C2 fix (Story #1171): use shard-pruning discovery that calls get_overlapping_shards
    # so only shards overlapping time_range are queried.
    provider_groups_raw = _discover_provider_shards_with_pruning(
        config, index_path, time_range, provider_filter
    )

    # Health-gate: filter out unhealthy shards per provider
    provider_groups: List[Tuple[str, List[str]]] = []
    for base_name, shards in provider_groups_raw:
        healthy, _ = filter_healthy_temporal_providers([(s, None) for s in shards])
        healthy_shards = [s for s, _ in healthy]
        if healthy_shards:
            provider_groups.append((base_name, healthy_shards))

    if not provider_groups:
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

    if len(provider_groups) == 1:
        # Single provider: query its shards sequentially, merge with RRF
        base_name, shards = provider_groups[0]
        results_by_shard = _query_shards_raw(
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
        if not results_by_shard:
            return TemporalSearchResults(
                results=[],
                query=query_text,
                filter_type="time_range" if time_range else "none",
                filter_value=time_range,
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
        )

    # Multiple providers: providers run in parallel, each provider's shards are
    # queried sequentially within the provider. Single RRF pass over ALL shard
    # results from ALL providers (H1 fix: eliminates double-RRF).
    all_results_by_shard: Dict[str, list] = {}
    warnings_multi: List[str] = []
    failed_providers: List[str] = []

    def _run_provider(base_name: str, shards: List[str]) -> Dict[str, list]:
        return _query_shards_raw(
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

    from .temporal_collection_naming import collection_display_name

    executor = ThreadPoolExecutor(max_workers=len(provider_groups))
    try:
        future_to_base: Dict[Future, str] = {
            executor.submit(_run_provider, base_name, shards): base_name
            for base_name, shards in provider_groups
        }
        try:
            for future in as_completed(
                future_to_base, timeout=TEMPORAL_QUERY_TIMEOUT_SECONDS
            ):
                base_name = future_to_base[future]
                try:
                    per_shard = future.result()
                    all_results_by_shard.update(per_shard)
                    record_temporal_success(base_name, _UNKNOWN_LATENCY_MS)
                except Exception as e:
                    record_temporal_failure(base_name, _UNKNOWN_LATENCY_MS)
                    failed_providers.append(base_name)
                    display = collection_display_name(base_name)
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

    if not all_results_by_shard:
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
        results_by_provider=all_results_by_shard,
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


def _discover_provider_shards_with_pruning(
    config: Any,
    index_path: Path,
    time_range: Optional[Tuple[str, str]],
    provider_filter: Optional[str] = None,
) -> List[Tuple[str, List[str]]]:
    """Discover per-provider overlapping shards, pruned to the query's time_range.

    For each configured embedding provider, calls get_overlapping_shards() to
    find only shard directories whose date range overlaps [time_range start, time_range end].
    Legacy monolithic collections are included when they exist on disk (AC4).

    Returns:
        List of (provider_base_name, [shard_names_in_ascending_chrono_order]).
        Providers with no overlapping shards are excluded.
    """
    from datetime import datetime, timezone
    from ..embedding_factory import EmbeddingProviderFactory
    from .temporal_collection_naming import (
        get_overlapping_shards,
        sanitize_model_name as _sanitize,
        is_sharded_temporal_collection,
    )
    from .temporal_search_service import ALL_TIME_RANGE

    index_path = Path(index_path)

    # Parse time_range strings to datetimes; treat ALL_TIME_RANGE sentinels as None (open-ended)
    if time_range is None:
        dt_start: Optional[datetime] = None
        dt_end: Optional[datetime] = None
    else:
        s, e = time_range
        dt_start = (
            None
            if s == ALL_TIME_RANGE[0]
            else datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        )
        dt_end = (
            None
            if e == ALL_TIME_RANGE[1]
            else datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        )

    configured = EmbeddingProviderFactory.get_configured_providers(config)
    result: List[Tuple[str, List[str]]] = []

    for provider_name in configured:
        if provider_filter and provider_filter not in provider_name:
            continue
        try:
            model_name = get_model_name_for_provider(provider_name, config)
        except ValueError:
            logger.debug("Skipping provider %s (unknown model)", provider_name)
            continue

        shards = get_overlapping_shards(model_name, index_path, dt_start, dt_end)
        if shards:
            base_name = f"{TEMPORAL_COLLECTION_PREFIX}{_sanitize(model_name)}"
            # Log each shard at DEBUG, distinguishing sharded vs legacy (C2 call site)
            for shard in shards:
                if is_sharded_temporal_collection(shard):
                    logger.debug(
                        "Provider %s: including shard %s", provider_name, shard
                    )
                else:
                    logger.debug(
                        "Provider %s: including legacy collection %s",
                        provider_name,
                        shard,
                    )
            result.append((base_name, shards))

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


def _query_shards_raw(
    config: Any,
    vector_store: Any,
    shard_names: List[str],
    query_text: str,
    overfetch_limit: int,
    time_range: Optional[Tuple[str, str]],
    file_path_filter: Optional[str],
    language: Optional[str] = None,
    exclude_language: Optional[str] = None,
    exclude_path: Optional[str] = None,
    diff_types: Optional[List[str]] = None,
    author: Optional[str] = None,
    chunk_type: Optional[str] = None,
    no_embedding_cache_shortcut: bool = False,
) -> Dict[str, list]:
    """Query shards SEQUENTIALLY and return raw per-shard result lists (no fusion).

    Shards are loaded one at a time to bound peak RAM usage. Returns a dict
    keyed by shard display name so the caller can do a single RRF pass over
    all results from all providers (H1 fix: no intermediate fusion here).

    Args:
        shard_names: Collection names in ascending chronological order.
        overfetch_limit: Per-shard limit (caller multiplies by OVERFETCH_MULTIPLIER).

    Returns:
        Dict mapping shard display name -> list of TemporalSearchResult.
        Empty dict when all shards return zero results.
    """
    import time as _time
    from .temporal_collection_naming import collection_display_name

    results_by_shard: Dict[str, list] = {}

    for shard_name in shard_names:  # SEQUENTIAL — never parallel
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

    return results_by_shard


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
    from .temporal_search_service import TemporalSearchResults

    overfetch_limit = limit * TEMPORAL_OVERFETCH_MULTIPLIER
    results_by_shard = _query_shards_raw(
        config,
        vector_store,
        shard_names,
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

    if not results_by_shard:
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
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
