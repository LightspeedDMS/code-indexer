"""The server's implementation of ``kernel.capabilities.ServerPlatformHost``.

A feature add-on imports no server module. What it needs from the server and
cannot get as a ``cidx_sdk`` capability yet (see the Protocol's docstring) it
reaches through ``SERVER_PLATFORM_HOST``, which ``kernel_wiring`` fills with
:class:`ServerPlatform` when the server package is imported.

The same two rules as ``kernel_wiring``: every method imports its server
function when CALLED (importing this module costs nothing, and a test that
patches a server binding is observed), and each method answers exactly what
the server function it wraps answers. Nothing here names a feature: these are
platform services any add-on may use.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from pathlib import Path

from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type, cast

_governed_lock = threading.Lock()
#: The memory governor's composite cache; service_init installs it.
_governed_composite: Optional[Any] = None


def set_governed_cache_composite(composite: Any) -> None:
    """Record the composite cache attached to the memory governor (service
    init), so features can add their own caches to it."""
    global _governed_composite
    with _governed_lock:
        _governed_composite = composite


class ServerPlatform:
    """Thin adapters over the server functions a feature still uses."""

    # app services ------------------------------------------------------------
    def app_attr(self, name: str) -> Any:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._lazy_module_attr_or_none(name)

    # repositories --------------------------------------------------------------
    def resolve_repo_path(self, alias: str) -> Optional[str]:
        from code_indexer.server.mcp.handlers import _utils

        return cast(Optional[str], _utils._resolve_global_repo_path_or_index(alias))

    def resolve_golden_repo_path(self, alias: str) -> Optional[str]:
        from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

        return cast(Optional[str], _resolve_golden_repo_path(alias))

    def try_global_fallback(
        self, alias: str, golden_repo_manager: Any
    ) -> Optional[str]:
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        return try_global_fallback(alias, golden_repo_manager)

    def refresh_scheduler(self) -> Any:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._get_app_refresh_scheduler()

    # MCP response shaping ----------------------------------------------------
    def mcp_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._mcp_response(data)

    def parse_json_string_array(self, value: Any) -> Any:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._parse_json_string_array(value)

    def parse_and_collapse_repo_alias(self, value: Any) -> Any:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._parse_and_collapse_repo_alias(value)

    def enforce_repo_count_cap(self, aliases: Any) -> Any:
        from code_indexer.server.mcp.handlers import _utils

        return _utils._enforce_repo_count_cap(aliases)

    def cap_breach_response(self, breach: Any) -> Dict[str, Any]:
        from code_indexer.server.mcp.handlers import _utils

        return _utils.cap_breach_response(breach)

    # query admission ---------------------------------------------------------
    def check_query_admission(self) -> Any:
        from code_indexer.server.services.query_admission_gate import (
            check_query_admission,
        )

        return check_query_admission()

    def memory_pressure_payload(self, admission: Any) -> Dict[str, Any]:
        from code_indexer.server.services.query_admission_gate import (
            memory_pressure_mcp_payload,
        )

        return memory_pressure_mcp_payload(admission)

    # search events -----------------------------------------------------------
    def xray_search_recorder(
        self,
        *,
        app: Any,
        tool: str,
        door: str,
        username: str,
        repo_alias: Optional[str],
        pattern: str,
        batch_repositories: Optional[Sequence[str]] = None,
    ) -> Callable[..., None]:
        from code_indexer.server.services.search_event_recording import (
            SearchOrigin,
            bind_xray_recorder,
            writer_from_app,
        )
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        source_app = app if app is not None else self.app_attr("app")
        return bind_xray_recorder(
            writer_from_app(source_app),
            origin=SearchOrigin(tool=tool, door=door),
            username=username,
            repo_alias=repo_alias,
            pattern=pattern,
            correlation_id=get_current_correlation_id(),
            batch_repositories=batch_repositories,
        )

    def run_recorded_xray(
        self,
        record_event: Callable[..., None],
        run: Callable[[], Dict[str, Any]],
        cancelled: Optional[Callable[[], bool]],
    ) -> Dict[str, Any]:
        from code_indexer.server.services.search_event_recording import (
            run_recorded_xray,
        )

        return run_recorded_xray(record_event, run, cancelled)

    def tracked_job_cancelled(self, tracker: Any, job_id: str) -> bool:
        from code_indexer.server.services.search_event_recording import (
            tracked_job_cancelled,
        )

        return tracked_job_cancelled(tracker, job_id)

    # paged payload cache -----------------------------------------------------
    def truncate_result_fields(
        self,
        result: Dict[str, Any],
        payload_cache: Any,
        fields: Sequence[str],
        *,
        owner: Optional[str],
    ) -> Dict[str, Any]:
        from code_indexer.server.mcp.handlers import xray_truncation

        return xray_truncation.truncate_result_fields(
            result, payload_cache, list(fields), owner=owner
        )

    def page_set_store_error(self) -> Type[Exception]:
        from code_indexer.server.mcp.handlers import xray_truncation

        return xray_truncation.PageSetStoreError

    # repository access -------------------------------------------------------
    def require_repo_access(
        self, service: Any, username: str, aliases: Any
    ) -> Optional[Tuple[str, str]]:
        from code_indexer.server.services.repo_access_guard import (
            AccessFilteringServiceUnavailableError,
            RepoAccessDeniedError,
            require_repo_access,
        )

        try:
            require_repo_access(service, username, aliases)
        except RepoAccessDeniedError as exc:
            return "access_denied", str(exc)
        except AccessFilteringServiceUnavailableError as exc:
            return "access_control_unavailable", str(exc)
        return None

    def repository_name_granted(self, username: str, alias: str) -> bool:
        from code_indexer.server.mcp.handlers import _utils
        from code_indexer.server.services.access_filtering_service import (
            AccessFilteringService,
        )

        access = _utils._get_access_filtering_service()
        if access is None:
            return False
        return bool(
            access.is_admin_user(username)
            or AccessFilteringService.alias_granted(
                alias, access.get_accessible_repos(username), {}
            )
        )

    def is_admin_role(self, user: Any) -> bool:
        from code_indexer.server.auth.user_manager import UserRole

        return bool(getattr(user, "role", None) == UserRole.ADMIN)

    # catalog access projection -------------------------------------------------
    def golden_catalog_surface_refusal(
        self,
        surface: str,
        alias: Any,
        username: str,
        access_service: Any,
        golden_repos_dir: Optional[str],
    ) -> Optional[str]:
        from code_indexer.server.services.cidx_meta_access.history_policy import (
            CatalogSurface,
            golden_catalog_surface_refusal,
        )

        return golden_catalog_surface_refusal(
            CatalogSurface[surface], alias, username, access_service, golden_repos_dir
        )

    # audit -------------------------------------------------------------------
    def conforming_audit_details(
        self, action_type: str, **candidates: Any
    ) -> Dict[str, Any]:
        from code_indexer.server.services.audit_outcome import conforming_details

        return conforming_details(action_type, **candidates)

    def record_audit_outcome(
        self,
        *,
        actor: str,
        action_type: str,
        target_type: str,
        target_id: Any,
        outcome: str,
        details: Dict[str, Any],
    ) -> None:
        from code_indexer.server.services.audit_outcome import record_outcome

        record_outcome(
            actor=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
        )

    # content seam ------------------------------------------------------------
    def always_excluded_dirs(self) -> Sequence[str]:
        from code_indexer.server.services.file_service import FileListingService

        return sorted(FileListingService.ALWAYS_EXCLUDED_DIRS)

    def supports_scoped_manifest(self, backend: Any) -> bool:
        from code_indexer.server.storage.shared.content_backend import (
            supports_scoped_manifest,
        )

        return bool(supports_scoped_manifest(backend))

    def scoped_manifest_entries(
        self, seam: Any, rel_dir: str, *, max_depth: int, prune_dirs: Sequence[str]
    ) -> Sequence[Any]:
        from code_indexer.server.services.file_service import scoped_manifest_entries

        return cast(
            Sequence[Any],
            scoped_manifest_entries(
                seam, rel_dir, max_depth=max_depth, prune_dirs=list(prune_dirs)
            ),
        )

    def manifest_entries(self, seam: Any, prune_dirs: Sequence[str]) -> Sequence[Any]:
        from code_indexer.server.services.file_service import manifest_entries

        return manifest_entries(seam, list(prune_dirs))

    def manifest_entries_cached(
        self, seam: Any, prune_dirs: Sequence[str]
    ) -> Optional[Sequence[Any]]:
        from code_indexer.server.services.file_service import manifest_entries_cached

        return manifest_entries_cached(seam, list(prune_dirs))

    def fetch_one(self, seam: Any, rel_path: str) -> Any:
        from code_indexer.server.services.file_service import fetch_one

        return fetch_one(seam, rel_path)

    def is_pod_local_golden_path(self, path: str) -> bool:
        from code_indexer.server.startup.content_backend_wiring import (
            is_pod_local_golden_path,
        )

        return bool(is_pod_local_golden_path(path))

    def golden_repo_content_root(self, path: str, *, consumer: str) -> Any:
        from code_indexer.server.startup.content_backend_wiring import (
            golden_repo_content_root,
        )

        return golden_repo_content_root(path, consumer=consumer)

    def delegated_repository_unsupported(self, feature: str, detail: str) -> Exception:
        from code_indexer.server.services.delegated_refusal import (
            DelegatedRepositoryUnsupportedError,
        )

        return DelegatedRepositoryUnsupportedError(feature, detail)

    # cidx-meta ---------------------------------------------------------------
    def shared_cidx_meta(self, cidx_meta: Any) -> Any:
        from code_indexer.server.services import cidx_meta_content

        return cidx_meta_content.shared_cidx_meta(cidx_meta)

    def cidx_meta_conflict_error(self) -> Type[Exception]:
        from code_indexer.server.services import cidx_meta_content

        return cidx_meta_content.CidxMetaConflictError

    def cidx_meta_unconditional(self) -> Any:
        from code_indexer.server.services import cidx_meta_content

        return cidx_meta_content.UNCONDITIONAL

    def commit_cidx_meta_files(
        self, cidx_meta: Any, relative_files: Sequence[str], message: str
    ) -> None:
        commit_cidx_meta_files(Path(cidx_meta), relative_files, message)

    def request_cidx_meta_refresh(self, scheduler: Any) -> None:
        from code_indexer.global_repos.meta_description_hook import (
            request_cidx_meta_refresh,
        )

        request_cidx_meta_refresh(scheduler)

    # execution resources -----------------------------------------------------
    def resizable_limiter(self, *, initial: int, k_min: int, k_max: int) -> Any:
        from code_indexer.server.services.resizable_limiter import ResizableLimiter

        return ResizableLimiter(initial=initial, k_min=k_min, k_max=k_max)

    def xray_executor_pool_size(self) -> int:
        from code_indexer.server.utils.config_manager import BackgroundJobsConfig

        return int(BackgroundJobsConfig().xray_max_concurrent_jobs)

    def attach_governed_cache(self, cache: Any) -> Callable[[], None]:
        with _governed_lock:
            composite = _governed_composite
        if composite is None:
            return _no_detach
        composite.add(cache)

        def _detach() -> None:
            composite.remove(cache)

        return _detach

    # documents served from repositories (the wiki) ---------------------------
    def backend_registry_attr(self, name: str, caller: str) -> Any:
        from code_indexer.server.utils.registry_factory import (
            resolve_backend_registry_attr,
        )

        backend, _ = resolve_backend_registry_attr(name, caller_name=caller)
        return backend

    def sqlite_connection_manager(self, db_path: str) -> Any:
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        return DatabaseConnectionManager.get_instance(db_path)

    def read_alias_target(
        self, golden_repos_dir: str, alias_name: str
    ) -> Optional[str]:
        from code_indexer.global_repos.alias_manager import AliasManager

        return AliasManager(str(Path(golden_repos_dir) / "aliases")).read_alias(
            alias_name
        )

    def resolve_alias_or_index_path(
        self, golden_repos_dir: str, alias_name: str, repo_entry: Any
    ) -> Optional[str]:
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        return cast(
            Optional[str],
            resolve_alias_or_index_path(
                AliasManager(str(Path(golden_repos_dir) / "aliases")),
                alias_name=alias_name,
                repo_entry=repo_entry,
            ),
        )

    def activated_repo_query(
        self, query_tracker: Any, activated_repo_manager: Any, username: str, alias: str
    ) -> Any:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        return track_activated_repo_query(
            query_tracker, activated_repo_manager, username, alias
        )

    def enforce_activated_repo_access(
        self, access_service: Any, username: str, alias: str, refusal: Any
    ) -> None:
        from code_indexer.server.routers import repo_access_http

        repo_access_http.enforce_activated_repo_access(
            access_service, username, alias, refusal=refusal
        )

    def is_access_admin(self, access_service: Any, username: str) -> bool:
        from code_indexer.server.routers import repo_access_http

        return bool(repo_access_http.is_access_admin(access_service, username))

    def run_recorded_search(
        self,
        app: Any,
        run: Callable[[], Any],
        *,
        tool: str,
        door: str,
        username: str,
        repo_alias: str,
        search_type: str,
        query_text: str,
    ) -> Any:
        from code_indexer.server.middleware.correlation import get_correlation_id
        from code_indexer.server.services import search_event_recording

        return search_event_recording.run_recorded_search(
            search_event_recording.writer_from_app(app),
            run,
            origin=search_event_recording.SearchOrigin(tool=tool, door=door),
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text,
            correlation_id=get_correlation_id(),
        )

    def sanitize_article_html(self, html: str) -> str:
        from code_indexer.server.utils.html_sanitizer import (
            WIKI_ARTICLE_ALLOWLIST,
            sanitize_html,
        )

        return sanitize_html(html, WIKI_ARTICLE_ALLOWLIST)

    def wiki_enabled_golden_aliases(self) -> Set[str]:
        from code_indexer.server.mcp.handlers import _utils

        return set(_utils._get_wiki_enabled_repos())

    # Web UI admin actions ----------------------------------------------------
    def require_admin_elevation(self, request: Any) -> None:
        from fastapi.security import HTTPAuthorizationCredentials

        from code_indexer.server.auth import dependencies

        credentials = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and header[7:].strip():
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=header[7:].strip()
            )
        user = dependencies._hybrid_auth_impl(request, credentials, require_admin=True)
        dependencies.require_elevation()(request, user)

    def admin_web_session(self, request: Any) -> Any:
        from code_indexer.server.web.pages._shared import _require_admin_session

        return _require_admin_session(request)

    def login_redirect_response(self, request: Any) -> Any:
        from code_indexer.server.web.pages._shared import _create_login_redirect

        return _create_login_redirect(request)

    def validate_login_csrf(self, request: Any, token: Optional[str]) -> bool:
        from code_indexer.server.web.pages._shared import validate_login_csrf_token

        return bool(validate_login_csrf_token(request, token))

    def web_error_partial(self, request: Any, message: str, status_code: int) -> Any:
        from code_indexer.server.web.pages._shared import templates

        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {"request": request, "error": message},
            status_code=status_code,
        )

    def golden_repos_page_response(
        self, request: Any, session: Any, success_message: str
    ) -> Any:
        from code_indexer.server.web.pages.golden_repos import (
            _create_golden_repos_page_response,
        )

        return _create_golden_repos_page_response(
            request, session, success_message=success_message
        )

    # services (SCIP, phase 4d) -----------------------------------------------
    def config_service(self) -> Any:
        from code_indexer.server.services.config_service import get_config_service

        return get_config_service()

    def running_app_state(self) -> Any:
        from code_indexer.server.utils.registry_factory import (
            _running_server_app_state,
        )

        return _running_server_app_state()

    def golden_repo_manager(self) -> Any:
        from code_indexer.server.repositories.golden_repo_manager import (
            get_golden_repo_manager,
        )

        return get_golden_repo_manager()

    def golden_index_backfill(self, golden_repos_dir: str) -> Any:
        from code_indexer.server.services.golden_index_backfill import (
            GoldenIndexBackfillReconciler,
        )

        return GoldenIndexBackfillReconciler(golden_repos_dir)

    # logging -----------------------------------------------------------------
    def format_error_log(self, error_code: str, message: str, **context: Any) -> str:
        from code_indexer.server.logging_utils import format_error_log

        return format_error_log(error_code, message, **context)

    def log_extra(self, error_code: str) -> Dict[str, Any]:
        from code_indexer.server.logging_utils import get_log_extra

        return get_log_extra(error_code)

    def correlation_id(self) -> Optional[str]:
        from code_indexer.server.telemetry.correlation_bridge import (
            get_current_correlation_id,
        )

        return get_current_correlation_id()

    def parse_json_column(
        self, raw: object, expected_type: Type[Any], field_name: str
    ) -> Any:
        from code_indexer.server.storage.json_column import parse_json_column

        return parse_json_column(raw, expected_type, field_name)

    # MCP handler gates -------------------------------------------------------
    def admin_role_required_response(self, user: Any) -> Optional[Dict[str, Any]]:
        from code_indexer.server.mcp.handlers import _utils

        return cast(
            Optional[Dict[str, Any]], _utils._admin_role_required_response(user)
        )

    def require_mcp_elevation(self, required_scope: str) -> Callable[..., Any]:
        from code_indexer.server.mcp.auth.elevation_decorator import (
            require_mcp_elevation,
        )

        return require_mcp_elevation(required_scope)

    def apply_reranking(self, **kwargs: Any) -> Tuple[List[Any], Dict[str, Any]]:
        from code_indexer.server.mcp import reranking

        return reranking._apply_reranking_sync(**kwargs)

    def api_metrics(self) -> Any:
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        return api_metrics_service

    # search events (SCIP) ----------------------------------------------------
    def search_origin(self, tool: str, door: str) -> Any:
        from code_indexer.server.services.search_event_recording import SearchOrigin

        return SearchOrigin(tool=tool, door=door)

    def search_event_writer(self, app: Any) -> Any:
        from code_indexer.server.services.search_event_recording import (
            writer_from_app,
        )

        return writer_from_app(app)

    def search_event_context(self) -> Any:
        from code_indexer.server.services.search_event_context import (
            get_search_event_ctx,
        )

        return get_search_event_ctx()

    def search_event_scope(self, **fields: Any) -> AbstractContextManager[Any]:
        from code_indexer.server.services.search_event_recording import (
            search_event_scope,
        )

        return search_event_scope(**fields)

    def note_current_repositories(self, *args: Any, **kwargs: Any) -> None:
        from code_indexer.server.services.search_event_context import (
            note_current_repositories,
        )

        note_current_repositories(*args, **kwargs)

    def note_current_resolution(self, *args: Any, **kwargs: Any) -> None:
        from code_indexer.server.services.search_event_context import (
            note_current_resolution,
        )

        note_current_resolution(*args, **kwargs)

    def repository_resolved(self, *args: Any, **kwargs: Any) -> Any:
        from code_indexer.server.services.search_event_outcomes import (
            repository_resolved,
        )

        return repository_resolved(*args, **kwargs)

    def scip_payload_result_count(self, payload: Dict[str, Any]) -> int:
        from code_indexer.server.services.search_event_outcomes import (
            scip_payload_result_count,
        )

        return int(scip_payload_result_count(payload))

    def scip_callchain_query_text(self, from_symbol: Any, to_symbol: Any) -> str:
        from code_indexer.server.services.search_event_recording import (
            scip_callchain_query_text,
        )

        return scip_callchain_query_text(from_symbol, to_symbol)

    def record_scip_search(
        self, writer: Any, payload: Dict[str, Any], **kwargs: Any
    ) -> None:
        from code_indexer.server.services.search_event_recording import (
            record_scip_search,
        )

        record_scip_search(writer, payload, **kwargs)

    def record_multi_repository_search(
        self, writer: Any, grouped: Any, **kwargs: Any
    ) -> None:
        from code_indexer.server.services.search_event_recording import (
            record_multi_repository_search,
        )

        record_multi_repository_search(writer, grouped, **kwargs)

    def recording_multi_repository_failure(
        self, writer: Any, **kwargs: Any
    ) -> AbstractContextManager[None]:
        from code_indexer.server.services.search_event_recording import (
            recording_multi_repository_failure,
        )

        return recording_multi_repository_failure(writer, **kwargs)

    def search_events_module(self) -> Any:
        from code_indexer.server.services import search_event_recording

        return search_event_recording

    # errors ------------------------------------------------------------------
    def access_unavailable_error(self) -> Type[Exception]:
        from code_indexer.server.services.repo_access_guard import (
            AccessFilteringServiceUnavailableError,
        )

        return AccessFilteringServiceUnavailableError

    def repo_access_denied_error(self) -> Type[Exception]:
        from code_indexer.server.services.repo_access_guard import (
            RepoAccessDeniedError,
        )

        return RepoAccessDeniedError

    def require_repo_access_or_raise(
        self, service: Any, username: str, aliases: Any
    ) -> None:
        from code_indexer.server.services.repo_access_guard import require_repo_access

        require_repo_access(service, username, aliases)

    def enforce_repo_access_http(
        self, service: Any, username: str, aliases: Any
    ) -> None:
        from code_indexer.server.routers import repo_access_http

        repo_access_http.enforce_repo_access(service, username, aliases)

    def unresolved_repository_error(self) -> Type[Exception]:
        from code_indexer.server.multi.models import UnresolvedRepositoryError

        return UnresolvedRepositoryError

    def search_request_error(self) -> Type[Exception]:
        from code_indexer.server.query.semantic_query_manager import (
            SearchRequestError,
        )

        return SearchRequestError

    def duplicate_job_errors(self) -> Tuple[Type[Exception], ...]:
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError as BackendDuplicateJobError,
        )
        from code_indexer.server.services.job_tracker import (
            DuplicateJobError as JobTrackerDuplicateJobError,
        )

        return (JobTrackerDuplicateJobError, BackendDuplicateJobError)

    def job_status(self) -> Any:
        from code_indexer.server.repositories.background_jobs import JobStatus

        return JobStatus

    # served indexes ----------------------------------------------------------
    def served_index_reads(self) -> AbstractContextManager[Any]:
        from code_indexer.server.services.served_index_cache import (
            served_index_reads,
        )

        return served_index_reads()

    def active_served_index_cache(self) -> Any:
        from code_indexer.server.services.served_index_cache import (
            active_served_index_cache,
        )

        return active_served_index_cache()

    # repository reads --------------------------------------------------------
    def server_content_backend(self) -> Any:
        from code_indexer.server.startup.content_backend_wiring import (
            server_content_backend,
        )

        return server_content_backend()

    def server_delegated_git_backend(self) -> Any:
        from code_indexer.server.startup.git_backend_wiring import (
            server_delegated_git_backend,
        )

        return server_delegated_git_backend()

    def build_commit_marker(self) -> str:
        from code_indexer.server.services.index_freshness import BUILD_COMMIT_MARKER

        return BUILD_COMMIT_MARKER

    def current_commit(self, repo_path: Path) -> Optional[str]:
        from code_indexer.server.services.index_freshness import current_commit

        return current_commit(repo_path)

    # self-healing ------------------------------------------------------------
    def git_state_manager(self, config: Any) -> Any:
        from code_indexer.server.services.git_state_manager import GitStateManager

        return GitStateManager(config=config)

    def extract_json_from_llm_response(self, text: str) -> Any:
        from code_indexer.server.self_monitoring.llm_response_parser import (
            extract_json_from_llm_response,
        )

        return extract_json_from_llm_response(text)


def _no_detach() -> None:
    """Nothing was attached (no memory governor in this process)."""


#: The identity cidx-meta commits are made with (the backup's own).
CIDX_META_COMMIT_IDENTITY = ("cidx-meta-backup", "cidx-meta-backup@example.invalid")


def commit_cidx_meta_files(
    cidx_meta: Path, relative_files: Sequence[str], message: str
) -> None:
    """git add + commit *relative_files* in the local cidx-meta repository,
    then run the configured cidx-meta backup sync (its failure is a WARNING,
    the commit stands).

    Raises:
        subprocess.CalledProcessError: git add or git commit failed.
    """
    import logging

    from code_indexer.server.git.git_subprocess_env import (
        build_non_interactive_git_env,
    )
    from code_indexer.utils.git_runner import run_git_command

    env = build_non_interactive_git_env()
    name, email = CIDX_META_COMMIT_IDENTITY
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    # cidx-meta mutations go through the git seam chokepoint like
    # every other server-side mutation (safe.directory handling included).
    run_git_command(
        ["git", "add"] + list(relative_files),
        cwd=cidx_meta,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    run_git_command(
        ["git", "commit", "-m", message],
        cwd=cidx_meta,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    failure = sync_cidx_meta_backup(cidx_meta)
    if failure:
        logging.getLogger(__name__).warning(
            "cidx-meta backup sync deferred-failure: %s", failure
        )


def sync_cidx_meta_backup(cidx_meta: Path) -> Optional[str]:
    """Run the cidx-meta backup sync when it is configured and enabled.

    Returns the sync's deferred failure, or None (synced, or not enabled).
    """
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )
    from code_indexer.server.services.cidx_meta_backup.sync import (
        CidxMetaBackupSync,
    )
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    backup_cfg = getattr(config, "cidx_meta_backup_config", None)
    if backup_cfg is None or not getattr(backup_cfg, "enabled", False):
        return None
    branch = detect_default_branch(str(cidx_meta)) or "master"
    result = CidxMetaBackupSync(cidx_meta_path=str(cidx_meta), branch=branch).sync()
    failure = getattr(result, "sync_failure", None)
    return str(failure) if failure else None
