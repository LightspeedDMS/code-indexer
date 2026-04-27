"""Admin handlers — auth, users, groups, API keys, MCP credentials, maintenance, logs, config.

Domain module for administrative handlers. Part of the handlers package
modularization (Story #496).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id

from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.mcp.handlers._utils import (
    _coerce_int,
    _mcp_response,
    _parse_json_string_array,
    _get_golden_repos_dir,
)
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation
from . import elevate_session as _elevate_session_module

logger = logging.getLogger(__name__)

# Named constants for admin operations
DEFAULT_AUDIT_LOG_LIMIT = 100
JOB_ID_LENGTH = 8


def _get_legacy():
    """Lazy import of _legacy for shared helpers."""
    from code_indexer.server.mcp.handlers import _legacy

    return _legacy


# ---------------------------------------------------------------------------
# Helpers re-imported from scip module (used by handle_query_audit_logs)
# ---------------------------------------------------------------------------


def _get_scip_helpers():
    """Lazy import of scip helpers used by audit log handler."""
    from code_indexer.server.mcp.handlers.scip import (
        _filter_audit_entries,
        _get_pr_logs_from_service,
        _get_cleanup_logs_from_service,
    )

    return (
        _filter_audit_entries,
        _get_pr_logs_from_service,
        _get_cleanup_logs_from_service,
    )


# =============================================================================
# USER MANAGEMENT
# =============================================================================


def list_users(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all users (admin only)."""
    try:
        all_users = _utils.app_module.user_manager.get_all_users()
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "users": [
                    {
                        "username": u.username,
                        "role": u.role.value,
                        "created_at": u.created_at.isoformat(),
                    }
                    for u in all_users
                ],
                "total": len(all_users),
            }
        )
    except Exception as e:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": str(e), "users": [], "total": 0}
        )


@require_mcp_elevation()
def create_user(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new user (admin only)."""
    try:
        username = params["username"]
        password = params["password"]
        role = UserRole(params["role"])

        new_user = _utils.app_module.user_manager.create_user(
            username=username, password=password, role=role
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "user": {
                    "username": new_user.username,
                    "role": new_user.role.value,
                    "created_at": new_user.created_at.isoformat(),
                },
                "message": f"User '{username}' created successfully",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "user": None})  # type: ignore[no-any-return]


# =============================================================================
# JOB MANAGEMENT
# =============================================================================


def get_job_statistics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get background job statistics.

    BackgroundJobManager doesn't have get_job_statistics method.
    Use get_active_job_count, get_pending_job_count, get_failed_job_count instead.
    """
    try:
        active = _utils.app_module.background_job_manager.get_active_job_count()
        pending = _utils.app_module.background_job_manager.get_pending_job_count()
        failed = _utils.app_module.background_job_manager.get_failed_job_count()

        stats = {
            "active": active,
            "pending": pending,
            "failed": failed,
            "total": active + pending + failed,
        }

        return _mcp_response({"success": True, "statistics": stats})  # type: ignore[no-any-return]
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "statistics": {}})  # type: ignore[no-any-return]


