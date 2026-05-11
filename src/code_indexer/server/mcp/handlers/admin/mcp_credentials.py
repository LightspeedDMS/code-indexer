"""Unified MCP credential handlers (Story #989).

Consolidates 8 scattered credential tools into 2 action-param tools:
  - handle_list_mcp_credentials: scope-based dispatcher (self/user/all/system)
  - handle_manage_mcp_credential: action-based dispatcher (create/delete)

Elevation rules preserved exactly:
  - _list_self: UNDECORATED (old handle_list_mcp_credentials was undecorated)
  - all other inner handlers: @require_mcp_elevation()
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation
from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.mcp.handlers._utils import _mcp_response

logger = logging.getLogger(__name__)


# =============================================================================
# Private inner handlers
# =============================================================================


def _list_self(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """List caller's own credentials — NO elevation (mirrors old handle_list_mcp_credentials)."""
    try:
        credentials = dependencies.mcp_credential_manager.get_credentials(user.username)
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "credentials": [
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                    for c in credentials
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-006",
                f"Error in _list_self: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _list_user(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """List a specific user's credentials (admin) — elevation required."""
    try:
        username = args.get("username", "")
        if not username:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: username"}
            )
        credentials = dependencies.mcp_credential_manager.get_credentials(username)
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "credentials": [
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                    for c in credentials
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-002",
                f"Error in _list_user: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _list_all(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """List all users' credentials (admin) — elevation required."""
    try:
        all_credentials = []
        all_users = _utils.app_module.user_manager.get_all_users()
        for target_user in all_users:
            user_creds = dependencies.mcp_credential_manager.get_credentials(
                target_user.username
            )
            for c in user_creds:
                all_credentials.append(
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "username": target_user.username,
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                )
        return _mcp_response({"success": True, "credentials": all_credentials})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-005",
                f"Error in _list_all: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _list_system(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """List system-managed credentials (admin role required) — elevation required."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Permission denied: admin role required"}
            )
        system_credentials = dependencies.user_manager.get_system_mcp_credentials()
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "system_credentials": system_credentials,
                "count": len(system_credentials),
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-CRED-001",
                f"Error in _list_system: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _create_self(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Create a credential for the caller — elevation required."""
    try:
        description = args.get("description", "")
        result = dependencies.mcp_credential_manager.generate_credential(
            user.username, name=description
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "credential_id": result.get("credential_id", ""),
                "credential": result.get("client_secret", ""),
                "client_id": result.get("client_id", ""),
                "client_secret": result.get("client_secret", ""),
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-007",
                f"Error in _create_self: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _delete_self(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Delete a caller's credential — elevation required."""
    try:
        credential_id = args.get("credential_id", "")
        if not credential_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: credential_id"}
            )
        result = dependencies.mcp_credential_manager.revoke_credential(
            user.username, credential_id
        )
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-001",
                f"Error in _delete_self: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _create_user(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Create a credential for a target user (admin) — elevation required."""
    try:
        target_user = args.get("target_user", "")
        if not target_user:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: target_user"}
            )
        description = args.get("description", "")
        result = dependencies.mcp_credential_manager.generate_credential(
            target_user, name=description
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "credential_id": result.get("credential_id", ""),
                "credential": result.get("client_secret", ""),
                "client_id": result.get("client_id", ""),
                "client_secret": result.get("client_secret", ""),
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-003",
                f"Error in _create_user: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _delete_user(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Delete a target user's credential (admin) — elevation required."""
    try:
        target_user = args.get("target_user", "")
        credential_id = args.get("credential_id", "")
        if not target_user:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: target_user"}
            )
        if not credential_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: credential_id"}
            )
        result = dependencies.mcp_credential_manager.revoke_credential(
            target_user, credential_id
        )
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-004",
                f"Error in _delete_user: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# Public unified dispatchers
# =============================================================================


def handle_list_mcp_credentials(
    args: Dict[str, Any], user: User, **kwargs: Any
) -> Dict[str, Any]:
    """List MCP credentials by scope.

    scope='self'   — list caller's own (no elevation)
    scope='user'   — list specific user's (admin, elevation required)
    scope='all'    — list all users' (admin, elevation required)
    scope='system' — list system-managed (admin role + elevation required)
    """
    scope = args.get("scope")
    if not scope:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Missing required parameter: scope"}
        )

    if scope == "self":
        return _list_self(args, user, **kwargs)
    elif scope == "user":
        username = args.get("username")
        if not username:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: username (required when scope='user')",
                }
            )
        return _list_user(args, user, **kwargs)  # type: ignore[no-any-return]
    elif scope == "all":
        return _list_all(args, user, **kwargs)  # type: ignore[no-any-return]
    elif scope == "system":
        return _list_system(args, user, **kwargs)  # type: ignore[no-any-return]
    else:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": f"Unknown scope: {scope!r}. Valid: self, user, all, system",
            }
        )


handle_list_mcp_credentials.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]


def handle_manage_mcp_credential(
    args: Dict[str, Any], user: User, **kwargs: Any
) -> Dict[str, Any]:
    """Create or delete MCP credentials for self or another user.

    action='create' without target_user — create for caller (elevation required)
    action='delete' without target_user — delete for caller (elevation required)
    action='create' with target_user    — admin create for user (elevation required)
    action='delete' with target_user    — admin delete for user (elevation required)
    """
    action = args.get("action")
    if not action:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Missing required parameter: action"}
        )

    target_user = args.get("target_user")

    if action == "create":
        if target_user:
            return _create_user(args, user, **kwargs)  # type: ignore[no-any-return]
        else:
            return _create_self(args, user, **kwargs)  # type: ignore[no-any-return]
    elif action == "delete":
        if target_user:
            return _delete_user(args, user, **kwargs)  # type: ignore[no-any-return]
        else:
            return _delete_self(args, user, **kwargs)  # type: ignore[no-any-return]
    else:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": f"Unknown action: {action!r}. Valid: create, delete",
            }
        )


handle_manage_mcp_credential.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]
