"""Shared-app binding guard helpers for the Phase 3 e2e server suite.

A second app built in the same process must not strip the shared session
app's process-wide wiring. These helpers read, validate and restore every
such binding; the autouse fixtures that drive them live in
``tests/e2e/server/conftest.py``.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Guard: a second app must not strip the shared app's process-wide wiring
# ---------------------------------------------------------------------------

# Correct with one app per process (production), a hazard here:
# - create_app() reassigns server.app's module-level managers, which MCP
#   handlers read (e.g. app_module.user_manager) -- a throwaway app built on
#   an isolated data dir leaves them on ANOTHER store;
# - the lifespan binds its AuditLogService as the PROCESS-WIDE audit capture
#   sink, hands it to the module-level password audit logger, and installs
#   its GroupAccessManager as the groups router's module-level manager;
#   shutdown stops that service and unbinds the sink -- leaving the shared
#   app with no bound sink and a groups router writing through a STOPPED
#   service (every later capture is a counted drop plus an ERROR line).
# Same hazard class as the auth guard above; see
# test_23_shared_app_globals_isolation.py.

# The names create_app() assigns through its `global` statements.
_GUARDED_APP_MODULE_ATTRS = (
    "jwt_manager",
    "user_manager",
    "refresh_token_manager",
    "golden_repo_manager",
    "background_job_manager",
    "job_tracker",
    "activated_repo_manager",
    "repository_listing_manager",
    "semantic_query_manager",
    "workspace_cleanup_service",
    "_server_hnsw_cache",
    "_server_fts_cache",
)


_Binding = Tuple[Callable[[], Any], Callable[[Any], None]]


def _module_attr(module: Any, name: str, write: Callable[[Any], None]) -> _Binding:
    # vars(): read the bound global without any PEP 562 lazy getter.
    return (lambda: vars(module).get(name), write)


def _audit_bindings() -> Dict[str, _Binding]:
    from code_indexer.server.auth.audit_logger import password_audit_logger
    from code_indexer.server.routers import groups
    from code_indexer.server.services import audit_capture

    def read_audit_sink() -> Any:
        try:
            sink = audit_capture.resolve_audit_sink("e2e-app-wiring")
        except audit_capture.AuditServiceUnresolvable:
            sink = None
        return (sink, audit_capture.audit_node_id())

    def write_audit_sink(value: Any) -> None:
        audit_capture.bind_audit_service(value[0], node_id=value[1])

    def write_password_logger(svc: Any) -> None:
        # set_audit_service rebuilds the logger's handlers: only on a change.
        if getattr(password_audit_logger, "_audit_service", None) is not svc:
            password_audit_logger.set_audit_service(svc)

    return {
        # lifespan.py:1235 bind_audit_service / :5968 clear_audit_service
        "audit_capture_sink": (read_audit_sink, write_audit_sink),
        # lifespan.py:1248 set_audit_service / :5970 set_audit_service(None)
        "password_audit_logger": (
            lambda: getattr(password_audit_logger, "_audit_service", None),
            write_password_logger,
        ),
        # lifespan.py:1190 set_group_manager (never cleared)
        "groups_router_manager": _module_attr(
            groups, "_group_manager", groups.set_group_manager
        ),
    }


def _lifespan_service_bindings(state: Any) -> Dict[str, _Binding]:
    from code_indexer.global_repos import meta_description_hook as mdh
    from code_indexer.server.services import (
        coalescer_registry,
        governed_call,
        search_embed_event_emit,
    )
    from code_indexer.server.web import mfa_routes
    from code_indexer.storage import temporal_metadata_backend_registry as tmbr
    from code_indexer.storage.shared import chunk_store_cache_cross_process as cscp

    def write_temporal_factory(factory: Any) -> None:
        if factory is None:
            tmbr.clear_temporal_metadata_backend_factory()
        else:
            tmbr.set_temporal_metadata_backend_factory(factory)

    def state_attr(name: str) -> _Binding:
        return (
            lambda: getattr(state, name, None),
            lambda value: setattr(state, name, value),
        )

    return {
        # lifespan.py:852 set_xray_executor mirrors onto the singleton app
        # (the shared app) / :5339 _xray_executor.shutdown()
        "xray_executor": state_attr("xray_executor"),
        # lifespan.py:886 set_xray_cell_limiter (same mirror)
        "xray_cell_limiter": state_attr("xray_cell_limiter"),
        # lifespan.py:1057 set_... / :5284 writer.stop(), :5285 clear_...
        "search_embed_event_writer": (
            search_embed_event_emit.get_search_embed_event_writer,
            search_embed_event_emit.set_search_embed_event_writer,
        ),
        # lifespan.py:1650 register_... / :1692, :5585 reset_registered_...
        "payload_cache_registration": (
            cscp.get_registered_payload_cache,
            cscp.register_payload_cache,
        ),
        # lifespan.py:1805 set_... (postgres only) / :5386 clear_...
        "temporal_metadata_factory": (
            tmbr.get_temporal_metadata_backend_factory,
            write_temporal_factory,
        ),
        # lifespan.py:2119 / :2120 / :2135 / :2148; :5851 set_debouncer(None)
        "meta_hook_tracking_backend": _module_attr(
            mdh, "_tracking_backend", mdh.set_tracking_backend
        ),
        "meta_hook_scheduler": _module_attr(mdh, "_scheduler", mdh.set_scheduler),
        "meta_hook_refresh_scheduler": _module_attr(
            mdh, "_refresh_scheduler", mdh.set_refresh_scheduler
        ),
        "meta_hook_debouncer": _module_attr(mdh, "_debouncer", mdh.set_debouncer),
        # lifespan.py:3378 set_totp_service / :5458 set_totp_service(None)
        "totp_service": (mfa_routes.get_totp_service, mfa_routes.set_totp_service),
        # lifespan.py:4990 set_coalescer_registry / :5370 clear_...
        "coalescer_registry": (
            coalescer_registry.get_coalescer_registry,
            coalescer_registry.set_coalescer_registry,
        ),
        # lifespan.py:5030 set_query_embedding_cache / :5420 stop, :5435 clear
        "query_embedding_cache": (
            governed_call.get_query_embedding_cache,
            governed_call.set_query_embedding_cache,
        ),
    }


def _auth_store_sqlite_path(store: Any, attr: str) -> str:
    """The SQLite file a process-wide auth store reads and writes.

    Invariant: token revocations (TokenBlacklist._sqlite_db_path) and
    elevation windows (ElevatedSessionManager._db_path) live in the SHARED
    app's cidx_server.db; every create_app() repoints both
    (service_init.py:261, :269).  Neither store exposes its path, so this
    is the one reader of those private attributes -- no fallback: a renamed
    attribute fails here, loudly.
    """
    assert attr in vars(store), f"{type(store).__name__}.{attr} no longer exists"
    return str(vars(store)[attr])


def _repoint_auth_store(store: Any, attr: str, path: str) -> None:
    # set_sqlite_path (re)creates schema: only on a change.
    if _auth_store_sqlite_path(store, attr) != path:
        store.set_sqlite_path(path)


_FILE_CRUD_WRITE_MODE_ATTRS = ("_golden_repos_dir", "_global_write_exceptions")


def _file_crud_write_mode_dirs() -> Tuple[Any, Dict[str, Path]]:
    """(marker dir, write exceptions) the process-wide FileCRUDService uses.

    Invariant: every create_app() rebinds both to ITS golden repos dir
    (service_init.py register_write_exception / set_golden_repos_dir), while
    enter_write_mode writes the marker under the shared app's
    app.state.golden_repos_dir.  Neither has a public reader -- no fallback:
    a renamed attribute fails here, loudly.
    """
    from code_indexer.server.services.file_crud_service import file_crud_service

    state = vars(file_crud_service)
    for attr in _FILE_CRUD_WRITE_MODE_ATTRS:
        assert attr in state, f"FileCRUDService.{attr} no longer exists"
    return (state["_golden_repos_dir"], dict(state["_global_write_exceptions"]))


def _bind_file_crud_write_mode_dirs(value: Tuple[Any, Dict[str, Path]]) -> None:
    from code_indexer.server.services.file_crud_service import file_crud_service

    golden_repos_dir, exceptions = value
    file_crud_service._global_write_exceptions.clear()
    for alias, path in exceptions.items():
        file_crud_service.register_write_exception(alias, path)
    file_crud_service.set_golden_repos_dir(golden_repos_dir)


def _create_app_bindings() -> Dict[str, _Binding]:
    from code_indexer.server import app as app_module
    from code_indexer.server.web import auth as web_auth
    from code_indexer.server.auth.elevated_session_manager import (
        elevated_session_manager,
    )
    from code_indexer.server.routers import repo_categories
    from code_indexer.server.services import dependency_latency_tracker
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )
    from code_indexer.server.services.xray_graph_governor import (
        cache_proxy,
        k_calibration_store,
    )

    bindings: Dict[str, _Binding] = {
        # service_init.py:335 set_instance / lifespan.py:5627 shutdown()
        "latency_tracker": (
            dependency_latency_tracker.get_instance,
            dependency_latency_tracker.set_instance,
        ),
        # service_init.py:195, :426, :687, :721 (rebound by every create_app)
        "xray_graph_cache": (
            cache_proxy.get_xray_graph_cache,
            cache_proxy.set_xray_graph_cache,
        ),
        "xray_k_provider": (
            k_calibration_store.get_xray_k_provider,
            k_calibration_store.set_xray_k_provider,
        ),
        "category_service": _module_attr(
            repo_categories, "_category_service", repo_categories.set_category_service
        ),
        "mcp_self_registration": (
            MCPSelfRegistrationService.get_instance,
            MCPSelfRegistrationService.set_instance,
        ),
        # service_init.py:261 get_token_blacklist().set_sqlite_path(db_path)
        "token_blacklist_sqlite_path": (
            lambda: _auth_store_sqlite_path(
                app_module.get_token_blacklist(), "_sqlite_db_path"
            ),
            lambda path: _repoint_auth_store(
                app_module.get_token_blacklist(), "_sqlite_db_path", path
            ),
        ),
        # inline_routes.py:292 init_session_manager(...) (never cleared): its
        # server_config decides the Secure cookie flag of every Web login.
        "web_session_manager": _module_attr(
            web_auth,
            "_session_manager",
            functools.partial(setattr, web_auth, "_session_manager"),
        ),
        # service_init.py:704 register_write_exception, :708 set_golden_repos_dir
        "file_crud_write_mode_dirs": (
            _file_crud_write_mode_dirs,
            _bind_file_crud_write_mode_dirs,
        ),
        # service_init.py:269 elevated_session_manager.set_sqlite_path(db_path)
        "elevated_session_sqlite_path": (
            lambda: _auth_store_sqlite_path(elevated_session_manager, "_db_path"),
            lambda path: _repoint_auth_store(
                elevated_session_manager, "_db_path", path
            ),
        ),
    }
    # app.py:365-388: the managers create_app() assigns via `global`.
    for name in _GUARDED_APP_MODULE_ATTRS:
        bindings[f"server.app.{name}"] = _module_attr(
            app_module, name, functools.partial(setattr, app_module, name)
        )
    return bindings


def _app_wiring_bindings(shared_app: FastAPI) -> Dict[str, _Binding]:
    """(read, write) for every process-wide binding the shared app installs.

    Comments name where src sets / clears each one.  Not listed, on purpose:
    the memory governor and ConfigService singletons (tests/conftest.py
    resets both around EVERY test), the lazily re-created parallel query
    executor (lifespan.py:5267 resets it; the next query builds a new one),
    per-event-loop and per-app objects, root logging handlers
    (preserve_root_logging_handlers at every throwaway site), and the
    postgres/cluster-only wiring (never runs in solo Phase 3).
    """
    return {
        **_audit_bindings(),
        **_lifespan_service_bindings(shared_app.state),
        **_create_app_bindings(),
    }


def _restore_app_wiring(golden: dict) -> None:
    """Write every recorded shared-app value back to its binding."""
    for name, (_read, write) in golden["bindings"].items():
        write(golden["values"][name])


# binding name -> the shared app's app.state attribute it must be bound to.
_STATE_OWNED_BINDINGS = {
    "groups_router_manager": "group_manager",
    "search_embed_event_writer": "search_embed_event_writer",
    "payload_cache_registration": "payload_cache",
    "meta_hook_scheduler": "description_refresh_scheduler",
    "meta_hook_debouncer": "cidx_meta_debouncer",
    "latency_tracker": "latency_tracker",
    "mcp_self_registration": "mcp_registration_service",
    # app_wiring.py:215-259 also puts create_app()'s managers on app.state.
    **{
        f"server.app.{name}": name
        for name in _GUARDED_APP_MODULE_ATTRS
        if name not in ("_server_hnsw_cache", "_server_fts_cache")
    },
}

# Invariant is PRESENCE (and, where noted, liveness) only: the shared app
# holds no independent reference to compare identity against.  The X-Ray
# executor/limiter are app.state itself (the binding IS the owner; the
# executor is also checked not shut down); the graph cache and K provider
# are held only by their module singletons; TOTP, the coalescer registry and
# the query-embedding cache are held only by the lifespan's locals.
_PRESENCE_ONLY_BINDINGS = (
    "xray_executor",
    "xray_cell_limiter",
    "xray_graph_cache",
    "xray_k_provider",
    "totp_service",
    "coalescer_registry",
    "query_embedding_cache",
)


def _assert_bindings_are_shared_apps_own(
    values: Dict[str, Any], shared_app: FastAPI
) -> None:
    from code_indexer.server import app as app_module
    from code_indexer.server import cache as cache_module

    state = shared_app.state
    audit_service = getattr(state, "audit_service", None)
    scheduler = getattr(state, "description_refresh_scheduler", None)
    lifecycle = getattr(state, "global_lifecycle_manager", None)
    pairs = {
        name: (values[name], getattr(state, attr, None))
        for name, attr in _STATE_OWNED_BINDINGS.items()
    }
    pairs.update(
        {
            "singleton app": (vars(app_module).get("app"), shared_app),
            "audit_capture_sink": (values["audit_capture_sink"][0], audit_service),
            "password_audit_logger": (values["password_audit_logger"], audit_service),
            "meta_hook_tracking_backend": (
                values["meta_hook_tracking_backend"],
                getattr(scheduler, "_tracking_backend", None),
            ),
            "meta_hook_refresh_scheduler": (
                values["meta_hook_refresh_scheduler"],
                getattr(lifecycle, "refresh_scheduler", None),
            ),
            # inline_routes.py:292 builds the manager from the app's own
            # server_config (values[...] is the recorded SessionManager).
            "web_session_manager": (
                getattr(values["web_session_manager"], "_config", None),
                getattr(state, "server_config", None),
            ),
            # service_init.py:688 hands the same service to the golden manager.
            "category_service": (
                values["category_service"],
                getattr(state.golden_repo_manager, "_repo_category_service", None),
            ),
            # service_init.py:171 / :200: the server.cache singletons.
            "server.app._server_hnsw_cache": (
                values["server.app._server_hnsw_cache"],
                vars(cache_module).get("_global_cache_instance"),
            ),
            "server.app._server_fts_cache": (
                values["server.app._server_fts_cache"],
                vars(cache_module).get("_global_fts_cache_instance"),
            ),
        }
    )
    classified = (
        set(pairs)
        | set(_PRESENCE_ONLY_BINDINGS)
        | _PATH_OWNED_BINDINGS
        | {_WRITE_MODE_BINDING}
    )
    unclassified = set(values) - classified - {"temporal_metadata_factory"}
    assert not unclassified, f"bindings with no stated invariant: {unclassified}"
    for name, (bound, own) in pairs.items():
        assert own is not None and bound is own, (
            f"{name}: not bound to the shared app's own object "
            f"(bound={bound!r}, shared app's={own!r})"
        )


_PATH_OWNED_BINDINGS = {"token_blacklist_sqlite_path", "elevated_session_sqlite_path"}
_WRITE_MODE_BINDING = "file_crud_write_mode_dirs"


def _assert_write_mode_dirs_are_shared_apps(
    values: Dict[str, Any], shared_app: FastAPI
) -> None:
    """Write mode checks markers and writes the catalog in the shared app's
    own golden repos dir -- the dir enter_write_mode writes markers under."""
    own = Path(getattr(shared_app.state, "golden_repos_dir"))
    marker_dir, exceptions = values[_WRITE_MODE_BINDING]
    assert marker_dir is not None and Path(marker_dir) == own, (
        f"{_WRITE_MODE_BINDING}: marker dir {marker_dir!r} is not the shared "
        f"app's golden repos dir {own}"
    )
    expected = {"cidx-meta-global": own / "cidx-meta"}
    assert {k: Path(v) for k, v in exceptions.items()} == expected, (
        f"{_WRITE_MODE_BINDING}: write exceptions {exceptions!r} are not the "
        f"shared app's {expected!r}"
    )


def _assert_auth_stores_in_shared_data_dir(
    values: Dict[str, Any], shared_app: FastAPI, shared_data_dir: Path
) -> None:
    """Both auth stores write the shared app's own cidx_server.db."""
    own_db = str(getattr(shared_app.state, "db_path_str"))
    assert Path(own_db).is_relative_to(shared_data_dir), (
        f"shared app's db {own_db!r} is outside its data dir {shared_data_dir}"
    )
    for name in sorted(_PATH_OWNED_BINDINGS):
        assert values[name] == own_db, (
            f"{name}: points at {values[name]!r}, not the shared app's {own_db!r}"
        )


def _assert_solo_mode_wiring(values: Dict[str, Any]) -> None:
    factory = values["temporal_metadata_factory"]
    assert factory is None, f"postgres-only temporal factory bound in solo: {factory!r}"
    node_id = values["audit_capture_sink"][1]
    assert node_id is None, f"unexpected audit node id {node_id!r} in solo mode"
    unset = [
        name
        for name, value in values.items()
        if value is None and name != "temporal_metadata_factory"
    ]
    assert not unset, f"shared startup left these bindings unset: {unset}"


def _thread_alive(owner: Any, attr: str) -> bool:
    thread = getattr(owner, attr, None)
    return thread is not None and thread.is_alive()


def _assert_shared_workers_live(values: Dict[str, Any]) -> None:
    assert _thread_alive(values["audit_capture_sink"][0], "_writer_thread"), (
        "shared audit writer is not running"
    )
    assert _thread_alive(values["search_embed_event_writer"], "_thread"), (
        "shared search embed event writer is not running"
    )
    assert not getattr(values["xray_executor"], "_shutdown", True), (
        "shared X-Ray executor is already shut down"
    )