def get_job_details(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed information about a specific job including error messages."""
    try:
        job_id = params.get("job_id")
        if not job_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        job = _utils.app_module.background_job_manager.get_job_status(
            job_id, user.username
        )
        if not job:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": f"Job '{job_id}' not found or access denied",
                }
            )

        return _mcp_response({"success": True, "job": job})  # type: ignore[no-any-return]
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# GLOBAL CONFIG
# =============================================================================


def handle_get_global_config(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_global_config tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    config = ops.get_config()
    return _mcp_response({"success": True, **config})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_set_global_config(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for set_global_config tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    refresh_interval = args.get("refresh_interval")

    if not refresh_interval:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Missing required parameter: refresh_interval"}
        )

    try:
        ops.set_config(refresh_interval)
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": True, "status": "updated", "refresh_interval": refresh_interval}
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# AUTHENTICATION
# =============================================================================


def handle_authenticate(
    args: Dict[str, Any], http_request, http_response
) -> Dict[str, Any]:
    """
    Handler for authenticate tool - validates API key and sets JWT cookie.

    This handler has a special signature (Request, Response) because it needs
    to set cookies in the HTTP response.
    """
    from code_indexer.server.auth.dependencies import jwt_manager, user_manager

    # Lazy import to avoid module import side effects during startup
    from code_indexer.server.auth.token_bucket import rate_limiter
    import math

    username = args.get("username")
    api_key = args.get("api_key")

    if not username or not api_key:
        return _mcp_response({"success": False, "error": "Missing username or api_key"})  # type: ignore[no-any-return]
    # Rate limit check BEFORE validating credentials
    allowed, retry_after = rate_limiter.consume(username)
    if not allowed:
        retry_after_int = int(math.ceil(retry_after))
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": f"Rate limit exceeded. Try again in {retry_after_int} seconds",
                "retry_after": retry_after_int,
            }
        )

    # Validate API key
    user = user_manager.validate_user_api_key(username, api_key)
    if not user:
        return _mcp_response({"success": False, "error": "Invalid credentials"})  # type: ignore[no-any-return]

    # Successful authentication should refund the consumed token
    rate_limiter.refund(username)

    # Create JWT token
    token = jwt_manager.create_token(
        {
            "username": user.username,
            "role": user.role.value,
            "created_at": user.created_at.isoformat(),
        }
    )

    # Set JWT as HttpOnly cookie
    http_response.set_cookie(
        key="cidx_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=jwt_manager.token_expiration_minutes * 60,
    )

    return _mcp_response(  # type: ignore[no-any-return]
        {
            "success": True,
            "message": "Authentication successful",
            "username": user.username,
            "role": user.role.value,
        }
    )


# =============================================================================
# REINDEX / INDEX STATUS
# =============================================================================


def trigger_reindex(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Trigger manual re-indexing for activated repository.

    Args:
        params: {
            "repository_alias": str - Repository alias to reindex
            "index_types": List[str] - Index types (semantic, fts, temporal, scip)
            "clear": bool - Rebuild from scratch vs incremental (default: False)
        }
        user: User requesting reindex

    Returns:
        MCP response with job details
    """
    import time
    from datetime import datetime, timezone
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    start_time = time.time()

    try:
        # Extract parameters
        repo_alias = params.get("repository_alias")
        index_types = params.get("index_types", [])
        clear = params.get("clear", False)

        if not repo_alias:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "repository_alias is required",
                }
            )

        if not index_types:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "index_types is required",
                }
            )

        # Create index manager and trigger reindex
        index_manager = ActivatedRepoIndexManager()
        job_id = index_manager.trigger_reindex(
            repo_alias=repo_alias,
            index_types=index_types,
            clear=clear,
            username=user.username,
        )

        # Calculate estimated duration based on index types
        # Rough estimates: semantic/fts/temporal=5min each, scip=2min
        duration_estimates = {
            "semantic": 5,
            "fts": 5,
            "temporal": 5,
            "scip": 2,
        }
        estimated_minutes = sum(duration_estimates.get(t, 5) for t in index_types)

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"trigger_reindex completed in {elapsed_ms}ms - "
            f"job_id={job_id}, repo={repo_alias}, types={index_types}",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "job_id": job_id,
                "status": "queued",
                "index_types": index_types,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "estimated_duration_minutes": estimated_minutes,
            }
        )

    except ValueError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-060",
                f"trigger_reindex validation error in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": str(e),
            }
        )
    except FileNotFoundError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-061",
                f"trigger_reindex repo not found in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": str(e),
            }
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(
            f"trigger_reindex error in {elapsed_ms}ms: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": str(e),
            }
        )


