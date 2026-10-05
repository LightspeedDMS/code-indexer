"""Admin handlers — auth, users, groups, API keys, MCP credentials, maintenance, logs, config.

Domain module for administrative handlers. Part of the handlers package
modularization (Story #496).
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies as dependencies
from code_indexer.server.auth.login_outcome import complete_login, reject_login
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)

from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.mcp.handlers._utils import (
    _admin_role_first,
    _coerce_int,
    _mcp_response,
    _parse_json_string_array,
    _get_golden_repos_dir,
)
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation
from code_indexer.server.services.audit_log_query import (
    AUDIT_LOG_MAX_LIMIT,
    DEFAULT_AUDIT_LOG_LIMIT,
    DIRECTION_OLDER,
    PR_URL_FIELD,
    TIER_ALL,
    AuditAggregate,
    AuditQueryError,
    CanonicalAuditRow,
    aggregate_fields,
    build_filters,
    decode_details,
    page_fields,
    query_audit_log,
    row_fields,
)
from . import elevate_session as _elevate_session_module
from .mcp_credentials import (
    handle_list_mcp_credentials,
    handle_manage_mcp_credential,
)

logger = logging.getLogger(__name__)

# Named constants for admin operations
JOB_ID_LENGTH = 8

# Issue #1646: handle_query_audit_logs maps `page` onto the shared read
# function's compatibility offset. This clamps `page` to a sane maximum so a
# pathological caller-supplied value can't produce an unbounded OFFSET.
_AUDIT_LOG_MAX_PAGE = 10_000


def _get_legacy():
    """Lazy import of _legacy for shared helpers."""
    from code_indexer.server.mcp.handlers import _legacy

    return _legacy


# =============================================================================
# USER MANAGEMENT
# =============================================================================


@_admin_role_first
@require_mcp_elevation()
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


def _assign_new_user_to_default_group(
    username: str, role: UserRole, assigned_by: str
) -> None:
    """
    Story #1593 AC7: auto-assign a freshly created user to a default
    group so fail-closed tool-access enforcement never strands them.
    Non-fatal: a group-assignment failure must not undo the
    already-created user account, so any error is logged and swallowed.
    """
    try:
        from ....services.constants import DEFAULT_GROUP_ADMINS, DEFAULT_GROUP_USERS

        group_manager = _get_group_manager()
        if group_manager is None:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-176",
                    f"Group manager not configured -- skipping auto-assignment "
                    f"for user '{username}'",
                )
            )
            return
        target_group_name = (
            DEFAULT_GROUP_ADMINS if role == UserRole.ADMIN else DEFAULT_GROUP_USERS
        )
        target_group = group_manager.get_group_by_name(target_group_name)
        if target_group is None:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-177",
                    f"Default group '{target_group_name}' not found for "
                    f"user '{username}' -- skipping auto-assignment",
                )
            )
            return
        group_manager.ensure_user_group_membership(
            username,
            target_group,
            assigned_by=assigned_by,
            audit_details={
                "group": target_group.name,
                "reason": "auto_assign_on_creation",
                "source": "mcp",
            },
        )
    except Exception as group_error:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-178",
                f"Failed to auto-assign user '{username}' to group: {group_error}",
            )
        )


@_admin_role_first
@require_mcp_elevation()
def create_user(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new user (admin only)."""
    try:
        username = params["username"]
        password = params["password"]
        role = UserRole(params["role"])

        new_user = _utils.app_module.user_manager.create_user_audited(
            username, password, role, actor=user.username
        )

        _assign_new_user_to_default_group(username, role, user.username)

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


