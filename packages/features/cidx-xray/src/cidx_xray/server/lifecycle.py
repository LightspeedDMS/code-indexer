"""``on_startup`` / ``on_shutdown`` of the ``xray`` feature.

What the core's lifespan and service init used to wire for X-Ray, now done by
the feature when the core starts it (once per process, off the event loop):

=================================  ===========================================
Startup step                       Was (core)
=================================  ===========================================
dedicated X-Ray thread pool        ``lifespan.py`` (Bug #1070)
execution backend selection,       ``lifespan.py`` +
placement host                     ``startup/xray_backend_wiring.py``
cell limiter, live resize on a     ``lifespan.py`` + ``config_service``
``xray_worker_threads`` change     (``_update_xray_setting``)
graph wire-file cache under the    ``service_init.py`` (Story #1787 AC14)
memory governor
K-calibration provider             ``service_init.py`` + ``storage/factory.py``
                                   (Story #1787 AC16)
=================================  ===========================================

An invalid ``xray_backend``, or ``delegated`` without a usable plugin, raises:
the core turns that into a refused startup, so X-Ray never quietly runs a
caller's evaluator in this pod. Everything installed here is process wiring
of this node; state shared across requests stays in the job tracker and the
payload cache (cluster-aware state).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable, List, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import CONFIG, STORAGE
from cidx_sdk.capability_types import (
    STORAGE_POSTGRES,
    ConfigCapability,
    StorageCapability,
)

from cidx_xray._platform import capabilities, server_platform
from cidx_xray.config import BACKEND, DEFAULT_WORKER_THREADS, WORKER_THREADS
from cidx_xray.server import runtime

logger = logging.getLogger(__name__)

#: The cell limiter's bounds (the setting itself is validated to 1..8).
CELL_LIMIT_MIN = 1
CELL_LIMIT_MAX = 50
#: Node-local TTL for the K-calibration read cache (Story #1787 AC16): keeps
#: calibration reads off any admission hot path without letting nodes
#: diverge (the store is the cluster-shared database).
K_CALIBRATION_TTL_SECONDS = 300.0


def _worker_threads(section: Any) -> int:
    try:
        return int(section[WORKER_THREADS])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_WORKER_THREADS


class XrayRuntime:
    """What one start of the feature installed, so stop undoes exactly it."""

    def __init__(self) -> None:
        self._cleanups: List[Callable[[], None]] = []

    def start(self, ctx: FeatureContext) -> None:
        """Install everything; on a failure, undo what was installed and
        re-raise (the core then refuses startup)."""
        try:
            self._start(ctx)
        except BaseException:
            self.stop()
            raise

    def _start(self, ctx: FeatureContext) -> None:
        from cidx_xray.server.backend_wiring import (
            FeaturePlacementHost,
            build_configured_xray_execution_backend,
        )

        runtime.CONTEXT.install(ctx)
        self._cleanups.append(runtime.CONTEXT.uninstall)
        config = cast(ConfigCapability, ctx.capability(CONFIG))
        section = runtime.config_section()
        platform = server_platform()

        # Bug #1070: X-Ray compute runs on its own pool, never the job
        # manager's.
        pool_size = platform.xray_executor_pool_size()
        executor = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="xray")
        runtime.EXECUTOR.install(executor)
        self._cleanups.append(lambda: _shutdown_executor(executor))
        logger.info("xray executor started (max_workers=%d) (Bug #1070)", pool_size)

        # Where evaluators execute (xray_backend, a bootstrap key). Outside
        # any try: a refused selection must stop startup.
        selection = SimpleNamespace(
            xray_backend=section.get(BACKEND),
            content_backend=getattr(
                config.runtime_config(), "content_backend", "local"
            ),
        )
        configured = build_configured_xray_execution_backend(selection)
        if configured is not None:
            runtime.EXECUTION_BACKEND.install(configured)
            self._cleanups.append(runtime.EXECUTION_BACKEND.uninstall)
        capabilities.XRAY_PLACEMENT_HOST.install(FeaturePlacementHost())
        self._cleanups.append(capabilities.XRAY_PLACEMENT_HOST.uninstall)

        # One gate for every X-Ray scan on this node, sized by the setting
        # and resized when this process commits a change to it.
        limit = _worker_threads(section)
        limiter = platform.resizable_limiter(
            initial=limit, k_min=CELL_LIMIT_MIN, k_max=CELL_LIMIT_MAX
        )
        runtime.CELL_LIMITER.install(limiter)
        self._cleanups.append(runtime.CELL_LIMITER.uninstall)
        self._cleanups.append(config.subscribe(_resize_on_change(limiter)))
        logger.info(
            "xray cell limiter started (limit=%d) — shared gate for all xray "
            "scan executions",
            limit,
        )

        self._start_graph_governor(ctx, platform)

    def _start_graph_governor(self, ctx: FeatureContext, platform: Any) -> None:
        from cidx_xray.server.graph_governor.cache_proxy import (
            XrayGraphCacheProxy,
            clear_xray_graph_cache,
            set_xray_graph_cache,
        )
        from cidx_xray.server.graph_governor.k_calibration_postgres import (
            XrayGraphKCalibrationPostgresBackend,
        )
        from cidx_xray.server.graph_governor.k_calibration_store import (
            SqliteKCalibrationBackend,
            TTLCachedKProvider,
            clear_xray_k_provider,
            set_xray_k_provider,
        )

        # Story #1787 AC14: the wire-file cache is governor-visible for
        # YELLOW proactive eviction, beside the core's own caches.
        graph_cache = XrayGraphCacheProxy()
        set_xray_graph_cache(graph_cache)
        self._cleanups.append(clear_xray_graph_cache)
        self._cleanups.append(platform.attach_governed_cache(graph_cache))

        # Story #1787 AC16: the K provider over the cluster-shared database.
        storage = cast(StorageCapability, ctx.capability(STORAGE))
        store: Any = (
            XrayGraphKCalibrationPostgresBackend(storage)
            if storage.mode == STORAGE_POSTGRES
            else SqliteKCalibrationBackend(storage.connection)
        )
        set_xray_k_provider(
            TTLCachedKProvider(store=store, ttl_seconds=K_CALIBRATION_TTL_SECONDS)
        )
        self._cleanups.append(clear_xray_k_provider)

    def stop(self) -> None:
        """Undo every start step, newest first. Never raises."""
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                cleanup()
            except Exception as exc:  # shutdown continues
                logger.warning("xray shutdown step failed (%s)", type(exc).__name__)


def _shutdown_executor(executor: ThreadPoolExecutor) -> None:
    runtime.EXECUTOR.uninstall()
    executor.shutdown(wait=False)


def _resize_on_change(limiter: Any) -> Callable[[], None]:
    """A config-commit callback resizing *limiter* to ``xray_worker_threads``."""

    def _resize() -> None:
        try:
            limit = _worker_threads(runtime.config_section())
            if limit != limiter.limit:
                limiter.set_limit(limit)
        except Exception as exc:  # the next commit or restart converges
            logger.warning(
                "Failed to live-reload xray cell limiter for xray_worker_threads: %s",
                exc,
            )

    return _resize