def get_index_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get indexing status for all index types.

    Args:
        params: {
            "repository_alias": str - Repository alias
        }
        user: User requesting status

    Returns:
        MCP response with index status for all types
    """
    import time
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    start_time = time.time()

    try:
        # Extract parameters
        repo_alias = params.get("repository_alias")

        if not repo_alias:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "repository_alias is required",
                }
            )

        # Create index manager and get status
        index_manager = ActivatedRepoIndexManager()
        status_data = index_manager.get_index_status(
            repo_alias=repo_alias,
            username=user.username,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"get_index_status completed in {elapsed_ms}ms - repo={repo_alias}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Build response with all index types
        response = {
            "success": True,
            "repository_alias": repo_alias,
        }
        response.update(status_data)

        return _mcp_response(response)  # type: ignore[no-any-return]

    except FileNotFoundError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-062",
                f"get_index_status repo not found in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": str(e),
            }
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(
            f"get_index_status error in {elapsed_ms}ms: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": str(e),
            }
        )


# =============================================================================
# ADMIN LOG MANAGEMENT TOOLS
# =============================================================================


def handle_admin_logs_query(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Query operational logs with pagination and filtering.

    Requires admin role. Returns logs from SQLite database with filters for search,
    level, correlation_id, and pagination controls.

    Args:
        args: Query parameters (page, page_size, search, level, sort_order)
        user: Authenticated user (must be admin)

    Returns:
        MCP-compliant response with logs array and pagination metadata
    """
    # Permission check: admin only
    if user.role != UserRole.ADMIN:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": "Permission denied. Admin role required to query logs.",
            }
        )

    # Get log database path from app.state
    log_db_path = getattr(_utils.app_module.app.state, "log_db_path", None)
    if not log_db_path:
        return _mcp_response({"success": False, "error": "Log database not configured"})  # type: ignore[no-any-return]

    # Initialize service
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path)

    # Extract parameters
    page = args.get("page", 1)
    page_size = args.get("page_size", 50)
    sort_order = args.get("sort_order", "desc")
    search = args.get("search")
    level = args.get("level")
    correlation_id = args.get("correlation_id")

    # Parse level (comma-separated string to list)
    levels = None
    if level:
        levels = [lv.strip() for lv in level.split(",")]

    # Query logs
    result = service.query(
        page=page,
        page_size=page_size,
        sort_order=sort_order,
        levels=levels,
        correlation_id=correlation_id,
        search=search,
    )

    return _mcp_response(  # type: ignore[no-any-return]
        {"success": True, "logs": result["logs"], "pagination": result["pagination"]}
    )


