"""Groups > Repository Access: bulk grant/revoke for one group.

``POST /admin/groups/repo-access/bulk`` carries the same protections as the
single-cell grant/revoke routes (admin session, elevation, CSRF header).
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from code_indexer.server.auth import dependencies
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.services.constants import (
    CIDX_META_REPO,
    DEFAULT_GROUP_ADMINS,
)

from .routes import (
    _get_golden_repo_manager,
    _get_group_manager,
    _require_admin_session,
    _validate_repo_access_csrf,
)

logger = logging.getLogger(__name__)

group_repo_access_web_router = APIRouter(tags=["groups-web"])

_ACTIONS = frozenset({"grant", "revoke"})
_REASON_CIDX_META = "always accessible"
_REASON_UNKNOWN = "not a registered repository"


class _BulkRefused(Exception):
    """A request refused before anything changed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status)


def _parse_body(body: Any) -> Tuple[int, str, List[str]]:
    if not isinstance(body, dict):
        raise _BulkRefused(400, "Request body must be a JSON object")
    group_id = body.get("group_id")
    action = body.get("action")
    names = body.get("repo_names")
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        raise _BulkRefused(400, "group_id must be an integer")
    if action not in _ACTIONS:
        raise _BulkRefused(400, "action must be 'grant' or 'revoke'")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise _BulkRefused(400, "repo_names must be a list of repository names")
    return group_id, str(action), names


def _apply_bulk_change(
    group_id: int, action: str, names: List[str], actor: str
) -> Dict[str, Any]:
    """Validate and apply one bulk change (synchronous; run off the loop)."""
    group_manager = _get_group_manager()
    group = group_manager.get_group(group_id)
    if group is None:
        raise _BulkRefused(404, "Group not found")
    if group.name == DEFAULT_GROUP_ADMINS:
        raise _BulkRefused(
            400, "The admins group always has access to every repository"
        )
    registered = {
        repo["alias"] for repo in _get_golden_repo_manager().list_golden_repos()
    }
    rejected: List[Dict[str, str]] = []
    valid: List[str] = []
    for name in dict.fromkeys(names):
        if name == CIDX_META_REPO:
            rejected.append({"repo_name": name, "reason": _REASON_CIDX_META})
        elif name not in registered:
            rejected.append({"repo_name": name, "reason": _REASON_UNKNOWN})
        else:
            valid.append(name)
    if action == "grant":
        changed = group_manager.grant_repos_access_audited(valid, group_id, actor=actor)
    else:
        changed = group_manager.revoke_repos_access_audited_aliases(
            valid, group_id, actor=actor
        )
    changed_set = set(changed)
    return {
        "success": True,
        "changed": list(changed),
        "rejected": rejected,
        "unchanged": [name for name in valid if name not in changed_set],
    }


@group_repo_access_web_router.post(
    "/groups/repo-access/bulk",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def bulk_repo_access(request: Request) -> JSONResponse:
    """Grant or revoke many repositories for one group (AJAX only)."""
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return _error(401, "Admin session required")
    is_valid, csrf_error = _validate_repo_access_csrf(request, True, None)
    if not is_valid:
        return _error(403, csrf_error or "Invalid CSRF token")
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(400, "Invalid JSON body")
    try:
        group_id, action, names = _parse_body(body)
        result = await asyncio.to_thread(
            _apply_bulk_change, group_id, action, names, session.username
        )
    except _BulkRefused as refused:
        return _error(refused.status, refused.message)
    except Exception as exc:
        logger.error(
            format_error_log(
                "SCIP-GENERAL-042",
                f"Bulk repository access change failed: {type(exc).__name__}: {exc}",
            )
        )
        return _error(500, "Bulk repository access change failed; nothing was changed")
    return JSONResponse(result)
