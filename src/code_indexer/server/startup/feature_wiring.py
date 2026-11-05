"""Feature add-on wiring for the CIDX server (modular plan phases 3 and 4a).

Two moments:

1. App creation -- :func:`wire_features`, called by ``server/app.py``
   ``create_app()`` right after the core's routes are registered and its
   schema is migrated. Reads the bootstrap key ``enabled_features``,
   discovers, loads and validates the features, hands them the core's
   capabilities, then consumes every contribution hook:

   =====================  ====================================================
   hook                   consumed by
   =====================  ====================================================
   ``migrations()``       ``features/migrations.py`` (PG runner lock / SQLite)
   ``tools()``            ``features/tools.py`` (TOOL_REGISTRY, handlers, docs)
   ``routers()``          ``features/web.py`` ``install_feature_web``
   ``ui_pages()``         ``features/web.py`` (pages, static, nav)
   ``config_section()``   ``features/config.py``; doors in ``features/web.py``
                         (mounted by ``app_wiring.create_fastapi_app``)
   ``background_jobs()``  ``features/jobs.py`` (on demand and scheduled)
   ``job_types()``,       ``features/dispatch_policy.py`` (dashboard, job
   tool timeout policy    manager, MCP dispatch timeout)
   ``index_builders()``   ``features/index_builders.py`` -> the kernel
                         registry the indexing pipeline reads
   ``web_query_modes()``  ``features/query_modes.py`` (Web UI query page)
   ``search_augmenters``  ``features/search_augmenters.py`` (MCP code search
                         response sections)
   ``health()``           ``features/registry.py`` health block (DEGRADED only)
   =====================  ====================================================

   Not inside any try: an enabled feature that is missing, refuses this SDK,
   lacks a capability, fails validation or fails a migration stops startup
   (the same fail-loud policy as a delegated seam with no plugin). Runs
   synchronously like the core's own schema migrations in
   ``initialize_services`` -- before the server accepts any request.
2. Lifespan (``startup/lifespan.py``):

   * :func:`load_configured_features` -- publishes the registry for the
     health surfaces (builds one when the app was not made by
     ``create_app``);
   * :func:`start_features` -- just before the lifespan yields: every
     ``on_startup``, then the recurring-job scheduler;
   * :func:`stop_features` -- first thing after the yield. Never raises.

   Their blocking work runs in a worker thread, never on the event loop.

With ``enabled_features`` empty (the default) nothing is imported or called;
the visible differences are the ``features`` health block and the (empty)
feature-settings doors.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

import anyio

from code_indexer.server.features.loader import (
    FeatureCandidate,
    discover_installed_features,
    load_features,
    normalize_enabled_features,
)
from code_indexer.server.features.registry import (
    CapabilityProvider,
    FeatureRegistry,
    set_active_registry,
)

logger = logging.getLogger(__name__)


def build_feature_registry(
    config: Any,
    *,
    discover: Callable[[], Dict[str, FeatureCandidate]] = discover_installed_features,
    capabilities: Optional[Mapping[str, CapabilityProvider]] = None,
) -> FeatureRegistry:
    """Discover, load and validate the features *config* enables.

    ``config`` is read with ``getattr`` so a partial or missing config means
    no features. *capabilities* are the providers handed to the features
    (``features/capabilities.py``); none when omitted. Blocking; call off
    the event loop.

    Raises:
        cidx_sdk.FeatureLoadError: any refusal (see ``features/loader.py``).
    """
    from code_indexer import __version__

    enabled = normalize_enabled_features(getattr(config, "enabled_features", None))
    provided = dict(capabilities or {})
    candidates = discover()
    loaded = load_features(
        enabled,
        candidates=candidates,
        provided_capabilities=frozenset(provided),
    )
    registry = FeatureRegistry(
        loaded,
        installed=list(candidates),
        capabilities=provided,
        core_version=__version__,
    )
    if loaded:
        logger.info(
            "Feature add-ons loaded: %s",
            ", ".join(
                f"{item.manifest.name} {item.manifest.version}" for item in loaded
            ),
        )
    else:
        logger.info("Feature add-ons: none enabled (%d installed)", len(candidates))
    return registry


def wire_features(
    app: Any,
    services: Mapping[str, Any],
    *,
    discover: Callable[[], Dict[str, FeatureCandidate]] = discover_installed_features,
) -> FeatureRegistry:
    """Load the enabled features into *app* and consume their contributions.

    *services* is ``initialize_services()``'s result (server config, storage
    selection, database path). Stores the registry on
    ``app.state.feature_registry``; the lifespan publishes it.

    Raises:
        cidx_sdk.FeatureLoadError: any refusal; nothing is half-installed in
            the MCP registries (tools are validated before any is added).
    """
    from code_indexer.server.features.capabilities import build_core_capabilities
    from code_indexer.server.features.dispatch_policy import install_dispatch_policy
    from code_indexer.server.features.index_builders import (
        install_feature_index_builders,
    )
    from code_indexer.server.features.migrations import run_feature_migrations
    from code_indexer.server.features.query_modes import install_feature_query_modes
    from code_indexer.server.features.search_augmenters import (
        install_feature_search_augmenters,
    )
    from code_indexer.server.features.tools import install_feature_tools
    from code_indexer.server.features.web import install_feature_web

    config = services.get("server_config")
    registry = build_feature_registry(
        config,
        discover=discover,
        capabilities=build_core_capabilities(app, services),
    )
    app.state.feature_registry = registry
    run_feature_migrations(
        registry.loaded,
        storage_mode=str(services.get("storage_mode") or "sqlite"),
        postgres_dsn=str(getattr(config, "postgres_dsn", "") or ""),
        sqlite_path=str(services.get("db_path_str") or ""),
    )
    install_feature_tools(registry.loaded)
    install_dispatch_policy(registry.loaded)
    install_feature_index_builders(registry.loaded)
    install_feature_query_modes(registry.loaded)
    install_feature_search_augmenters(registry.loaded)
    install_feature_web(app, registry.loaded)
    return registry


async def load_configured_features(app: Any, config: Any) -> FeatureRegistry:
    """Publish the app's registry for the health surfaces.

    ``create_app`` built it (:func:`wire_features`); an app made another way
    gets one built here, off the event loop, without consuming hooks.
    """
    registry: Optional[FeatureRegistry] = getattr(app.state, "feature_registry", None)
    if registry is None:
        registry = await anyio.to_thread.run_sync(build_feature_registry, config)
        app.state.feature_registry = registry
    set_active_registry(registry)
    return registry


async def start_features(app: Any) -> None:
    """Run every loaded feature's ``on_startup`` once, then start the
    recurring-job scheduler (worker thread)."""
    from code_indexer.server.features.jobs import FeatureJobScheduler

    registry: Optional[FeatureRegistry] = getattr(app.state, "feature_registry", None)
    if registry is None:
        return
    try:
        await anyio.to_thread.run_sync(registry.start)
    except BaseException:
        # registry.start already stopped what it started; startup is ending.
        set_active_registry(None)
        raise
    scheduler = getattr(app.state, "feature_job_scheduler", None)
    if scheduler is None:
        scheduler = FeatureJobScheduler(registry)
        app.state.feature_job_scheduler = scheduler
    scheduler.start()


async def stop_features(app: Any) -> None:
    """Stop the scheduler, run ``on_shutdown`` for started features and
    unpublish. Never raises."""
    registry: Optional[FeatureRegistry] = getattr(app.state, "feature_registry", None)
    scheduler = getattr(app.state, "feature_job_scheduler", None)
    try:
        if scheduler is not None:
            await anyio.to_thread.run_sync(scheduler.stop)
        if registry is not None:
            await anyio.to_thread.run_sync(registry.stop)
    except Exception as exc:
        logger.warning("feature shutdown failed (%s)", type(exc).__name__)
    finally:
        set_active_registry(None)