def admin_logs_export(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Export operational logs in JSON or CSV format.

    Requires admin role. Returns ALL logs matching filter criteria (no pagination)
    formatted as JSON or CSV for offline analysis or external tool import.

    Args:
        args: Export parameters (format, search, level, correlation_id)
        user: Authenticated user (must be admin)

    Returns:
        MCP-compliant response with format, count, data, and filters metadata
    """
    # Permission check: admin only
    if user.role != UserRole.ADMIN:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": "Permission denied. Admin role required to export logs.",
            }
        )

    # Get log database path from app.state
    log_db_path = getattr(_utils.app_module.app.state, "log_db_path", None)
    if not log_db_path:
        return _mcp_response({"success": False, "error": "Log database not configured"})  # type: ignore[no-any-return]

    # Initialize services
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService
    from code_indexer.server.services.log_export_formatter import LogExportFormatter

    service = LogAggregatorService(log_db_path)
    formatter = LogExportFormatter()

    # Extract parameters
    export_format = args.get("format", "json")
    search = args.get("search")
    level = args.get("level")
    correlation_id = args.get("correlation_id")

    # Validate format
    if export_format not in ["json", "csv"]:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": f"Invalid format '{export_format}'. Must be 'json' or 'csv'.",
            }
        )

    # Parse level (comma-separated string to list)
    levels = None
    if level:
        levels = [lv.strip() for lv in level.split(",")]

    # Query ALL logs matching filters (no pagination)
    logs = service.query_all(
        levels=levels, correlation_id=correlation_id, search=search
    )

    # Format output
    filters = {"search": search, "level": level, "correlation_id": correlation_id}

    if export_format == "json":
        data = formatter.to_json(logs, filters)
    else:  # csv
        data = formatter.to_csv(logs)

    return _mcp_response(  # type: ignore[no-any-return]
        {
            "success": True,
            "format": export_format,
            "count": len(logs),
            "data": data,
            "filters": filters,
        }
    )


# =============================================================================
# Story #722: Session Impersonation for Delegated Queries
# =============================================================================


@require_mcp_elevation()
def handle_set_session_impersonation(
    args: Dict[str, Any], user: User, session_state=None
) -> Dict[str, Any]:
    """
    Handler for set_session_impersonation tool.

    Allows ADMIN users to set or clear session impersonation.
    When impersonating, all subsequent tool calls use the target user's permissions.

    Args:
        args: Tool arguments containing optional 'username' to impersonate
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for managing impersonation

    Returns:
        dict with status and impersonating username (or null if cleared)
    """
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.auth.audit_logger import password_audit_logger

    username = args.get("username")

    # Check if user is ADMIN
    if user.role != UserRole.ADMIN:
        password_audit_logger.log_impersonation_denied(
            actor_username=user.username,
            target_username=username or "(clear)",
            reason="Impersonation requires ADMIN role",
            session_id=session_state.session_id if session_state else "unknown",
            ip_address="unknown",
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {"status": "error", "error": "Impersonation requires ADMIN role"}
        )

    # Handle clearing impersonation
    if username is None:
        if session_state and session_state.is_impersonating:
            previous_target = session_state.impersonated_user.username
            session_state.clear_impersonation()
            password_audit_logger.log_impersonation_cleared(
                actor_username=user.username,
                previous_target=previous_target,
                session_id=session_state.session_id,
                ip_address="unknown",
            )
        return _mcp_response({"status": "ok", "impersonating": None})  # type: ignore[no-any-return]

    # Look up target user and set impersonation
    try:
        # Bug fix: Use _utils.app_module.user_manager (properly configured with SQLite backend)
        # instead of creating new UserManager() which defaults to JSON file storage
        target_user = _utils.app_module.user_manager.get_user(username)

        if target_user is None:
            return _mcp_response(  # type: ignore[no-any-return]
                {"status": "error", "error": f"User not found: {username}"}
            )

        if session_state:
            session_state.set_impersonation(target_user)
            password_audit_logger.log_impersonation_set(
                actor_username=user.username,
                target_username=username,
                session_id=session_state.session_id,
                ip_address="unknown",
            )

        return _mcp_response({"status": "ok", "impersonating": username})  # type: ignore[no-any-return]

    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-118",
                f"Error in set_session_impersonation: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"status": "error", "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# GROUP & ACCESS MANAGEMENT HANDLERS (Story #742)
# =============================================================================


def _get_group_manager():
    """Get the GroupAccessManager from app.state."""
    return getattr(_utils.app_module.app.state, "group_manager", None)


def _validate_group_id(
    args: Dict[str, Any], group_manager: Any
) -> tuple[Optional[int], Any, Optional[Dict[str, Any]]]:
    """Validate and parse group_id, check group exists.

    Returns:
        Tuple of (group_id, group, error_response) - error_response is None on success
    """
    group_id_str = args.get("group_id", "")
    if not group_id_str:
        return (
            None,
            None,
            _mcp_response(
                {"success": False, "error": "Missing required parameter: group_id"}
            ),
        )
    try:
        group_id = int(group_id_str)
    except ValueError:
        return (
            None,
            None,
            _mcp_response(
                {"success": False, "error": f"Invalid group_id: {group_id_str}"}
            ),
        )
    group = group_manager.get_group(group_id)
    if not group:
        return (
            None,
            None,
            _mcp_response({"success": False, "error": f"Group not found: {group_id}"}),
        )
    return group_id, group, None


def handle_list_groups(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all groups with member counts and repository access information."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        groups = group_manager.get_all_groups()
        result_groups = []
        for group in groups:
            member_count = group_manager.get_user_count_in_group(group.id)
            repos = group_manager.get_group_repos(group.id)
            result_groups.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "member_count": member_count,
                    "repo_count": len(repos),
                }
            )
        return _mcp_response({"success": True, "groups": result_groups})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-129",
                f"Error in handle_list_groups: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_create_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new custom group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        name = args.get("name", "")
        if not name:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: name"}
            )

        try:
            group = group_manager.create_group(
                name=name, description=args.get("description", "")
            )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_create",
                target_type="group",
                target_id=str(group.id),
                details=f"Created group '{group.name}' via MCP",
            )
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": True, "group_id": group.id, "name": group.name}
            )
        except ValueError as e:
            return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-130",
                f"Error in handle_create_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_get_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed information about a specific group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        members = group_manager.get_users_in_group(group_id)
        repos = group_manager.get_group_repos(group_id)
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "members": members,
                "repos": repos,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-131",
                f"Error in handle_get_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_update_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Update a custom group's name and/or description."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, _, error = _validate_group_id(args, group_manager)
        if error:
            return error

        try:
            updated_group = group_manager.update_group(
                group_id=group_id,
                name=args.get("name"),
                description=args.get("description"),
            )
            if not updated_group:
                return _mcp_response(  # type: ignore[no-any-return]
                    {"success": False, "error": f"Group not found: {group_id}"}
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_update",
                target_type="group",
                target_id=str(group_id),
                details=f"Updated group '{updated_group.name}' via MCP",
            )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except ValueError as e:
            return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-132",
                f"Error in handle_update_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_delete_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete a custom group."""
    from ....services.group_access_manager import (
        DefaultGroupCannotBeDeletedError,
        GroupHasUsersError,
    )

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error
        group_name = group.name

        try:
            result = group_manager.delete_group(group_id)
            if not result:
                return _mcp_response(  # type: ignore[no-any-return]
                    {"success": False, "error": f"Group not found: {group_id}"}
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_delete",
                target_type="group",
                target_id=str(group_id),
                details=f"Deleted group '{group_name}' via MCP",
            )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except (DefaultGroupCannotBeDeletedError, GroupHasUsersError) as e:
            return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-133",
                f"Error in handle_delete_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_add_member_to_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Assign a user to a group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        user_id = args.get("user_id", "")
        if not user_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: user_id"}
            )

        group_manager.assign_user_to_group(
            user_id=user_id, group_id=group_id, assigned_by=user.username
        )
        group_manager.log_audit(
            admin_id=user.username,
            action_type="user_group_change",
            target_type="user",
            target_id=user_id,
            details=f"Assigned user '{user_id}' to group '{group.name}' via MCP",
        )
        return _mcp_response({"success": True})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-134",
                f"Error in handle_add_member_to_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_remove_member_from_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Remove a user from a group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        user_id = args.get("user_id", "")
        if not user_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: user_id"}
            )

        group_manager.remove_user_from_group(user_id=user_id, group_id=group_id)
        group_manager.log_audit(
            admin_id=user.username,
            action_type="user_group_change",
            target_type="user",
            target_id=user_id,
            details=f"Removed user '{user_id}' from group '{group.name}' via MCP",
        )
        return _mcp_response({"success": True})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-135",
                f"Error in handle_remove_member_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_add_repos_to_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Grant a group access to one or more repositories."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_names = _parse_json_string_array(args.get("repo_names", []))
        if not repo_names:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: repo_names"}
            )

        added_count = 0
        for repo_name in repo_names:
            if group_manager.grant_repo_access(
                repo_name=repo_name, group_id=group_id, granted_by=user.username
            ):
                added_count += 1
                group_manager.log_audit(
                    admin_id=user.username,
                    action_type="repo_access_grant",
                    target_type="repo",
                    target_id=repo_name,
                    details=f"Granted access to '{repo_name}' for group '{group.name}' via MCP",
                )
        return _mcp_response({"success": True, "added_count": added_count})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-TOOL-042",
                f"Error in handle_add_repos_to_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_remove_repo_from_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Revoke a group's access to a single repository."""
    from ....services.group_access_manager import CidxMetaCannotBeRevokedError

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_name = args.get("repo_name", "")
        if not repo_name:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: repo_name"}
            )

        try:
            if not group_manager.revoke_repo_access(
                repo_name=repo_name, group_id=group_id
            ):
                return _mcp_response(  # type: ignore[no-any-return]
                    {
                        "success": False,
                        "error": f"Repository '{repo_name}' not found in group's access list",
                    }
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="repo_access_revoke",
                target_type="repo",
                target_id=repo_name,
                details=f"Revoked access to '{repo_name}' from group '{group.name}' via MCP",
            )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except CidxMetaCannotBeRevokedError:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "cidx-meta access cannot be revoked from any group",
                }
            )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-001",
                f"Error in handle_remove_repo_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_bulk_remove_repos_from_group(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Revoke a group's access to multiple repositories."""
    from ....services.group_access_manager import CidxMetaCannotBeRevokedError
    from ....services.constants import CIDX_META_REPO

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_names = _parse_json_string_array(args.get("repo_names", []))
        if not repo_names:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: repo_names"}
            )

        removed_count = 0
        for repo_name in repo_names:
            if repo_name == CIDX_META_REPO:
                continue
            try:
                if group_manager.revoke_repo_access(
                    repo_name=repo_name, group_id=group_id
                ):
                    removed_count += 1
                    group_manager.log_audit(
                        admin_id=user.username,
                        action_type="repo_access_revoke",
                        target_type="repo",
                        target_id=repo_name,
                        details=f"Revoked access to '{repo_name}' from group '{group.name}' via MCP",
                    )
            except CidxMetaCannotBeRevokedError:
                continue
        return _mcp_response({"success": True, "removed_count": removed_count})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-002",
                f"Error in handle_bulk_remove_repos_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# User Self-Service API Keys