@_admin_role_first
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
        ops.set_config(refresh_interval, actor=user.username)
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
        # The attempt's one outcome row; the typed name is recorded only
        # when it names an existing account.
        reject_login(
            username,
            account_exists=user_manager.get_user(username) is not None,
            method="api_key",
            stage="credentials",
            reason="bad_credentials",
        )
        return _mcp_response({"success": False, "error": "Invalid credentials"})  # type: ignore[no-any-return]

    # Successful authentication should refund the consumed token
    rate_limiter.refund(username)

    # Create JWT token (the login's one success row is recorded with it)
    token = complete_login(
        user.username,
        method="api_key",
        mfa="not_applicable",
        flow="mcp_jwt",
        issue=lambda: jwt_manager.create_token(
            {
                "username": user.username,
                "role": user.role.value,
                "created_at": user.created_at.isoformat(),
            }
        ),
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

        # Inject app's shared background_job_manager so the job is visible
        # to the job-status API (GET /api/jobs/{job_id}).
        _bjm = _utils.app_module.background_job_manager
        if _bjm is None:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Server not fully initialized: background_job_manager unavailable",
                }
            )
        # Create index manager and trigger reindex
        index_manager = ActivatedRepoIndexManager(
            background_job_manager=_bjm,
            activated_repo_manager=getattr(
                _utils.app_module.app.state, "activated_repo_manager", None
            ),
        )
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

        # Inject app's shared managers for correct path resolution and
        # consistency with the trigger_reindex handler.
        _bjm = _utils.app_module.background_job_manager
        if _bjm is None:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Server not fully initialized: background_job_manager unavailable",
                }
            )
        # Create index manager and get status
        index_manager = ActivatedRepoIndexManager(
            background_job_manager=_bjm,
            activated_repo_manager=getattr(
                _utils.app_module.app.state, "activated_repo_manager", None
            ),
        )
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


@_admin_role_first
@require_mcp_elevation()
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

    # Initialize service. Bug #1553: pass the (possibly None) logs_backend so
    # cluster-mode reads follow the same store the writer thread uses --
    # without this, cluster mode always reads the frozen, empty node-local
    # logs.db once the writer's backend is wired at startup.
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService

    logs_backend = getattr(_utils.app_module.app.state, "logs_backend", None)
    service = LogAggregatorService(log_db_path, logs_backend=logs_backend)

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


