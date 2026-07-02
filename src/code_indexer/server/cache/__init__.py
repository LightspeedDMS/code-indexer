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


def get_global_cache() -> HNSWIndexCache:
    """
    Get or create the global HNSW index cache instance.

    This is a singleton pattern - one cache instance shared across
    all server components (SemanticQueryManager, FilesystemVectorStore, etc).

    The cache is initialized with configuration from:
    1. ~/.cidx-server/config.json (if exists)
    2. Environment variables (CIDX_INDEX_CACHE_TTL_MINUTES)
    3. Default values (10 minute TTL)

    Returns:
        Global HNSWIndexCache instance
    """
    global _global_cache_instance

    if _global_cache_instance is None:
        # Try to load configuration from server config file
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
            # Try environment variables, fall back to defaults
            config = HNSWIndexCacheConfig.from_env()
            logger.info(
                f"Initialized HNSW cache with env/default config: TTL={config.ttl_minutes}min",
                extra={"correlation_id": get_correlation_id()},
            )

        # Fix B.1 (Issue #878): overlay opinionated default size cap.
        _apply_default_size_cap(config, cache_kind="HNSW")

        _global_cache_instance = HNSWIndexCache(config=config)

        # Start background cleanup thread
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

    The cache is initialized with configuration from:
    1. ~/.cidx-server/config.json (if exists)
    2. Environment variables (CIDX_FTS_CACHE_TTL_MINUTES)
    3. Default values (10 minute TTL, reload_on_access=True)

    Returns:
        Global FTSIndexCache instance
    """
    global _global_fts_cache_instance

    if _global_fts_cache_instance is None:
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
            # Try environment variables, fall back to defaults
            config = FTSIndexCacheConfig.from_env()
            logger.info(
                f"Initialized FTS cache with env/default config: "
                f"TTL={config.ttl_minutes}min, reload_on_access={config.reload_on_access}",
                extra={"correlation_id": get_correlation_id()},
            )

        # Fix B.1 (Issue #878): overlay opinionated default size cap.
        _apply_default_size_cap(config, cache_kind="FTS")

        _global_fts_cache_instance = FTSIndexCache(config=config)

        # Start background cleanup thread
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
]