# =============================================================================


def handle_list_api_keys(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all API keys for the authenticated user."""
    try:
        keys = _utils.app_module.user_manager.get_api_keys(user.username)
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "keys": [
                    {
                        "id": k.get("key_id", k.get("id", "")),
                        "description": k.get("name", k.get("description", "")),
                        "created_at": k.get("created_at", ""),
                        "last_used": k.get("last_used_at"),
                    }
                    for k in keys
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-003",
                f"Error in handle_list_api_keys: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_create_api_key(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new API key for the authenticated user."""
    try:
        from code_indexer.server.auth.api_key_manager import ApiKeyManager

        description = args.get("description", "")
        api_key_manager = ApiKeyManager(user_manager=_utils.app_module.user_manager)
        api_key, key_id = api_key_manager.generate_key(user.username, name=description)
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "key_id": key_id,
                "api_key": api_key,
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-004",
                f"Error in handle_create_api_key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_delete_api_key(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete an API key belonging to the authenticated user."""
    try:
        key_id = args.get("key_id", "")
        if not key_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: key_id",
                }
            )

        result = _utils.app_module.user_manager.delete_api_key(user.username, key_id)
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-005",
                f"Error in handle_delete_api_key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# User Self-Service MCP Credentials
# =============================================================================


def handle_list_mcp_credentials(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all MCP credentials for the authenticated user."""
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
                f"Error in handle_list_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_create_mcp_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new MCP credential for the authenticated user."""
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
                f"Error in handle_create_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_delete_mcp_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete an MCP credential belonging to the authenticated user."""
    try:
        credential_id = args.get("credential_id", "")
        if not credential_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: credential_id",
                }
            )

        result = dependencies.mcp_credential_manager.revoke_credential(
            user.username, credential_id
        )
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-001",
                f"Error in handle_delete_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# Admin Operations - Part 1
