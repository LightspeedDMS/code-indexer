"""cancel_job MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change. A core tool: it lived in the X-Ray handler package until
that package moved into the cidx-xray feature (modular plan phase 4c), and it
cancels any background job.
"""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.auth.user_manager import User, UserRole

from ._utils import _lazy_module_attr_or_none, _mcp_response


def _get_background_job_manager() -> Any:
    """The live BackgroundJobManager, or None when the app is not built
    (Bug #1709: a read, never a construction of the app singleton)."""
    return _lazy_module_attr_or_none("background_job_manager")


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


def _register(registry: Dict[str, Any]) -> None:
    """Register the cancel_job handler in the HANDLER_REGISTRY."""
    registry["cancel_job"] = handle_cancel_job
