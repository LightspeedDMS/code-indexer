"""Server pieces every SCIP server module shares.

Each name is a thin call into the server platform (``cidx_scip._platform``),
kept under the name the module used when it lived in the core's server
package, so the module code reads the same. Nothing here is resolved at
import: every call asks the platform, so importing a SCIP server module
needs no running server, and a test that patches the core binding a method
wraps is observed.
"""

from __future__ import annotations

import functools
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar, cast

from cidx_scip._platform import server_platform

F = TypeVar("F", bound=Callable[..., Any])

#: ``UserRole.ADMIN``'s value: the role a SCIP admin tool requires.
ADMIN_ROLE = "admin"


# app services ----------------------------------------------------------------
def lazy_app_attr(name: str) -> Any:
    """The server app module's already-built *name*, or None (never builds
    it: Bug #1709)."""
    return server_platform().app_attr(name)


def app_state() -> Any:
    """The server app's ``state``, or None when no app is built."""
    return getattr(lazy_app_attr("app"), "state", None)


def get_config_service() -> Any:
    """The server's config service."""
    return server_platform().config_service()


def running_app_state() -> Any:
    """The running server's ``app.state``, or None outside a server."""
    return server_platform().running_app_state()


def get_golden_repo_manager() -> Any:
    """The registered golden repo manager."""
    return server_platform().golden_repo_manager()


# logging ---------------------------------------------------------------------
def format_error_log(error_code: str, message: str, **context: Any) -> str:
    """``[CODE] message key=value...``."""
    return server_platform().format_error_log(error_code, message, **context)


def get_log_extra(error_code: str) -> Dict[str, Any]:
    """The ``extra`` of a coded log line."""
    return server_platform().log_extra(error_code)


def get_correlation_id() -> Optional[str]:
    """The current request's correlation id, or None (also outside a server:
    log lines must never fail)."""
    try:
        return server_platform().correlation_id()
    except RuntimeError:
        return None


def parse_json_column(raw: object, expected_type: Type[Any], field_name: str) -> Any:
    return server_platform().parse_json_column(raw, expected_type, field_name)


# users -----------------------------------------------------------------------
def is_admin(user: Any) -> bool:
    """Whether *user* (anything with a ``role``) has the admin role."""
    role = getattr(user, "role", None)
    return getattr(role, "value", role) == ADMIN_ROLE


def admin_role_first(handler: F) -> F:
    """Refuse a non-admin before *handler* (and any gate it carries) runs --
    the core's ``_admin_role_first``: the admin role holds however the call
    was admitted, including by a group tool grant."""

    @functools.wraps(handler)
    def wrapper(params: Dict[str, Any], user: Any, *extra: Any, **kwargs: Any) -> Any:
        refusal = server_platform().admin_role_required_response(user)
        if refusal is not None:
            return refusal
        return handler(params, user, *extra, **kwargs)

    return cast(F, wrapper)


def require_mcp_elevation(required_scope: str = "full") -> Callable[[F], F]:
    """The core's TOTP elevation gate for an MCP handler, applied per call.

    The wrapper carries the dispatcher's session-key marker, so the core
    hands it the caller's session key exactly as for its own gated tools.
    """

    def decorator(handler: F) -> F:
        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            gate = server_platform().require_mcp_elevation(required_scope)
            return gate(handler)(*args, **kwargs)

        setattr(wrapper, "__mcp_requires_session_key__", True)
        return cast(F, wrapper)

    return decorator


# query admission -------------------------------------------------------------
def check_query_admission() -> Any:
    return server_platform().check_query_admission()


def memory_pressure_mcp_payload(admission: Any) -> Dict[str, Any]:
    return server_platform().memory_pressure_payload(admission)


def mcp_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """*data* in the MCP ``content`` envelope (the core's serialization)."""
    return server_platform().mcp_response(data)


def try_global_fallback(alias: str, golden_repo_manager: Any) -> Optional[str]:
    return server_platform().try_global_fallback(alias, golden_repo_manager)


def apply_reranking_sync(**kwargs: Any) -> Tuple[Any, Dict[str, Any]]:
    return server_platform().apply_reranking(**kwargs)


# search events ---------------------------------------------------------------
@dataclass(frozen=True)
class SearchOrigin:
    """The tool and front door a search event is recorded under."""

    tool: str
    door: str


def writer_from_app(app: Any) -> Any:
    """*app*'s search-event writer, or None."""
    return server_platform().search_event_writer(app)


def get_search_event_ctx() -> Any:
    return server_platform().search_event_context()


def search_event_scope(**fields: Any) -> AbstractContextManager[Any]:
    return server_platform().search_event_scope(**fields)


def in_scip_search_scope(door: F) -> F:
    """Run a SCIP search door inside a fresh search event context (Bug #2111):
    the SCIP service notes on it the repositories it searched; the door
    records from it."""

    @functools.wraps(door)
    def _scoped(*args: Any, **kwargs: Any) -> Any:
        with search_event_scope(
            username="", repo_alias=None, search_type="scip", query_text=""
        ):
            return door(*args, **kwargs)

    return cast(F, _scoped)