# =============================================================================


def handle_admin_list_user_mcp_credentials(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """List all MCP credentials for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        if not username:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
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
                f"Error in handle_admin_list_user_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_admin_create_user_mcp_credential(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Create a new MCP credential for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        if not username:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
            )

        description = args.get("description", "")
        result = dependencies.mcp_credential_manager.generate_credential(
            username, name=description
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
                f"Error in handle_admin_create_user_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# Admin Operations - Part 2
# =============================================================================


def handle_admin_delete_user_mcp_credential(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Delete an MCP credential for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        credential_id = args.get("credential_id", "")

        if not username:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
            )
        if not credential_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Missing required parameter: credential_id",
                }
            )

        result = dependencies.mcp_credential_manager.revoke_credential(
            username, credential_id
        )
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-004",
                f"Error in handle_admin_delete_user_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_admin_list_all_mcp_credentials(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """List all MCP credentials across all users (admin only)."""
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

        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "credentials": all_credentials,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-005",
                f"Error in handle_admin_list_all_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# SYSTEM CREDENTIAL HANDLERS (Story #275)
# =============================================================================


def handle_admin_list_system_mcp_credentials(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """List system-managed MCP credentials owned by the admin user (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Permission denied: admin role required",
                }
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
                f"Error in handle_admin_list_system_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# ADMIN OPERATIONS MCP HANDLERS (Story #744)
# Audit Logs, Maintenance Mode
# =============================================================================


def handle_query_audit_logs(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Query security audit logs with optional filtering (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to query audit logs.",
                }
            )

        # Support both "action" and "action_type" as filter parameter names
        action_filter = args.get("action") or args.get("action_type")

        (
            _filter_audit_entries,
            _get_pr_logs_from_service,
            _get_cleanup_logs_from_service,
        ) = _get_scip_helpers()

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        pr_logs = _get_pr_logs_from_service(limit=limit)
        cleanup_logs = _get_cleanup_logs_from_service(limit=limit)

        all_entries = [
            {
                "timestamp": log.get("timestamp", ""),
                "user": log.get("repo_alias", ""),
                "action": log.get("event_type") or log.get("action_type", ""),
                "action_type": log.get("event_type") or log.get("action_type", ""),
                "resource": log.get("pr_url", ""),
                "details": log,
            }
            for log in pr_logs
        ] + [
            {
                "timestamp": log.get("timestamp", ""),
                "user": "system",
                "action": log.get("event_type") or log.get("action_type", ""),
                "action_type": log.get("event_type") or log.get("action_type", ""),
                "resource": log.get("repo_path", ""),
                "details": log,
            }
            for log in cleanup_logs
        ]

        # Story #458: Also query the main audit_logs table for general admin actions
        # (e.g., open_delegation_executed, group/user management events)
        import code_indexer.server.app as _app_module

        _svc = getattr(getattr(_app_module, "app", None), "state", None)
        _audit_svc = getattr(_svc, "audit_service", None) if _svc else None
        if _audit_svc is not None:
            audit_rows, _ = _audit_svc.query(
                action_type=action_filter if action_filter else None,
                admin_id=args.get("user"),
                date_from=args.get("from_date"),
                date_to=args.get("to_date"),
                limit=limit,
            )
            for row in audit_rows:
                details_str = row.get("details") or "{}"
                try:
                    details_obj = (
                        json.loads(details_str)
                        if isinstance(details_str, str)
                        else details_str
                    )
                except (ValueError, TypeError):
                    details_obj = {}
                all_entries.append(
                    {
                        "timestamp": row.get("timestamp", ""),
                        "user": row.get("admin_id", ""),
                        "action": row.get("action_type", ""),
                        "action_type": row.get("action_type", ""),
                        "target_type": row.get("target_type", ""),
                        "target_id": row.get("target_id", ""),
                        "admin_id": row.get("admin_id", ""),
                        "resource": row.get("target_id", ""),
                        "details": details_obj,
                    }
                )

        filtered = _filter_audit_entries(
            all_entries,
            args.get("user"),
            action_filter,
            args.get("from_date"),
            args.get("to_date"),
            limit,
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": True, "entries": filtered, "total": len(filtered)}
        )
    except RuntimeError as e:
        logger.critical("AuditLogService configuration error: %s", e)
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": f"Server configuration error: {e}"}
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-006",
                f"Error in handle_query_audit_logs: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_enter_maintenance_mode(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Enter server maintenance mode (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to enter maintenance mode.",
                }
            )

        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        result = state.enter_maintenance_mode()
        if args.get("message"):
            result["custom_message"] = args["message"]
        return _mcp_response({"success": True, **result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-007",
                f"Error in handle_enter_maintenance_mode: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_exit_maintenance_mode(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Exit server maintenance mode (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to exit maintenance mode.",
                }
            )

        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        result = state.exit_maintenance_mode()
        return _mcp_response({"success": True, **result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-008",
                f"Error in handle_exit_maintenance_mode: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


def handle_get_maintenance_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get current server maintenance mode status (any authenticated user)."""
    try:
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        status = state.get_status()
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "in_maintenance": status.get("maintenance_mode", False),
                "message": status.get("message"),
                "since": status.get("entered_at"),
                "drained": status.get("drained", False),
                "running_jobs": status.get("running_jobs", 0),
                "queued_jobs": status.get("queued_jobs", 0),
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-009",
                f"Error in handle_get_maintenance_status: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# DEPENDENCY ANALYSIS (Story #195)
# =============================================================================


def handle_trigger_dependency_analysis(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Trigger dependency map analysis manually (Story #195).

    Args:
        args: Tool arguments with optional mode ("full" or "delta")
        user: The authenticated user making the request

    Returns:
        MCP response with job_id, mode, and status
    """
    from code_indexer.server.services.config_service import get_config_service

    try:
        # AC4: Default mode is delta
        mode = args.get("mode", "delta") or "delta"

        # AC8: Validate mode parameter
        if mode not in ["full", "delta"]:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": f"Invalid mode '{mode}'. Must be 'full' or 'delta'.",
                    "job_id": None,
                }
            )

        # AC6: Check if feature is enabled
        _server_config = get_config_service().get_config()
        _ci_config = (
            _server_config.claude_integration_config if _server_config else None
        )
        if not _ci_config or not getattr(_ci_config, "dependency_map_enabled", False):
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Dependency map analysis is disabled",
                    "job_id": None,
                }
            )

        # AC5: Check if analysis is already running
        dependency_map_service = getattr(
            _utils.app_module.app.state, "dependency_map_service", None
        )
        if not dependency_map_service:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Dependency map service not available",
                    "job_id": None,
                }
            )

        if not dependency_map_service.is_available():
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Dependency map analysis already in progress",
                    "job_id": None,
                }
            )

        # AC5 (Story #919): dry-run graph-repair mode — synchronous, no background job
        dry_run_raw = args.get("dry_run_graph_only", False)
        if dry_run_raw is not False and not isinstance(dry_run_raw, bool):
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "dry_run_graph_only must be a boolean",
                    "job_id": None,
                }
            )
        dry_run_graph_only: bool = bool(dry_run_raw)
        if dry_run_graph_only:
            dry_run_report = dependency_map_service.run_graph_repair_dry_run()
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": True,
                    "job_id": None,
                    "mode": mode,
                    "status": "completed",
                    "message": "Dry-run graph-channel repair report",
                    "graph_repair_dry_run_report": dry_run_report,
                }
            )

        # Generate job ID
        job_id = f"dep-map-{mode}-{uuid.uuid4().hex[:8]}-{int(datetime.now(timezone.utc).timestamp())}"

        # AC2/AC3: Spawn background thread for analysis
        def run_analysis_job():
            """Background job to run dependency map analysis."""
            try:
                if mode == "full":
                    dependency_map_service.run_full_analysis(job_id=job_id)
                else:
                    dependency_map_service.run_delta_analysis(job_id=job_id)
            except Exception as e:
                logger.error(
                    format_error_log(
                        "DEPMAP-TRIGGER-001",
                        f"Background dependency map analysis failed: {e}",
                        extra={
                            "correlation_id": get_correlation_id(),
                            "job_id": job_id,
                        },
                    )
                )

        # Start background daemon thread
        thread = threading.Thread(target=run_analysis_job, daemon=True)
        thread.start()

        # AC2/AC3: Return job_id immediately
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": True,
                "job_id": job_id,
                "mode": mode,
                "status": "queued",
                "message": f"Dependency map {mode} analysis started",
            }
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "DEPMAP-TRIGGER-002",
                f"Error triggering dependency map analysis: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e), "job_id": None})  # type: ignore[no-any-return]


