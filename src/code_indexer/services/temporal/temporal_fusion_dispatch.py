"""Temporal fusion dispatch — shared query execution across all paths (Story #634).

Story #1291: recall resolves to EXACTLY ONE embedder's shard set per query
(RRF-fused only across that embedder's own quarterly shards) -- there is no
longer a cross-embedder parallel-fan-out-and-fuse path (AC9 forbids mixing
providers in a single ranked result set).
Used by CLI, server (semantic_query_manager), multi_search_service, and daemon.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .temporal_fusion import (
    TEMPORAL_OVERFETCH_MULTIPLIER,
    fuse_rrf_multi,
    make_temporal_dedup_key,
)
from .temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
    sanitize_model_name,
)
from .temporal_health import (
    filter_healthy_temporal_providers,
    record_temporal_success,
    record_temporal_failure,
)
from ..path_pattern_matcher import parse_exclude_patterns

logger = logging.getLogger(__name__)


class TemporalEmbedderUnavailableError(RuntimeError):
    """Raised when a registered TemporalEmbedder adapter has no query-capable
    client (e.g. StandardTemporalEmbedder with no Cohere API key configured).

    Code review Finding 3 (Story #1291): _build_query_provider_for_embedder
    must fail loud with this typed error instead of returning a bare None --
    a None provider would otherwise propagate into TemporalSearchService and
    only surface as an opaque AttributeError deep inside query_temporal()
    (Messi #13 anti-silent-failure).
    """


TEMPORAL_QUERY_TIMEOUT_SECONDS = 15

# Story #1213 Story 3: Reduced per-shard overfetch multiplier under YELLOW memory pressure.
# TEMPORAL_OVERFETCH_MULTIPLIER=3 is the normal value; YELLOW reduces this to 2 to lower
# peak RAM usage while returning enough candidates for good RRF fusion quality.
YELLOW_OVERFETCH_MULTIPLIER = 2

# Latency placeholder when per-future timing is not available
_UNKNOWN_LATENCY_MS = 0.0


def _effective_overfetch_multiplier(vector_store: Any) -> int:
    """Return the overfetch multiplier for the current memory band.

    YELLOW band -> YELLOW_OVERFETCH_MULTIPLIER (2).
    All other bands or no governor -> TEMPORAL_OVERFETCH_MULTIPLIER (3).
    """
    from code_indexer.server.services.memory_governor import MemoryBand

    gov = getattr(vector_store, "memory_governor", None)
    if gov is not None and gov.band == MemoryBand.YELLOW:
        return YELLOW_OVERFETCH_MULTIPLIER
    return TEMPORAL_OVERFETCH_MULTIPLIER


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
    # Story #1291 AC7/AC8: explicit embedder override for recall selection.
    temporal_embedder: Optional[str] = None,
) -> Any:
    """Execute a temporal query against EXACTLY ONE embedder's collections.

    Story #1291 (AC7/AC8/AC9): recall selects a SINGLE embedder's shard set
    per query -- an omitted `temporal_embedder` uses
    `config.temporal.active_embedder`; an explicit `temporal_embedder`
    selects THAT embedder and NEVER falls back to active_embedder (an
    explicit target with zero indexed collections returns an empty, typed
    "not indexed for embedder X" result). Cross-embedder RRF fusion is
    structurally impossible: discovery below returns at most one provider
    group, and a defensive invariant check below fails loud if that
    contract is ever violated.

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
        temporal_embedder: Optional explicit embedder name override (AC7/AC8).

    Returns:
        TemporalSearchResults for the resolved embedder's collections (RRF
        fused ONLY across that embedder's own quarterly shards).
    """
    from .temporal_search_service import TemporalSearchResults

    # Auto-migrate legacy collection if present (Story #629, wired by audit fix F2)
    from .temporal_migration import migrate_legacy_temporal_collection

    migrate_legacy_temporal_collection(index_path, config)

    # C1/C2 fix (Story #1171): use shard-pruning discovery that calls get_overlapping_shards
    # so only shards overlapping time_range are queried. Story #1291: discovery
    # resolves to AT MOST ONE embedder (the override or active_embedder).
    provider_groups_raw = _discover_provider_shards_with_pruning(
        config,
        index_path,
        time_range,
        provider_filter,
        temporal_embedder=temporal_embedder,
    )

    # Health-gate: filter out unhealthy shards per provider
    provider_groups: List[Tuple[str, List[str]]] = []
    for base_name, shards in provider_groups_raw:
        healthy, _ = filter_healthy_temporal_providers([(s, None) for s in shards])
        healthy_shards = [s for s, _ in healthy]
        if healthy_shards:
            provider_groups.append((base_name, healthy_shards))

    # AC9 (defensive invariant, Messi #15): discovery must NEVER resolve to
    # more than one embedder -- cross-embedder fusion is forbidden. This is
    # a fail-loud guard, not a happy-path branch: it should be unreachable
    # given _discover_provider_shards_with_pruning's contract.
    if len(provider_groups) > 1:
        raise RuntimeError(
            "Internal invariant violation: temporal query resolved to "
            f"{len(provider_groups)} embedder(s) "
            f"({[base for base, _ in provider_groups]}) -- cross-embedder "
            f"fusion is forbidden (Story #1291 AC9)."
        )

    if not provider_groups:
        if temporal_embedder:
            # AC8: an EXPLICIT override with no v2 collections is a typed
            # "not indexed for this embedder" result -- NEVER a silent
            # redirect to active_embedder.
            warning_msg = (
                f"Temporal embedder '{temporal_embedder}' has no indexed "
                f"collections. Run cidx index --index-commits with "
                f"temporal.embedders including '{temporal_embedder}' first."
            )
        else:
            warning_msg = (
                "No temporal indexes available. "
                "Run cidx index --index-commits to create temporal indexes."
            )
        logger.warning(warning_msg)
        return TemporalSearchResults(
            results=[],
            query=query_text,
            filter_type="time_range" if time_range else "none",
            filter_value=time_range,
            warning=warning_msg,
        )

    # Exactly one provider: query its (own) shards sequentially, RRF-merge
    # ACROSS THAT EMBEDDER'S OWN quarterly shards only (never another
    # embedder's).
    base_name, shards = provider_groups[0]
    results_by_shard = _query_shards_raw(
        config,
        vector_store,
        shards,
        query_text,
        limit * _effective_overfetch_multiplier(vector_store),
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


def _discover_provider_shards_with_pruning(
    config: Any,
    index_path: Path,
    time_range: Optional[Tuple[str, str]],
    provider_filter: Optional[str] = None,
    temporal_embedder: Optional[str] = None,
) -> List[Tuple[str, List[str]]]:
    """Discover the SINGLE resolved embedder's overlapping shards (AC7/AC8/AC9).

    Story #1291: resolves to AT MOST ONE embedder -- `temporal_embedder`
    when explicitly given (NEVER falling back to active_embedder even if
    that embedder has no shards), otherwise `config.temporal.active_embedder`
    only. This is a hard change from Story #1290's behavior of iterating
    the FULL `config.temporal.embedders` list (which allowed cross-embedder
    RRF fusion downstream) -- AC9 forbids that outright.

    Calls get_overlapping_shards() to find only shard directories whose date
    range overlaps [time_range start, time_range end]. Legacy monolithic
    collections are included when they exist on disk (AC4).

    Returns:
        List of AT MOST ONE (embedder_base_name, [shard_names]) tuple. Empty
        list when the resolved embedder has no overlapping shards.
    """
    from datetime import datetime, timezone
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

    if temporal_embedder:
        # AC8: explicit override -- ONLY this embedder, no fallback.
        resolved_embedder: Optional[str] = temporal_embedder
    else:
        # AC7: omitted override -- active_embedder ONLY (never the full
        # config.temporal.embedders set -- that would allow cross-embedder
        # fusion downstream, forbidden by AC9).
        _active = getattr(config.temporal, "active_embedder", None)
        resolved_embedder = _active if isinstance(_active, str) and _active else None

    if not resolved_embedder:
        return []

    if provider_filter and provider_filter not in resolved_embedder:
        return []

    shards = get_overlapping_shards(resolved_embedder, index_path, dt_start, dt_end)
    if not shards:
        return []

    base_name = f"{TEMPORAL_COLLECTION_PREFIX}{_sanitize(resolved_embedder)}"
    for shard in shards:
        if is_sharded_temporal_collection(shard):
            logger.debug("Embedder %s: including shard %s", resolved_embedder, shard)
        else:
            logger.debug(
                "Embedder %s: including legacy collection %s",
                resolved_embedder,
                shard,
            )
    return [(base_name, shards)]


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
        finally:
            # Story #1213 Story 3: Conditional eviction via MemoryGovernor.
            #
            # Bug #1171 (unconditional evict) was the proven-safe baseline.
            # We now consult the governor before evicting so GREEN-band servers
            # can retain shard HNSWs across queries for cross-query warm-cache reuse.
            #
            # Fail-safe contract (SAFETY-CRITICAL):
            #   - gov is None  (CLI/solo)          → ALWAYS evict  (#1171 byte-identical)
            #   - gov.should_evict_after_shard() raises → caught here → ALWAYS evict
            #   - gov disabled / RED / pre-first-sample → should_evict returns True → evict
            #   - gov GREEN                        → should_evict returns False → retain
            #
            # Cache key: str((base_path / shard_name).resolve()) — matches #1171 exactly.
            _hnsw_cache = getattr(vector_store, "hnsw_index_cache", None)
            if _hnsw_cache is not None:
                _gov = getattr(vector_store, "memory_governor", None)
                _should_evict = True  # fail-safe default
                _gov_healthy = (
                    False  # True only if should_evict_after_shard() returned normally
                )
                if _gov is not None:
                    try:
                        _should_evict = _gov.should_evict_after_shard()
                        _gov_healthy = True
                    except Exception as _gov_exc:  # noqa: BLE001
                        logger.warning(
                            "GOV should_evict_after_shard() raised — fail-safe evict: %s",
                            _gov_exc,
                        )
                        _should_evict = True  # explicit fail-safe
                if _should_evict:
                    _shard_path = Path(vector_store.base_path) / shard_name
                    _hnsw_cache.invalidate(str(_shard_path.resolve()))
                    # Only update governor counters/trim/log when governor is healthy;
                    # a broken governor must not prevent the eviction from completing.
                    if _gov is not None and _gov_healthy:
                        _gov.counters.shards_evicted_after_use += 1
                        _gov.maybe_trim()
                        # GOV-002: emitted from the dispatch evict call-site (Story 4).
                        # freed_mb=0.0 is best-effort — no expensive size computation
                        # on the eviction hot path.
                        _gov.log_gov002_evict(shard=shard_name, freed_mb=0.0)

    return results_by_shard


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
    # Split comma-joined path_filter string using the same parse_exclude_patterns
    # contract already used for exclude_path below (Bug #1210).  Single patterns
    # pass through as a 1-element list; None/empty -> None.
    path_filter = parse_exclude_patterns(file_path_filter) or None

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


def _build_query_provider_for_embedder(config: Any, embedder_name: str) -> Any:
    """Construct a QUERY-capable embedding provider pinned to `embedder_name`.

    Story #1291: resolves via the SAME TemporalEmbedder registry used by the
    INDEXING side (create_embedder), returning the adapter's already-pinned
    internal client -- a VoyageAIClient for ContextualTemporalEmbedder, a
    CohereEmbeddingProvider for StandardTemporalEmbedder. This is a drop-in
    EmbeddingProvider for the existing FilesystemVectorStore.search() /
    coalesced_query_embedding() plumbing (governor, cache, lane). Only
    voyage_ai.py's get_embedding() routes voyage-context-4 through the
    contextualized endpoint internally (AC14); Cohere has no equivalent
    special-casing (embed-v4.0 uses the ordinary embed endpoint for both
    indexing and query).

    Registry-based resolution generalizes to any FUTURE registered adapter
    automatically -- no per-embedder-family string matching. The legacy
    Voyage-only construction is kept as a fallback ONLY for a name that
    is not (or no longer) registered in the TemporalEmbedder registry.
    """
    from .embedders.contextual import ContextualTemporalEmbedder
    from .embedders.registry import create_embedder as _create_temporal_embedder
    from .embedders.standard import StandardTemporalEmbedder

    try:
        adapter = _create_temporal_embedder(embedder_name, config)
    except KeyError:
        adapter = None

    if isinstance(adapter, (ContextualTemporalEmbedder, StandardTemporalEmbedder)):
        if adapter._client is None:
            raise TemporalEmbedderUnavailableError(
                f"Temporal embedder '{embedder_name}' is registered but has "
                f"no query-capable client (missing credentials) -- cannot "
                f"build a query provider for it."
            )
        return adapter._client

    if not embedder_name.startswith("cohere"):
        from ...config import VoyageAIConfig
        from ...services.voyage_ai import VoyageAIClient

        base_voyage_config = getattr(config, "voyage_ai", None)
        if base_voyage_config is not None:
            voyage_config = base_voyage_config.model_copy(
                update={"model": embedder_name}
            )
        else:
            voyage_config = VoyageAIConfig(model=embedder_name)
        return VoyageAIClient(voyage_config)

    raise ValueError(
        f"Unsupported temporal embedder '{embedder_name}' -- no query-side "
        f"provider constructor registered for this embedder family."
    )


def _create_embedding_provider_for_collection(config: Any, collection_name: str) -> Any:
    """Create the correct embedding provider for a temporal collection.

    Story #1290: reverse-maps the collection slug against
    `config.temporal.embedders` (the per-commit embedder adapter registry),
    NOT the regular semantic-search provider/model. Falls back to
    `config.temporal.active_embedder` for legacy collections or unknown
    slugs.

    Args:
        config: CIDX Config object
        collection_name: Temporal collection name, e.g.
            'code-indexer-temporal-voyage_context_4'

    Returns:
        Configured EmbeddingProvider instance pinned to the matching
        temporal embedder's model.
    """
    import re as _re

    slug = ""
    if collection_name.startswith(TEMPORAL_COLLECTION_PREFIX):
        slug = collection_name[len(TEMPORAL_COLLECTION_PREFIX) :]
        # Strip quarterly shard suffix -YYYYQN before matching embedder slug
        slug = _re.sub(r"-\d{4}Q[1-4]$", "", slug)

    embedders = list(getattr(config.temporal, "embedders", []) or [])
    for embedder_name in embedders:
        if sanitize_model_name(embedder_name) == slug:
            return _build_query_provider_for_embedder(config, embedder_name)

    fallback_embedder = getattr(config.temporal, "active_embedder", None)
    logger.warning(
        "Could not match collection '%s' to a configured temporal embedder "
        "(%s); falling back to active_embedder '%s'",
        collection_name,
        embedders,
        fallback_embedder,
    )
    if fallback_embedder:
        return _build_query_provider_for_embedder(config, fallback_embedder)

    raise ValueError(
        f"Could not resolve an embedding provider for temporal collection "
        f"'{collection_name}': no matching entry in config.temporal.embedders "
        f"and no active_embedder configured."
    )


def _make_config_manager(config: Any) -> Any:
    """Create a minimal config manager wrapper for TemporalSearchService."""

    class _ConfigManagerShim:
        def get_config(self) -> Any:
            return config

    return _ConfigManagerShim()
