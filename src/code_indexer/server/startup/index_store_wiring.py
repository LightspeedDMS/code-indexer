"""
Index store wiring for CIDX server startup.

Builds the ``IndexStore`` refresh publishes through, selected by the
``index_store_backend`` server config field. Same fail-fast policy as the git,
content and ripgrep wiring:

- ``local``     : the index is read where it is built (today). The DEFAULT, so
                  an unset or absent config behaves exactly as cidx does now.
- ``delegated`` : each refresh publishes the built index to the artifact
                  service and reads back a checked-out, verified version. No
                  silent fallback: an unusable plugin raises at startup. A
                  served snapshot then holds index state only (no working
                  tree), so repository bytes must come through the content
                  and ripgrep seams (``SEAMS_REQUIRED_BY_DELEGATED``), which
                  map a snapshot path to the base clone; with either one
                  local, file/regex/X-Ray reads of a snapshot would come back
                  silently empty, so startup refuses that combination.
- anything else : raises immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from code_indexer.kernel.index_store_seam import local_index_store
from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    IndexStore,
)

logger = logging.getLogger(__name__)

VALID_INDEX_STORE_BACKENDS = ("local", "delegated")

#: Server config fields that must also be ``delegated`` with a delegated store.
SEAMS_REQUIRED_BY_DELEGATED = ("content_backend", "ripgrep_backend")


def build_index_store(config: Any, *, artifacts: Optional[Any] = None) -> IndexStore:
    """Build the IndexStore configured by *config* (read via ``getattr``)."""
    backend_type = getattr(config, "index_store_backend", "local") or "local"
    if backend_type not in VALID_INDEX_STORE_BACKENDS:
        raise RuntimeError(
            f"Unknown index_store_backend {backend_type!r}. "
            f"Valid values are {VALID_INDEX_STORE_BACKENDS}."
        )
    if backend_type == "delegated":
        local_seams = [
            field
            for field in SEAMS_REQUIRED_BY_DELEGATED
            if (getattr(config, field, "local") or "local") != "delegated"
        ]
        if local_seams:
            raise RuntimeError(
                "index_store_backend='delegated' requires "
                + " and ".join(f"{field}='delegated'" for field in local_seams)
                + ": a served snapshot holds index state only, so reading "
                "repository files from it locally would return nothing."
            )
        if artifacts is None:
            raise RuntimeError(
                "index_store_backend='delegated' selected but no index-artifact "
                "client is available. Install a plugin registering a factory on "
                "the code_indexer.index_artifacts entry point, or set "
                "CIDX_INDEX_ARTIFACTS='package.module:factory'. Refusing to fall "
                "back to the local store: built indexes would never be published."
            )
        logger.info("IndexStore: using delegated store (%s)", type(artifacts).__name__)
        return DelegatedIndexStore(artifacts)
    # The local mode's module, through the seam resolver: with none installed
    # this refuses, naming the seam (the kernel holds no implementation).
    store = local_index_store()
    logger.info("IndexStore: using %s (index read where built)", type(store).__name__)
    return store


def build_configured_index_store(config: Any) -> IndexStore:
    """Resolve the artifact plugin (only when delegated) and build the store."""
    from code_indexer.server.services.index_artifacts_plugin import (
        resolve_index_artifacts,
    )

    backend_type = getattr(config, "index_store_backend", "local") or "local"
    artifacts = resolve_index_artifacts() if backend_type == "delegated" else None
    return build_index_store(config, artifacts=artifacts)


def server_index_store() -> IndexStore:
    """The store the running server built at startup (``app.state.index_store``).

    ``LocalIndexStore`` outside a server process (CLI, tests) or before startup
    built one: indexes are then read where they were built, as before.  Read
    without importing the app, so a CLI process never constructs a store.
    Raises ``RuntimeError`` if the parked value is not an ``IndexStore``.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    store = getattr(_running_server_app_state(), "index_store", None)
    if store is None:
        return local_index_store()
    if not hasattr(store, "is_local"):
        raise RuntimeError(f"app.state.index_store is not an IndexStore: {type(store)}")
    parked: IndexStore = store
    return parked