def handle_admin_embedding_stats_query(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Query embedding/reranker call tracking stats (Story #1418 Phase 3
    Component 7, vendor cost reconciliation).

    Requires admin role. Read-only reporting tool -- no TOTP step-up
    elevation required (mirrors get_job_statistics's lighter tier, not
    admin_logs_query's elevation-gated tier).

    Args:
        args: Query parameters (provider, purpose, golden_repo_alias,
            job_id, start_time, end_time, limit, offset).
        user: Authenticated user (must be admin).

    Returns:
        MCP-compliant response with records array and count.
    """
    if user.role != UserRole.ADMIN:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": "Permission denied. Admin role required to query "
                "embedding stats.",
            }
        )

    backend_registry = getattr(_utils.app_module.app.state, "backend_registry", None)
    if backend_registry is None:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Backend registry not available"}
        )
    backend = getattr(backend_registry, "embedding_call_stats", None)
    if backend is None:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Embedding call stats backend not available"}
        )

    _max_query_limit = 1000
    limit = args.get("limit", 200)
    offset = args.get("offset", 0)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not (1 <= limit <= _max_query_limit)
    ):
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": f"limit must be an integer between 1 and {_max_query_limit}",
            }
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "offset must be a non-negative integer"}
        )

    from dataclasses import asdict

    records = backend.query(
        provider=args.get("provider"),
        purpose=args.get("purpose"),
        golden_repo_alias=args.get("golden_repo_alias"),
        job_id=args.get("job_id"),
        start_time=args.get("start_time"),
        end_time=args.get("end_time"),
        limit=limit,
        offset=offset,
    )
    return _mcp_response(  # type: ignore[no-any-return]
        {
            "success": True,
            "records": [asdict(r) for r in records],
            "count": len(records),
        }
    )


@_admin_role_first
@require_mcp_elevation()
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

    # Initialize services. Bug #1553: pass logs_backend so cluster-mode
    # exports follow the same store the writer thread uses.
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService
    from code_indexer.server.services.log_export_formatter import LogExportFormatter

    logs_backend = getattr(_utils.app_module.app.state, "logs_backend", None)
    service = LogAggregatorService(log_db_path, logs_backend=logs_backend)
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

    # Impersonation is managed by the AUTHENTICATED principal, never by the
    # user currently impersonated: an administrator can always clear or
    # change it, whatever the impersonated user's own role.  The dispatcher
    # passes the CURRENT authenticated caller for this tool
    # (tool_access.AUTHENTICATED_PRINCIPAL_TOOLS), loaded for this request --
    # never a user snapshot stored when the session was created.
    principal = user

    if principal.role != UserRole.ADMIN:
        password_audit_logger.log_impersonation_denied(
            actor_username=principal.username,
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
                actor_username=principal.username,
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

        if session_state is None:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "status": "error",
                    "error": "session_state_unavailable",
                    "message": (
                        "No MCP session state is available for this request. "
                        "Impersonation requires a stateful MCP session. "
                        "Ensure the client supports session state and retry."
                    ),
                }
            )

        session_state.set_impersonation(target_user)
        password_audit_logger.log_impersonation_set(
            actor_username=principal.username,
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
            )
        )
        return _mcp_response(  # type: ignore[no-any-return]
            {"status": "error", "error": f"{type(e).__name__}: {e}"}
        )


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
    group_id, error = _parse_group_id(args)
    if error:
        return None, None, error
    group = group_manager.get_group(group_id)
    if not group:
        return None, None, _group_not_found(group_id)
    return group_id, group, None


def _parse_group_id(
    args: Dict[str, Any],
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Parse group_id only; existence is left to the audited operation.

    Returns:
        Tuple of (group_id, error_response) - error_response is None on success
    """
    group_id_str = args.get("group_id", "")
    if not group_id_str:
        return None, _mcp_response(
            {"success": False, "error": "Missing required parameter: group_id"}
        )
    try:
        return int(group_id_str), None
    except ValueError:
        return None, _mcp_response(
            {"success": False, "error": f"Invalid group_id: {group_id_str}"}
        )


def _group_not_found(group_id: Optional[int]) -> Dict[str, Any]:
    return _mcp_response({"success": False, "error": f"Group not found: {group_id}"})  # type: ignore[no-any-return]


@_admin_role_first
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@_admin_role_first
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
                details={"name": group.name, "source": "mcp"},
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@_admin_role_first
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@_admin_role_first
@require_mcp_elevation()
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
                details={"name": updated_group.name, "source": "mcp"},
            )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except ValueError as e:
            return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-132",
                f"Error in handle_update_group: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@_admin_role_first
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
                details={"name": group_name, "source": "mcp"},
            )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except (DefaultGroupCannotBeDeletedError, GroupHasUsersError) as e:
            return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-133",
                f"Error in handle_delete_group: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _add_member(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Assign a user to a group (inner handler — Story #992).

    A membership is only written for a name that has an account.
    """
    from ....services.group_access_manager import (
        GroupNotFoundError,
        UnknownAccountError,
    )

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )
        user_manager = dependencies.user_manager
        if user_manager is None:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "User manager not configured"}
            )

        group_id, error = _parse_group_id(args)
        if error:
            return error

        user_id = args.get("user_id", "")
        if not user_id:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: user_id"}
            )

        try:
            group_manager.assign_user_to_group_audited(
                user_id,
                group_id,
                actor=user.username,
                account_exists=lambda name: user_manager.get_user(name) is not None,
            )
        except UnknownAccountError:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": f"User not found: {user_id}"}
            )
        except GroupNotFoundError:
            return _group_not_found(group_id)
        return _mcp_response({"success": True})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-134",
                f"Error in handle_add_member_to_group: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _remove_member(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Remove a user from a group (inner handler — Story #992)."""
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
            details={
                "user_id": user_id,
                "removed_from_group": group.name,
                "source": "mcp",
            },
        )
        return _mcp_response({"success": True})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-135",
                f"Error in handle_remove_member_from_group: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _add_repos(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Grant a group access to one or more repositories (inner handler — Story #992)."""
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
                    details={
                        "repo": repo_name,
                        "group": group.name,
                        "source": "mcp",
                    },
                )
        return _mcp_response({"success": True, "added_count": added_count})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-TOOL-042",
                f"Error in handle_add_repos_to_group: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _remove_repo(args: Dict[str, Any], user: User, **kwargs: Any) -> Dict[str, Any]:
    """Revoke a group's access to a single repository (inner handler — Story #992)."""
    from ....services.group_access_manager import (
        CidxMetaCannotBeRevokedError,
        GroupNotFoundError,
    )

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, error = _parse_group_id(args)
        if error:
            return error

        repo_name = args.get("repo_name", "")
        if not repo_name:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: repo_name"}
            )

        try:
            if not group_manager.revoke_repo_access_audited(
                repo_name, group_id, actor=user.username
            ):
                return _mcp_response(  # type: ignore[no-any-return]
                    {
                        "success": False,
                        "error": f"Repository '{repo_name}' not found in group's access list",
                    }
                )
            return _mcp_response({"success": True})  # type: ignore[no-any-return]
        except GroupNotFoundError:
            return _group_not_found(group_id)
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def _bulk_remove_repos(
    args: Dict[str, Any], user: User, **kwargs: Any
) -> Dict[str, Any]:
    """Revoke a group's access to multiple repositories (inner handler — Story #992)."""
    from ....services.group_access_manager import GroupNotFoundError

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, error = _parse_group_id(args)
        if error:
            return error

        repo_names = _parse_json_string_array(args.get("repo_names", []))
        if not repo_names:
            return _mcp_response(  # type: ignore[no-any-return]
                {"success": False, "error": "Missing required parameter: repo_names"}
            )

        # cidx-meta is skipped silently; one row per revoked repository and
        # one summary row for the absent ones (the audited entry point).
        try:
            removed_count = group_manager.revoke_repos_access_audited(
                repo_names, group_id, actor=user.username
            )
        except GroupNotFoundError:
            return _group_not_found(group_id)
        return _mcp_response({"success": True, "removed_count": removed_count})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-002",
                f"Error in handle_bulk_remove_repos_from_group: {e}",
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