def note_current_repositories(*args: Any, **kwargs: Any) -> None:
    server_platform().note_current_repositories(*args, **kwargs)


def note_current_resolution(*args: Any, **kwargs: Any) -> None:
    server_platform().note_current_resolution(*args, **kwargs)


def repository_resolved(*args: Any, **kwargs: Any) -> Any:
    return server_platform().repository_resolved(*args, **kwargs)


def scip_payload_result_count(payload: Dict[str, Any]) -> int:
    return server_platform().scip_payload_result_count(payload)


def scip_callchain_query_text(from_symbol: Any, to_symbol: Any) -> str:
    return server_platform().scip_callchain_query_text(from_symbol, to_symbol)


def _core_origin(origin: Any) -> Any:
    return server_platform().search_origin(origin.tool, origin.door)


def record_scip_search(
    writer: Any, payload: Dict[str, Any], *, origin: SearchOrigin, **kwargs: Any
) -> None:
    """Record one SCIP search from its response payload. Never raises."""
    server_platform().record_scip_search(
        writer, payload, origin=_core_origin(origin), **kwargs
    )


def record_multi_repository_search(
    writer: Any, grouped: Any, *, origin: SearchOrigin, **kwargs: Any
) -> None:
    server_platform().record_multi_repository_search(
        writer, grouped, origin=_core_origin(origin), **kwargs
    )


def recording_multi_repository_failure(
    writer: Any, *, origin: SearchOrigin, **kwargs: Any
) -> AbstractContextManager[None]:
    return server_platform().recording_multi_repository_failure(
        writer, origin=_core_origin(origin), **kwargs
    )


# errors ----------------------------------------------------------------------
def access_unavailable_error() -> Type[Exception]:
    """``AccessFilteringServiceUnavailableError``."""
    return server_platform().access_unavailable_error()


def repo_access_denied_error() -> Type[Exception]:
    """``RepoAccessDeniedError``."""
    return server_platform().repo_access_denied_error()


def is_access_unavailable(exc: BaseException) -> bool:
    """Whether *exc* is the core's "no access service" refusal."""
    try:
        return isinstance(exc, access_unavailable_error())
    except RuntimeError:
        return False


def require_repo_access(service: Any, username: str, aliases: Any) -> None:
    server_platform().require_repo_access_or_raise(service, username, aliases)


def duplicate_job_errors() -> Tuple[Type[Exception], ...]:
    return server_platform().duplicate_job_errors()


def job_status() -> Any:
    """The background job status enum (``JobStatus``)."""
    return server_platform().job_status()


# served indexes --------------------------------------------------------------
def reads_served_indexes(fn: F) -> F:
    """Run *fn* holding the served-index checkouts it reads (the core's
    ``served_index_cache.reads_served_indexes``). The body is called through
    ``__wrapped__``, so a test can stand a body in for the real one."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with server_platform().served_index_reads():
            return getattr(wrapper, "__wrapped__")(*args, **kwargs)

    return cast(F, wrapper)


def active_served_index_cache() -> Any:
    return server_platform().active_served_index_cache()


# repository reads ------------------------------------------------------------
def server_content_backend() -> Any:
    return server_platform().server_content_backend()


def golden_repo_content_root(path: Any, *, consumer: str) -> Any:
    return server_platform().golden_repo_content_root(str(path), consumer=consumer)


def server_delegated_git_backend() -> Any:
    return server_platform().server_delegated_git_backend()


def build_commit_marker() -> str:
    return server_platform().build_commit_marker()


def current_commit(repo_path: Path) -> Optional[str]:
    return server_platform().current_commit(Path(repo_path))


# self-healing ----------------------------------------------------------------
def git_state_manager(config: Any) -> Any:
    """A git state manager (branch, commit, PR after a dependency fix)."""
    return server_platform().git_state_manager(config)


def extract_json_from_llm_response(text: str) -> Any:
    return server_platform().extract_json_from_llm_response(text)


def unresolved_repository_error() -> Type[Exception]:
    """A multi-repository search's unresolved-alias error (a
    ``FileNotFoundError``: a rejection, not a failed search)."""
    return server_platform().unresolved_repository_error()


def enforce_repo_access_http(service: Any, username: str, aliases: Any) -> None:
    """The REST repository guard: HTTP 403 for a repository the caller may
    not read, HTTP 500 ``access_control_unavailable`` without the access
    service (``aliases=None`` only requires the service)."""
    server_platform().enforce_repo_access_http(service, username, aliases)


def api_metrics() -> Any:
    """The REST API metrics service."""
    return server_platform().api_metrics()


def golden_index_backfill(golden_repos_dir: str) -> Any:
    """The core's golden-repo index backfill engine (``run_cycle``)."""
    return server_platform().golden_index_backfill(golden_repos_dir)