# =============================================================================
# Registration
# =============================================================================


def _register(registry: dict) -> None:
    """Register admin handlers into HANDLER_REGISTRY."""
    registry["elevate_session"] = _elevate_session_module.elevate_session
    registry["list_users"] = list_users
    registry["create_user"] = create_user
    registry["get_job_statistics"] = get_job_statistics
    registry["get_job_details"] = get_job_details
    registry["get_global_config"] = handle_get_global_config
    registry["set_global_config"] = handle_set_global_config
    registry["authenticate"] = handle_authenticate
    registry["trigger_reindex"] = trigger_reindex
    registry["get_index_status"] = get_index_status
    registry["admin_logs_query"] = handle_admin_logs_query
    registry["admin_logs_export"] = admin_logs_export
    registry["set_session_impersonation"] = handle_set_session_impersonation
    registry["list_groups"] = handle_list_groups
    registry["create_group"] = handle_create_group
    registry["get_group"] = handle_get_group
    registry["update_group"] = handle_update_group
    registry["delete_group"] = handle_delete_group
    registry["add_member_to_group"] = handle_add_member_to_group
    registry["remove_member_from_group"] = handle_remove_member_from_group
    registry["add_repos_to_group"] = handle_add_repos_to_group
    registry["remove_repo_from_group"] = handle_remove_repo_from_group
    registry["bulk_remove_repos_from_group"] = handle_bulk_remove_repos_from_group
    registry["list_api_keys"] = handle_list_api_keys
    registry["create_api_key"] = handle_create_api_key
    registry["delete_api_key"] = handle_delete_api_key
    registry["list_mcp_credentials"] = handle_list_mcp_credentials
    registry["create_mcp_credential"] = handle_create_mcp_credential
    registry["delete_mcp_credential"] = handle_delete_mcp_credential
    registry["admin_list_user_mcp_credentials"] = handle_admin_list_user_mcp_credentials
    registry["admin_create_user_mcp_credential"] = (
        handle_admin_create_user_mcp_credential
    )
    registry["admin_delete_user_mcp_credential"] = (
        handle_admin_delete_user_mcp_credential
    )
    registry["admin_list_all_mcp_credentials"] = handle_admin_list_all_mcp_credentials
    registry["admin_list_system_mcp_credentials"] = (
        handle_admin_list_system_mcp_credentials
    )
    registry["query_audit_logs"] = handle_query_audit_logs
    # Story #924: enter/exit maintenance MCP tools removed — endpoints
    # are localhost-only and auto-updater driven, not exposed via MCP.
    registry["get_maintenance_status"] = handle_get_maintenance_status
    registry["trigger_dependency_analysis"] = handle_trigger_dependency_analysis
