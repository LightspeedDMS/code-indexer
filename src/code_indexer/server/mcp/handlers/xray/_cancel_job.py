"""cancel_job MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change.
"""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.auth.user_manager import User, UserRole

from ._infra import _get_background_job_manager

from .._utils import _mcp_response


def handle_cancel_job(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the cancel_job tool.

    Cancels a running or pending background job. For xray_search/xray_explore
    jobs with registered child processes, sends SIGTERM then SIGKILL.
    """
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    job_id = params.get("job_id")
    if not job_id:
        return _mcp_response({"success": False, "message": "job_id is required"})

    bjm = _get_background_job_manager()
    is_admin = hasattr(user, "role") and user.role == UserRole.ADMIN
    result = bjm.cancel_job(job_id, user.username, is_admin)
    return _mcp_response(result)