@require_mcp_elevation()
def handle_create_api_key(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new API key for the authenticated user."""
    try:
        from code_indexer.server.auth.api_key_manager import ApiKeyManager

        description = args.get("description", "")
        api_key_manager = ApiKeyManager(user_manager=_utils.app_module.user_manager)
        api_key, key_id = api_key_manager.generate_key_audited(
            user.username, name=description, actor=user.username
        )
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

        result = _utils.app_module.user_manager.delete_api_key_audited(
            user.username, key_id, actor=user.username
        )
        return _mcp_response({"success": result})  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-005",
                f"Error in handle_delete_api_key: {e}",
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# ADMIN OPERATIONS MCP HANDLERS (Story #744)
# Audit Logs, Maintenance Mode
# =============================================================================


def _resolve_audit_log_pagination(args: Dict[str, Any]) -> tuple:
    """Resolve (limit, offset) from the `limit`/`page` MCP arguments.

    Issue #1646: `page` was previously never read, so every call returned
    the same first-`limit` slice regardless of `page`. Both `limit` and
    `page` are clamped against named maxima to protect against a
    pathological caller-supplied value producing an unbounded SQL fetch or
    OFFSET (the shared read function clamps the offset once more).
    """
    limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
    if limit <= 0:
        limit = DEFAULT_AUDIT_LOG_LIMIT
    elif limit > AUDIT_LOG_MAX_LIMIT:
        limit = AUDIT_LOG_MAX_LIMIT
    page = _coerce_int(args.get("page"), 1)
    if page < 1:
        page = 1
    elif page > _AUDIT_LOG_MAX_PAGE:
        page = _AUDIT_LOG_MAX_PAGE
    return limit, (page - 1) * limit


def _get_audit_service() -> Any:
    """Resolve app.state.audit_service, raising if the server isn't configured."""
    import code_indexer.server.app as _app_module

    _svc = getattr(getattr(_app_module, "app", None), "state", None)
    audit_svc = getattr(_svc, "audit_service", None) if _svc else None
    if audit_svc is None:
        raise RuntimeError("AuditLogService not available on app.state")
    return audit_svc


def _audit_flag(args: Dict[str, Any], name: str) -> bool:
    """A JSON boolean argument (absent means False); anything else is refused."""
    value = args.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise AuditQueryError(f"{name} must be a boolean")
    return value


def _build_audit_log_entry(row: CanonicalAuditRow) -> Dict[str, Any]:
    """One query_audit_logs entry: the shared row fields, ``details``
    decoded, plus the older ``user`` / ``action`` / ``resource`` aliases
    (the row's own ``admin_id`` / ``action_type``; ``resource`` is the
    recorded PR URL for PR-creation rows, else ``target_id``).

    ``resource`` is read from the ALLOWLISTED details only: the shared read
    path keeps ``pr_url`` solely as a plain web URL (no userinfo, query or
    fragment), so this never exposes more than ``details`` does.
    """
    entry = row_fields(row)
    details = decode_details(row.details)
    entry["details"] = details
    entry["user"] = row.admin_id
    entry["action"] = row.action_type
    pr_url = details.get(PR_URL_FIELD)
    entry["resource"] = pr_url if isinstance(pr_url, str) else row.target_id
    return entry


def _query_audit_log_from_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Map the MCP arguments onto the shared read function (one call)."""
    # Checked on the SUPPLIED arguments, before page 1 becomes offset 0 (and
    # then "no offset"): an explicit page never combines with a cursor.
    if args.get("cursor") and args.get("page") is not None:
        raise AuditQueryError("cursor and page cannot be combined")
    limit, offset = _resolve_audit_log_pagination(args)
    filters = build_filters(
        # Both "action" and "action_type" name the action filter.
        action_type=args.get("action") or args.get("action_type"),
        actor=args.get("user"),
        target_type=args.get("target_type"),
        target_id=args.get("target_id"),
        outcome=args.get("outcome"),
        source=args.get("source"),
        ip_address=args.get("ip_address"),
        correlation_id=args.get("correlation_id"),
        date_from=args.get("from_date"),
        date_to=args.get("to_date"),
    )
    result = query_audit_log(
        _get_audit_service(),
        filters,
        tier=args.get("tier") or TIER_ALL,
        cursor=args.get("cursor") or None,
        direction=args.get("direction") or DIRECTION_OLDER,
        limit=limit,
        legacy_offset=offset or None,
        aggregate=_audit_flag(args, "aggregate"),
        all_time=_audit_flag(args, "all_time"),
    )
    if isinstance(result, AuditAggregate):
        return {"success": True, "entries": [], **aggregate_fields(result)}
    return {
        "success": True,
        "entries": [_build_audit_log_entry(row) for row in result.rows],
        **page_fields(result),
    }


@_admin_role_first
@require_mcp_elevation()
def handle_query_audit_logs(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Query the audit log (admin only) through the shared read function.

    A thin adapter over ``services/audit_log_query.query_audit_log``, the
    same function the REST route and the Web Audit Logs page read through:
    one call returns both the entries and the capped ``total``.  A bad
    argument (filter, tier, cursor, mode) returns ``success: false``.
    """
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to query audit logs.",
                }
            )
        return _mcp_response(_query_audit_log_from_args(args))  # type: ignore[no-any-return]
    except AuditQueryError as e:
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]
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
            )
        )
        return _mcp_response({"success": False, "error": str(e)})  # type: ignore[no-any-return]


