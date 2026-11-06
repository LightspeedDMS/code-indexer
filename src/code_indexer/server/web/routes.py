"""
Web Admin UI Routes.

Provides admin web interface routes for CIDX server administration.

The routes live in per-domain modules under ``web/pages/`` (modular phase 6,
``plans/designs/modular-phase6-web-routes-inventory.md``). This module
aggregates them into the four routers the app mounts -- ``web_router`` at
``/admin``, ``user_router`` at ``/user``, ``login_router`` at the root and
``api_router`` at ``/api`` -- in the order the routes were registered when they
all lived here, and re-exports the names other modules and tests import from
``code_indexer.server.web.routes``.

Patching a re-exported name here (``mock.patch("...web.routes.X")``,
``monkeypatch.setattr(routes, "X", ...)``) also rebinds it in every page module
that uses it, exactly as it did when the code was one module; see
``_RoutesModule``. Module-level state that the page code rebinds at run time is
not copied here but read from and written to its owning page module.
"""

import sys
from types import ModuleType
from typing import Any, Dict, Tuple

from fastapi import APIRouter

from code_indexer.server.auto_update.deployment_executor import RESTART_SIGNAL_PATH

from ..services.config_service import get_config_service
from .auth import get_session_manager
from .pages import (
    _shared,
    session,
    dashboard,
    users,
    groups,
    golden_repos,
    activated_repos,
    jobs,
    query,
    discovery,
    analytics_export,
    config,
    siem_config,
    credentials,
    ssh_keys,
    logs,
    embedding_stats,
    self_monitoring,
    restart,
    config_values,
)
from .pages._shared import (
    CSRF_COOKIE_NAME,
    _create_login_redirect,
    _get_activated_repo_manager,
    _get_background_job_manager,
    _get_csrf_serializer,
    _get_golden_repo_manager,
    _get_group_manager,
    _get_repo_category_service,
    _get_ssh_key_manager,
    _get_token_manager,
    _get_user_mfa_status,
    _get_users_list,
    _require_admin_session,
    _require_authenticated_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)
from .pages.session import (
    _extract_jti_from_request,
)
from .pages.users import (
    _USERS_PAGE_ERROR_MESSAGES,
    _USERS_PAGE_SUCCESS_MESSAGES,
    _create_users_page_response,
    _resolve_users_page_messages,
    change_user_password,
    delete_user,
)
from .pages.groups import (
    _create_groups_page_response,
    _format_datetime_display,
    assign_user_to_group,
)
from .pages.golden_repos import (
    MAX_BATCH_CREATE_REPOS,
    _batch_create_repos,
    _create_golden_repos_page_response,
    _detect_index_flags,
    _get_golden_repo_branch_service,
    _get_golden_repos_list,
    _get_single_repo_enriched,
    _load_temporal_status,
    _safe_ts_slice,
    _sanitize_batch_create_error,
    batch_create_golden_repos,
    change_golden_repo_branch,
    force_resync_golden_repo,
    generate_unique_alias,
    get_golden_repo_branches,
    golden_repo_details_partial,
    refresh_golden_repo,
    save_temporal_options,
)
from .pages.activated_repos import (
    _build_deactivating_map,
    _get_all_activated_repos,
)
from .pages.jobs import (
    _apply_job_filters,
    _get_all_jobs,
    _get_job_tracker,
)
from .pages.query import (
    _add_to_query_history,
    _create_query_page_response,
    _get_all_activated_repos_for_query,
    _get_semantic_query_manager,
    query_results_partial_post,
    query_submit,
)
from .pages.discovery import (
    _DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY,
    _DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING,
    _get_discovery_branch_fetch_executor,
    _get_gitlab_provider,
    _load_hidden_ids,
    _resolve_provider,
    fetch_discovery_branches,
    shutdown_discovery_branch_fetch_executor,
)
from .pages.config import (
    _ci_token_display,
    _create_config_page_response,
    _initialized_oidc_manager,
    _prepare_oidc_candidate_from_worker_thread,
    _prepare_oidc_managers,
    clear_query_embedding_cache_table,
    update_config_section,
)
from .pages.ssh_keys import (
    _create_ssh_keys_page_response,
    delete_ssh_key,
)
from .pages.logs import (
    export_logs_web,
    logs_list_partial,
    logs_page,
)
from .pages.embedding_stats import (
    _load_embedding_stats_dashboard_data,
)
from .pages.self_monitoring import (
    _add_scan_duration,
    _calculate_next_scan_time,
    _get_last_scan_time,
    _get_scan_status,
    _load_self_monitoring_data,
)
from .pages.restart import (
    _delayed_restart,
    _schedule_delayed_restart,
)
from .pages.config_values import (
    RESTART_REQUIRED_FIELDS,
    _VALID_CONFIG_SECTIONS,
    _get_current_config,
    _get_qec_total_entries,
    _validate_config_section,
)


web_router = APIRouter()
web_router.include_router(session.web_router)
web_router.include_router(dashboard.web_router)
web_router.include_router(users.web_router)
web_router.include_router(groups.web_router)
web_router.include_router(golden_repos.web_router)
web_router.include_router(activated_repos.web_router)
web_router.include_router(jobs.web_router)
web_router.include_router(query.web_router)
web_router.include_router(discovery.web_router)
web_router.include_router(analytics_export.web_router)
web_router.include_router(config.web_router)
web_router.include_router(siem_config.web_router)
web_router.include_router(credentials.web_router)
web_router.include_router(ssh_keys.web_router)
web_router.include_router(logs.web_router)
web_router.include_router(embedding_stats.web_router)
web_router.include_router(self_monitoring.web_router)
web_router.include_router(restart.web_router)