def start_checkout_sweeper(app: Any, resource_config: Any) -> Optional[Any]:
    """Start this pod's sweep of leftover IndexStore checkouts
    (``server/services/checkout_sweep.py``) and park it on
    ``app.state.checkout_sweeper``. Every pod runs one (the state is
    pod-local). None -- and no thread -- under the local store."""
    app.state.checkout_sweeper = None
    if server_index_store().is_local:
        return None
    from code_indexer.server.services.checkout_sweep import CheckoutSweeper

    manager = app.state.activated_repo_manager
    sweeper = CheckoutSweeper(
        activated_repo_manager=manager,
        activated_repos_dir=str(manager.activated_repos_dir),
        golden_repos_dir=getattr(app.state, "golden_repos_dir", None),
        query_tracker=getattr(app.state, "query_tracker", None),
        grace_seconds=resource_config.index_checkout_sweep_grace_seconds,
    )
    sweeper.start(resource_config.index_checkout_sweep_interval_seconds)
    app.state.checkout_sweeper = sweeper
    logger.info("IndexStore checkout sweep started on this pod")
    return sweeper


def stop_checkout_sweeper(app: Any) -> None:
    sweeper = getattr(app.state, "checkout_sweeper", None)
    if sweeper is not None:
        sweeper.stop()


def _golden_write_locked(app: Any) -> Any:
    """``repo -> bool``: the golden repository's write lock is held (a refresh
    is building its index or temporal shards in place). Read live from the
    refresh scheduler; with none wired, nothing counts as building."""

    def locked(repo: str) -> bool:
        lifecycle = getattr(app.state, "global_lifecycle_manager", None)
        scheduler = getattr(lifecycle, "refresh_scheduler", None)
        if scheduler is None:
            return False
        return bool(scheduler.is_write_locked(repo))

    return locked


def build_served_index_cache_limits(resource_config: Any) -> Any:
    """The watermarks configured in ``resource_config`` (``served_index_cache_*``)."""
    from code_indexer.server.services.served_index_cache import (
        ServedIndexCacheLimits,
    )

    return ServedIndexCacheLimits(
        max_bytes=int(resource_config.served_index_cache_max_bytes),
        low_bytes=int(resource_config.served_index_cache_low_bytes),
        min_free_percent=float(resource_config.served_index_cache_min_free_percent),
        target_free_percent=float(
            resource_config.served_index_cache_target_free_percent
        ),
        min_idle_seconds=float(resource_config.served_index_cache_min_idle_seconds),
    )


def start_served_index_cache(app: Any, resource_config: Any) -> Optional[Any]:
    """Start this pod's served-index cache (``server/services/served_index_cache.py``):
    LRU eviction of the checkouts it served, by disk usage. Installed for the
    readers' hooks and parked on ``app.state.served_index_cache``. Every pod
    runs one (the disk is pod-local). None -- no thread, no hook -- under the
    local store. Invalid watermarks raise."""
    from code_indexer.server.services.served_index_cache import (
        ServedIndexCache,
        install_served_index_cache,
        require_positive_interval,
    )

    app.state.served_index_cache = None
    install_served_index_cache(None)
    if server_index_store().is_local:
        return None
    require_positive_interval(
        "served_index_cache_interval_seconds",
        resource_config.served_index_cache_interval_seconds,
    )
    limits = build_served_index_cache_limits(resource_config)
    manager = app.state.activated_repo_manager
    golden = getattr(app.state, "golden_repos_dir", None)
    cache = ServedIndexCache(
        activated_repos_dir=str(manager.activated_repos_dir),
        golden_repos_dir=golden,
        limits=limits,
        query_tracker=getattr(app.state, "query_tracker", None),
        build_locked=_golden_write_locked(app),
        # get_cidx_meta_path: the reader leases' root under golden-repos.
        lease_root=Path(golden) / "cidx-meta" if golden else None,
    )
    install_served_index_cache(cache)
    cache.start(resource_config.served_index_cache_interval_seconds)
    app.state.served_index_cache = cache
    logger.info(
        "Served-index cache started on this pod (max %d bytes, low %d bytes, "
        "min free %.1f%%, target free %.1f%%)",
        limits.max_bytes,
        limits.low_bytes,
        limits.min_free_percent,
        limits.target_free_percent,
    )
    return cache


def stop_served_index_cache(app: Any) -> None:
    from code_indexer.server.services.served_index_cache import (
        install_served_index_cache,
    )

    cache = getattr(app.state, "served_index_cache", None)
    install_served_index_cache(None)
    if cache is not None:
        cache.stop()