# =============================================================================
# DEPENDENCY ANALYSIS (Story #195)
# =============================================================================


@_admin_role_first
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
            # Story #1035 AC6: read active sentinel to surface job_id in error envelope
            _active_job_id: str = "unknown"
            _sentinel_dir_pf = (
                dependency_map_service.get_sentinel_dir()
                if hasattr(dependency_map_service, "get_sentinel_dir")
                else None
            )
            if _sentinel_dir_pf is not None:
                from code_indexer.server.services.shared_job_sentinel import (
                    SharedJobSentinel,
                )
                from code_indexer.server.services.dependency_map_service import (
                    ANALYSIS_STALE_TIMEOUT_SECONDS as _ANALYSIS_STALE_TIMEOUT_SECONDS,
                )

                _sentinel_pf = SharedJobSentinel(
                    sentinel_dir=_sentinel_dir_pf,
                    stale_timeout_seconds=_ANALYSIS_STALE_TIMEOUT_SECONDS,
                )
                _active_info = _sentinel_pf.read_active("analysis")
                if _active_info is not None:
                    _active_job_id = _active_info.job_id
            return _mcp_response(  # type: ignore[no-any-return]
                {
                    "success": False,
                    "error": "already in progress",
                    "job_id": _active_job_id,
                    "mode": mode,
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

        # Story #1035 AC13: synchronous sentinel claim before spawning thread
        _pre_claimed = False
        _sentinel_obj = None
        _sentinel_dir_sc = (
            dependency_map_service.get_sentinel_dir()
            if hasattr(dependency_map_service, "get_sentinel_dir")
            else None
        )
        if isinstance(_sentinel_dir_sc, (str, Path)):
            from code_indexer.server.services.shared_job_sentinel import (
                SharedJobSentinel as _SharedJobSentinel,
            )
            from code_indexer.server.services.dependency_map_service import (
                ANALYSIS_STALE_TIMEOUT_SECONDS as _ANALYSIS_STALE_TIMEOUT_SECONDS_SC,
            )

            _sentinel_obj = _SharedJobSentinel(
                sentinel_dir=_sentinel_dir_sc,
                stale_timeout_seconds=_ANALYSIS_STALE_TIMEOUT_SECONDS_SC,
            )
            _node_id_sc = (
                dependency_map_service._get_node_id()
                if hasattr(dependency_map_service, "_get_node_id")
                else "unknown"
            )
            _claim = _sentinel_obj.try_claim("analysis", job_id, _node_id_sc)
            if not _claim.success:
                _conflict_job_id = (
                    _claim.active.job_id if _claim.active is not None else "unknown"
                )
                return _mcp_response(  # type: ignore[no-any-return]
                    {
                        "success": False,
                        "error": "already in progress",
                        "job_id": _conflict_job_id,
                        "mode": mode,
                    }
                )
            _pre_claimed = True

        # AC2/AC3: Spawn background thread for analysis
        def run_analysis_job() -> None:
            """Background job to run dependency map analysis."""
            from code_indexer.server.services.shared_job_sentinel import (
                AnalysisAlreadyRunningError as _BgAnalysisAlreadyRunningError,
            )
            from code_indexer.server.services.job_tracker import (
                DuplicateJobError as _BgDuplicateJobError,
            )

            try:
                if mode == "full":
                    dependency_map_service.run_full_analysis(
                        job_id=job_id, pre_claimed=_pre_claimed
                    )
                else:
                    dependency_map_service.run_delta_analysis(
                        job_id=job_id, pre_claimed=_pre_claimed
                    )
            except (_BgDuplicateJobError, _BgAnalysisAlreadyRunningError) as e:
                _active_dup_id = (
                    getattr(e, "existing_job_id", None)
                    or getattr(e, "active_job_id", None)
                    or "unknown"
                )
                logger.info(
                    "MCP dep-map trigger rejected: already in progress, job_id=%s",
                    _active_dup_id,
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "DEPMAP-TRIGGER-001",
                        f"Background dependency map analysis failed: {e}",
                    )
                )

        # Start background daemon thread
        thread = threading.Thread(target=run_analysis_job, daemon=True)
        try:
            thread.start()
        except Exception:
            # N1: release sentinel on thread-spawn failure to avoid leak
            if _pre_claimed and _sentinel_obj is not None:
                _sentinel_obj.release("analysis", expected_job_id=job_id)
            raise

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
            )
        )
        return _mcp_response({"success": False, "error": str(e), "job_id": None})  # type: ignore[no-any-return]