user_router = APIRouter()
user_router.include_router(credentials.user_router)
user_router.include_router(session.user_router)

login_router = APIRouter()
login_router.include_router(session.login_router)

api_router = APIRouter()
api_router.include_router(session.api_router)


__all__ = [
    "CSRF_COOKIE_NAME",
    "MAX_BATCH_CREATE_REPOS",
    "RESTART_REQUIRED_FIELDS",
    "RESTART_SIGNAL_PATH",
    "_DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY",
    "_DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING",
    "_USERS_PAGE_ERROR_MESSAGES",
    "_USERS_PAGE_SUCCESS_MESSAGES",
    "_VALID_CONFIG_SECTIONS",
    "_add_scan_duration",
    "_add_to_query_history",
    "_apply_job_filters",
    "_batch_create_repos",
    "_build_deactivating_map",
    "_calculate_next_scan_time",
    "_ci_token_display",
    "_create_config_page_response",
    "_create_golden_repos_page_response",
    "_create_groups_page_response",
    "_create_login_redirect",
    "_create_query_page_response",
    "_create_ssh_keys_page_response",
    "_create_users_page_response",
    "_delayed_restart",
    "_detect_index_flags",
    "_extract_jti_from_request",
    "_format_datetime_display",
    "_get_activated_repo_manager",
    "_get_all_activated_repos",
    "_get_all_activated_repos_for_query",
    "_get_all_jobs",
    "_get_background_job_manager",
    "_get_csrf_serializer",
    "_get_current_config",
    "_get_discovery_branch_fetch_executor",
    "_get_gitlab_provider",
    "_get_golden_repo_branch_service",
    "_get_golden_repo_manager",
    "_get_golden_repos_list",
    "_get_group_manager",
    "_get_job_tracker",
    "_get_last_scan_time",
    "_get_qec_total_entries",
    "_get_repo_category_service",
    "_get_scan_status",
    "_get_semantic_query_manager",
    "_get_single_repo_enriched",
    "_get_ssh_key_manager",
    "_get_token_manager",
    "_get_user_mfa_status",
    "_get_users_list",
    "_initialized_oidc_manager",
    "_load_embedding_stats_dashboard_data",
    "_load_hidden_ids",
    "_load_self_monitoring_data",
    "_load_temporal_status",
    "_prepare_oidc_candidate_from_worker_thread",
    "_prepare_oidc_managers",
    "_require_admin_session",
    "_require_authenticated_session",
    "_resolve_provider",
    "_resolve_users_page_messages",
    "_safe_ts_slice",
    "_sanitize_batch_create_error",
    "_schedule_delayed_restart",
    "_validate_config_section",
    "api_router",
    "assign_user_to_group",
    "batch_create_golden_repos",
    "change_golden_repo_branch",
    "change_user_password",
    "clear_query_embedding_cache_table",
    "delete_ssh_key",
    "delete_user",
    "export_logs_web",
    "fetch_discovery_branches",
    "force_resync_golden_repo",
    "generate_csrf_token",
    "generate_unique_alias",
    "get_config_service",
    "get_csrf_token_from_cookie",
    "get_golden_repo_branches",
    "get_session_manager",
    "golden_repo_details_partial",
    "logger",
    "login_router",
    "logs_list_partial",
    "logs_page",
    "query_results_partial_post",
    "query_submit",
    "refresh_golden_repo",
    "save_temporal_options",
    "set_csrf_cookie",
    "shutdown_discovery_branch_fetch_executor",
    "templates",
    "update_config_section",
    "user_router",
    "validate_login_csrf_token",
    "web_router",
]

# Every page module, so a patch of a re-exported name reaches all of them.
_PAGE_MODULES: Tuple[ModuleType, ...] = (
    _shared,
    session,
    dashboard,
    users,
    groups,
    golden_repos,
    activated_repos,
    jobs,
    query,
    discovery,
    analytics_export,
    config,
    siem_config,
    credentials,
    ssh_keys,
    logs,
    embedding_stats,
    self_monitoring,
    restart,
    config_values,
)

# Module-level state the page code rebinds with ``global``: a copy bound here
# would go stale, so reads and writes go to the owning module.
_FORWARDED: Dict[str, ModuleType] = {
    "_DISCOVERY_BRANCH_FETCH_EXECUTOR": discovery,
    "_DISCOVERY_BRANCH_FETCH_GATE": discovery,
    "_restart_in_progress": restart,
}

_MISSING = object()


def __getattr__(name: str) -> Any:
    owner = _FORWARDED.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(owner, name)


class _RoutesModule(ModuleType):
    """Keeps ``web.routes.X = value`` meaning what it meant before the split.

    Production code never assigns to this module; tests do, to replace a helper
    for the code that calls it. Before the split that code lived here, so the
    assignment reached it. Now it reaches every page module whose binding of
    the name is the object being replaced, which is the same set of call sites.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        owner = _FORWARDED.get(name)
        if owner is not None:
            setattr(owner, name, value)
            return
        current = self.__dict__.get(name, _MISSING)
        if current is not _MISSING:
            for module in _PAGE_MODULES:
                if module.__dict__.get(name, _MISSING) is current:
                    setattr(module, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        owner = _FORWARDED.get(name)
        if owner is not None:
            delattr(owner, name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _RoutesModule
