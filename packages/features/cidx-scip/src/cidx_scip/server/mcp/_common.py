"""Helpers the SCIP MCP handlers and REST routes share.

They lived in the core's handler utilities (``mcp/handlers/_utils.py``) and
access guard while SCIP was core code; they are SCIP's own, so they moved
with the feature. Server pieces come through ``cidx_scip.server._core``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, cast

from cidx_scip.server import _core, runtime

logger = logging.getLogger(__name__)

#: Why every SCIP query is refused while the access service is missing.
SCIP_ACCESS_CONTROL_UNAVAILABLE = (
    "SCIP query refused: repository access control is unavailable, so access "
    "to SCIP indexes cannot be verified"
)

# Logged once per process: with the service missing every SCIP query is
# refused, and one ERROR says so without flooding the log.
_scip_access_refusal_lock = threading.Lock()
_scip_access_refusal_logged = False


def _log_scip_access_refusal_once() -> None:
    """Log QUERY-MIGRATE-015 at ERROR the first time a SCIP query is refused."""
    global _scip_access_refusal_logged
    with _scip_access_refusal_lock:
        if _scip_access_refusal_logged:
            return
        _scip_access_refusal_logged = True
    logger.error(
        _core.format_error_log("QUERY-MIGRATE-015", SCIP_ACCESS_CONTROL_UNAVAILABLE),
        extra=_core.get_log_extra("QUERY-MIGRATE-015"),
    )


def require_scip_access_service(access_filtering_service: Any) -> Any:
    """The access service a SCIP query filters by; refuses the query without one.

    SCIPQueryService treats a missing service as "no filtering" and would
    answer from every repository, so the MCP SCIP handlers fail closed instead:
    every caller is refused, admins included (admin status cannot be
    established without the service either). The REST SCIP routes already
    fail closed the same way (require_repo_access). Logged once per process
    (QUERY-MIGRATE-015).

    Raises:
        AccessFilteringServiceUnavailableError: access_filtering_service is None.
    """
    if access_filtering_service is None:
        _log_scip_access_refusal_once()
        raise _core.access_unavailable_error()(SCIP_ACCESS_CONTROL_UNAVAILABLE)
    return access_filtering_service


def _get_golden_repos_dir() -> str:
    """``golden_repos_dir`` from app.state.

    Raises:
        RuntimeError: it is not configured on app.state.
    """
    golden_repos_dir = getattr(_core.app_state(), "golden_repos_dir", None)
    if golden_repos_dir:
        return cast(str, golden_repos_dir)
    raise RuntimeError(
        "golden_repos_dir not configured in app.state. "
        "Server must set app.state.golden_repos_dir during startup."
    )


def _get_scip_query_service() -> Any:
    """A SCIPQueryService over the server's golden repositories, scoped by
    the access filtering service.

    Raises:
        AccessFilteringServiceUnavailableError: access_filtering_service is
            not on app.state.
        RuntimeError: golden_repos_dir is not configured.
    """
    from cidx_scip.server.query_service import SCIPQueryService

    access_filtering_service = require_scip_access_service(
        getattr(_core.app_state(), "access_filtering_service", None)
    )
    return SCIPQueryService(
        golden_repos_dir=_get_golden_repos_dir(),
        access_filtering_service=access_filtering_service,
    )


def _get_scip_audit_repository() -> Any:
    """The dependency-installation audit repository the feature started.

    Raises:
        RuntimeError: the feature has not started in this process.
    """
    repository = runtime.AUDIT_REPOSITORY.get()
    if repository is None:
        raise RuntimeError("the scip feature's audit repository is not started")
    return repository


def _validate_symbol_format(symbol: Optional[str], param_name: str) -> Optional[str]:
    """None when *symbol* is usable as a call chain endpoint, else why not."""
    if not symbol or not symbol.strip():
        return f"{param_name} cannot be empty"
    return None


def _apply_scip_payload_truncation(
    results: List[Dict[str, Any]],
    *,
    owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Apply payload truncation to SCIP query results (Story #685).

    For SCIP results with large context fields (> preview_size_chars), replaces
    context with context_preview + context_cache_handle. This reduces response
    size while allowing clients to fetch full context on demand.

    Args:
        results: List of SCIP result dicts with optional 'context' field
        owner: Username of the caller whose request created the entries; only
            that user can fetch the returned handles.

    Returns:
        Modified results list with truncation applied to context fields
    """
    payload_cache = getattr(_core.app_state(), "payload_cache", None)
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        context = result_dict.get("context")

        # Handle a missing or None context field
        if "context" not in result_dict or context is None:
            result_dict["context_cache_handle"] = None
            result_dict["context_has_more"] = False
            continue

        try:
            if len(context) > preview_size:
                # Large context: store full content and replace with preview
                cache_handle = payload_cache.store(context, owner=owner)
                result_dict["context_preview"] = context[:preview_size]
                result_dict["context_cache_handle"] = cache_handle
                result_dict["context_has_more"] = True
                result_dict["context_total_size"] = len(context)
                del result_dict["context"]
            else:
                # Small context: keep as-is, add metadata
                result_dict["context_cache_handle"] = None
                result_dict["context_has_more"] = False
        except Exception as e:
            logger.warning(
                _core.format_error_log(
                    "MCP-GENERAL-028",
                    f"Failed to truncate SCIP context: {e}",
                )
            )
            # Keep original context on error, add metadata
            result_dict["context_cache_handle"] = None
            result_dict["context_has_more"] = False

    return results