# =============================================================================
# Story #992: Unified Group Management Dispatchers
# =============================================================================

_VALID_MEMBER_ACTIONS = frozenset({"add", "remove"})
_VALID_REPO_ACTIONS = frozenset({"add", "remove", "bulk_remove"})


@_admin_role_first
def handle_manage_group_members(
    args: Dict[str, Any], user: User, **kwargs: Any
) -> Dict[str, Any]:
    """
    Unified group member management dispatcher (Story #992).

    Dispatches to elevation-gated inner handlers based on 'action':
      - 'add'    -> _add_member(args, user, **kwargs)
      - 'remove' -> _remove_member(args, user, **kwargs)

    The admin role is checked first (``_admin_role_first``); elevation is
    enforced by each inner handler.
    """
    action = args.get("action", "")
    if not action:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Missing required parameter: action"}
        )
    if action not in _VALID_MEMBER_ACTIONS:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": (
                    f"Invalid action '{action}'. "
                    f"Valid actions: {sorted(_VALID_MEMBER_ACTIONS)}"
                ),
            }
        )
    if action == "add":
        return _add_member(args, user, **kwargs)  # type: ignore[no-any-return]
    # action == "remove"
    return _remove_member(args, user, **kwargs)  # type: ignore[no-any-return]


handle_manage_group_members.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]


