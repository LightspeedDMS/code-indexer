"""What the ``depmap`` feature wires at ``on_startup`` and removes at
``on_shutdown`` (moved from the core's lifespan, modular phase 4g).

* The feature context (``runtime.CONTEXT``).
* The dependency-map service (Story #193): tracking backend (PostgreSQL in
  cluster mode, the server SQLite database solo), analyzer, cluster dedup
  pool, repair invoker (Story #927), its scheduler, the job manager's cancel
  handlers (Bug #1153), kept in the feature's slot ``runtime.SERVICE``.
  Startup cleanups of this pod's dependency-map directory (stale staging
  directory, eager output directory, this node's stale sentinels) are skipped
  under a delegated content backend.
* The description fact check (Story #724 v2) in the core's kernel slot
  ``DESCRIPTION_FACT_CHECK_HOST``: the analyzer's verification pass, enabled
  by ``dep_map_fact_check_enabled``.

A failure to start the service is logged and leaves the server running
without it (as in the lifespan before the move); the tools and pages then
answer that the dependency map is unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from cidx_sdk import FeatureContext

from cidx_depmap._platform import (
    DependencyMapTrackingBackend,
    DescriptionRefreshTrackingBackend,
    capabilities,
    get_config_service,
    server_platform,
)
from cidx_depmap.server import runtime
from cidx_depmap.server.dependency_map_analyzer import DependencyMapAnalyzer
from cidx_depmap.server.dependency_map_service import DependencyMapService

logger = logging.getLogger(__name__)


def _ensure_dep_map_output_dir(golden_repos_dir: Union[str, Path]) -> Path:
    """Eagerly create the dependency-map output directory (Bug #1621).

    Without this, {golden_repos_dir}/cidx-meta/dependency-map/ is only ever
    created lazily, as a side effect of the first analysis run. That meant
    `_get_dep_map_output_dir()` (web/routes.py) returned None for
    every request on a never-before-visited node -- including the very
    request that would otherwise trigger the directory's creation -- causing
    a spurious "Dashboard analysis infrastructure unavailable" error on the
    first dashboard load. The next poll a few seconds later would then
    succeed once something else happened to create the directory.

    This is a single, trivial O(1) directory creation, unconditionally safe
    to run once at startup regardless of whether the dependency-map feature
    is enabled -- it does not scale with fleet size (unlike golden-repo-count
    work, which must never run unbacked on the startup path).

    Returns the (created, or already-existing) directory path.
    """
    dep_map_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"
    dep_map_dir.mkdir(parents=True, exist_ok=True)
    return dep_map_dir


def _dep_map_dir_is_on_this_disk() -> bool:
    """Whether cidx-meta's dependency map is on this pod's disk (local content
    backend). Under a delegated backend it is shared content behind
    workspace-mcp and analysis is refused here (dep_map_delegated), so the
    startup staging cleanup, eager output directory and stale-sentinel sweep
    -- all on this pod's directory -- are skipped."""
    from cidx_depmap._platform import is_content_seam_delegated

    return not is_content_seam_delegated()


def _make_dep_map_repair_invoker_fn(
    dep_map_dir: Path,
    tracking_backend: Any,
    job_tracker: Any,
    dep_map_service: Any,
) -> Callable[[str], None]:
    """Build the repair invoker closure for DependencyMapService (Story #927).

    Extracted as a module-level factory so the wiring can be tested directly
    (Codex Pass 3 regression guard).

    Story #927 Codex Pass 4-5: the lifespan startup must construct
    DependencyMapService FIRST, then call this factory with the constructed
    service instance, then bind the returned closure via
    ``dependency_map_service.set_repair_invoker_fn(...)`` BEFORE
    ``start_scheduler()``. The full ordering is:
        1. dependency_map_service = DependencyMapService(...)
        2. _dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(...)
        3. dependency_map_service.set_repair_invoker_fn(_dep_map_repair_invoker_fn)
        4. dependency_map_service.start_scheduler()

    This late-binding pattern is required because the closure captures
    ``dep_map_service`` by parameter binding (Python by-value), so the service
    must exist at factory call time.

    Args:
        dep_map_dir: Path to the dependency-map directory (captured by closure).
        tracking_backend: DependencyMapTrackingBackend for job status updates.
        job_tracker: JobTracker for unified job tracking.
        dep_map_service: DependencyMapService instance (provides repo metadata + analyzer).

    Returns:
        A callable(job_id: str) -> None that delegates to _execute_repair_body.
    """

    def _invoker(job_id: str) -> None:
        from cidx_depmap.server.web.routes import _execute_repair_body

        _execute_repair_body(
            job_id=job_id,
            output_dir=dep_map_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=None,
            dep_map_service=dep_map_service,  # Story #927 Pass 2: executor needs repo metadata + analyzer
        )

    return _invoker


class DescriptionFactCheck:
    """``kernel.capabilities.DescriptionFactCheckHost``: the analyzer's
    verification pass over a generated repository description."""

    def enabled(self) -> bool:
        return bool(runtime.settings()["dep_map_fact_check_enabled"])

    def verify(
        self,
        document_path: Path,
        repo_list: List[Dict[str, Any]],
        golden_repos_dir: Path,
        cidx_meta_path: Path,
    ) -> bool:
        settings = runtime.settings()
        analyzer = DependencyMapAnalyzer(
            golden_repos_root=Path(golden_repos_dir),
            cidx_meta_path=cidx_meta_path,
            pass_timeout=int(settings["dependency_map_pass_timeout_seconds"]),
        )
        config = runtime.DepMapClaudeConfig(
            get_config_service().get_config().claude_integration_config
        )
        return bool(
            analyzer.invoke_verification_pass(
                document_path=document_path, repo_list=repo_list, config=config
            )
        )


class DepMapRuntime:
    """The started feature's wiring (one per process)."""

    def __init__(self) -> None:
        self._service: Optional[Any] = None

    def start(self, ctx: FeatureContext) -> None:
        runtime.CONTEXT.install(ctx)
        capabilities.DESCRIPTION_FACT_CHECK_HOST.install(DescriptionFactCheck())
        app = server_platform().app_attr("app")
        state = getattr(app, "state", None)
        if state is None:
            logger.warning("dependency map not started: no server app")
            return
        try:
            self._service = _start_service(state)
            runtime.SERVICE.install(self._service)
        except Exception as exc:
            # Logged, never blocking startup (as in the lifespan before).
            logger.warning(
                "Failed to initialize dependency map scheduler: %s", exc, exc_info=True
            )

    def stop(self) -> None:
        service, self._service = self._service, None
        if service is not None:
            try:
                service.stop_scheduler()
                logger.info("Dependency map scheduler stopped")
            except Exception as exc:
                logger.error(
                    "Error stopping dependency map scheduler: %s", exc, exc_info=True
                )
        runtime.SERVICE.uninstall()
        capabilities.DESCRIPTION_FACT_CHECK_HOST.uninstall()
        runtime.CONTEXT.uninstall()


def _start_service(state: Any) -> Any:
    """Build, wire and start the dependency-map service over the server's
    *state*; returns it (``DepMapRuntime.start`` installs it in
    ``runtime.SERVICE``)."""
    golden_repo_manager = state.golden_repo_manager
    backend_registry = getattr(state, "backend_registry", None)
    storage_mode = getattr(state, "storage_mode", "sqlite")
    server_data_dir = getattr(state, "server_data_dir")
    golden_repos_dir = getattr(state, "golden_repos_dir")
    mcp_registration_service = getattr(state, "mcp_registration_service", None)
    job_tracker = getattr(state, "job_tracker", None)
    background_job_manager = state.background_job_manager
    global_lifecycle_manager = getattr(state, "global_lifecycle_manager", None)
    logger.info("Server startup: Initializing dependency map scheduler")

    # Get dependencies
    config_service = get_config_service()
    server_config = config_service.get_config()
    db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
    golden_repos_manager = golden_repo_manager

    # Create tracking backend
    if backend_registry is not None:
        tracking_backend = backend_registry.dependency_map_tracking
    else:
        tracking_backend = DependencyMapTrackingBackend(db_path)
    tracking_backend.cleanup_stale_status_on_startup()

    # Create description refresh tracking backend for lifecycle backfill (Epic #725).
    # This is the DescriptionRefreshTrackingBackend (description_refresh_tracking table),
    # which is separate from DependencyMapTrackingBackend. Must be passed explicitly
    # because DependencyMapService only holds DependencyMapTrackingBackend by default.
    if backend_registry is not None:
        description_refresh_tracking_backend = (
            backend_registry.description_refresh_tracking
        )
    else:
        description_refresh_tracking_backend = DescriptionRefreshTrackingBackend(
            db_path
        )

    # See _dep_map_dir_is_on_this_disk: skipped under delegated.
    _dep_map_shared = not _dep_map_dir_is_on_this_disk()

    # Bug #383: Clean stale staging directory on server startup.
    # A prior crashed analysis may have left dependency-map.staging/ behind.
    # RefreshScheduler would index it and bake it into versioned snapshots,
    # polluting semantic search. Remove it before the scheduler starts.
    try:
        staging_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map.staging"
        if not _dep_map_shared and staging_dir.exists():
            import shutil as _shutil

            _shutil.rmtree(staging_dir)
            logger.info(
                "Cleaned stale dependency-map.staging directory on startup (Bug #383)",
            )
    except Exception as _staging_err:
        logger.debug(f"Staging dir startup cleanup failed (non-fatal): {_staging_err}")

    # Create analyzer
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    analyzer = DependencyMapAnalyzer(
        golden_repos_root=Path(golden_repos_dir),
        cidx_meta_path=cidx_meta_path,
        pass_timeout=int(runtime.settings()["dependency_map_pass_timeout_seconds"]),
        mcp_registration_service=mcp_registration_service,
        analysis_model=(
            server_config.golden_repos_config.analysis_model
            if server_config.golden_repos_config
            else "opus"
        ),
    )

    # Story #927: cluster dedup pool (None = solo mode)
    _dep_map_pg_pool = (
        (backend_registry.critical_connection_pool or backend_registry.connection_pool)
        if storage_mode == "postgres" and backend_registry is not None
        else None
    )

    # Story #927: closures capture dep_map_dir, tracking_backend, job_tracker
    _dep_map_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"

    # Bug #1621: eagerly create the dep-map output directory here, before
    # anything below checks for its existence. Without this, a
    # never-before-visited node has no cidx-meta/dependency-map/ on disk
    # until the first analysis run happens to create it lazily -- so the
    # very first dashboard HTMX poll sees `_get_dep_map_output_dir()`
    # return None and renders a spurious "infrastructure unavailable"
    # error, even though the directory would exist moments later.
    if not _dep_map_shared:
        _ensure_dep_map_output_dir(golden_repos_dir)

    # Bug #1040 follow-up: clean stale sentinel lock files on startup.
    # Server restart kills analysis threads but leaves sentinel files on
    # disk, blocking the dep-map UI with a stuck "Processing..." state.
    # CLUSTER SAFETY (Codex SENTINEL-1): only delete sentinels owned by
    # THIS node. Sibling nodes may have live jobs behind their sentinels.
    if not _dep_map_shared and _dep_map_dir.exists():
        import glob as _glob_sentinel
        import json as _json_sentinel
        import socket as _socket_sentinel

        _this_node_id = ""
        try:
            _sc_path = Path(server_data_dir) / "config.json"
            if _sc_path.exists():
                from cidx_depmap._platform import configured_node_id

                _this_node_id = configured_node_id(
                    _json_sentinel.loads(_sc_path.read_text(encoding="utf-8"))
                )
            if not _this_node_id:
                _this_node_id = _socket_sentinel.gethostname()
        except Exception as exc:
            logger.warning(
                "lifespan: swallowed %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            _this_node_id = _socket_sentinel.gethostname()

        for _stale_lock in _glob_sentinel.glob(str(_dep_map_dir / "_active_*.lock")):
            try:
                _lock_text = Path(_stale_lock).read_text(encoding="utf-8")
                _lock_data = _json_sentinel.loads(_lock_text)
                _lock_node = _lock_data.get("node_id", "")
                if _lock_node and _lock_node != _this_node_id:
                    logger.info(
                        "Startup: keeping sentinel %s (owned by %s, we are %s)",
                        _stale_lock,
                        _lock_node,
                        _this_node_id,
                    )
                    continue
                Path(_stale_lock).unlink()
                logger.info(
                    "Startup: removed stale sentinel %s (owned by %s)",
                    _stale_lock,
                    _lock_node or "unknown",
                )
            except (_json_sentinel.JSONDecodeError, KeyError):
                Path(_stale_lock).unlink(missing_ok=True)
                logger.info("Startup: removed corrupt sentinel %s", _stale_lock)
            except OSError as _e:
                logger.warning(
                    "Startup: failed to remove stale sentinel %s: %s",
                    _stale_lock,
                    _e,
                )

    def _dep_map_health_check_fn():
        from cidx_depmap.server.dep_map_health_detector import DepMapHealthDetector

        return DepMapHealthDetector().detect(_dep_map_dir)

    # Story #927 Codex Pass 4: construct service FIRST so the closure can
    # capture the real instance, not the pre-construction None placeholder.
    dependency_map_service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=runtime.DepMapConfigManager(config_service),
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=(
            global_lifecycle_manager.refresh_scheduler
            if global_lifecycle_manager
            else None
        ),
        job_tracker=job_tracker,  # Story #312: Unified job tracking (Epic #261)
        description_refresh_tracking_backend=description_refresh_tracking_backend,  # Epic #725
        pg_pool=_dep_map_pg_pool,  # Story #927: cluster dedup lock
        health_check_fn=_dep_map_health_check_fn,  # Story #927: anomaly gate
        repair_invoker_fn=None,  # late-bound below via set_repair_invoker_fn
        storage_mode=storage_mode,  # Story #927 Pass 2: anti-fallback guard
    )

    # Build the closure with the real constructed service instance
    _dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(
        dep_map_dir=_dep_map_dir,
        tracking_backend=tracking_backend,
        job_tracker=job_tracker,
        dep_map_service=dependency_map_service,  # Story #927 Pass 4: now the real instance
    )

    # Late-bind the closure into the service
    dependency_map_service.set_repair_invoker_fn(_dep_map_repair_invoker_fn)

    # Startup migration: delete oversized CLAUDE.md from pre-v10.10 deployments.
    # Pre-v10.7 code wrote the entire repo catalogue into CLAUDE.md (context overflow).
    # Pre-v10.10 code renamed it to dep_map_repo_catalogue.md.
    # v10.10+ no longer creates either file — delete any oversized CLAUDE.md that
    # remains from a prior version so it does not bloat Claude CLI context.
    _gr_root = Path(golden_repos_manager.golden_repos_dir)
    _old_claude_md = _gr_root / "CLAUDE.md"
    _CLAUDE_MD_MAX_SAFE_SIZE = 10_000  # 10KB; new CLAUDE.md is ~500 bytes
    try:
        if (
            _old_claude_md.exists()
            and _old_claude_md.stat().st_size > _CLAUDE_MD_MAX_SAFE_SIZE
        ):
            logger.info(
                "Startup migration: deleting oversized CLAUDE.md (%d bytes) at %s",
                _old_claude_md.stat().st_size,
                _gr_root,
            )
            _old_claude_md.unlink()
    except Exception as _mig_err:
        logger.warning(
            "Startup migration: failed to delete oversized CLAUDE.md (non-fatal): %s",
            _mig_err,
        )

    # Start scheduler — invoker is bound, safe to start
    dependency_map_service.start_scheduler()

    # Bug #1153: Wire dep-map cooperative-cancellation handlers so that
    # BackgroundJobManager.cancel_job() can stop the dep-map worker thread
    # and release the SharedJobSentinel when a cancel is requested via the
    # generic MCP cancel_job tool.  Without this, the worker keeps running
    # and new triggers 409 forever.
    background_job_manager.register_cancel_handler(
        "dependency_map_full",
        dependency_map_service.cancel_running_analysis,
    )
    background_job_manager.register_cancel_handler(
        "dependency_map_delta",
        dependency_map_service.cancel_running_analysis,
    )

    if runtime.settings()["dependency_map_enabled"]:
        logger.info(
            f"Dependency map scheduler started "
            f"(interval: {runtime.settings()['dependency_map_interval_hours']}h)",
        )
    else:
        logger.info(
            "Dependency map scheduler is disabled (can be enabled in Web UI)",
        )

    return dependency_map_service
