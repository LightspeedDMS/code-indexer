"""
Server-side cache module for CIDX server.

Story #526: Provides singleton HNSW index cache for server-wide performance optimization.
Story #XXX: Provides singleton FTS (Tantivy) index cache for FTS query performance.
Story #679: Provides PayloadCache for semantic search result truncation.
Bug #878 (Fix B.1): Applies an opinionated default ``max_cache_size_mb`` at
singleton init so HNSW / FTS native memory is bounded even when the
configuration on disk omits the size cap. Dataclass defaults remain
``None``; the overlay is only applied inside the server-init helpers below.
Test coverage: tests/unit/server/cache/test_size_cap_defaults.py.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from .hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
    HNSWIndexCacheEntry,
    HNSWIndexCacheStats,
)
from .fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
    FTSIndexCacheEntry,
    FTSIndexCacheStats,
)
from .payload_cache import (
    PayloadCache,
    PayloadCacheConfig,
    CacheNotFoundError,
)
from code_indexer.server.logging_utils import format_error_log

# Server-wide singleton cache instances
# Initialized on first import, shared across all server components
_global_cache_instance = None
_global_fts_cache_instance = None

# Fix B.1 (Issue #878): Opinionated default cap for HNSW / FTS index caches.
# Applied by get_global_cache() / get_global_fts_cache() when the loaded
# configuration has ``max_cache_size_mb is None``. Keeps native memory
# bounded for hot repositories whose access-based TTL would otherwise keep
# them cached forever. Operators can override by setting an explicit value
# in config.json / via the documented environment variables.
DEFAULT_MAX_CACHE_SIZE_MB = 4096

# Story #1166: Minimum per-worker cap so no worker is starved under heavy
# subdivision (e.g. 32 workers × 4096 MB → 128 MB → floored to 256 MB).
# Exported for observability / tests.
MIN_CAP_PER_WORKER_MB = 256


def _apply_default_size_cap(
    config: "HNSWIndexCacheConfig | FTSIndexCacheConfig",
    cache_kind: str,
) -> None:
    """
    Overlay the opinionated default ``max_cache_size_mb`` when the loaded
    config has ``None``.

    Mutates ``config`` in place and emits an INFO log record so operators
    can see they are running on the default rather than an explicit value.
    No-op when ``config.max_cache_size_mb`` is already set.

    Args:
        config: HNSW or FTS cache config instance to mutate.
        cache_kind: Human-readable tag (e.g. "HNSW", "FTS") used in the log
            message for operator clarity.
    """
    import logging

    if config.max_cache_size_mb is not None:
        return

    config.max_cache_size_mb = DEFAULT_MAX_CACHE_SIZE_MB
    logging.getLogger(__name__).info(
        f"Applying default max_cache_size_mb={DEFAULT_MAX_CACHE_SIZE_MB}MB "
        f"for {cache_kind} cache. Set an explicit value in server config "
        "to override.",
        extra={"correlation_id": get_correlation_id()},
    )


def _divided_cap(configured_cap: int, worker_count: int) -> int:
    """
    Compute the per-worker cache cap by dividing *configured_cap* by
    *worker_count*, floored at MIN_CAP_PER_WORKER_MB.

    Protects against div-by-zero and negative worker counts via
    ``max(1, worker_count)`` so that misconfigured values (0, -1) fall back
    to the full cap rather than raising.

    Args:
        configured_cap: The resolved per-node cap in MB (after default overlay).
        worker_count: Number of uvicorn workers sharing this node's budget.

    Returns:
        Effective per-worker cap in MB, always >= MIN_CAP_PER_WORKER_MB.
    """
    effective_divisor = max(1, worker_count)
    raw = configured_cap // effective_divisor
    return max(MIN_CAP_PER_WORKER_MB, raw)


def _load_hnsw_config() -> "HNSWIndexCacheConfig":
    """
    Load HNSWIndexCacheConfig from ~/.cidx-server/config.json (if present),
    falling back to environment variables / defaults, and overlay the
    opinionated default size cap (Fix B.1).

    Extracted from get_global_cache() to avoid duplication with
    initialize_caches() (Messi anti-duplication rule).

    Returns:
        HNSWIndexCacheConfig with max_cache_size_mb guaranteed to be set.
    """
    from pathlib import Path
    import logging

    logger = logging.getLogger(__name__)
    config_file = Path.home() / ".cidx-server" / "config.json"

    if config_file.exists():
        try:
            config = HNSWIndexCacheConfig.from_file(str(config_file))
            logger.info(
                f"Loaded HNSW cache config from {config_file}: TTL={config.ttl_minutes}min",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-007",
                    f"Failed to load cache config from {config_file}: {e}. Using defaults.",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            config = HNSWIndexCacheConfig.from_env()
    else:
        config = HNSWIndexCacheConfig.from_env()
        logger.info(
            f"Initialized HNSW cache with env/default config: TTL={config.ttl_minutes}min",
            extra={"correlation_id": get_correlation_id()},
        )

    # Fix B.1 (Issue #878): overlay opinionated default size cap.
    _apply_default_size_cap(config, cache_kind="HNSW")
    return config


def _load_fts_config() -> "FTSIndexCacheConfig":
    """
    Load FTSIndexCacheConfig from ~/.cidx-server/config.json (if present),
    falling back to environment variables / defaults, and overlay the
    opinionated default size cap (Fix B.1).

    Extracted from get_global_fts_cache() to avoid duplication with
    initialize_caches() (Messi anti-duplication rule).

    Returns:
        FTSIndexCacheConfig with max_cache_size_mb guaranteed to be set.
    """
    from pathlib import Path
    import logging

    logger = logging.getLogger(__name__)
    config_file = Path.home() / ".cidx-server" / "config.json"

    if config_file.exists():
        try:
            config = FTSIndexCacheConfig.from_file(str(config_file))
            logger.info(
                f"Loaded FTS cache config from {config_file}: "
                f"TTL={config.ttl_minutes}min, reload_on_access={config.reload_on_access}",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-008",
                    f"Failed to load FTS cache config from {config_file}: {e}. Using defaults.",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            config = FTSIndexCacheConfig.from_env()
    else:
        config = FTSIndexCacheConfig.from_env()
        logger.info(
            f"Initialized FTS cache with env/default config: "
            f"TTL={config.ttl_minutes}min, reload_on_access={config.reload_on_access}",
            extra={"correlation_id": get_correlation_id()},
        )

    # Fix B.1 (Issue #878): overlay opinionated default size cap.
    _apply_default_size_cap(config, cache_kind="FTS")
    return config


def initialize_caches(worker_count: int) -> None:
    """
    Eagerly initialize HNSW and FTS index-cache singletons with a per-worker
    memory budget.

    Under ``uvicorn --workers N`` every worker process builds its own HNSW
    and FTS singletons.  Without this call each worker would build at the
    full per-node cap (DEFAULT_MAX_CACHE_SIZE_MB), so a node with N workers
    can hold up to N × cap of native memory.  This function divides the
    configured cap by *worker_count* (floored at MIN_CAP_PER_WORKER_MB) so
    the total stays within the operator-configured budget.

    Must be called BEFORE the first request handler can trigger the lazy
    ``get_global_cache()`` / ``get_global_fts_cache()`` path.  Idempotent:
    if either singleton is already initialized it is skipped (no second
    background-cleanup thread is spawned).

    When ``initialize_caches`` is NOT called (CLI / single-worker / tests),
    the lazy getters build at the full cap — behaviour is identical to
    before Story #1166.

    Args:
        worker_count: Number of uvicorn workers sharing this node's budget.
            Misconfigured values (0, negative) are safe — they fall back to
            the full cap via ``max(1, worker_count)``.
    """
    import logging

    global _global_cache_instance, _global_fts_cache_instance

    logger = logging.getLogger(__name__)

    # --- HNSW ---
    if _global_cache_instance is None:
        hnsw_config = _load_hnsw_config()
        hnsw_config.max_cache_size_mb = _divided_cap(
            hnsw_config.max_cache_size_mb,  # type: ignore[arg-type]
            worker_count,
        )
        _global_cache_instance = HNSWIndexCache(config=hnsw_config)
        _global_cache_instance.start_background_cleanup()
        logger.info(
            "Story #1166: HNSW cache initialized with per-worker cap "
            f"{hnsw_config.max_cache_size_mb}MB "
            f"(workers={worker_count}, floor={MIN_CAP_PER_WORKER_MB}MB)",
            extra={"correlation_id": get_correlation_id()},
        )
    else:
        logger.debug(
            "Story #1166: HNSW cache already initialized — skipping re-construction",
            extra={"correlation_id": get_correlation_id()},
        )

    # --- FTS ---
    if _global_fts_cache_instance is None:
        fts_config = _load_fts_config()
        fts_config.max_cache_size_mb = _divided_cap(
            fts_config.max_cache_size_mb,  # type: ignore[arg-type]
            worker_count,
        )
        _global_fts_cache_instance = FTSIndexCache(config=fts_config)
        _global_fts_cache_instance.start_background_cleanup()
        logger.info(
            "Story #1166: FTS cache initialized with per-worker cap "
            f"{fts_config.max_cache_size_mb}MB "
            f"(workers={worker_count}, floor={MIN_CAP_PER_WORKER_MB}MB)",
            extra={"correlation_id": get_correlation_id()},
        )
    else:
        logger.debug(
            "Story #1166: FTS cache already initialized — skipping re-construction",
            extra={"correlation_id": get_correlation_id()},
        )


def get_global_cache() -> HNSWIndexCache:
    """
    Get or create the global HNSW index cache instance.

    This is a singleton pattern - one cache instance shared across
    all server components (SemanticQueryManager, FilesystemVectorStore, etc).

    When ``initialize_caches(worker_count)`` has been called during server
    startup the already-built (per-worker-capped) singleton is returned
    unchanged.  When it has NOT been called (CLI / single-worker / tests)
    the singleton is built here at the full DEFAULT_MAX_CACHE_SIZE_MB cap,
    preserving pre-Story-#1166 behaviour exactly.

    The cache is initialized with configuration from:
    1. ~/.cidx-server/config.json (if exists)
    2. Environment variables (CIDX_INDEX_CACHE_TTL_MINUTES)
    3. Default values (10 minute TTL)

    Returns:
        Global HNSWIndexCache instance
    """
    global _global_cache_instance

    if _global_cache_instance is None:
        config = _load_hnsw_config()
        _global_cache_instance = HNSWIndexCache(config=config)
        _global_cache_instance.start_background_cleanup()

    return _global_cache_instance


def reset_global_cache() -> None:
    """
    Reset the global cache instance (for testing purposes).

    Stops background cleanup and clears the singleton.
    """
    global _global_cache_instance

    if _global_cache_instance is not None:
        _global_cache_instance.stop_background_cleanup()
        _global_cache_instance = None


def get_global_fts_cache() -> FTSIndexCache:
    """
    Get or create the global FTS (Tantivy) index cache instance.

    This is a singleton pattern - one cache instance shared across
    all server components for FTS search operations.

    When ``initialize_caches(worker_count)`` has been called during server
    startup the already-built (per-worker-capped) singleton is returned
    unchanged.  When it has NOT been called (CLI / single-worker / tests)
    the singleton is built here at the full DEFAULT_MAX_CACHE_SIZE_MB cap,
    preserving pre-Story-#1166 behaviour exactly.

    The cache is initialized with configuration from:
    1. ~/.cidx-server/config.json (if exists)
    2. Environment variables (CIDX_FTS_CACHE_TTL_MINUTES)
    3. Default values (10 minute TTL, reload_on_access=True)

    Returns:
        Global FTSIndexCache instance
    """
    global _global_fts_cache_instance

    if _global_fts_cache_instance is None:
        config = _load_fts_config()
        _global_fts_cache_instance = FTSIndexCache(config=config)
        _global_fts_cache_instance.start_background_cleanup()

    return _global_fts_cache_instance


def reset_global_fts_cache() -> None:
    """
    Reset the global FTS cache instance (for testing purposes).

    Stops background cleanup and clears the singleton.
    """
    global _global_fts_cache_instance

    if _global_fts_cache_instance is not None:
        _global_fts_cache_instance.stop_background_cleanup()
        _global_fts_cache_instance = None


def get_total_index_memory_mb() -> float:
    """Get combined index memory from HNSW and FTS caches.

    Returns total memory in MB used by all cached indexes.
    Returns 0.0 if caches are not yet initialized.
    """
    total_mb = 0.0
    hnsw = _global_cache_instance
    if hnsw is not None:
        total_mb += hnsw.get_stats().total_memory_mb
    fts = _global_fts_cache_instance
    if fts is not None:
        total_mb += fts.get_stats().total_memory_mb
    return total_mb


__all__ = [
    # HNSW cache exports
    "HNSWIndexCache",
    "HNSWIndexCacheConfig",
    "HNSWIndexCacheEntry",
    "HNSWIndexCacheStats",
    "get_global_cache",
    "reset_global_cache",
    # FTS cache exports
    "FTSIndexCache",
    "FTSIndexCacheConfig",
    "FTSIndexCacheEntry",
    "FTSIndexCacheStats",
    "get_global_fts_cache",
    "reset_global_fts_cache",
    # Combined index memory
    "get_total_index_memory_mb",
    # Payload cache exports (Story #679)
    "PayloadCache",
    "PayloadCacheConfig",
    "CacheNotFoundError",
    # Fix B.1 default size cap (exported for observability / tests)
    "DEFAULT_MAX_CACHE_SIZE_MB",
    # Story #1166: per-worker budget division (exported for observability / tests)
    "MIN_CAP_PER_WORKER_MB",
    "initialize_caches",
]