@_admin_role_first
def handle_manage_group_repos(
    args: Dict[str, Any], user: User, **kwargs: Any
) -> Dict[str, Any]:
    """
    Unified group repo management dispatcher (Story #992).

    Dispatches to elevation-gated inner handlers based on 'action':
      - 'add'         -> _add_repos(args, user, **kwargs)
      - 'remove'      -> _remove_repo(args, user, **kwargs)
      - 'bulk_remove' -> _bulk_remove_repos(args, user, **kwargs)

    The admin role is checked first (``_admin_role_first``); elevation is
    enforced by each inner handler.
    The 'repos' list parameter is forwarded as 'repo_names' for add/bulk_remove,
    and 'repo_name' (first element) for remove.
    """
    action = args.get("action", "")
    if not action:
        return _mcp_response(  # type: ignore[no-any-return]
            {"success": False, "error": "Missing required parameter: action"}
        )
    if action not in _VALID_REPO_ACTIONS:
        return _mcp_response(  # type: ignore[no-any-return]
            {
                "success": False,
                "error": (
                    f"Invalid action '{action}'. "
                    f"Valid actions: {sorted(_VALID_REPO_ACTIONS)}"
                ),
            }
        )
    if action == "add":
        # Forward 'repos' as 'repo_names' for inner handler compatibility
        inner_args = {**args}
        if "repos" in inner_args and "repo_names" not in inner_args:
            inner_args["repo_names"] = inner_args.pop("repos")
        return _add_repos(inner_args, user, **kwargs)  # type: ignore[no-any-return]
    if action == "remove":
        # Forward 'repos' (list) or accept 'repo_name' (scalar) for inner handler
        inner_args = {**args}
        if "repos" in inner_args and "repo_name" not in inner_args:
            repos_list = inner_args.pop("repos")
            inner_args["repo_name"] = repos_list[0] if repos_list else ""
        return _remove_repo(inner_args, user, **kwargs)  # type: ignore[no-any-return]
    # action == "bulk_remove"
    inner_args = {**args}
    if "repos" in inner_args and "repo_names" not in inner_args:
        inner_args["repo_names"] = inner_args.pop("repos")
    return _bulk_remove_repos(inner_args, user, **kwargs)  # type: ignore[no-any-return]


handle_manage_group_repos.__mcp_requires_session_key__ = True  # type: ignore[attr-defined]


# =============================================================================
# Memory Governor stats (Story 4)
# =============================================================================


@_admin_role_first
def handle_get_memory_governor_stats(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Return the §3.5 memory-governor snapshot via MCP.

    Returns the full snapshot when the governor is active, or a minimal
    not-active response when the governor has not been initialised.
    Never raises.
    """
    from code_indexer.server.services.memory_governor import get_memory_governor

    gov = get_memory_governor()
    if gov is None:
        return {"enabled": False, "band": None, "active": False}
    snapshot: Dict[str, Any] = gov.get_snapshot()
    return snapshot


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
    registry["admin_embedding_stats_query"] = handle_admin_embedding_stats_query
    registry["set_session_impersonation"] = handle_set_session_impersonation
    registry["list_groups"] = handle_list_groups
    registry["create_group"] = handle_create_group
    registry["get_group"] = handle_get_group
    registry["update_group"] = handle_update_group
    registry["delete_group"] = handle_delete_group
    # Story #992: 5 narrow group tools replaced by 2 action-param tools (hard-cut).
    registry["manage_group_members"] = handle_manage_group_members
    registry["manage_group_repos"] = handle_manage_group_repos
    registry["list_api_keys"] = handle_list_api_keys
    registry["create_api_key"] = handle_create_api_key
    registry["delete_api_key"] = handle_delete_api_key
    registry["list_mcp_credentials"] = handle_list_mcp_credentials
    registry["manage_mcp_credential"] = handle_manage_mcp_credential
    registry["query_audit_logs"] = handle_query_audit_logs
    # Story #924: enter/exit maintenance MCP tools removed — endpoints
    # are localhost-only and auto-updater driven, not exposed via MCP.
    registry["get_maintenance_status"] = handle_get_maintenance_status
    registry["trigger_dependency_analysis"] = handle_trigger_dependency_analysis
    registry["get_memory_governor_stats"] = handle_get_memory_governor_stats
