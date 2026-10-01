"""Cached-payload retrieval and temporal-job-poll handlers.

Domain module for search handlers. Part of the handlers package
modularization (Story #496).

Issue #1935 Part: split out of the former flat search.py (2,498 lines)
into this package, one module per domain seam, each < 1,000 lines.
Pure move -- zero behaviour change.

NOTE: Functions in this module were extracted verbatim from _legacy.py
(via search.py). Pre-existing method lengths and duplication are
preserved intentionally to avoid behavioral changes during extraction.
Refactoring is tracked separately.
"""

import logging
from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.temporal_poll_job_status import (
    poll_temporal_job_status,
)
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)

from .. import _utils
from .._utils import _coerce_int, _get_access_filtering_service, _mcp_response

logger = logging.getLogger("code_indexer.server.mcp.handlers.search")


def handle_get_cached_content(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_cached_content tool.

    Retrieves cached content by handle with pagination support.
    Implements AC5 of Story #679.
    """
    # Story #331 AC8: Accepted risk - cache handles are UUID4 (unguessable)
    # and short-lived (TTL-based). Cross-user cache access requires knowing
    # the exact UUID, which is not feasible. Full user-scoping tracking
    # would add complexity without meaningful security benefit.
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    handle = args.get("handle")
    page = max(0, _coerce_int(args.get("page"), 0))

    if not handle:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: handle",
            }
        )

    # Bug #1928 P3 (Opus): a pages-v1-prefixed handle is an xray manifest,
    # not ordinary content -- routing it through fetch_cached_page here
    # would corrupt pagination (1-indexed vs this tool's 0-indexed
    # convention) or leak internals, so reject it with a clear message.
    from code_indexer.server.mcp.handlers import xray_truncation

    if handle.startswith(xray_truncation._PAGES_V1_HANDLE_PREFIX):
        return _mcp_response(
            {
                "success": False,
                "error": "wrong_tool_for_handle",
                "message": (
                    "This handle is an xray truncated-result manifest -- "
                    "fetch it via the `cidx_fetch_cached_payload` MCP tool "
                    "(1-indexed page parameter), not get_cached_content."
                ),
            }
        )

    payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return _mcp_response(
            {
                "success": False,
                "error": "Cache service not available",
            }
        )

    try:
        result = payload_cache.retrieve(handle, page=page)
        return _mcp_response(
            {
                "success": True,
                "content": result.content,
                "page": result.page,
                "total_pages": result.total_pages,
                "has_more": result.has_more,
            }
        )
    except CacheNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-117",
                f"Cache handle not found or expired: {handle}",
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "cache_expired",
                "message": str(e),
                "handle": handle,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in get_cached_content: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_poll_search_job(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for poll_search_job tool (Story #1400 Phase 8).

    Non-blocking check for an async-hybrid temporal query job. Thin reader
    around poll_temporal_job_status: ownership/authorization is via
    background_job_manager.get_job_status(job_id, username, is_admin)
    (returns None for BOTH not-found AND unauthorized, by design -- this
    handler cannot and must not try to distinguish them).
    """
    job_id = args.get("job_id")
    if not job_id:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: job_id"}
        )

    bjm = getattr(_utils.app_module, "background_job_manager", None)
    if bjm is None:
        return _mcp_response(
            {"success": False, "error": "Background job service not available"}
        )

    is_admin = hasattr(user, "role") and user.role == UserRole.ADMIN
    job_status = bjm.get_job_status(job_id, user.username, is_admin=is_admin)

    def _read_snapshot() -> Optional[Dict[str, Any]]:
        _payload_cache = getattr(_utils.app_module.app.state, "payload_cache", None)
        snapshot: Optional[Dict[str, Any]] = read_temporal_snapshot(
            _payload_cache, job_id
        )
        return snapshot

    result = poll_temporal_job_status(
        job_status=job_status,
        read_snapshot_fn=_read_snapshot,
        access_filtering_service=_get_access_filtering_service(),
        username=user.username,
        is_admin=is_admin,
        config_service=get_config_service(),
    )
    result["success"] = result["status"] != "not_found"
    return _mcp_response(result)
