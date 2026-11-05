"""Server pieces every X-Ray MCP handler module shares.

Each name is a thin call into the server platform (``cidx_xray._platform``),
kept under the name the handlers used when they lived in the core's handler
package, so handler code and the tests that patch it read the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence

from cidx_xray._platform import server_platform


def _mcp_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """*data* in the MCP ``content`` envelope (the core's serialization)."""
    return server_platform().mcp_response(data)


def _parse_json_string_array(value: Any) -> Any:
    return server_platform().parse_json_string_array(value)


def _parse_and_collapse_repo_alias(value: Any) -> Any:
    return server_platform().parse_and_collapse_repo_alias(value)


def _lazy_app_attr(name: str) -> Any:
    """The server app module's already-built *name*, or None (never builds
    it: Bug #1709)."""
    return server_platform().app_attr(name)


def _lazy_singleton_app_or_none() -> Any:
    """The already-constructed server app singleton, or None (Bug #1678)."""
    return _lazy_app_attr("app")


def _app_payload_cache() -> Any:
    """The server app's payload cache, or None when it is not wired."""
    return getattr(
        getattr(_lazy_singleton_app_or_none(), "state", None), "payload_cache", None
    )


def truncate_paged_result(
    result: Dict[str, Any],
    fields: Sequence[str],
    *,
    owner: Optional[str],
    label: str,
    payload_cache: Any,
    logger: Any,
) -> Dict[str, Any]:
    """Page *fields* of an X-Ray result into the payload cache (Bug #1928:
    whole, unmodified entries per cache page; inline budget =
    payload_max_fetch_size_chars). Only *owner* can fetch the pages.

    A payload cache of None gives a bounded ``cache_unavailable`` result (the
    core's own guard). A failed page-set write (pages and pages-v1 manifest
    are one atomic batch) is an explicit ``cache_store_failed`` error that
    keeps the result's other metadata -- never a cache handle pointing at
    data that was not written.
    """
    platform = server_platform()
    try:
        return platform.truncate_result_fields(
            result, payload_cache, fields, owner=owner
        )
    except platform.page_set_store_error() as exc:
        logger.error("%s truncation: page-set store failed: %s", label, exc)
        base_metadata = getattr(exc, "base_metadata", {})
        return {
            **base_metadata,
            "success": False,
            "error": "cache_store_failed",
            "message": f"Failed to store the truncated result in cache: {exc}",
        }


def promote_bare_alias(alias: str, username: str, logger: Any) -> str:
    """Story #1039: the bare-to-global alias fallback of the read-only X-Ray
    handlers.

    A bare golden alias (``repo`` for ``repo-global``) the caller has not
    activated under that name is promoted to its ``-global`` alias; anything
    else -- a ``-global`` alias, an activated alias, no repository managers
    in this process -- is returned unchanged.
    """
    if alias.endswith("-global"):
        return alias
    activated = _lazy_app_attr("activated_repo_manager")
    golden = _lazy_app_attr("golden_repo_manager")
    if activated is None or golden is None:
        return alias
    if activated.user_has_activated_repo(username, alias):
        return alias
    promoted = server_platform().try_global_fallback(alias, golden)
    if promoted is None:
        return alias
    logger.info("bare-alias fallback: %r -> %r for user %r", alias, promoted, username)
    return str(promoted)


def check_query_admission() -> Any:
    """The server's memory-pressure admission decision (``.allowed``)."""
    return server_platform().check_query_admission()


def memory_pressure_mcp_payload(admission: Any) -> Dict[str, Any]:
    """The MCP payload refusing a query under memory pressure."""
    return server_platform().memory_pressure_payload(admission)


#: What :func:`xray_recorder` returns: call it with ``(result, cancelled)``,
#: or ``(None, cancelled, error=exc)`` when the job raised (Story #2117).
XRayRecorder = Callable[..., None]


def xray_recorder(
    *,
    tool: str,
    door: str,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    batch_repositories: Optional[Sequence[str]] = None,
    app: Any = None,
) -> XRayRecorder:
    """Story #2107: bind one X-Ray search's event fields now (the latency
    clock starts here); the job records the event when its result is known.
    Written to *app*'s search-event writer (None: the server app)."""
    return server_platform().xray_search_recorder(
        app=app,
        tool=tool,
        door=door,
        username=username,
        repo_alias=repo_alias,
        pattern=pattern,
        batch_repositories=batch_repositories,
    )


def run_recorded_xray(
    record_event: XRayRecorder,
    run: Callable[[], Dict[str, Any]],
    cancelled: Optional[Callable[[], bool]],
) -> Dict[str, Any]:
    """Run one X-Ray search (the engine or batch call only) and record it:
    its result when it returns, a failure row when it raises (re-raised)."""
    return server_platform().run_recorded_xray(record_event, run, cancelled)


def tracked_job_cancelled(tracker: Any, job_id: str) -> bool:
    """Whether a job-tracker job was cancelled while it ran."""
    return server_platform().tracked_job_cancelled(tracker, job_id)


@dataclass(frozen=True)
class SearchOrigin:
    """The tool and front door a search event is recorded under."""

    tool: str
    door: str
