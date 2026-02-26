"""
Web Admin UI Routes.

Provides admin web interface routes for CIDX server administration.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import quote

from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from fastapi import APIRouter, Request, Response, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth.user_manager import UserRole, SSOPasswordChangeError
from ..auth import dependencies
from .auth import (
    get_session_manager,
    SessionData,
    require_admin_session,
)
from ..services.ci_token_manager import CITokenManager, TokenValidationError
from ..services.config_service import get_config_service
from code_indexer import __version__ as cidx_version
from code_indexer.server.logging_utils import format_error_log, get_log_extra

logger = logging.getLogger(__name__)

# Self-Monitoring constants (Story #74)
SCAN_HISTORY_LIMIT = 50
ISSUES_HISTORY_LIMIT = 100

# Story #198: Settings that require server restart
# These settings are read during server startup (singleton init, thread pool creation)
# and changing them has no effect until the server is restarted
RESTART_REQUIRED_FIELDS = [
    "host",  # Server binding address (read at uvicorn startup)
    "port",  # Server binding port (read at uvicorn startup)
    "telemetry_enabled",  # Telemetry integration (read at server startup)
    "langfuse_enabled",  # Langfuse integration (read at server startup)
    "max_concurrent_claude_cli",  # ClaudeCliManager thread pool size (singleton init)
    "multi_search_max_workers",  # Multi-search thread pool size (singleton init)
    "scip_multi_max_workers",  # SCIP multi-repo thread pool size (singleton init)
    "max_concurrent_background_jobs",  # BackgroundJobManager thread pool size (singleton init)
    "subprocess_max_workers",  # Subprocess executor pool size (singleton init)
    "dependency_map_enabled",  # Dependency map scheduler (background thread init)
]


def _get_token_manager() -> CITokenManager:
    """Create CITokenManager with SQLite backend (Story #702 migration)."""
    from ..services.config_service import get_config_service

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"

    return CITokenManager(
        server_dir_path=str(server_dir),
        use_sqlite=True,
        db_path=str(db_path),
    )


def _get_ssh_key_manager():
    """Create SSHKeyManager with SQLite backend (Story #702 migration)."""
    from ..services.config_service import get_config_service
    from ..services.ssh_key_manager import SSHKeyManager

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    metadata_dir = server_dir / "data" / "ssh_keys"

    return SSHKeyManager(
        metadata_dir=metadata_dir,
        use_sqlite=True,
        db_path=db_path,
    )


# Get templates directory path
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Add enumerate to Jinja2 globals for honeycomb template (Story #712 AC1)
templates.env.globals["enumerate"] = enumerate


# Helper function for server time in templates (Story #89)
def _get_server_time_for_template() -> str:
    """Get current server time for Jinja2 templates (Story #89)."""
    from datetime import datetime, timezone as tz

    current_time = datetime.now(tz.utc)
    return current_time.isoformat().replace("+00:00", "Z")


# Add server time function to Jinja2 globals for server clock (Story #89)
templates.env.globals["get_server_time"] = _get_server_time_for_template

# Create router
web_router = APIRouter()
# Create user router for non-admin user routes
user_router = APIRouter()
# Create login router for unified authentication (root level /login)
login_router = APIRouter()

# CSRF cookie name and settings
CSRF_COOKIE_NAME = "_csrf"
CSRF_MAX_AGE_SECONDS = 600  # 10 minutes


def _get_csrf_serializer() -> URLSafeTimedSerializer:
    """Get the CSRF token serializer using session manager's secret key."""
    session_manager = get_session_manager()
    # Access the secret key from the serializer
    return URLSafeTimedSerializer(session_manager._serializer.secret_key)


def _get_repo_category_service():
    """Create RepoCategoryService with database path (Story #183 helper)."""
    from ..services.config_service import get_config_service
    from ..services.repo_category_service import RepoCategoryService

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")

    return RepoCategoryService(db_path)


def generate_csrf_token() -> str:
    """Generate a new CSRF token."""
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, path: str = "/") -> None:
    """
    Set a signed CSRF token cookie.

    Args:
        response: FastAPI Response object
        token: CSRF token to sign and store
        path: Cookie path (default: "/" for unified login)
    """
    serializer = _get_csrf_serializer()
    signed_value = serializer.dumps(token, salt="csrf-login")

    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=signed_value,
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax",  # Changed from strict to allow HTMX partial requests
        max_age=CSRF_MAX_AGE_SECONDS,
        path=path,  # Cookie path (default "/" for unified login)
    )


def validate_login_csrf_token(request: Request, submitted_token: Optional[str]) -> bool:
    """
    Validate CSRF token for login form using signed cookie.

    Args:
        request: FastAPI Request object
        submitted_token: CSRF token from form submission

    Returns:
        True if valid, False otherwise
    """
    logger.debug(
        "CSRF validation: submitted_token=%s, has_csrf_cookie=%s, all_cookies=%s",
        submitted_token[:20] + "..." if submitted_token else None,
        CSRF_COOKIE_NAME in request.cookies,
        list(request.cookies.keys()),
        extra={"correlation_id": get_correlation_id()},
    )

    if not submitted_token:
        logger.debug(
            "CSRF validation failed: no submitted_token",
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_cookie:
        logger.debug(
            "CSRF validation failed: no csrf_cookie in request",
            extra={"correlation_id": get_correlation_id()},
        )
        return False

    try:
        serializer = _get_csrf_serializer()
        stored_token = serializer.loads(
            csrf_cookie,
            salt="csrf-login",
            max_age=CSRF_MAX_AGE_SECONDS,
        )
        result = secrets.compare_digest(stored_token, submitted_token)
        logger.debug(
            "CSRF validation result: %s (stored=%s, submitted=%s)",
            result,
            stored_token[:20] + "..." if stored_token else None,
            submitted_token[:20] + "..." if submitted_token else None,
            extra={"correlation_id": get_correlation_id()},
        )
        return result
    except (SignatureExpired, BadSignature) as e:
        logger.debug(
            "CSRF validation failed: %s",
            type(e).__name__,
            extra={"correlation_id": get_correlation_id()},
        )
        return False


def get_csrf_token_from_cookie(request: Request) -> Optional[str]:
    """
    Retrieve existing CSRF token from cookie.

    Args:
        request: FastAPI Request object

    Returns:
        CSRF token if valid cookie exists, None otherwise
    """
    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_cookie:
        return None

    try:
        serializer = _get_csrf_serializer()
        token = serializer.loads(
            csrf_cookie,
            salt="csrf-login",
            max_age=CSRF_MAX_AGE_SECONDS,
        )
        return cast(Optional[str], token)
    except (SignatureExpired, BadSignature):
        return None


# Old /admin/login routes removed - replaced by unified login at root level
# See login_router below for unified login implementation


@web_router.get("/logout")
def logout(request: Request):
    """
    Logout and clear session.

    Redirects to unified login page after clearing session.
    """
    session_manager = get_session_manager()
    response = RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    session_manager.clear_session(response)
    return response


def _get_dashboard_service():
    """Get dashboard service, handling import lazily to avoid circular imports."""
    from ..services.dashboard_service import dashboard_service

    return dashboard_service


def _get_server_time() -> str:
    """
    Get current server time for template context (Story #89).

    Returns current UTC time in ISO 8601 format for server clock initialization.

    Returns:
        ISO 8601 formatted timestamp string (e.g., "2026-02-04T14:32:15.123456Z")
    """
    from datetime import datetime, timezone as tz

    current_time = datetime.now(tz.utc)
    return current_time.isoformat().replace("+00:00", "Z")


@web_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """
    Dashboard page - main admin landing page.

    Requires authenticated admin session.
    Displays system health, job statistics, repository counts, and recent activity.
    """
    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        # Not authenticated - redirect to unified login
        return _create_login_redirect(request)

    if session.role != "admin":
        # Not admin - forbidden
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    # Get aggregated dashboard data (Bug #671: Pass user role to show all repos for admins)
    dashboard_service = _get_dashboard_service()
    dashboard_data = dashboard_service.get_dashboard_data(
        session.username, session.role
    )

    # Story #30 AC5: Health section is now lazy-loaded via HTMX
    # No need to fetch database_health here - it will be loaded
    # asynchronously via /admin/partials/dashboard-health endpoint

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "dashboard",
            "show_nav": True,
            "server_time": _get_server_time(),
            "health": dashboard_data.health,
            "job_counts": dashboard_data.job_counts,
            "repo_counts": dashboard_data.repo_counts,
            "recent_jobs": dashboard_data.recent_jobs,
        },
    )


@web_router.get("/partials/dashboard-health", response_class=HTMLResponse)
def dashboard_health_partial(request: Request):
    """
    Partial refresh endpoint for dashboard health section.

    Returns HTML fragment for htmx partial updates.
    Story #712 AC1-AC4: Includes database health honeycomb data.
    Story #30 AC6: Uses cached database health (60s TTL) while
    system metrics (CPU, memory, disk, network) remain real-time.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    # System metrics are always real-time (AC3)
    health_data = dashboard_service.get_health_partial()

    # Story #30 AC6: Use cached database health (60s TTL)
    # Story #30 Bug Fix: Use singleton to ensure cache is shared across requests.
    # Previously, creating new DatabaseHealthService() on each request meant the
    # instance-level cache was always empty.
    from ..services.database_health_service import get_database_health_service

    db_health_service = get_database_health_service()
    database_health = db_health_service.get_all_database_health_cached()

    return templates.TemplateResponse(
        "partials/dashboard_health.html",
        {
            "request": request,
            "health": health_data,
            "database_health": database_health,
            "server_version": cidx_version,
        },
    )


@web_router.get("/partials/dashboard-stats", response_class=HTMLResponse)
def dashboard_stats_partial(
    request: Request,
    time_filter: str = "24h",
    recent_filter: str = "24h",
    api_filter: int = 60,
):
    """
    Partial refresh endpoint for dashboard statistics section.

    Story #541 AC3/AC5: Support time filtering for job stats and recent activity.
    Rolling window API metrics: Support configurable time window for API activity.

    Args:
        request: HTTP request
        time_filter: Time filter for job stats ("24h", "7d", "30d")
        recent_filter: Time filter for recent activity ("24h", "7d", "30d")
        api_filter: Time window in seconds for API metrics (default 60).
            Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    # Story #712 AC6: Pass user_role to prevent activated repos count flash
    # Rolling window API metrics: Pass api_window for configurable time window
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter=time_filter,
        recent_filter=recent_filter,
        user_role=session.role,
        api_window=api_filter,
    )

    return templates.TemplateResponse(
        "partials/dashboard_stats.html",
        {
            "request": request,
            "job_counts": stats_data["job_counts"],
            "repo_counts": stats_data["repo_counts"],
            "recent_jobs": stats_data["recent_jobs"],
            "api_metrics": stats_data.get("api_metrics", {}),
            "time_filter": time_filter,
            "recent_filter": recent_filter,
            "api_filter": api_filter,
        },
    )


@web_router.get("/partials/dashboard-job-counts", response_class=HTMLResponse)
def dashboard_job_counts_partial(
    request: Request,
    time_filter: str = "24h",
):
    """
    Story #69: Granular partial endpoint for job counts data ONLY.

    Returns HTML fragment containing only job statistics cards,
    excluding dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        time_filter: Time filter for job stats ("24h", "7d", "30d")

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter=time_filter,
        recent_filter="24h",  # Not used for job counts
        user_role=session.role,
        api_window=60,  # Not used for job counts
    )

    return templates.TemplateResponse(
        "partials/dashboard_job_counts.html",
        {
            "request": request,
            "job_counts": stats_data["job_counts"],
            "time_filter": time_filter,
        },
    )


@web_router.get("/partials/dashboard-recent-jobs", response_class=HTMLResponse)
def dashboard_recent_jobs_partial(
    request: Request,
    recent_filter: str = "24h",
):
    """
    Story #69: Granular partial endpoint for recent jobs table body ONLY.

    Returns HTML fragment containing only table rows for recent jobs,
    excluding table structure and dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        recent_filter: Time filter for recent activity ("24h", "7d", "30d")

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter="24h",  # Not used for recent jobs
        recent_filter=recent_filter,
        user_role=session.role,
        api_window=60,  # Not used for recent jobs
    )

    return templates.TemplateResponse(
        "partials/dashboard_recent_jobs.html",
        {
            "request": request,
            "recent_jobs": stats_data["recent_jobs"],
            "recent_filter": recent_filter,
        },
    )


@web_router.get("/partials/dashboard-api-metrics", response_class=HTMLResponse)
def dashboard_api_metrics_partial(
    request: Request,
    api_filter: int = 60,
):
    """
    Story #69: Granular partial endpoint for API metrics data ONLY.

    Returns HTML fragment containing only API activity statistics cards,
    excluding dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        api_filter: Time window in seconds for API metrics (default 60).
            Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter="24h",  # Not used for API metrics
        recent_filter="24h",  # Not used for API metrics
        user_role=session.role,
        api_window=api_filter,
    )

    return templates.TemplateResponse(
        "partials/dashboard_api_metrics.html",
        {
            "request": request,
            "api_metrics": stats_data.get("api_metrics", {}),
            "api_filter": api_filter,
        },
    )


@web_router.get("/partials/dashboard-langfuse", response_class=HTMLResponse)
def dashboard_langfuse_partial(request: Request):
    """
    Story #168: Langfuse status card partial endpoint.

    Returns HTML fragment containing Langfuse sync status, metrics, and manual sync trigger.
    Card is only visible when langfuse.pull_enabled is true.

    Auto-refreshes every 30 seconds via HTMX.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    langfuse_data = dashboard_service.get_langfuse_metrics()

    return templates.TemplateResponse(
        "partials/dashboard_langfuse.html",
        {
            "request": request,
            "langfuse": langfuse_data,
        },
    )


@web_router.post("/langfuse-sync/trigger", response_class=JSONResponse)
def langfuse_sync_trigger(request: Request):
    """
    Story #168 AC4: Trigger immediate Langfuse sync.

    C1 fix: Non-blocking trigger using background thread, returns 202 Accepted.
    H4 fix: Returns 409 Conflict if sync already in progress.

    Returns:
        202 Accepted - Sync triggered successfully
        409 Conflict - Sync already in progress
        503 Service Unavailable - Sync service not initialized
        401 Unauthorized - Authentication required
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"status": "error", "message": "Authentication required"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        from ..app import langfuse_sync_service

        if langfuse_sync_service is None:
            return JSONResponse(
                {"status": "error", "message": "Langfuse sync service not initialized"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # C1/H4: Non-blocking trigger with concurrent sync guard
        triggered = langfuse_sync_service.trigger_sync()

        if not triggered:
            return JSONResponse(
                {"status": "error", "message": "Sync already in progress"},
                status_code=status.HTTP_409_CONFLICT,
            )

        return JSONResponse(
            {"status": "success", "message": "Sync triggered successfully"},
            status_code=status.HTTP_202_ACCEPTED,
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "LANGFUSE-SYNC-001",
                f"Failed to trigger Langfuse sync: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        # M5: Generic error message (keep detailed logging above)
        return JSONResponse(
            {"status": "error", "message": "Internal error triggering sync"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# Placeholder routes for other admin pages
# These will redirect to login if not authenticated


def _create_login_redirect(request: Request) -> RedirectResponse:
    """Create redirect to unified login with redirect_to parameter."""
    from urllib.parse import quote

    current_path = str(request.url.path)
    if request.url.query:
        current_path += f"?{request.url.query}"

    redirect_url = f"/login?redirect_to={quote(current_path)}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


def _require_admin_session(request: Request) -> Optional[SessionData]:
    """Check for valid admin session, return None if not authenticated."""
    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session or session.role != "admin":
        return None

    return session


def _get_users_list():
    """Get list of all users from user manager."""
    user_manager = dependencies.user_manager
    if not user_manager:
        return []
    users = user_manager.get_all_users()
    return sorted(users, key=lambda u: u.username.lower())


def _create_users_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create users page response with all necessary context."""
    csrf_token = generate_csrf_token()
    users = _get_users_list()

    response = templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "username": session.username,
            "current_username": session.username,
            "current_page": "users",
            "show_nav": True,
            "csrf_token": csrf_token,
            "users": [
                {
                    "username": u.username,
                    "role": u.role.value,
                    "created_at": (
                        u.created_at.strftime("%Y-%m-%d %H:%M")
                        if u.created_at
                        else "N/A"
                    ),
                    "email": u.email,
                }
                for u in users
            ],
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token, path="/")
    return response


@web_router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    """Users management page - list all users with CRUD operations."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_users_page_response(request, session)


@web_router.post("/users/create", response_class=HTMLResponse)
def create_user(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Create a new user."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_users_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate password match
    if new_password != confirm_password:
        return _create_users_page_response(
            request, session, error_message="Passwords do not match"
        )

    # Validate role
    try:
        role_enum = UserRole(role)
    except ValueError:
        return _create_users_page_response(
            request, session, error_message=f"Invalid role: {role}"
        )

    # Create user
    user_manager = dependencies.user_manager
    if not user_manager:
        return _create_users_page_response(
            request, session, error_message="User manager not available"
        )

    try:
        user_manager.create_user(new_username, new_password, role_enum)

        # Auto-assign new user to appropriate group based on role
        try:
            from ..services.constants import DEFAULT_GROUP_ADMINS, DEFAULT_GROUP_USERS

            group_manager = _get_group_manager()
            if role_enum == UserRole.ADMIN:
                target_group = group_manager.get_group_by_name(DEFAULT_GROUP_ADMINS)
            else:
                target_group = group_manager.get_group_by_name(DEFAULT_GROUP_USERS)

            if target_group:
                group_manager.assign_user_to_group(
                    new_username, target_group.id, session.username
                )
                group_manager.log_audit(
                    admin_id=session.username,
                    action_type="user_group_assign",
                    target_type="user",
                    target_id=new_username,
                    details=f"Auto-assigned to '{target_group.name}' group on creation",
                )
                logger.info(
                    f"Auto-assigned new user '{new_username}' to '{target_group.name}' group"
                )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SCIP-GENERAL-040",
                    f"Failed to auto-assign user '{new_username}' to group: {e}",
                )
            )

        return _create_users_page_response(
            request,
            session,
            success_message=f"User '{new_username}' created successfully",
        )
    except ValueError as e:
        return _create_users_page_response(request, session, error_message=str(e))


@web_router.post("/users/{username}/role", response_class=HTMLResponse)
def update_user_role(
    request: Request,
    username: str,
    role: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Update a user's role."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_users_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Prevent demoting self
    if username == session.username and role != "admin":
        return _create_users_page_response(
            request, session, error_message="Cannot demote your own admin account"
        )

    # Validate role
    try:
        role_enum = UserRole(role)
    except ValueError:
        return _create_users_page_response(
            request, session, error_message=f"Invalid role: {role}"
        )

    # Update user
    user_manager = dependencies.user_manager
    if not user_manager:
        return _create_users_page_response(
            request, session, error_message="User manager not available"
        )

    try:
        user_manager.update_user_role(username, role_enum)
        return _create_users_page_response(
            request,
            session,
            success_message=f"User '{username}' role updated successfully",
        )
    except ValueError as e:
        return _create_users_page_response(request, session, error_message=str(e))


@web_router.post("/users/{username}/password", response_class=HTMLResponse)
def change_user_password(
    request: Request,
    username: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Change a user's password (admin only)."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_users_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate password match
    if new_password != confirm_password:
        return _create_users_page_response(
            request, session, error_message="Passwords do not match"
        )

    # Change password
    user_manager = dependencies.user_manager
    if not user_manager:
        return _create_users_page_response(
            request, session, error_message="User manager not available"
        )

    try:
        user_manager.change_password(username, new_password)
        return _create_users_page_response(
            request,
            session,
            success_message=f"Password for '{username}' changed successfully",
        )
    except SSOPasswordChangeError:
        # Bug #68: SSO users cannot change passwords locally
        return _create_users_page_response(
            request,
            session,
            error_message="Cannot change password for SSO users. Authentication is managed by the identity provider.",
        )
    except ValueError as e:
        return _create_users_page_response(request, session, error_message=str(e))


@web_router.post("/users/{username}/email", response_class=HTMLResponse)
def update_user_email(
    request: Request,
    username: str,
    new_email: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    """Update a user's email (admin only). Empty string clears the email."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_users_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Update email
    user_manager = dependencies.user_manager
    if not user_manager:
        return _create_users_page_response(
            request, session, error_message="User manager not available"
        )

    try:
        # Allow empty email to clear it
        email_value = new_email.strip() if new_email else None
        user_manager.update_user(
            username, new_email=email_value if email_value else None
        )

        return _create_users_page_response(
            request,
            session,
            success_message=f"Email for '{username}' updated successfully",
        )
    except ValueError as e:
        return _create_users_page_response(request, session, error_message=str(e))


@web_router.post("/users/{username}/delete", response_class=HTMLResponse)
async def delete_user(
    request: Request,
    username: str,
    csrf_token: Optional[str] = Form(None),
):
    """Delete a user."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_users_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Prevent deleting self
    if username == session.username:
        return _create_users_page_response(
            request, session, error_message="Cannot delete your own account"
        )

    # Delete user
    user_manager = dependencies.user_manager
    if not user_manager:
        return _create_users_page_response(
            request, session, error_message="User manager not available"
        )

    try:
        user_manager.delete_user(username)

        # Clean up OIDC identity link if OIDC manager exists
        from ..auth.oidc import routes as oidc_routes

        if oidc_routes.oidc_manager:
            import aiosqlite

            async with aiosqlite.connect(oidc_routes.oidc_manager.db_path) as db:
                await db.execute(
                    "DELETE FROM oidc_identity_links WHERE username = ?", (username,)
                )
                await db.commit()

        # Clean up group membership (Bug fix: prevent orphaned group memberships)
        try:
            group_manager = _get_group_manager()
            user_group = group_manager.get_user_group(username)
            if user_group:
                group_manager.remove_user_from_group(username, user_group.id)
                logger.info(f"Cleaned up group membership for deleted user: {username}")
        except RuntimeError:
            # group_manager not available - skip cleanup
            logger.warning(
                format_error_log(
                    "SCIP-GENERAL-041",
                    f"group_manager not available, skipped group cleanup for: {username}",
                )
            )

        return _create_users_page_response(
            request, session, success_message=f"User '{username}' deleted successfully"
        )
    except ValueError as e:
        return _create_users_page_response(request, session, error_message=str(e))


@web_router.get("/partials/users-list", response_class=HTMLResponse)
def users_list_partial(request: Request):
    """
    Partial refresh endpoint for users list section.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()
    users = _get_users_list()

    response = templates.TemplateResponse(
        "partials/users_list.html",
        {
            "request": request,
            "current_username": session.username,
            "csrf_token": csrf_token,
            "users": [
                {
                    "username": u.username,
                    "role": u.role.value,
                    "created_at": (
                        u.created_at.strftime("%Y-%m-%d %H:%M")
                        if u.created_at
                        else "N/A"
                    ),
                    "email": u.email,
                }
                for u in users
            ],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


# ==============================================================================
# Groups Management Routes (Story #710: Admin User and Group Management Interface)
# ==============================================================================


def _format_datetime_display(
    iso_string: Optional[str], fmt: str = "%Y-%m-%d %H:%M"
) -> str:
    """Format ISO datetime string for display."""
    if not iso_string:
        return "N/A"
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except (ValueError, AttributeError):
        return iso_string


def _get_group_manager():
    """Get GroupAccessManager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "group_manager", None)
    if manager is None:
        raise RuntimeError(
            "group_manager not initialized. "
            "Server must set app.state.group_manager during startup."
        )
    return manager


def _get_groups_data() -> List[Dict[str, Any]]:
    """Get all groups with user and repo counts."""
    group_manager = _get_group_manager()
    groups = group_manager.get_all_groups()

    groups_data = []
    for group in groups:
        user_count = group_manager.get_user_count_in_group(group.id)
        repos = group_manager.get_group_repos(group.id)
        # cidx-meta is always included, so subtract 1 for actual repo count
        repo_count = len(repos) - 1 if repos else 0

        groups_data.append(
            {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "is_default": group.is_default,
                "user_count": user_count,
                "repo_count": repo_count,
                "created_at": _format_datetime_display(
                    group.created_at.isoformat() if group.created_at else None
                ),
            }
        )

    return groups_data


def _create_groups_page_response(
    request: Request,
    session: SessionData,
    active_tab: str = "groups",
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create groups page response with all necessary context."""
    csrf_token = generate_csrf_token()
    group_manager = _get_group_manager()

    # Get groups data with counts
    groups_data = _get_groups_data()

    # Get users with their group assignments
    # First get all users from user manager
    all_system_users = _get_users_list()
    assigned_users, _ = group_manager.get_all_users_with_groups()

    # Create a map of assigned users by user_id
    assigned_map = {u["user_id"]: u for u in assigned_users}

    # Merge: all system users with their group info (or None if unassigned)
    users_with_groups = []
    for user in all_system_users:
        username = user.username
        if username in assigned_map:
            user_data = assigned_map[username]
            user_data["assigned_at"] = _format_datetime_display(
                user_data.get("assigned_at")
            )
            users_with_groups.append(user_data)
        else:
            # Unassigned user
            users_with_groups.append(
                {
                    "user_id": username,
                    "group_id": None,
                    "group_name": None,
                    "assigned_at": None,
                    "assigned_by": None,
                }
            )

    # Get all groups for the dropdown
    all_groups = [
        {"id": g.id, "name": g.name, "is_default": g.is_default}
        for g in group_manager.get_all_groups()
    ]

    # Get audit logs (limited to 100 most recent)
    audit_logs, total_count = group_manager.get_audit_logs(limit=100)
    for log in audit_logs:
        log["timestamp"] = _format_datetime_display(
            log.get("timestamp"), "%Y-%m-%d %H:%M:%S"
        )

    # Get golden repos for repo access tab
    golden_repos = []
    repo_access_map: Dict[int, List[str]] = {}
    try:
        golden_repo_manager = _get_golden_repo_manager()
        all_repos_data = golden_repo_manager.list_golden_repos()
        # list_golden_repos returns List[Dict] with 'alias' key
        golden_repos = [
            {"name": repo["alias"]}
            for repo in sorted(all_repos_data, key=lambda x: x["alias"].lower())
        ]

        # Build repo access map: group_id -> list of repo names
        for group in group_manager.get_all_groups():
            repos_for_group = group_manager.get_group_repos(group.id)
            # Filter out cidx-meta as it's always accessible
            repo_access_map[group.id] = [r for r in repos_for_group if r != "cidx-meta"]
    except RuntimeError as e:
        # GoldenRepoManager not initialized during startup
        logger.debug("Golden repo manager not available for repo access tab: %s", e)

    response = templates.TemplateResponse(
        "groups.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "groups",
            "show_nav": True,
            "active_tab": active_tab,
            "csrf_token": csrf_token,
            "groups": groups_data,
            "users_with_groups": users_with_groups,
            "all_groups": all_groups,
            "audit_logs": audit_logs,
            "total_count": total_count,
            "golden_repos": golden_repos,
            "repo_access_map": repo_access_map,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token, path="/")
    return response


@web_router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request):
    """Groups management page - list all groups with CRUD operations."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_groups_page_response(request, session)


@web_router.post("/groups/create", response_class=HTMLResponse)
def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Create a new custom group."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_groups_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        group_manager = _get_group_manager()
        group = group_manager.create_group(name.strip(), description.strip())

        group_manager.log_audit(
            admin_id=session.username,
            action_type="group_create",
            target_type="group",
            target_id=str(group.id),
            details=json.dumps({"name": group.name, "description": group.description}),
        )

        return _create_groups_page_response(
            request, session, success_message=f"Group '{name}' created successfully"
        )
    except ValueError as e:
        return _create_groups_page_response(request, session, error_message=str(e))


@web_router.post("/groups/{group_id}/update", response_class=HTMLResponse)
def update_group(
    request: Request,
    group_id: int,
    name: str = Form(...),
    description: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Update a custom group's name and/or description."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_groups_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        group_manager = _get_group_manager()
        old_group = group_manager.get_group(group_id)
        if not old_group:
            return _create_groups_page_response(
                request, session, error_message=f"Group {group_id} not found"
            )

        updated_group = group_manager.update_group(
            group_id, name=name.strip(), description=description.strip()
        )

        if updated_group:
            group_manager.log_audit(
                admin_id=session.username,
                action_type="group_update",
                target_type="group",
                target_id=str(group_id),
                details=json.dumps(
                    {
                        "old_name": old_group.name,
                        "new_name": name,
                        "old_description": old_group.description,
                        "new_description": description,
                    }
                ),
            )
            return _create_groups_page_response(
                request, session, success_message=f"Group '{name}' updated successfully"
            )
        else:
            return _create_groups_page_response(
                request, session, error_message=f"Group {group_id} not found"
            )
    except ValueError as e:
        return _create_groups_page_response(request, session, error_message=str(e))


@web_router.post("/groups/{group_id}/delete", response_class=HTMLResponse)
def delete_group(
    request: Request,
    group_id: int,
    csrf_token: Optional[str] = Form(None),
):
    """Delete a custom group."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_groups_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        group_manager = _get_group_manager()
        group = group_manager.get_group(group_id)
        if not group:
            return _create_groups_page_response(
                request, session, error_message=f"Group {group_id} not found"
            )

        group_name = group.name
        deleted = group_manager.delete_group(group_id)

        if deleted:
            group_manager.log_audit(
                admin_id=session.username,
                action_type="group_delete",
                target_type="group",
                target_id=str(group_id),
                details=json.dumps({"name": group_name}),
            )
            return _create_groups_page_response(
                request,
                session,
                success_message=f"Group '{group_name}' deleted successfully",
            )
        else:
            return _create_groups_page_response(
                request, session, error_message=f"Failed to delete group {group_id}"
            )
    except Exception as e:
        return _create_groups_page_response(request, session, error_message=str(e))


@web_router.post("/groups/users/{user_id:path}/assign", response_class=HTMLResponse)
def assign_user_to_group(
    request: Request,
    user_id: str,
    group_id: int = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Assign a user to a different group."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_groups_page_response(
            request, session, active_tab="users", error_message="Invalid CSRF token"
        )

    try:
        group_manager = _get_group_manager()
        old_group = group_manager.get_user_group(user_id)
        old_group_name = old_group.name if old_group else "None"

        new_group = group_manager.get_group(group_id)
        if not new_group:
            return _create_groups_page_response(
                request,
                session,
                active_tab="users",
                error_message=f"Group {group_id} not found",
            )

        group_manager.assign_user_to_group(user_id, group_id, session.username)

        group_manager.log_audit(
            admin_id=session.username,
            action_type="user_group_change",
            target_type="user",
            target_id=user_id,
            details=json.dumps(
                {
                    "old_group": old_group_name,
                    "new_group": new_group.name,
                }
            ),
        )

        return _create_groups_page_response(
            request,
            session,
            active_tab="users",
            success_message=f"User '{user_id}' assigned to group '{new_group.name}'",
        )
    except Exception as e:
        return _create_groups_page_response(
            request, session, active_tab="users", error_message=str(e)
        )


@web_router.get("/partials/groups-list", response_class=HTMLResponse)
def groups_list_partial(request: Request):
    """Partial refresh endpoint for groups list section."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    groups_data = _get_groups_data()

    response = templates.TemplateResponse(
        "partials/groups_list.html",
        {"request": request, "csrf_token": csrf_token, "groups": groups_data},
    )
    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/partials/groups-users-list", response_class=HTMLResponse)
def groups_users_list_partial(request: Request):
    """Partial refresh endpoint for users group assignments section."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    group_manager = _get_group_manager()

    # Get all users from user manager
    all_system_users = _get_users_list()
    assigned_users, _ = group_manager.get_all_users_with_groups()

    # Create a map of assigned users by user_id
    assigned_map = {u["user_id"]: u for u in assigned_users}

    # Merge: all system users with their group info (or None if unassigned)
    users_with_groups = []
    for user in all_system_users:
        username = user.username
        if username in assigned_map:
            user_data = assigned_map[username]
            user_data["assigned_at"] = _format_datetime_display(
                user_data.get("assigned_at")
            )
            users_with_groups.append(user_data)
        else:
            # Unassigned user
            users_with_groups.append(
                {
                    "user_id": username,
                    "group_id": None,
                    "group_name": None,
                    "assigned_at": None,
                    "assigned_by": None,
                }
            )

    all_groups = [
        {"id": g.id, "name": g.name, "is_default": g.is_default}
        for g in group_manager.get_all_groups()
    ]

    response = templates.TemplateResponse(
        "partials/groups_users_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "users_with_groups": users_with_groups,
            "all_groups": all_groups,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/partials/groups-audit-logs", response_class=HTMLResponse)
def groups_audit_logs_partial(
    request: Request,
    action_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Partial refresh endpoint for audit logs section with filtering."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    group_manager = _get_group_manager()
    audit_logs, total_count = group_manager.get_audit_logs(
        action_type=action_type or None,
        date_from=date_from or None,
        date_to=date_to or None,
        limit=100,
    )

    for log in audit_logs:
        log["timestamp"] = _format_datetime_display(
            log.get("timestamp"), "%Y-%m-%d %H:%M:%S"
        )

    return templates.TemplateResponse(
        "partials/groups_audit_logs.html",
        {"request": request, "audit_logs": audit_logs, "total_count": total_count},
    )


@web_router.get("/partials/groups-repo-access", response_class=HTMLResponse)
def groups_repo_access_partial(request: Request):
    """Partial refresh endpoint for repository access section."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    group_manager = _get_group_manager()

    # Get all groups
    all_groups = [
        {"id": g.id, "name": g.name, "is_default": g.is_default}
        for g in group_manager.get_all_groups()
    ]

    # Get golden repos
    golden_repos = []
    repo_access_map: Dict[int, List[str]] = {}
    try:
        golden_repo_manager = _get_golden_repo_manager()
        all_repos_data = golden_repo_manager.list_golden_repos()
        # list_golden_repos returns List[Dict] with 'alias' key
        golden_repos = [
            {"name": repo["alias"]}
            for repo in sorted(all_repos_data, key=lambda x: x["alias"].lower())
        ]

        # Build repo access map
        for group in group_manager.get_all_groups():
            repos_for_group = group_manager.get_group_repos(group.id)
            repo_access_map[group.id] = [r for r in repos_for_group if r != "cidx-meta"]
    except RuntimeError as e:
        logger.debug("Golden repo manager not available: %s", e)

    response = templates.TemplateResponse(
        "partials/groups_repo_access.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "all_groups": all_groups,
            "golden_repos": golden_repos,
            "repo_access_map": repo_access_map,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


# Story #199: Helper functions for AJAX/form dual-mode repo access endpoints
async def _parse_repo_access_request(
    request: Request,
    is_ajax: bool,
    form_repo_name: Optional[str],
    form_group_id: Optional[int],
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Parse repo_name and group_id from AJAX JSON or form data.

    Returns:
        Tuple of (repo_name, group_id, error_message). Error message is None on success.
    """
    if is_ajax:
        try:
            body = await request.json()
            repo_name = body.get("repo_name")
            group_id = body.get("group_id")
            return repo_name, group_id, None
        except (json.JSONDecodeError, ValueError):
            return None, None, "Invalid JSON body"
    else:
        return form_repo_name, form_group_id, None


def _validate_repo_access_csrf(
    request: Request,
    is_ajax: bool,
    form_csrf_token: Optional[str],
) -> Tuple[bool, Optional[str]]:
    """Validate CSRF token from header (AJAX) or form body.

    Returns:
        Tuple of (is_valid, error_message). Error message is None if valid.
    """
    cookie_token = get_csrf_token_from_cookie(request)

    if is_ajax:
        csrf_token = request.headers.get("X-CSRF-Token")
    else:
        csrf_token = form_csrf_token

    if not cookie_token or cookie_token != csrf_token:
        return False, "Invalid CSRF token"

    return True, None


def _repo_access_error_response(
    is_ajax: bool,
    request: Request,
    session: SessionData,
    error_msg: str,
    status_code: int = 400,
):
    """Return appropriate error response based on request type."""
    if is_ajax:
        return JSONResponse(
            {"success": False, "error": error_msg},
            status_code=status_code
        )
    return _create_groups_page_response(
        request, session, active_tab="repos", error_message=error_msg
    )


def _repo_access_success_response(
    is_ajax: bool,
    request: Request,
    session: SessionData,
    success_message: str,
):
    """Return appropriate success response based on request type."""
    if is_ajax:
        return JSONResponse({"success": True})
    return _create_groups_page_response(
        request, session, active_tab="repos", success_message=success_message
    )


@web_router.post("/groups/repo-access/grant")
async def grant_repo_access(
    request: Request,
    repo_name: Optional[str] = Form(None),
    group_id: Optional[int] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Grant repository access to a group.

    Supports both AJAX (JSON) and form POST requests (Story #199).
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Validate CSRF
    is_valid, error_msg = _validate_repo_access_csrf(request, is_ajax, csrf_token)
    if not is_valid:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 403)

    # Parse request
    repo_name, group_id, error_msg = await _parse_repo_access_request(
        request, is_ajax, repo_name, group_id
    )
    if error_msg:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 400)

    if not repo_name or group_id is None:
        return _repo_access_error_response(
            is_ajax, request, session, "Missing required parameters: repo_name and group_id", 400
        )

    group_manager = _get_group_manager()

    try:
        group = group_manager.get_group(group_id)
        if not group:
            return _repo_access_error_response(is_ajax, request, session, "Group not found", 404)

        success = group_manager.grant_repo_access(
            repo_name=repo_name,
            group_id=group_id,
            granted_by=session.username,
        )

        if success:
            group_manager.log_audit(
                admin_id=session.username,
                action_type="repo_access_grant",
                target_type="repo",
                target_id=repo_name,
                details=f"Granted access to group '{group.name}'",
            )

        message = (
            f"Granted '{repo_name}' access to '{group.name}'" if success
            else f"'{group.name}' already has access to '{repo_name}'"
        )
        return _repo_access_success_response(is_ajax, request, session, message)

    except Exception as e:
        logger.error(
            format_error_log("SCIP-GENERAL-042", "Failed to grant repo access: %s", e)
        )
        return _repo_access_error_response(is_ajax, request, session, str(e), 500)


@web_router.post("/groups/repo-access/revoke")
async def revoke_repo_access(
    request: Request,
    repo_name: Optional[str] = Form(None),
    group_id: Optional[int] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Revoke repository access from a group.

    Supports both AJAX (JSON) and form POST requests (Story #199).
    """
    # H1 fix: Import exception class before try block (not inside it)
    from code_indexer.server.services.group_access_manager import (
        CidxMetaCannotBeRevokedError,
    )

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Validate CSRF
    is_valid, error_msg = _validate_repo_access_csrf(request, is_ajax, csrf_token)
    if not is_valid:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 403)

    # Parse request
    repo_name, group_id, error_msg = await _parse_repo_access_request(
        request, is_ajax, repo_name, group_id
    )
    if error_msg:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 400)

    if not repo_name or group_id is None:
        return _repo_access_error_response(
            is_ajax, request, session, "Missing required parameters: repo_name and group_id", 400
        )

    group_manager = _get_group_manager()

    try:
        group = group_manager.get_group(group_id)
        if not group:
            return _repo_access_error_response(is_ajax, request, session, "Group not found", 404)

        success = group_manager.revoke_repo_access(
            repo_name=repo_name,
            group_id=group_id,
        )

        if success:
            group_manager.log_audit(
                admin_id=session.username,
                action_type="repo_access_revoke",
                target_type="repo",
                target_id=repo_name,
                details=f"Revoked access from group '{group.name}'",
            )

        message = (
            f"Revoked '{repo_name}' access from '{group.name}'" if success
            else f"'{group.name}' did not have access to '{repo_name}'"
        )
        return _repo_access_success_response(is_ajax, request, session, message)

    except CidxMetaCannotBeRevokedError:
        return _repo_access_error_response(
            is_ajax, request, session, "cidx-meta access cannot be revoked", 400
        )
    except Exception as e:
        logger.error(
            format_error_log("SCIP-GENERAL-043", "Failed to revoke repo access: %s", e)
        )
        return _repo_access_error_response(is_ajax, request, session, str(e), 500)


def _get_golden_repo_manager():
    """Get golden repository manager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "golden_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "golden_repo_manager not initialized. "
            "Server must set app.state.golden_repo_manager during startup."
        )
    return manager


def _golden_repo_not_found_error():
    """Lazy import of GoldenRepoNotFoundError to avoid circular imports."""
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoNotFoundError,
    )
    return GoldenRepoNotFoundError


# Module-level lazy sentinel for exception matching
_GoldenRepoNotFoundError = type("_LazyError", (Exception,), {})
try:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoNotFoundError as _GoldenRepoNotFoundError,
    )
except ImportError:
    pass


def _get_golden_repo_branch_service():
    """Get golden repo branch service from app state (may return None)."""
    from code_indexer.server import app as app_module

    return getattr(app_module.app.state, "golden_repo_branch_service", None)


def generate_unique_alias(repo_name: str, golden_repo_manager) -> str:
    """
    Generate unique alias from repository name.

    Examples:
        "org/my-project" -> "my-project"
        "group/subgroup/project" -> "project"

    If alias exists, add suffix: "project-2", "project-3", etc.

    Args:
        repo_name: Repository name (may include path components like org/project)
        golden_repo_manager: GoldenRepoManager instance to check for conflicts

    Returns:
        Unique alias string (lowercase, special chars replaced with dashes)
    """
    import re

    # Extract project name (last path component)
    base_alias = repo_name.split("/")[-1]

    # Clean up: lowercase, replace special chars with dashes
    base_alias = re.sub(r"[^a-z0-9-]", "-", base_alias.lower())

    # Collapse multiple dashes into one
    base_alias = re.sub(r"-+", "-", base_alias)

    # Remove leading/trailing dashes
    base_alias = base_alias.strip("-")

    # Handle empty result
    if not base_alias:
        base_alias = "repo"

    # Check for conflicts with existing golden repos
    existing_repos = golden_repo_manager.list_golden_repos()
    existing_aliases = {r["alias"] for r in existing_repos}

    if base_alias not in existing_aliases:
        return base_alias

    # Add numeric suffix
    suffix = 2
    while f"{base_alias}-{suffix}" in existing_aliases:
        suffix += 1

    return f"{base_alias}-{suffix}"


def _batch_create_repos(
    repos: List[Dict[str, str]],
    submitter_username: str,
    golden_repo_manager,
) -> Dict[str, Any]:
    """
    Create multiple golden repositories from discovered repos.

    Args:
        repos: List of repo objects with clone_url, alias, branch, platform
        submitter_username: Username of the admin submitting the batch
        golden_repo_manager: GoldenRepoManager instance

    Returns:
        Dict with success, results array, and summary string
    """
    results = []

    for repo_data in repos:
        try:
            # Generate unique alias from repo name
            alias = generate_unique_alias(repo_data["alias"], golden_repo_manager)

            # Create golden repo
            job_id = golden_repo_manager.add_golden_repo(
                repo_url=repo_data["clone_url"],
                alias=alias,
                default_branch=repo_data.get("branch", "main"),
                submitter_username=submitter_username,
            )

            results.append(
                {
                    "alias": alias,
                    "status": "success",
                    "job_id": job_id,
                }
            )
        except Exception as e:
            results.append(
                {
                    "alias": repo_data.get("alias", "unknown"),
                    "status": "failed",
                    "error": str(e),
                }
            )

    success_count = len([r for r in results if r["status"] == "success"])
    failed_count = len([r for r in results if r["status"] == "failed"])

    return {
        "success": failed_count == 0,
        "results": results,
        "summary": f"{success_count} succeeded, {failed_count} failed",
    }


def _get_golden_repos_list():
    """Get list of all golden repositories with global alias, version, and index info."""
    try:
        import os
        import json
        from pathlib import Path

        manager = _get_golden_repo_manager()
        repos = manager.list_golden_repos()

        server_data_dir = os.environ.get(
            "CIDX_SERVER_DATA_DIR",
            os.path.expanduser("~/.cidx-server"),
        )
        golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"

        # Get global registry to check global activation status
        try:
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
            )

            registry = get_server_global_registry(str(golden_repos_dir))
            global_repos = {r["repo_name"]: r for r in registry.list_global_repos()}
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SCIP-GENERAL-044",
                    "Could not load global registry: %s",
                    e,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            global_repos = {}

        # Get alias info for version and target path
        aliases_dir = golden_repos_dir / "aliases"

        # Get category lookup by repo alias (Story #183)
        category_lookup = {}
        try:
            category_service = _get_repo_category_service()
            repo_map = category_service.get_repo_category_map()
            for alias, info in repo_map.items():
                if info.get("category_name"):
                    category_lookup[alias] = {
                        "category_id": info["category_id"],
                        "category_name": info["category_name"],
                        "category_priority": info["priority"]
                    }
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SCIP-GENERAL-048",
                    "Could not load category information: %s",
                    e,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Add status, global alias, version, and index information for display
        for repo in repos:
            # Default status to 'ready' if not set
            if "status" not in repo:
                repo["status"] = "ready"
            # Format last_indexed date if available
            if "created_at" in repo and repo["created_at"]:
                repo["last_indexed"] = repo["created_at"][:10]  # Just the date part
            else:
                repo["last_indexed"] = None

            # Add global alias info if globally activated
            alias = repo.get("alias", "")
            global_alias_name = f"{alias}-global"
            index_path = None
            version = None

            if alias in global_repos:
                repo["global_alias"] = global_repos[alias]["alias_name"]
                repo["globally_queryable"] = True

                # Read alias file to get actual target path and version
                alias_file = aliases_dir / f"{global_alias_name}.json"
                if alias_file.exists():
                    try:
                        with open(alias_file, "r") as f:
                            alias_data = json.load(f)
                        index_path = alias_data.get("target_path")
                        # Extract version from path (e.g., v_1764703630)
                        if index_path and ".versioned" in index_path:
                            version = Path(index_path).name
                        repo["version"] = version
                        repo["last_refresh"] = (
                            alias_data.get("last_refresh", "")[:19]
                            if alias_data.get("last_refresh")
                            else None
                        )
                    except Exception as e:
                        logger.warning(
                            format_error_log(
                                "SCIP-GENERAL-045",
                                "Could not read alias file %s: %s",
                                alias_file,
                                e,
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        repo["version"] = None
                        repo["last_refresh"] = None
                else:
                    repo["version"] = None
                    repo["last_refresh"] = None
            else:
                repo["global_alias"] = None
                repo["globally_queryable"] = False
                repo["version"] = None
                repo["last_refresh"] = None
                # Use clone_path for non-global repos
                index_path = repo.get("clone_path")

            # Fetch temporal status for globally activated repos
            if repo.get("global_alias"):
                try:
                    from code_indexer.server.services.dashboard_service import (
                        DashboardService,
                    )

                    dashboard = DashboardService()
                    temporal_status = dashboard.get_temporal_index_status(
                        username="_global", repo_alias=repo["global_alias"]
                    )
                    repo["temporal_status"] = temporal_status
                except Exception as e:
                    logger.warning(
                        format_error_log(
                            "SCIP-GENERAL-046",
                            "Failed to get temporal status for %s: %s",
                            repo.get("alias"),
                            e,
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    repo["temporal_status"] = {"format": "error", "message": str(e)}
            else:
                repo["temporal_status"] = {"format": "none"}

            # Check available indexes (factual check of filesystem)
            repo["has_semantic"] = False
            repo["has_fts"] = False
            repo["has_temporal"] = False
            repo["has_scip"] = False

            if index_path:
                index_base = Path(index_path) / ".code-indexer"
                if index_base.exists():
                    # Check semantic index (any model directory with hnsw_index.bin)
                    index_dir = index_base / "index"
                    if index_dir.exists():
                        for model_dir in index_dir.iterdir():
                            if (
                                model_dir.is_dir()
                                and (model_dir / "hnsw_index.bin").exists()
                            ):
                                repo["has_semantic"] = True
                                break

                    # Check FTS index (tantivy_index with files)
                    tantivy_dir = index_base / "tantivy_index"
                    if tantivy_dir.exists() and any(tantivy_dir.iterdir()):
                        repo["has_fts"] = True

                    # Check temporal index (code-indexer-temporal collection)
                    temporal_dir = (
                        index_dir / "code-indexer-temporal"
                        if index_dir.exists()
                        else None
                    )
                    if (
                        temporal_dir
                        and temporal_dir.exists()
                        and (temporal_dir / "hnsw_index.bin").exists()
                    ):
                        repo["has_temporal"] = True

                    # Check SCIP index (.code-indexer/scip/ with .scip.db files)
                    # CRITICAL: .scip protobuf files are DELETED after database conversion
                    # Only .scip.db (SQLite) files persist after 'cidx scip generate'
                    scip_dir = index_base / "scip"
                    if scip_dir.exists():
                        # Check for any .scip.db files in scip directory or subdirectories
                        scip_files = list(scip_dir.glob("**/*.scip.db"))
                        if scip_files:
                            repo["has_scip"] = True

            # Add category information to each repo (Story #183)
            alias = repo.get("alias", "")
            if alias in category_lookup:
                repo["category_id"] = category_lookup[alias]["category_id"]
                repo["category_name"] = category_lookup[alias]["category_name"]
                repo["category_priority"] = category_lookup[alias]["category_priority"]
            else:
                repo["category_id"] = None
                repo["category_name"] = None
                repo["category_priority"] = None

        return sorted(repos, key=lambda r: r.get("alias", "").lower())
    except Exception as e:
        logger.error(
            format_error_log(
                "SCIP-GENERAL-047",
                "Failed to get golden repos list: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return []


def _create_golden_repos_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create golden repos page response with all necessary context."""
    csrf_token = generate_csrf_token()
    repos = _get_golden_repos_list()
    users = _get_users_list()

    # Get categories for dropdown (Story #183)
    try:
        category_service = _get_repo_category_service()
        categories = category_service.list_categories()
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-049",
                "Could not load categories for template: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        categories = []

    response = templates.TemplateResponse(
        "golden_repos.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "golden-repos",
            "show_nav": True,
            "csrf_token": csrf_token,
            "repos": repos,
            "users": [{"username": u.username} for u in users],
            "categories": categories,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/golden-repos", response_class=HTMLResponse)
def golden_repos_page(request: Request):
    """Golden repositories management page - list all golden repos with CRUD operations."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_golden_repos_page_response(request, session)


@web_router.post("/golden-repos/add", response_class=HTMLResponse)
def add_golden_repo(
    request: Request,
    alias: str = Form(...),
    repo_url: str = Form(...),
    default_branch: str = Form("main"),
    csrf_token: Optional[str] = Form(None),
):
    """Add a new golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not alias or not alias.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Repository name is required"
        )

    if not repo_url or not repo_url.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Repository path/URL is required"
        )

    # Try to add the repository
    try:
        manager = _get_golden_repo_manager()
        job_id = manager.add_golden_repo(
            repo_url=repo_url.strip(),
            alias=alias.strip(),
            default_branch=default_branch.strip() or "main",
            submitter_username=session.username,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' add job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        # Handle common error cases
        if "already exists" in error_msg.lower():
            error_msg = f"Repository alias '{alias}' already exists"
        elif "invalid" in error_msg.lower() or "inaccessible" in error_msg.lower():
            error_msg = f"Invalid or inaccessible repository: {repo_url}"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post("/golden-repos/batch-create")
def batch_create_golden_repos(
    request: Request,
    repos: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """
    Create multiple golden repositories from discovered repos.

    Body params:
        repos: JSON array of objects with:
            - clone_url: Repository URL
            - alias: Generated alias (project name)
            - branch: Default branch
            - platform: gitlab or github
        csrf_token: CSRF token for validation
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return JSONResponse(
            {"success": False, "error": "Invalid CSRF token"},
            status_code=403,
        )

    # Parse JSON repos array
    try:
        repo_list = json.loads(repos)
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"success": False, "error": f"Invalid JSON: {e}"},
            status_code=400,
        )

    if not isinstance(repo_list, list):
        return JSONResponse(
            {"success": False, "error": "repos must be a JSON array"},
            status_code=400,
        )

    # Process batch creation
    manager = _get_golden_repo_manager()
    results = _batch_create_repos(repo_list, session.username, manager)

    return JSONResponse(results)


@web_router.post("/golden-repos/{alias}/delete", response_class=HTMLResponse)
def delete_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Delete a golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to delete the repository
    try:
        manager = _get_golden_repo_manager()
        job_id = manager.remove_golden_repo(
            alias=alias,
            submitter_username=session.username,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' deletion job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post("/golden-repos/{alias}/refresh", response_class=HTMLResponse)
def refresh_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Refresh (re-index) a golden repository."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to refresh the repository
    try:
        manager = _get_golden_repo_manager()
        # Validate repo exists before scheduling
        if alias not in manager.golden_repos:
            raise Exception(f"Repository '{alias}' not found")
        # Delegate to RefreshScheduler (index-source-first versioned pipeline)
        from code_indexer.server import app as app_module

        lifecycle_manager = getattr(
            app_module.app.state, "global_lifecycle_manager", None
        )
        if not lifecycle_manager or not lifecycle_manager.refresh_scheduler:
            raise Exception("RefreshScheduler not available")
        # Resolution from bare alias to global format happens inside RefreshScheduler
        job_id = lifecycle_manager.refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=session.username
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' refresh job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post("/golden-repos/{alias}/force-resync", response_class=HTMLResponse)
def force_resync_golden_repo(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Force re-sync (reset and re-index) a golden repository, discarding local divergence."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to force re-sync the repository
    try:
        manager = _get_golden_repo_manager()
        # Validate repo exists before scheduling
        if alias not in manager.golden_repos:
            raise Exception(f"Repository '{alias}' not found")
        # Delegate to RefreshScheduler with force_reset=True
        from code_indexer.server import app as app_module

        lifecycle_manager = getattr(
            app_module.app.state, "global_lifecycle_manager", None
        )
        if not lifecycle_manager or not lifecycle_manager.refresh_scheduler:
            raise Exception("RefreshScheduler not available")
        # force_reset=True discards divergent local state before re-indexing
        job_id = lifecycle_manager.refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=session.username, force_reset=True
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{alias}' force re-sync job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{alias}' not found"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.post("/golden-repos/{alias}/wiki-toggle", response_class=HTMLResponse)
def toggle_wiki_enabled(
    request: Request,
    alias: str,
    wiki_enabled: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Toggle wiki_enabled for a golden repo (Story #280)."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    if not validate_login_csrf_token(request, csrf_token):
        return templates.TemplateResponse(
            "partials/error_message.html",
            {"request": request, "error": "Invalid CSRF token"},
            status_code=400,
        )
    manager = _get_golden_repo_manager()
    enabling = wiki_enabled == "1"
    manager.set_wiki_enabled(alias, enabling)
    if enabling:
        # Lifecycle hook: Populate initial view counts from front matter (Story #287, AC2)
        try:
            import threading
            from pathlib import Path
            from ..wiki.wiki_cache import WikiCache
            from ..wiki.wiki_service import WikiService
            from ...global_repos.alias_manager import AliasManager
            cache = WikiCache(manager.db_path)
            cache.ensure_tables()
            aliases_dir = str(Path(manager.golden_repos_dir) / "aliases")
            actual_path = AliasManager(aliases_dir).read_alias(f"{alias}-global")
            if actual_path:
                svc = WikiService()
                threading.Thread(
                    target=svc.populate_views_from_front_matter,
                    args=(alias, Path(actual_path), cache),
                    daemon=True,
                ).start()
        except Exception as exc:
            logger.warning("Failed to trigger view population for %s: %s", alias, exc)
    return _create_golden_repos_page_response(
        request, session, success_message="Wiki setting updated successfully"
    )


@web_router.post("/golden-repos/{alias}/wiki-refresh", response_class=HTMLResponse)
def refresh_wiki_cache(
    request: Request,
    alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """Clear wiki render cache for a golden repo (Story #283)."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    if not validate_login_csrf_token(request, csrf_token):
        return templates.TemplateResponse(
            "partials/error_message.html",
            {"request": request, "error": "Invalid CSRF token"},
            status_code=400,
        )
    from ..wiki.wiki_cache import WikiCache
    manager = _get_golden_repo_manager()
    cache = WikiCache(manager.db_path)
    cache.invalidate_repo(alias)
    return _create_golden_repos_page_response(
        request, session, success_message="Wiki cache cleared"
    )


@web_router.post("/golden-repos/{alias}/change-branch")
async def change_golden_repo_branch(
    request: Request,
    alias: str,
):
    """Change the active branch of a golden repository (Story #303)."""
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    try:
        body = await request.json()
        branch = body.get("branch")
        if not branch:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required field: branch"},
            )

        manager = _get_golden_repo_manager()
        import asyncio
        result = await asyncio.to_thread(manager.change_branch, alias, branch)
        return JSONResponse(content=result)

    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except (FileNotFoundError, _GoldenRepoNotFoundError) as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except RuntimeError as e:
        error_msg = str(e).lower()
        if any(kw in error_msg for kw in (
            "conflict", "locked", "busy", "indexed", "refreshed", "write lock",
        )):
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=500, content={"error": str(e)})
    except Exception as e:
        logger.error(f"Branch change failed for {alias}: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Branch change failed: {e}"},
        )


@web_router.get("/golden-repos/{alias}/branches")
def get_golden_repo_branches(
    request: Request,
    alias: str,
):
    """Get list of branches for a golden repository (Story #303, AC5)."""
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    try:
        from code_indexer.server.services.golden_repo_branch_service import (
            GoldenRepoBranchService,
        )

        manager = _get_golden_repo_manager()
        branch_service = GoldenRepoBranchService(manager)
        branches = branch_service.get_golden_repo_branches(alias)
        return JSONResponse(
            content={
                "branches": [
                    {
                        "name": b.name,
                        "last_commit_hash": b.last_commit_hash,
                        "last_commit_author": b.last_commit_author,
                        "branch_type": b.branch_type,
                        "is_default": b.is_default,
                    }
                    for b in branches
                ]
            }
        )
    except (FileNotFoundError, _GoldenRepoNotFoundError) as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to get branches for {alias}: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@web_router.post("/golden-repos/activate", response_class=HTMLResponse)
def activate_golden_repo(
    request: Request,
    golden_alias: str = Form(...),
    username: str = Form(...),
    user_alias: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    """Activate a golden repository for a user."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_golden_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate inputs
    if not golden_alias or not golden_alias.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Golden repository alias is required"
        )

    if not username or not username.strip():
        return _create_golden_repos_page_response(
            request, session, error_message="Username is required"
        )

    # Use golden_alias as user_alias if not provided
    effective_user_alias = (
        user_alias.strip() if user_alias.strip() else golden_alias.strip()
    )

    # Try to activate the repository
    try:
        activated_manager = _get_activated_repo_manager()
        job_id = activated_manager.activate_repository(
            username=username.strip(),
            golden_repo_alias=golden_alias.strip(),
            user_alias=effective_user_alias,
        )
        return _create_golden_repos_page_response(
            request,
            session,
            success_message=f"Repository '{golden_alias}' activation for user '{username}' submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Golden repository '{golden_alias}' not found"
        elif "already" in error_msg.lower():
            error_msg = f"User '{username}' already has this repository activated"
        return _create_golden_repos_page_response(
            request, session, error_message=error_msg
        )


@web_router.get("/golden-repos/{alias}/details", response_class=HTMLResponse)
def golden_repo_details(
    request: Request,
    alias: str,
):
    """Get details for a specific golden repository."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    try:
        manager = _get_golden_repo_manager()
        repo = manager.get_golden_repo(alias)
        if not repo:
            raise HTTPException(
                status_code=404, detail=f"Repository '{alias}' not found"
            )

        # Return repository details as JSON-like HTML response
        # Get existing CSRF token from cookie or generate new one
        csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

        response = templates.TemplateResponse(
            "partials/golden_repos_list.html",
            {
                "request": request,
                "csrf_token": csrf_token,
                "repos": [repo.to_dict()],
            },
        )

        # Set CSRF cookie to ensure token is available for form submission
        set_csrf_cookie(response, csrf_token)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-026",
                "Failed to get golden repo details for '%s': %s",
                alias,
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        raise HTTPException(status_code=404, detail=f"Repository '{alias}' not found")


@web_router.get("/partials/golden-repos-list", response_class=HTMLResponse)
def golden_repos_list_partial(request: Request):
    """
    Partial refresh endpoint for golden repos list section.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()
    repos = _get_golden_repos_list()

    # Get categories for dropdown (Story #183)
    try:
        category_service = _get_repo_category_service()
        categories = category_service.list_categories()
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-050",
                "Could not load categories for partial template: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        categories = []

    response = templates.TemplateResponse(
        "partials/golden_repos_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repos": repos,
            "categories": categories,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


def _get_activated_repo_manager():
    """Get activated repository manager, handling import lazily to avoid circular imports."""
    from ..repositories.activated_repo_manager import ActivatedRepoManager
    import os
    from pathlib import Path

    # Get data directory from environment or use default
    # Must match app.py: data_dir = server_data_dir / "data"
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", os.path.expanduser("~/.cidx-server")
    )
    data_dir = str(Path(server_data_dir) / "data")
    return ActivatedRepoManager(data_dir=data_dir)


def _get_all_activated_repos() -> list:
    """
    Get all activated repositories across all users.

    Returns:
        List of activated repository dictionaries with username added and temporal_status
    """
    import os
    from ..services.dashboard_service import DashboardService

    try:
        manager = _get_activated_repo_manager()
        dashboard_service = DashboardService()
        all_repos = []

        # Get base activated-repos directory
        activated_repos_dir = manager.activated_repos_dir

        if not os.path.exists(activated_repos_dir):
            return []

        # Build category lookup by golden repo alias (Story #183 - AC7)
        category_lookup = {}
        try:
            category_service = _get_repo_category_service()
            repo_map = category_service.get_repo_category_map()
            for alias, info in repo_map.items():
                category_lookup[alias] = {
                    "category_name": info.get("category_name") or "Unassigned",
                    "category_id": info.get("category_id"),
                    "category_priority": info.get("priority"),
                }
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SCIP-GENERAL-051",
                    "Could not load category information for activated repos: %s",
                    e,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Iterate over all user directories
        for username in os.listdir(activated_repos_dir):
            user_dir = os.path.join(activated_repos_dir, username)
            if os.path.isdir(user_dir):
                # Get repositories for this user
                user_repos = manager.list_activated_repositories(username)
                for repo in user_repos:
                    # Add username to repo data
                    repo["username"] = username
                    # Set default status if not present
                    if "status" not in repo:
                        repo["status"] = "active"

                    # Add category info from golden repo (Story #183 - AC7)
                    golden_alias = repo.get("golden_repo_alias", "")
                    cat_info = category_lookup.get(golden_alias, {})
                    if isinstance(cat_info, dict):
                        repo["category_name"] = cat_info.get("category_name", "Unassigned")
                        repo["category_id"] = cat_info.get("category_id")
                        repo["category_priority"] = cat_info.get("category_priority")
                    else:
                        # Backward compatibility with old string format
                        repo["category_name"] = cat_info or "Unassigned"
                        repo["category_id"] = None
                        repo["category_priority"] = None

                    # Fetch temporal status for this repository
                    try:
                        temporal_status = dashboard_service.get_temporal_index_status(
                            username=username, repo_alias=repo.get("user_alias", "")
                        )
                        repo["temporal_status"] = temporal_status
                    except Exception as e:
                        # Honest error handling - indicate failure clearly
                        logger.error(
                            format_error_log(
                                "STORE-GENERAL-027",
                                "Failed to get temporal status for repo %s/%s: %s",
                                username,
                                repo.get("user_alias", "unknown"),
                                e,
                                exc_info=True,
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        # Provide error temporal_status with honest error format
                        repo["temporal_status"] = {
                            "error": str(e),
                            "format": "error",
                            "file_count": 0,
                            "needs_reindex": False,
                            "message": f"Unable to determine temporal index status: {str(e)}",
                        }

                    all_repos.append(repo)

        # Sort by user_alias alphabetically (case-insensitive)
        all_repos.sort(key=lambda r: r.get("user_alias", "").lower())
        return all_repos

    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-028",
                "Failed to get activated repos: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return []


def _get_unique_golden_repos(repos: list) -> list:
    """Get list of unique golden repo aliases from activated repos."""
    golden_repos = set()
    for repo in repos:
        golden_alias = repo.get("golden_repo_alias")
        if golden_alias:
            golden_repos.add(golden_alias)
    return sorted(list(golden_repos))


def _get_unique_users(repos: list) -> list:
    """Get list of unique usernames from activated repos."""
    users = set()
    for repo in repos:
        username = repo.get("username")
        if username:
            users.add(username)
    return sorted(list(users))


def _filter_repos(
    repos: list,
    search: Optional[str] = None,
    golden_repo: Optional[str] = None,
    user: Optional[str] = None,
) -> list:
    """Filter repositories based on search criteria."""
    filtered = repos

    if search:
        search_lower = search.lower()
        filtered = [
            r
            for r in filtered
            if search_lower in r.get("user_alias", "").lower()
            or search_lower in r.get("username", "").lower()
            or search_lower in r.get("golden_repo_alias", "").lower()
        ]

    if golden_repo:
        filtered = [r for r in filtered if r.get("golden_repo_alias") == golden_repo]

    if user:
        filtered = [r for r in filtered if r.get("username") == user]

    return filtered


def _paginate_repos(repos: list, page: int = 1, per_page: int = 25) -> tuple:
    """Paginate repositories list.

    Returns:
        Tuple of (paginated_repos, total_pages, current_page)
    """
    total = len(repos)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated = repos[start_idx:end_idx]

    return paginated, total_pages, page


def _create_repos_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
    search: Optional[str] = None,
    golden_repo_filter: Optional[str] = None,
    user_filter: Optional[str] = None,
    page: int = 1,
) -> HTMLResponse:
    """Create repos page response with all necessary context."""
    csrf_token = generate_csrf_token()

    # Get all activated repos
    all_repos = _get_all_activated_repos()

    # Get unique values for filter dropdowns
    golden_repos = _get_unique_golden_repos(all_repos)
    users = _get_unique_users(all_repos)

    # Apply filters
    filtered_repos = _filter_repos(all_repos, search, golden_repo_filter, user_filter)

    # Paginate
    paginated_repos, total_pages, current_page = _paginate_repos(filtered_repos, page)

    response = templates.TemplateResponse(
        "repos.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "repos",
            "show_nav": True,
            "csrf_token": csrf_token,
            "repos": paginated_repos,
            "golden_repos": golden_repos,
            "users": users,
            "search": search,
            "golden_repo_filter": golden_repo_filter,
            "user_filter": user_filter,
            "page": current_page,
            "total_pages": total_pages,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/repos", response_class=HTMLResponse)
def repos_page(
    request: Request,
    search: Optional[str] = None,
    golden_repo: Optional[str] = None,
    user: Optional[str] = None,
    page: int = 1,
):
    """
    Activated repositories management page.

    Displays all activated repositories with filtering and pagination.
    Sorted by activation date (newest first).
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_repos_page_response(
        request,
        session,
        search=search,
        golden_repo_filter=golden_repo,
        user_filter=user,
        page=page,
    )


@web_router.get("/partials/repos-list", response_class=HTMLResponse)
def repos_list_partial(
    request: Request,
    search: Optional[str] = None,
    golden_repo: Optional[str] = None,
    user: Optional[str] = None,
    page: int = 1,
):
    """
    Partial refresh endpoint for repos list section.

    Returns HTML fragment for htmx partial updates.
    Supports filtering and pagination parameters.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()

    # Get all activated repos
    all_repos = _get_all_activated_repos()

    # Apply filters
    filtered_repos = _filter_repos(all_repos, search, golden_repo, user)

    # Paginate
    paginated_repos, total_pages, current_page = _paginate_repos(filtered_repos, page)

    response = templates.TemplateResponse(
        "partials/repos_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repos": paginated_repos,
            "search": search,
            "golden_repo_filter": golden_repo,
            "user_filter": user,
            "page": current_page,
            "total_pages": total_pages,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/repos/{username}/{user_alias}/details", response_class=HTMLResponse)
def repo_details(
    request: Request,
    username: str,
    user_alias: str,
):
    """
    Get details for a specific activated repository.

    Returns detailed information about the repository.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    try:
        manager = _get_activated_repo_manager()
        repo = manager.get_repository(username, user_alias)

        if not repo:
            raise HTTPException(
                status_code=404,
                detail=f"Repository '{user_alias}' not found for user '{username}'",
            )

        # Add username to repo data
        repo["username"] = username

        # Return repository details as HTML partial
        # Get existing CSRF token from cookie or generate new one
        csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

        response = templates.TemplateResponse(
            "partials/repos_list.html",
            {
                "request": request,
                "csrf_token": csrf_token,
                "repos": [repo],
            },
        )

        # Set CSRF cookie to ensure token is available for form submission
        set_csrf_cookie(response, csrf_token)
        return response
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"Repository '{user_alias}' not found for user '{username}'",
        )


@web_router.post(
    "/repos/{username}/{user_alias}/deactivate", response_class=HTMLResponse
)
def deactivate_repo(
    request: Request,
    username: str,
    user_alias: str,
    csrf_token: Optional[str] = Form(None),
):
    """
    Deactivate an activated repository.

    Removes the activated repository for the specified user.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_repos_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Try to deactivate the repository
    try:
        manager = _get_activated_repo_manager()
        job_id = manager.deactivate_repository(
            username=username,
            user_alias=user_alias,
        )
        return _create_repos_page_response(
            request,
            session,
            success_message=f"Repository '{user_alias}' deactivation job submitted (Job ID: {job_id})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{user_alias}' not found for user '{username}'"
        return _create_repos_page_response(request, session, error_message=error_msg)


@web_router.post(
    "/activated-repos/{username}/{alias}/wiki-toggle", response_class=JSONResponse
)
def toggle_user_wiki_enabled(
    request: Request,
    username: str,
    alias: str,
    wiki_enabled: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Toggle wiki_enabled for an activated repo (Story #291, AC1).

    Accepts wiki_enabled="1" to enable or "0" to disable.
    Returns JSON {"success": true} or {"error": "..."}.
    Requires admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not validate_login_csrf_token(request, csrf_token):
        return JSONResponse({"error": "Invalid CSRF token"}, status_code=400)

    try:
        manager = _get_activated_repo_manager()
        enabling = wiki_enabled == "1"
        manager.set_wiki_enabled(username, alias, enabling)
        if not enabling:
            try:
                from ..wiki.wiki_cache import WikiCache

                golden_manager = _get_golden_repo_manager()
                cache = WikiCache(golden_manager.db_path)
                cache.invalidate_user_wiki(username, alias)
            except Exception as cache_exc:
                logger.warning(
                    "Failed to invalidate wiki cache for %s/%s: %s",
                    username,
                    alias,
                    cache_exc,
                )
        return JSONResponse({"success": True})
    except Exception as exc:
        logger.warning(
            "Failed to toggle wiki for %s/%s: %s", username, alias, exc
        )
        return JSONResponse({"error": str(exc)}, status_code=400)


def _get_background_job_manager():
    """Get the background job manager instance."""
    try:
        from ..app import background_job_manager

        return background_job_manager
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-029",
                "Failed to get background job manager: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return None


def _get_all_jobs(
    status_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
):
    """
    Get all jobs with filters and pagination.

    Story #271: Delegates to BackgroundJobManager.get_jobs_for_display() which
    merges in-memory active jobs with historical SQLite jobs, applies filters
    at the database level, and handles pagination correctly.

    Returns jobs from background job manager with optional filtering.
    """
    job_manager = _get_background_job_manager()
    if not job_manager:
        return [], 0, 1

    return job_manager.get_jobs_for_display(
        status_filter=status_filter,
        type_filter=type_filter,
        search_text=search,
        page=page,
        page_size=page_size,
    )


def _get_queue_status() -> dict:
    """Get current queue status from job manager."""
    from ..jobs.config import SyncJobConfig

    # Default values if job manager is not available
    queue_status = {
        "running_count": 0,
        "queued_count": 0,
        "max_total_concurrent_jobs": SyncJobConfig.DEFAULT_MAX_TOTAL_CONCURRENT_JOBS,
        "max_concurrent_jobs_per_user": SyncJobConfig.DEFAULT_MAX_CONCURRENT_JOBS_PER_USER,
    }

    job_manager = _get_background_job_manager()
    if job_manager:
        queue_status["running_count"] = job_manager.get_active_job_count()
        queue_status["queued_count"] = job_manager.get_pending_job_count()
        # Use resource_config for limits (BackgroundJobManager doesn't have these as direct attributes)
        if hasattr(job_manager, "resource_config") and job_manager.resource_config:
            queue_status["max_total_concurrent_jobs"] = getattr(
                job_manager.resource_config,
                "max_total_concurrent_jobs",
                SyncJobConfig.DEFAULT_MAX_TOTAL_CONCURRENT_JOBS,
            )
            queue_status["max_concurrent_jobs_per_user"] = getattr(
                job_manager.resource_config,
                "max_concurrent_jobs_per_user",
                SyncJobConfig.DEFAULT_MAX_CONCURRENT_JOBS_PER_USER,
            )

    return queue_status


def _create_jobs_page_response(
    request: Request,
    session: SessionData,
    status_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
):
    """Create jobs page response with filters and pagination."""
    # Generate CSRF token
    csrf_token = generate_csrf_token()

    # Get jobs
    jobs, total_count, total_pages = _get_all_jobs(
        status_filter=status_filter,
        type_filter=type_filter,
        search=search,
        page=page,
    )

    # Get queue status
    queue_status = _get_queue_status()

    response = templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "jobs",
            "show_nav": True,
            "csrf_token": csrf_token,
            "jobs": jobs,
            "total_count": total_count,
            "total_pages": total_pages,
            "page": page,
            "status_filter": status_filter,
            "type_filter": type_filter,
            "search": search,
            "success_message": success_message,
            "error_message": error_message,
            "queue_status": queue_status,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    status_filter: Optional[str] = None,
    job_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
):
    """Jobs monitoring page - view and manage background jobs."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_jobs_page_response(
        request,
        session,
        status_filter=status_filter,
        type_filter=job_type,
        search=search,
        page=page,
    )


@web_router.get("/partials/jobs-list", response_class=HTMLResponse)
def jobs_list_partial(
    request: Request,
    status_filter: Optional[str] = None,
    job_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
):
    """Partial endpoint for jobs list - used by htmx for dynamic updates."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()

    # Get jobs
    jobs, total_count, total_pages = _get_all_jobs(
        status_filter=status_filter,
        type_filter=job_type,
        search=search,
        page=page,
    )

    response = templates.TemplateResponse(
        "partials/jobs_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "jobs": jobs,
            "total_count": total_count,
            "total_pages": total_pages,
            "page": page,
            "status_filter": status_filter,
            "type_filter": job_type,
            "search": search,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(
    request: Request,
    job_id: str,
):
    """
    Cancel a running or pending job.

    CSRF protection removed: This endpoint is protected by admin session
    authentication via _require_admin_session(), making CSRF redundant.
    Session auth is sufficient for internal admin actions.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Get job manager
    job_manager = _get_background_job_manager()
    if not job_manager:
        return _create_jobs_page_response(
            request, session, error_message="Job manager not available"
        )

    # Cancel job
    try:
        result = job_manager.cancel_job(job_id, session.username)
        if result.get("success"):
            return _create_jobs_page_response(
                request,
                session,
                success_message=f"Job {job_id[:8]}... cancelled successfully",
            )
        else:
            return _create_jobs_page_response(
                request,
                session,
                error_message=result.get("message", "Failed to cancel job"),
            )
    except Exception as e:
        return _create_jobs_page_response(
            request, session, error_message=f"Error cancelling job: {str(e)}"
        )


@web_router.get("/api/queue-status")
def get_queue_status_api(request: Request):
    """
    Get current job queue status.

    Returns JSON with:
    - running_count: Number of currently running jobs
    - queued_count: Number of jobs waiting in queue
    - max_total_concurrent_jobs: System-wide concurrency limit
    - max_concurrent_jobs_per_user: Per-user concurrency limit
    - average_job_duration_minutes: Average job duration for wait estimates
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Authentication required"},
            status_code=401,
        )

    queue_status = _get_queue_status()

    # Add average_job_duration_minutes to the response
    # BackgroundJobManager doesn't have this attribute, use SyncJobConfig defaults
    from ..jobs.config import SyncJobConfig

    queue_status["average_job_duration_minutes"] = (
        SyncJobConfig.DEFAULT_AVERAGE_JOB_DURATION_MINUTES
    )

    return JSONResponse({"success": True, **queue_status})


# Session-based query history storage (in-memory, per session)
# Key: session_id, Value: list of query dicts
_query_history: dict = {}
MAX_QUERY_HISTORY = 10


def _get_session_query_history(session_username: str) -> list:
    """Get query history for a session."""
    return cast(list, _query_history.get(session_username, []))


def _add_to_query_history(
    session_username: str, query_text: str, repository: str, search_mode: str
) -> None:
    """Add a query to session history."""
    if session_username not in _query_history:
        _query_history[session_username] = []

    history = _query_history[session_username]

    # Add new query at the beginning
    history.insert(
        0,
        {
            "query_text": query_text,
            "repository": repository,
            "search_mode": search_mode,
        },
    )

    # Keep only MAX_QUERY_HISTORY items
    if len(history) > MAX_QUERY_HISTORY:
        _query_history[session_username] = history[:MAX_QUERY_HISTORY]


def _get_all_activated_repos_for_query() -> list:
    """
    Get all activated repositories for query dropdown.

    Returns list of repos with user_alias, username, and is_global flag.
    Includes both user-activated repos and globally activated repos.
    """
    import os
    from pathlib import Path

    repos = []

    # Add globally activated repos first
    try:
        server_data_dir = os.environ.get(
            "CIDX_SERVER_DATA_DIR",
            os.path.expanduser("~/.cidx-server"),
        )
        golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"

        from code_indexer.server.utils.registry_factory import (
            get_server_global_registry,
        )

        registry = get_server_global_registry(str(golden_repos_dir))
        global_repos = registry.list_global_repos()

        for global_repo in global_repos:
            repos.append(
                {
                    "user_alias": global_repo["alias_name"],
                    "username": "global",
                    "is_global": True,
                    "repo_name": global_repo.get("repo_name", ""),
                    "path": global_repo.get("index_path"),
                }
            )
    except Exception as e:
        logger.warning(
            format_error_log(
                "STORE-GENERAL-030",
                "Could not load global repos for query: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )

    # Add user-activated repos
    user_repos = _get_all_activated_repos()
    for repo in user_repos:
        repo["is_global"] = False
        repos.append(repo)

    return sorted(repos, key=lambda r: r.get("user_alias", "").lower())


def _create_query_page_response(
    request: Request,
    session: SessionData,
    query_text: str = "",
    selected_repository: str = "",
    search_mode: str = "semantic",
    limit: int = 10,
    language: str = "",
    path_pattern: str = "",
    min_score: str = "",
    results: Optional[list] = None,
    query_executed: bool = False,
    error_message: Optional[str] = None,
    success_message: Optional[str] = None,
    time_range_all: bool = False,
    time_range: str = "",
    at_commit: str = "",
    include_removed: bool = False,
    case_sensitive: bool = False,
    fuzzy: bool = False,
    regex: bool = False,
    scip_query_type: str = "definition",
    scip_exact: bool = False,
) -> HTMLResponse:
    """Create query page response with all necessary context."""
    csrf_token = generate_csrf_token()
    repositories = _get_all_activated_repos_for_query()
    query_history = _get_session_query_history(session.username)

    response = templates.TemplateResponse(
        "query.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "query",
            "show_nav": True,
            "csrf_token": csrf_token,
            "repositories": repositories,
            "query_history": query_history,
            "query_text": query_text,
            "selected_repository": selected_repository,
            "search_mode": search_mode,
            "limit": limit,
            "language": language,
            "path_pattern": path_pattern,
            "min_score": min_score,
            "results": results,
            "query_executed": query_executed,
            "error_message": error_message,
            "success_message": success_message,
            "time_range_all": time_range_all,
            "time_range": time_range,
            "at_commit": at_commit,
            "include_removed": include_removed,
            "case_sensitive": case_sensitive,
            "fuzzy": fuzzy,
            "regex": regex,
            "scip_query_type": scip_query_type,
            "scip_exact": scip_exact,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/query", response_class=HTMLResponse)
def query_page(request: Request):
    """Query testing interface page."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_query_page_response(request, session)


@web_router.post("/query", response_class=HTMLResponse)
def query_submit(
    request: Request,
    query_text: str = Form(""),
    repository: str = Form(""),
    search_mode: str = Form("semantic"),
    limit: int = Form(10),
    language: str = Form(""),
    path_pattern: str = Form(""),
    min_score: str = Form(""),
    csrf_token: Optional[str] = Form(None),
    time_range_all: bool = Form(False),
    time_range: str = Form(""),
    at_commit: str = Form(""),
    include_removed: bool = Form(False),
    case_sensitive: bool = Form(False),
    fuzzy: bool = Form(False),
    regex: bool = Form(False),
    scip_query_type: str = Form("definition"),
    scip_exact: bool = Form(False),
):
    """Process query form submission."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Invalid CSRF token",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            include_removed=include_removed,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    # Validate required fields
    if not query_text or not query_text.strip():
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Query text is required",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            include_removed=include_removed,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    if not repository:
        return _create_query_page_response(
            request,
            session,
            query_text=query_text,
            selected_repository=repository,
            search_mode=search_mode,
            limit=limit,
            language=language,
            path_pattern=path_pattern,
            min_score=min_score,
            error_message="Please select a repository",
            time_range_all=time_range_all,
            time_range=time_range,
            at_commit=at_commit,
            include_removed=include_removed,
            case_sensitive=case_sensitive,
            fuzzy=fuzzy,
            regex=regex,
            scip_query_type=scip_query_type,
            scip_exact=scip_exact,
        )

    # Add to query history
    _add_to_query_history(session.username, query_text.strip(), repository, search_mode)

    # Handle temporal search mode - default to time_range_all if no specific temporal params
    if search_mode == "temporal":
        if not time_range and not at_commit and not time_range_all:
            time_range_all = True

    # Parse min_score
    parsed_min_score = None
    if min_score and min_score.strip():
        try:
            parsed_min_score = float(min_score)
        except ValueError:
            pass

    # Execute actual query
    results = []
    query_executed = True
    error_message = None

    try:
        # Handle SCIP query mode
        if search_mode == "scip":
            from code_indexer.scip.query.primitives import SCIPQueryEngine
            import glob

            # Find the username for this repository
            repo_parts = repository.split(" (")
            user_alias = repo_parts[0] if repo_parts else repository

            # Get the repository from all available repos
            all_repos = _get_all_activated_repos_for_query()
            target_repo = None
            for repo in all_repos:
                if repo.get("user_alias") == user_alias:
                    target_repo = repo
                    break

            if not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            else:
                # Determine repository path
                repo_path = target_repo.get("path")

                # For global repos, resolve path from GlobalRegistry
                if not repo_path and target_repo.get("is_global"):
                    try:
                        import os
                        from code_indexer.server.utils.registry_factory import (
                            get_server_global_registry,
                        )

                        server_data_dir = os.environ.get(
                            "CIDX_SERVER_DATA_DIR",
                            os.path.expanduser("~/.cidx-server"),
                        )
                        golden_repos_dir = (
                            Path(server_data_dir) / "data" / "golden-repos"
                        )
                        registry = get_server_global_registry(str(golden_repos_dir))
                        global_repo_meta = registry.get_global_repo(user_alias)
                        if global_repo_meta:
                            repo_path = global_repo_meta.get("index_path")
                    except Exception as e:
                        logger.warning(
                            format_error_log(
                                "STORE-GENERAL-031",
                                f"Failed to resolve global repo path for '{user_alias}': {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )

                if not repo_path:
                    error_message = f"Repository '{user_alias}' path not found"
                else:
                    # Find SCIP index files
                    scip_pattern = str(
                        Path(repo_path) / ".code-indexer" / "scip" / "**" / "*.scip"
                    )
                    scip_files = glob.glob(scip_pattern, recursive=True)

                    if not scip_files:
                        error_message = f"No SCIP index found for repository '{user_alias}'. Run 'cidx scip index' first."
                    else:
                        try:
                            # Execute query based on type
                            query_results = []
                            if scip_query_type == "impact":
                                from code_indexer.scip.query.composites import (
                                    analyze_impact,
                                )
                                from code_indexer.scip.query.primitives import (
                                    QueryResult,
                                )

                                scip_dir = Path(repo_path) / ".code-indexer" / "scip"
                                # Use depth=2 (lower than CLI default of 3) to balance coverage vs Web UI response time
                                impact_result = analyze_impact(
                                    query_text.strip(),
                                    scip_dir,
                                    depth=2,
                                    project=str(repo_path),
                                )
                                # Convert affected_symbols to QueryResult format
                                for affected in impact_result.affected_symbols:
                                    query_results.append(
                                        QueryResult(
                                            symbol=affected.symbol,
                                            project=str(repo_path),
                                            file_path=str(affected.file_path),
                                            line=affected.line,
                                            column=affected.column,
                                            kind="impact",
                                            relationship=affected.relationship,
                                            context=None,
                                        )
                                    )
                            elif scip_query_type == "callchain":
                                from code_indexer.scip.query.primitives import (
                                    QueryResult,
                                )

                                parts = query_text.strip().split(maxsplit=1)
                                if len(parts) != 2:
                                    raise ValueError(
                                        "Call chain requires two symbols: 'from_symbol to_symbol'"
                                    )
                                scip_file = Path(scip_files[0])
                                engine = SCIPQueryEngine(scip_file)
                                chains = engine.trace_call_chain(
                                    parts[0], parts[1], max_depth=5
                                )
                                for chain in chains:
                                    query_results.append(
                                        QueryResult(
                                            symbol=" -> ".join(chain.path),
                                            project=str(repo_path),
                                            file_path="(call chain)",
                                            line=0,
                                            column=0,
                                            kind="callchain",
                                            relationship=f"length={chain.length}",
                                            context=None,
                                        )
                                    )
                            elif scip_query_type == "context":
                                from code_indexer.scip.query.composites import (
                                    get_smart_context,
                                )
                                from code_indexer.scip.query.primitives import (
                                    QueryResult,
                                )

                                scip_dir = Path(repo_path) / ".code-indexer" / "scip"
                                context_result = get_smart_context(
                                    query_text.strip(),
                                    scip_dir,
                                    limit=limit,
                                    min_score=float(min_score) if min_score else 0.0,
                                    project=str(repo_path),
                                )
                                for ctx_file in context_result.files:
                                    query_results.append(
                                        QueryResult(
                                            symbol=query_text.strip(),
                                            project=str(repo_path),
                                            file_path=str(ctx_file.path),
                                            line=0,
                                            column=0,
                                            kind="context",
                                            relationship=f"score={ctx_file.relevance_score:.2f}, symbols={len(ctx_file.symbols)}",
                                            context=None,
                                        )
                                    )
                            else:
                                # For definition/references/dependencies/dependents, use engine
                                scip_file = Path(scip_files[0])
                                engine = SCIPQueryEngine(scip_file)

                                if scip_query_type == "definition":
                                    query_results = engine.find_definition(
                                        query_text.strip(), exact=scip_exact
                                    )
                                elif scip_query_type == "references":
                                    query_results = engine.find_references(
                                        query_text.strip(),
                                        limit=limit,
                                        exact=scip_exact,
                                    )
                                elif scip_query_type == "dependencies":
                                    query_results = engine.get_dependencies(
                                        query_text.strip(), exact=scip_exact
                                    )
                                elif scip_query_type == "dependents":
                                    query_results = engine.get_dependents(
                                        query_text.strip(), exact=scip_exact
                                    )

                            # Format results for template
                            for result in query_results:
                                results.append(
                                    {
                                        "file_path": result.file_path,
                                        "line_numbers": str(result.line),
                                        "content": f"{result.kind}: {result.symbol}",
                                        "score": 1.0,  # SCIP results don't have similarity scores
                                        "language": _detect_language_from_path(
                                            result.file_path
                                        ),
                                        "repository_alias": user_alias,
                                        "scip_symbol": result.symbol,
                                        "scip_kind": result.kind,
                                    }
                                )
                        except FileNotFoundError as e:
                            logger.error(
                                format_error_log(
                                    "STORE-GENERAL-032",
                                    "SCIP query failed - file not found: %s",
                                    e,
                                    exc_info=True,
                                    extra={"correlation_id": get_correlation_id()},
                                )
                            )
                            error_message = f"SCIP index not found or corrupted for repository '{user_alias}'. Generate an index with: `cidx scip generate`"
                        except Exception as e:
                            logger.error(
                                format_error_log(
                                    "STORE-GENERAL-033",
                                    "SCIP query execution failed: %s",
                                    e,
                                    exc_info=True,
                                    extra={"correlation_id": get_correlation_id()},
                                )
                            )
                            error_message = f"SCIP query failed for repository '{user_alias}': {str(e)}. Try regenerating the index with: `cidx scip generate`"

        else:
            # Handle semantic/FTS/temporal queries
            query_manager = _get_semantic_query_manager()
            if not query_manager:
                error_message = "Query service not available"
            else:
                # Find the username for this repository
                # Repository format is "user_alias (username)"
                repo_parts = repository.split(" (")
                user_alias = repo_parts[0] if repo_parts else repository

            # Get the repository from all available repos (including global)
            all_repos = _get_all_activated_repos_for_query()
            target_repo = None
            for repo in all_repos:
                if repo.get("user_alias") == user_alias:
                    target_repo = repo
                    break

            if not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            elif target_repo.get("is_global"):
                # Handle global repository query
                import os
                from code_indexer.global_repos.alias_manager import AliasManager
                from ..services.search_service import (
                    SemanticSearchService,
                    SemanticSearchRequest,
                )

                server_data_dir = os.environ.get(
                    "CIDX_SERVER_DATA_DIR",
                    os.path.expanduser("~/.cidx-server"),
                )
                aliases_dir = (
                    Path(server_data_dir) / "data" / "golden-repos" / "aliases"
                )
                alias_manager = AliasManager(str(aliases_dir))

                # Resolve alias to target path
                target_path = alias_manager.read_alias(user_alias)
                if not target_path:
                    error_message = f"Global repository '{user_alias}' alias not found"
                else:
                    # Use SemanticSearchService for direct path query
                    search_service = SemanticSearchService()
                    search_request = SemanticSearchRequest(
                        query=query_text.strip(),
                        limit=limit,
                        include_source=True,
                    )

                    try:
                        search_response = search_service.search_repository_path(
                            target_path, search_request
                        )

                        # Convert results to template format
                        for result in search_response.results:
                            results.append(
                                {
                                    "file_path": result.file_path,
                                    "line_numbers": str(result.line_start or 1),
                                    "content": result.content or "",
                                    "score": result.score,
                                    "language": _detect_language_from_path(
                                        result.file_path
                                    ),
                                }
                            )
                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "STORE-GENERAL-034",
                                "Global repo query failed: %s",
                                e,
                                exc_info=True,
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        error_message = f"Query failed: {str(e)}"
            else:
                repo_username = target_repo.get("username", session.username)

                # Execute query for user-activated repositories
                query_response = query_manager.query_user_repositories(
                    username=repo_username,
                    query_text=query_text.strip(),
                    repository_alias=user_alias,
                    limit=limit,
                    min_score=parsed_min_score,
                    language=language if language else None,
                    path_filter=path_pattern if path_pattern else None,
                    search_mode=search_mode,
                    time_range=time_range if time_range else None,
                    time_range_all=time_range_all,
                    at_commit=at_commit if at_commit else None,
                    include_removed=include_removed,
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy,
                    regex=regex,
                )

                # Convert results to template format with full metadata
                for result in query_response.get("results", []):
                    results.append(
                        {
                            "file_path": result.get("file_path", ""),
                            "line_numbers": f"{result.get('line_number', 1)}",
                            "content": result.get("code_snippet", ""),
                            "score": result.get("similarity_score", 0.0),
                            "language": _detect_language_from_path(
                                result.get("file_path", "")
                            ),
                            "repository_alias": result.get("repository_alias", ""),
                            "source_repo": result.get("source_repo"),
                            "metadata": result.get("metadata"),
                            "temporal_context": result.get("temporal_context"),
                        }
                    )

    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-035",
                "Query execution failed: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        error_message = f"Query failed: {str(e)}"

    return _create_query_page_response(
        request,
        session,
        query_text=query_text,
        selected_repository=repository,
        search_mode=search_mode,
        limit=limit,
        language=language,
        path_pattern=path_pattern,
        min_score=min_score,
        results=results if not error_message else None,
        query_executed=query_executed,
        error_message=error_message,
        time_range_all=time_range_all,
        time_range=time_range,
        at_commit=at_commit,
        include_removed=include_removed,
        case_sensitive=case_sensitive,
        fuzzy=fuzzy,
        regex=regex,
        scip_query_type=scip_query_type,
        scip_exact=scip_exact,
    )


@web_router.get("/partials/query-results", response_class=HTMLResponse)
def query_results_partial(request: Request):
    """
    Partial refresh endpoint for query results.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        "partials/query_results.html",
        {
            "request": request,
            "results": None,
            "query_executed": False,
            "query_text": "",
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


def _get_semantic_query_manager():
    """Get the semantic query manager instance."""
    try:
        from ..app import semantic_query_manager

        return semantic_query_manager
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-036",
                "Failed to get semantic query manager: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return None


def _execute_scip_query(
    target_repo: dict,
    user_alias: str,
    query_text: str,
    scip_query_type: str,
    scip_exact: bool,
    limit: int,
    min_score: str,
) -> tuple[list, Optional[str]]:
    """
    Execute SCIP query for repository. Supports all 7 SCIP query types.

    Returns tuple of (results_list, error_message).
    """
    from code_indexer.scip.query.primitives import SCIPQueryEngine, QueryResult
    import glob

    results: List[Dict[str, Any]] = []
    repo_path = target_repo.get("path")

    # For global repos, resolve path from GlobalRegistry
    if not repo_path and target_repo.get("is_global"):
        try:
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
            )
            import os

            server_data_dir = os.environ.get(
                "CIDX_SERVER_DATA_DIR",
                os.path.expanduser("~/.cidx-server"),
            )
            golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"
            registry = get_server_global_registry(str(golden_repos_dir))
            global_repo_meta = registry.get_global_repo(user_alias)
            if global_repo_meta:
                repo_path = global_repo_meta.get("index_path")
        except Exception as e:
            logger.warning(
                format_error_log(
                    "STORE-GENERAL-037",
                    f"Failed to resolve global repo path for '{user_alias}': {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    if not repo_path:
        return results, f"Repository '{user_alias}' path not found"

    scip_pattern = str(Path(repo_path) / ".code-indexer" / "scip" / "**" / "*.scip")
    scip_files = glob.glob(scip_pattern, recursive=True)
    if not scip_files:
        return (
            results,
            f"No SCIP index found for repository '{user_alias}'. Run 'cidx scip index' first.",
        )

    try:
        query_results = []
        scip_dir = Path(repo_path) / ".code-indexer" / "scip"

        if scip_query_type == "impact":
            from code_indexer.scip.query.composites import analyze_impact

            res = analyze_impact(
                query_text.strip(), scip_dir, depth=2, project=str(repo_path)
            )
            query_results = [
                QueryResult(
                    symbol=a.symbol,
                    project=str(repo_path),
                    file_path=str(a.file_path),
                    line=a.line,
                    column=a.column,
                    kind="impact",
                    relationship=a.relationship,
                    context=None,
                )
                for a in res.affected_symbols
            ]
        elif scip_query_type == "callchain":
            parts = query_text.strip().split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(
                    "Call chain requires two symbols: 'from_symbol to_symbol'"
                )
            engine = SCIPQueryEngine(Path(scip_files[0]))
            chains = engine.trace_call_chain(parts[0], parts[1], max_depth=5)
            query_results = [
                QueryResult(
                    symbol=" -> ".join(c.path),
                    project=str(repo_path),
                    file_path="(call chain)",
                    line=0,
                    column=0,
                    kind="callchain",
                    relationship=f"length={c.length}",
                    context=None,
                )
                for c in chains
            ]
        elif scip_query_type == "context":
            from code_indexer.scip.query.composites import get_smart_context

            res = get_smart_context(
                query_text.strip(),
                scip_dir,
                limit=limit,
                min_score=float(min_score) if min_score else 0.0,
                project=str(repo_path),
            )
            query_results = [
                QueryResult(
                    symbol=query_text.strip(),
                    project=str(repo_path),
                    file_path=str(f.path),
                    line=0,
                    column=0,
                    kind="context",
                    relationship=f"score={f.relevance_score:.2f}, symbols={len(f.symbols)}",
                    context=None,
                )
                for f in res.files
            ]
        else:
            engine = SCIPQueryEngine(Path(scip_files[0]))
            if scip_query_type == "definition":
                query_results = engine.find_definition(
                    query_text.strip(), exact=scip_exact
                )
            elif scip_query_type == "references":
                query_results = engine.find_references(
                    query_text.strip(), limit=limit, exact=scip_exact
                )
            elif scip_query_type == "dependencies":
                query_results = engine.get_dependencies(
                    query_text.strip(), exact=scip_exact
                )
            elif scip_query_type == "dependents":
                query_results = engine.get_dependents(
                    query_text.strip(), exact=scip_exact
                )

        # Format results for template
        for result in query_results:
            results.append(
                {
                    "file_path": result.file_path,
                    "line_numbers": str(result.line),
                    "content": f"{result.kind}: {result.symbol}",
                    "score": 1.0,  # SCIP results don't have similarity scores
                    "language": _detect_language_from_path(result.file_path),
                    "repository_alias": user_alias,
                    "scip_symbol": result.symbol,
                    "scip_kind": result.kind,
                }
            )
    except FileNotFoundError as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-038",
                "SCIP query failed - file not found: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return (
            results,
            f"SCIP index not found or corrupted for repository '{user_alias}'. Generate an index with: `cidx scip generate`",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-039",
                "SCIP query execution failed: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return (
            results,
            f"SCIP query failed for repository '{user_alias}': {str(e)}. Try regenerating the index with: `cidx scip generate`",
        )

    return results, None


@web_router.post("/partials/query-results", response_class=HTMLResponse)
def query_results_partial_post(
    request: Request,
    query_text: str = Form(""),
    repository: str = Form(""),
    search_mode: str = Form("semantic"),
    limit: int = Form(10),
    language: str = Form(""),
    path_pattern: str = Form(""),
    min_score: str = Form(""),
    csrf_token: Optional[str] = Form(None),
    time_range_all: bool = Form(False),
    time_range: str = Form(""),
    at_commit: str = Form(""),
    include_removed: bool = Form(False),
    case_sensitive: bool = Form(False),
    fuzzy: bool = Form(False),
    regex: bool = Form(False),
    scip_query_type: str = Form("definition"),
    scip_exact: bool = Form(False),
):
    """
    Execute query and return results partial via htmx.

    Returns HTML fragment for htmx partial updates.

    Note: CSRF validation is intentionally not performed for this endpoint because:
    1. Session authentication already protects against unauthorized access
    2. HTMX requests are same-origin (browser enforces this)
    3. HTMX adds specific headers (HX-Request) that indicate the request origin
    4. The main form submission route (/admin/query) retains CSRF protection
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Note: csrf_token parameter is accepted but not validated for HTMX partials
    # See docstring above for security rationale

    # Validate required fields
    if not query_text or not query_text.strip():
        return templates.TemplateResponse(
            "partials/query_results.html",
            {
                "request": request,
                "results": None,
                "query_executed": False,
                "query_text": query_text,
                "error_message": "Query text is required",
            },
        )

    if not repository:
        return templates.TemplateResponse(
            "partials/query_results.html",
            {
                "request": request,
                "results": None,
                "query_executed": False,
                "query_text": query_text,
                "error_message": "Please select a repository",
            },
        )

    # Add to query history
    _add_to_query_history(session.username, query_text.strip(), repository, search_mode)

    # Handle temporal search mode - default to time_range_all if no specific temporal params
    if search_mode == "temporal":
        if not time_range and not at_commit and not time_range_all:
            time_range_all = True

    # Parse min_score
    parsed_min_score = None
    if min_score and min_score.strip():
        try:
            parsed_min_score = float(min_score)
        except ValueError:
            pass

    # Execute actual query
    results = []
    query_executed = True
    error_message = None

    try:
        query_manager = _get_semantic_query_manager()
        if not query_manager:
            error_message = "Query service not available"
        else:
            # Find the username for this repository
            # Repository format is "user_alias (username)"
            repo_parts = repository.split(" (")
            user_alias = repo_parts[0] if repo_parts else repository

            # Get the repository owner from activated repos
            all_repos = _get_all_activated_repos_for_query()
            target_repo = None
            for repo in all_repos:
                if repo.get("user_alias") == user_alias:
                    target_repo = repo
                    break

            if not target_repo:
                error_message = f"Repository '{user_alias}' not found"
            elif search_mode == "scip":
                # Execute SCIP query using helper function
                scip_results, scip_error = _execute_scip_query(
                    target_repo,
                    user_alias,
                    query_text,
                    scip_query_type,
                    scip_exact,
                    limit,
                    min_score,
                )
                results.extend(scip_results)
                if scip_error:
                    error_message = scip_error
            elif target_repo.get("is_global"):
                # Handle global repository query
                import os
                from code_indexer.global_repos.alias_manager import AliasManager
                from ..services.search_service import (
                    SemanticSearchService,
                    SemanticSearchRequest,
                )

                server_data_dir = os.environ.get(
                    "CIDX_SERVER_DATA_DIR",
                    os.path.expanduser("~/.cidx-server"),
                )
                aliases_dir = (
                    Path(server_data_dir) / "data" / "golden-repos" / "aliases"
                )
                alias_manager = AliasManager(str(aliases_dir))

                # Resolve alias to target path
                target_path = alias_manager.read_alias(user_alias)
                if not target_path:
                    error_message = f"Global repository '{user_alias}' alias not found"
                else:
                    # Use SemanticSearchService for direct path query
                    search_service = SemanticSearchService()
                    search_request = SemanticSearchRequest(
                        query=query_text.strip(),
                        limit=limit,
                        include_source=True,
                    )

                    try:
                        search_response = search_service.search_repository_path(
                            target_path, search_request
                        )

                        # Convert results to template format
                        for result in search_response.results:
                            results.append(
                                {
                                    "file_path": result.file_path,
                                    "line_numbers": str(result.line_start or 1),
                                    "content": result.content or "",
                                    "score": result.score,
                                    "language": _detect_language_from_path(
                                        result.file_path
                                    ),
                                }
                            )
                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "STORE-GENERAL-040",
                                "Global repo query failed: %s",
                                e,
                                exc_info=True,
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )
                        error_message = f"Query failed: {str(e)}"
            else:
                # Execute query for user-activated repositories
                repo_username = target_repo.get("username", session.username)

                query_response = query_manager.query_user_repositories(
                    username=repo_username,
                    query_text=query_text.strip(),
                    repository_alias=user_alias,
                    limit=limit,
                    min_score=parsed_min_score,
                    language=language if language else None,
                    path_filter=path_pattern if path_pattern else None,
                    search_mode=search_mode,
                    time_range=time_range if time_range else None,
                    time_range_all=time_range_all,
                    at_commit=at_commit if at_commit else None,
                    include_removed=include_removed,
                    case_sensitive=case_sensitive,
                    fuzzy=fuzzy,
                    regex=regex,
                )

                # Convert results to template format with full metadata
                for result in query_response.get("results", []):
                    results.append(
                        {
                            "file_path": result.get("file_path", ""),
                            "line_numbers": f"{result.get('line_number', 1)}",
                            "content": result.get("code_snippet", ""),
                            "score": result.get("similarity_score", 0.0),
                            "language": _detect_language_from_path(
                                result.get("file_path", "")
                            ),
                            "repository_alias": result.get("repository_alias", ""),
                            "source_repo": result.get("source_repo"),
                            "metadata": result.get("metadata"),
                            "temporal_context": result.get("temporal_context"),
                        }
                    )

    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-041",
                "Query execution failed: %s",
                e,
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        error_message = f"Query failed: {str(e)}"

    csrf_token_new = generate_csrf_token()
    response = templates.TemplateResponse(
        "partials/query_results.html",
        {
            "request": request,
            "results": results if not error_message else None,
            "query_executed": query_executed,
            "query_text": query_text,
            "error_message": error_message,
            "search_mode": search_mode,
        },
    )

    set_csrf_cookie(response, csrf_token_new)
    return response


def _detect_language_from_path(file_path: str) -> str:
    """Detect programming language from file path extension."""
    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".sh": "bash",
        ".bash": "bash",
    }
    from pathlib import Path

    ext = Path(file_path).suffix.lower()
    return ext_to_lang.get(ext, "plaintext")


async def _reload_oidc_configuration():
    """Reload OIDC configuration without server restart."""
    from ..auth.oidc import routes as oidc_routes
    from ..auth.oidc.oidc_manager import OIDCManager
    from ..auth.oidc.state_manager import StateManager
    from ..services.config_service import get_config_service

    config_service = get_config_service()
    config = config_service.get_config()

    # Only reload if OIDC is enabled
    oidc_config = config.oidc_provider_config
    if oidc_config is None or not oidc_config.enabled:
        logger.info(
            "OIDC is disabled, skipping reload",
            extra={"correlation_id": get_correlation_id()},
        )
        # Clear the existing OIDC manager
        oidc_routes.oidc_manager = None
        oidc_routes.state_manager = None
        return

    # Create new OIDC manager with updated configuration
    # Reuse existing user_manager and jwt_manager from module level
    from .. import app as app_module

    logger.info(
        f"Creating new OIDC manager with config: email_claim={oidc_config.email_claim}, username_claim={oidc_config.username_claim}",
        extra={"correlation_id": get_correlation_id()},
    )

    state_manager = StateManager()
    oidc_manager = OIDCManager(
        config=oidc_config,
        user_manager=app_module.user_manager,
        jwt_manager=app_module.jwt_manager,
    )

    # Initialize OIDC database schema (no network calls)
    # Provider metadata will be discovered lazily on next SSO login attempt
    await oidc_manager.initialize()

    # Inject GroupAccessManager into OIDCManager for SSO provisioning (Story #708)
    if (
        hasattr(app_module.app.state, "group_manager")
        and app_module.app.state.group_manager
    ):
        oidc_manager.group_manager = app_module.app.state.group_manager
        logger.info(
            "GroupAccessManager injected into reloaded OIDCManager for SSO auto-provisioning",
            extra={"correlation_id": get_correlation_id()},
        )

    # Replace the old managers with new ones
    oidc_routes.oidc_manager = oidc_manager
    oidc_routes.state_manager = state_manager

    logger.info(
        f"OIDC configuration reloaded for provider: {oidc_config.provider_name} (will initialize on next login)",
        extra={"correlation_id": get_correlation_id()},
    )
    logger.info(
        f"New OIDC manager config - email_claim: {oidc_manager.config.email_claim}, username_claim: {oidc_manager.config.username_claim}",
        extra={"correlation_id": get_correlation_id()},
    )


def _get_current_config() -> dict:
    """Get current configuration from ConfigService (persisted to ~/.cidx-server/config.json)."""
    from ..services.config_service import get_config_service
    from ..utils.config_manager import (
        OIDCProviderConfig,
        TelemetryConfig,
        LangfuseConfig,
        SearchLimitsConfig,
        FileContentLimitsConfig,
        GoldenReposConfig,
        # Story #3 - Phase 2: P0/P1 settings (AC2-AC11)
        McpSessionConfig,
        HealthConfig,
        ScipConfig,
        # Story #3 - Phase 2: P2 settings (AC12-AC26)
        GitTimeoutsConfig,
        ErrorHandlingConfig,
        ApiLimitsConfig,
        WebSecurityConfig,
        # Story #3 - Phase 2: P3 settings (AC36)
        AuthConfig,
        # Story #32 - Unified content limits configuration
        ContentLimitsConfig,
        # Story #223 - Indexing configuration
        IndexingConfig,
    )
    from dataclasses import asdict

    config_service = get_config_service()
    settings = config_service.get_all_settings()

    # Ensure OIDC config has all required fields with defaults
    oidc_config = settings.get("oidc")
    if not oidc_config:
        # Provide defaults if OIDC config is missing
        oidc_config = asdict(OIDCProviderConfig())

    # Get job queue settings from SyncJobConfig defaults
    # Note: BackgroundJobManager doesn't have these attributes directly, they're managed
    # via SyncJobConfig for the sync job system. For display purposes, use defaults.
    from ..jobs.config import SyncJobConfig

    job_queue_config = {
        "max_total_concurrent_jobs": SyncJobConfig.DEFAULT_MAX_TOTAL_CONCURRENT_JOBS,
        "max_concurrent_jobs_per_user": SyncJobConfig.DEFAULT_MAX_CONCURRENT_JOBS_PER_USER,
        "average_job_duration_minutes": SyncJobConfig.DEFAULT_AVERAGE_JOB_DURATION_MINUTES,
    }

    # Ensure telemetry config has all required fields with defaults
    telemetry_config = settings.get("telemetry")
    if not telemetry_config:
        # Provide defaults if telemetry config is missing
        telemetry_config = asdict(TelemetryConfig())

    # Ensure langfuse config has all required fields with defaults
    langfuse_config = settings.get("langfuse")
    if not langfuse_config:
        # Provide defaults if langfuse config is missing
        langfuse_config = asdict(LangfuseConfig())

    # Get claude_delegation config (Story #721)
    claude_delegation_config = settings.get("claude_delegation", {})

    # Get search_limits, file_content_limits, and golden_repos config (Story #3)
    # Provide defaults if config sections are missing (backward compatibility)
    search_limits_config = settings.get("search_limits")
    if not search_limits_config:
        search_limits_config = asdict(SearchLimitsConfig())

    file_content_limits_config = settings.get("file_content_limits")
    if not file_content_limits_config:
        file_content_limits_config = asdict(FileContentLimitsConfig())

    golden_repos_config = settings.get("golden_repos")
    if not golden_repos_config:
        golden_repos_config = asdict(GoldenReposConfig())
    else:
        # Merge defaults for keys added after initial config creation
        for key, value in asdict(GoldenReposConfig()).items():
            golden_repos_config.setdefault(key, value)

    # Story #3 - Phase 2: P0/P1 settings (AC2-AC11)
    # Get mcp_session, health, scip config with defaults for backward compatibility
    mcp_session_config = settings.get("mcp_session")
    if not mcp_session_config:
        mcp_session_config = asdict(McpSessionConfig())

    health_config = settings.get("health")
    if not health_config:
        health_config = asdict(HealthConfig())

    scip_config = settings.get("scip")
    if not scip_config:
        scip_config = asdict(ScipConfig())

    # Story #3 - Phase 2: P2 settings (AC12-AC26)
    # Get git_timeouts, error_handling, api_limits, web_security config with defaults
    git_timeouts_config = settings.get("git_timeouts")
    if not git_timeouts_config:
        git_timeouts_config = asdict(GitTimeoutsConfig())

    error_handling_config = settings.get("error_handling")
    if not error_handling_config:
        error_handling_config = asdict(ErrorHandlingConfig())

    api_limits_config = settings.get("api_limits")
    if not api_limits_config:
        api_limits_config = asdict(ApiLimitsConfig())

    web_security_config = settings.get("web_security")
    if not web_security_config:
        web_security_config = asdict(WebSecurityConfig())

    # Story #3 - Phase 2: P3 settings (AC36)
    auth_config = settings.get("auth")
    if not auth_config:
        auth_config = asdict(AuthConfig())

    # Story #20: Provider API Keys (Anthropic/VoyageAI)
    # Bug #153: Provide defaults when claude_cli is None or empty dict
    claude_cli_raw = settings.get("claude_cli")
    if not claude_cli_raw:
        # If None or empty dict, use full defaults
        claude_cli_config = {
            "max_concurrent_claude_cli": 3,
            "description_refresh_interval_hours": 24,
            "description_refresh_enabled": False,
            "research_assistant_timeout_seconds": 300,
            "dependency_map_enabled": False,
            "dependency_map_interval_hours": 168,
            "dependency_map_pass_timeout_seconds": 600,
            "dependency_map_pass1_max_turns": 50,
            "dependency_map_pass2_max_turns": 60,
            "dependency_map_delta_max_turns": 30,
        }
    else:
        # If dict exists, merge with defaults (preserve existing values)
        claude_cli_config = {
            "max_concurrent_claude_cli": claude_cli_raw.get(
                "max_concurrent_claude_cli", 3
            ),
            "description_refresh_interval_hours": claude_cli_raw.get(
                "description_refresh_interval_hours", 24
            ),
            "description_refresh_enabled": claude_cli_raw.get(
                "description_refresh_enabled", False
            ),
            "research_assistant_timeout_seconds": claude_cli_raw.get(
                "research_assistant_timeout_seconds", 300
            ),
            "dependency_map_enabled": claude_cli_raw.get(
                "dependency_map_enabled", False
            ),
            "dependency_map_interval_hours": claude_cli_raw.get(
                "dependency_map_interval_hours", 168
            ),
            "dependency_map_pass_timeout_seconds": claude_cli_raw.get(
                "dependency_map_pass_timeout_seconds", 600
            ),
            "dependency_map_pass1_max_turns": claude_cli_raw.get(
                "dependency_map_pass1_max_turns", 50
            ),
            "dependency_map_pass2_max_turns": claude_cli_raw.get(
                "dependency_map_pass2_max_turns", 60
            ),
            "dependency_map_delta_max_turns": claude_cli_raw.get(
                "dependency_map_delta_max_turns", 30
            ),
        }
        # Preserve additional keys like API keys
        for key, value in claude_cli_raw.items():
            if key not in claude_cli_config:
                claude_cli_config[key] = value

    provider_api_keys_config = {
        "anthropic_configured": bool(claude_cli_config.get("anthropic_api_key")),
        "voyageai_configured": bool(claude_cli_config.get("voyageai_api_key")),
    }

    # Convert to template-friendly format
    return {
        "server": settings["server"],
        "cache": settings["cache"],
        "timeouts": settings["timeouts"],
        "password_security": settings["password_security"],
        "oidc": oidc_config,
        "job_queue": job_queue_config,
        "telemetry": telemetry_config,
        "langfuse": langfuse_config,
        "claude_delegation": claude_delegation_config,
        "search_limits": search_limits_config,
        "file_content_limits": file_content_limits_config,
        "golden_repos": golden_repos_config,
        # Story #3 - Phase 2: P0/P1 settings
        "mcp_session": mcp_session_config,
        "health": health_config,
        "scip": scip_config,
        # Story #3 - Phase 2: P2 settings (AC12-AC26)
        "git_timeouts": git_timeouts_config,
        "error_handling": error_handling_config,
        "api_limits": api_limits_config,
        "web_security": web_security_config,
        # Story #3 - Phase 2: P3 settings (AC36)
        "auth": auth_config,
        # Story #20: Provider API Keys
        "provider_api_keys": provider_api_keys_config,
        # Claude CLI integration settings (for Claude Integration config section)
        "claude_cli": claude_cli_config,
        # Story #25: Multi-search limits configuration
        "multi_search": settings.get("multi_search", {}),
        # Story #26: Background jobs configuration
        "background_jobs": settings.get("background_jobs", {}),
        # Story #28: Omni-search configuration
        "omni_search": settings.get("omni_search", {}),
        # Story #32: Unified content limits configuration
        "content_limits": settings.get("content_limits", asdict(ContentLimitsConfig())),
        # Story #223: Indexing configuration
        "indexing": settings.get("indexing", asdict(IndexingConfig())),
    }


def _validate_config_section(section: str, data: dict) -> Optional[str]:
    """Validate configuration for a section, return error message if invalid."""
    if section == "server":
        # Validate host - cannot be empty
        host = data.get("host")
        if host is not None:
            host_str = str(host).strip()
            if not host_str:
                return "Host cannot be empty"

        port = data.get("port")
        if port is not None:
            try:
                port_int = int(port)
                if port_int < 1 or port_int > 65535:
                    return "Port must be between 1 and 65535"
            except (ValueError, TypeError):
                return "Port must be a valid number"

        workers = data.get("workers")
        if workers is not None:
            try:
                workers_int = int(workers)
                if workers_int < 1:
                    return "Workers must be a positive number"
            except (ValueError, TypeError):
                return "Workers must be a valid number"

        # Validate log_level - must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL
        log_level = data.get("log_level")
        if log_level is not None:
            valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if log_level.upper() not in valid_log_levels:
                return f"Log level must be one of: {', '.join(valid_log_levels)}"

        jwt_expiration = data.get("jwt_expiration_minutes")
        if jwt_expiration is not None:
            try:
                jwt_int = int(jwt_expiration)
                if jwt_int < 1:
                    return "JWT expiration must be a positive number"
            except (ValueError, TypeError):
                return "JWT expiration must be a valid number"

    elif section == "cache":
        # Validate cache TTL values
        for field in ["index_cache_ttl_minutes", "fts_cache_ttl_minutes"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_float = float(value)
                    if val_float <= 0:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # Validate cleanup intervals
        for field in ["index_cache_cleanup_interval", "fts_cache_cleanup_interval"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # Validate payload cache settings (Story #679)
        for field in [
            "payload_preview_size_chars",
            "payload_max_fetch_size_chars",
            "payload_cache_ttl_seconds",
            "payload_cleanup_interval_seconds",
        ]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "timeouts":
        # Validate timeout values (must be positive integers)
        for field in [
            "git_clone_timeout",
            "git_pull_timeout",
            "git_refresh_timeout",
            "cidx_index_timeout",
        ]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "password_security":
        # Validate password length settings
        min_length = data.get("min_length")
        max_length = data.get("max_length")

        if min_length is not None:
            try:
                min_int = int(min_length)
                if min_int < 1:
                    return "Minimum password length must be at least 1"
            except (ValueError, TypeError):
                return "Minimum password length must be a valid number"

        if max_length is not None:
            try:
                max_int = int(max_length)
                if max_int < 1:
                    return "Maximum password length must be at least 1"
            except (ValueError, TypeError):
                return "Maximum password length must be a valid number"

        # Validate required char classes (1-4)
        char_classes = data.get("required_char_classes")
        if char_classes is not None:
            try:
                cc_int = int(char_classes)
                if cc_int < 1 or cc_int > 4:
                    return "Required character classes must be between 1 and 4"
            except (ValueError, TypeError):
                return "Required character classes must be a valid number"

    # Old sections kept for backwards compatibility during transition
    elif section == "indexing":
        batch_size = data.get("batch_size")
        if batch_size is not None:
            try:
                batch_int = int(batch_size)
                if batch_int < 1:
                    return "Batch size must be a positive number"
            except (ValueError, TypeError):
                return "Batch size must be a valid number"

    elif section == "query":
        for field in ["default_limit", "max_limit", "timeout"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        min_score = data.get("min_score")
        if min_score is not None:
            try:
                score_float = float(min_score)
                if score_float < 0 or score_float > 1:
                    return "Min score must be between 0 and 1"
            except (ValueError, TypeError):
                return "Min score must be a valid number"

    elif section == "security":
        for field in ["session_timeout", "token_expiration"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 60:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be at least 60 seconds"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "job_queue":
        # Validate max_total_concurrent_jobs (1-50)
        max_total = data.get("max_total_concurrent_jobs")
        if max_total is not None:
            try:
                max_total_int = int(max_total)
                if max_total_int < 1 or max_total_int > 50:
                    return "Max Concurrent Jobs (System-wide) must be between 1 and 50"
            except (ValueError, TypeError):
                return "Max Concurrent Jobs (System-wide) must be a valid number"

        # Validate max_concurrent_jobs_per_user (1-10)
        max_per_user = data.get("max_concurrent_jobs_per_user")
        if max_per_user is not None:
            try:
                max_per_user_int = int(max_per_user)
                if max_per_user_int < 1 or max_per_user_int > 10:
                    return "Max Concurrent Jobs (Per User) must be between 1 and 10"
            except (ValueError, TypeError):
                return "Max Concurrent Jobs (Per User) must be a valid number"

        # Validate average_job_duration_minutes (1-120)
        avg_duration = data.get("average_job_duration_minutes")
        if avg_duration is not None:
            try:
                avg_duration_int = int(avg_duration)
                if avg_duration_int < 1 or avg_duration_int > 120:
                    return "Average Job Duration must be between 1 and 120 minutes"
            except (ValueError, TypeError):
                return "Average Job Duration must be a valid number"

    elif section == "telemetry":
        # Validate trace_sample_rate (0.0 to 1.0)
        trace_sample_rate = data.get("trace_sample_rate")
        if trace_sample_rate is not None:
            try:
                rate_float = float(trace_sample_rate)
                if rate_float < 0 or rate_float > 1:
                    return "Trace sample rate must be between 0.0 and 1.0"
            except (ValueError, TypeError):
                return "Trace sample rate must be a valid number"

        # Validate collector_protocol
        collector_protocol = data.get("collector_protocol")
        if collector_protocol is not None:
            if collector_protocol.lower() not in ["grpc", "http"]:
                return "Collector protocol must be 'grpc' or 'http'"

        # Validate machine_metrics_interval_seconds
        interval = data.get("machine_metrics_interval_seconds")
        if interval is not None:
            try:
                interval_int = int(interval)
                if interval_int < 1:
                    return "Machine metrics interval must be at least 1 second"
            except (ValueError, TypeError):
                return "Machine metrics interval must be a valid number"

    elif section == "langfuse":
        # Validate Langfuse host URL format
        host = data.get("host")
        if host is not None:
            host_str = str(host).strip()
            if host_str and not (
                host_str.startswith("http://") or host_str.startswith("https://")
            ):
                return "Langfuse host must start with http:// or https://"

    elif section == "search_limits":
        # Validate max_result_size_mb (1-100 MB)
        max_size = data.get("max_result_size_mb")
        if max_size is not None:
            try:
                max_size_int = int(max_size)
                if max_size_int < 1 or max_size_int > 100:
                    return "Max Result Size must be between 1 and 100 MB"
            except (ValueError, TypeError):
                return "Max Result Size must be a valid number"

        # Validate timeout_seconds (5-300 seconds)
        timeout = data.get("timeout_seconds")
        if timeout is not None:
            try:
                timeout_int = int(timeout)
                if timeout_int < 5 or timeout_int > 300:
                    return "Timeout must be between 5 and 300 seconds"
            except (ValueError, TypeError):
                return "Timeout must be a valid number"

    elif section == "file_content_limits":
        # Validate max_tokens_per_request (1000-50000 tokens)
        max_tokens = data.get("max_tokens_per_request")
        if max_tokens is not None:
            try:
                max_tokens_int = int(max_tokens)
                if max_tokens_int < 1000 or max_tokens_int > 50000:
                    return "Max Tokens per Request must be between 1000 and 50000"
            except (ValueError, TypeError):
                return "Max Tokens per Request must be a valid number"

        # Validate chars_per_token (1-10 characters)
        chars_per_token = data.get("chars_per_token")
        if chars_per_token is not None:
            try:
                chars_int = int(chars_per_token)
                if chars_int < 1 or chars_int > 10:
                    return "Characters per Token must be between 1 and 10"
            except (ValueError, TypeError):
                return "Characters per Token must be a valid number"

    elif section == "golden_repos":
        # Validate refresh_interval_seconds (minimum 60 seconds)
        refresh_interval = data.get("refresh_interval_seconds")
        if refresh_interval is not None:
            try:
                interval_int = int(refresh_interval)
                if interval_int < 60:
                    return "Refresh Interval must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Refresh Interval must be a valid number"

        # Validate analysis_model (must be opus or sonnet)
        analysis_model = data.get("analysis_model")
        if analysis_model is not None:
            if analysis_model not in ("opus", "sonnet"):
                return "Analysis Model must be 'opus' or 'sonnet'"

    elif section == "mcp_session":
        # Story #3 Phase 2 AC2-AC3: MCP Session configuration
        session_ttl = data.get("session_ttl_seconds")
        if session_ttl is not None:
            try:
                ttl_int = int(session_ttl)
                if ttl_int < 60:
                    return "Session TTL must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Session TTL must be a valid number"

        cleanup_interval = data.get("cleanup_interval_seconds")
        if cleanup_interval is not None:
            try:
                interval_int = int(cleanup_interval)
                if interval_int < 60:
                    return "Cleanup Interval must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Cleanup Interval must be a valid number"

    elif section == "health":
        # Story #3 Phase 2 AC4-AC8: Health monitoring configuration
        for field in [
            "memory_warning_threshold_percent",
            "memory_critical_threshold_percent",
            "disk_warning_threshold_percent",
            "disk_critical_threshold_percent",
            "cpu_sustained_threshold_percent",
        ]:
            value = data.get(field)
            if value is not None:
                try:
                    val_float = float(value)
                    if val_float < 0 or val_float > 100:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be between 0 and 100"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # AC37: System Metrics Cache TTL
        from ..services.constants import (
            MIN_SYSTEM_METRICS_CACHE_TTL_SECONDS,
            MAX_SYSTEM_METRICS_CACHE_TTL_SECONDS,
        )

        cache_ttl = data.get("metrics_cache_ttl_seconds")
        if cache_ttl is not None:
            try:
                ttl_float = float(cache_ttl)
                if (
                    ttl_float < MIN_SYSTEM_METRICS_CACHE_TTL_SECONDS
                    or ttl_float > MAX_SYSTEM_METRICS_CACHE_TTL_SECONDS
                ):
                    return f"System Metrics Cache TTL must be between {MIN_SYSTEM_METRICS_CACHE_TTL_SECONDS} and {MAX_SYSTEM_METRICS_CACHE_TTL_SECONDS} seconds"
            except (ValueError, TypeError):
                return "System Metrics Cache TTL must be a valid number"

    elif section == "scip":
        # Story #3 Phase 2 AC9-AC11: SCIP configuration
        # Story #3 Phase 2 AC31-AC34: SCIP query limits
        from ..services.constants import (
            MIN_SCIP_REFERENCE_LIMIT,
            MAX_SCIP_REFERENCE_LIMIT,
            MIN_SCIP_DEPENDENCY_DEPTH,
            MAX_SCIP_DEPENDENCY_DEPTH,
            MIN_SCIP_CALLCHAIN_MAX_DEPTH,
            MAX_SCIP_CALLCHAIN_MAX_DEPTH,
            MIN_SCIP_CALLCHAIN_LIMIT,
            MAX_SCIP_CALLCHAIN_LIMIT,
        )

        indexing_timeout = data.get("indexing_timeout_seconds")
        if indexing_timeout is not None:
            try:
                timeout_int = int(indexing_timeout)
                if timeout_int < 60:
                    return "Indexing Timeout must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Indexing Timeout must be a valid number"

        scip_timeout = data.get("scip_generation_timeout_seconds")
        if scip_timeout is not None:
            try:
                timeout_int = int(scip_timeout)
                if timeout_int < 60:
                    return "SCIP Generation Timeout must be at least 60 seconds"
            except (ValueError, TypeError):
                return "SCIP Generation Timeout must be a valid number"

        stale_threshold = data.get("temporal_stale_threshold_days")
        if stale_threshold is not None:
            try:
                days_int = int(stale_threshold)
                if days_int < 1:
                    return "Temporal Stale Threshold must be at least 1 day"
            except (ValueError, TypeError):
                return "Temporal Stale Threshold must be a valid number"

        # P3 settings (AC31-AC34)
        scip_ref_limit = data.get("scip_reference_limit")
        if scip_ref_limit is not None:
            try:
                limit_int = int(scip_ref_limit)
                if (
                    limit_int < MIN_SCIP_REFERENCE_LIMIT
                    or limit_int > MAX_SCIP_REFERENCE_LIMIT
                ):
                    return f"SCIP Reference Limit must be between {MIN_SCIP_REFERENCE_LIMIT} and {MAX_SCIP_REFERENCE_LIMIT}"
            except (ValueError, TypeError):
                return "SCIP Reference Limit must be a valid number"

        scip_dep_depth = data.get("scip_dependency_depth")
        if scip_dep_depth is not None:
            try:
                depth_int = int(scip_dep_depth)
                if (
                    depth_int < MIN_SCIP_DEPENDENCY_DEPTH
                    or depth_int > MAX_SCIP_DEPENDENCY_DEPTH
                ):
                    return f"SCIP Dependency Depth must be between {MIN_SCIP_DEPENDENCY_DEPTH} and {MAX_SCIP_DEPENDENCY_DEPTH}"
            except (ValueError, TypeError):
                return "SCIP Dependency Depth must be a valid number"

        scip_callchain_depth = data.get("scip_callchain_max_depth")
        if scip_callchain_depth is not None:
            try:
                depth_int = int(scip_callchain_depth)
                if (
                    depth_int < MIN_SCIP_CALLCHAIN_MAX_DEPTH
                    or depth_int > MAX_SCIP_CALLCHAIN_MAX_DEPTH
                ):
                    return f"SCIP Callchain Max Depth must be between {MIN_SCIP_CALLCHAIN_MAX_DEPTH} and {MAX_SCIP_CALLCHAIN_MAX_DEPTH}"
            except (ValueError, TypeError):
                return "SCIP Callchain Max Depth must be a valid number"

        scip_callchain_limit = data.get("scip_callchain_limit")
        if scip_callchain_limit is not None:
            try:
                limit_int = int(scip_callchain_limit)
                if (
                    limit_int < MIN_SCIP_CALLCHAIN_LIMIT
                    or limit_int > MAX_SCIP_CALLCHAIN_LIMIT
                ):
                    return f"SCIP Callchain Limit must be between {MIN_SCIP_CALLCHAIN_LIMIT} and {MAX_SCIP_CALLCHAIN_LIMIT}"
            except (ValueError, TypeError):
                return "SCIP Callchain Limit must be a valid number"

    elif section == "git_timeouts":
        # Story #3 Phase 2 AC12-AC14: Git timeouts configuration
        # Story #3 Phase 2 AC27-AC28: API provider timeouts
        from ..services.constants import (
            MIN_GIT_LOCAL_TIMEOUT_SECONDS,
            MIN_GIT_REMOTE_TIMEOUT_SECONDS,
            MIN_GITHUB_API_TIMEOUT_SECONDS,
            MAX_GITHUB_API_TIMEOUT_SECONDS,
            MIN_GITLAB_API_TIMEOUT_SECONDS,
            MAX_GITLAB_API_TIMEOUT_SECONDS,
        )

        git_local = data.get("git_local_timeout")
        if git_local is not None:
            try:
                timeout_int = int(git_local)
                if timeout_int < MIN_GIT_LOCAL_TIMEOUT_SECONDS:
                    return f"Git Local Timeout must be at least {MIN_GIT_LOCAL_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Git Local Timeout must be a valid number"

        git_remote = data.get("git_remote_timeout")
        if git_remote is not None:
            try:
                timeout_int = int(git_remote)
                if timeout_int < MIN_GIT_REMOTE_TIMEOUT_SECONDS:
                    return f"Git Remote Timeout must be at least {MIN_GIT_REMOTE_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Git Remote Timeout must be a valid number"

        # P3 settings (AC27-AC28)
        github_api = data.get("github_api_timeout")
        if github_api is not None:
            try:
                timeout_int = int(github_api)
                if (
                    timeout_int < MIN_GITHUB_API_TIMEOUT_SECONDS
                    or timeout_int > MAX_GITHUB_API_TIMEOUT_SECONDS
                ):
                    return f"GitHub API Timeout must be between {MIN_GITHUB_API_TIMEOUT_SECONDS} and {MAX_GITHUB_API_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "GitHub API Timeout must be a valid number"

        gitlab_api = data.get("gitlab_api_timeout")
        if gitlab_api is not None:
            try:
                timeout_int = int(gitlab_api)
                if (
                    timeout_int < MIN_GITLAB_API_TIMEOUT_SECONDS
                    or timeout_int > MAX_GITLAB_API_TIMEOUT_SECONDS
                ):
                    return f"GitLab API Timeout must be between {MIN_GITLAB_API_TIMEOUT_SECONDS} and {MAX_GITLAB_API_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "GitLab API Timeout must be a valid number"

    elif section == "error_handling":
        # Story #3 Phase 2 AC16-AC18: Error handling configuration
        from ..services.constants import (
            MIN_RETRY_ATTEMPTS,
            MAX_RETRY_ATTEMPTS,
            MIN_BASE_RETRY_DELAY_SECONDS,
            MAX_BASE_RETRY_DELAY_SECONDS,
            MIN_MAX_RETRY_DELAY_SECONDS,
            MAX_MAX_RETRY_DELAY_SECONDS,
        )

        max_retry = data.get("max_retry_attempts")
        if max_retry is not None:
            try:
                retry_int = int(max_retry)
                if retry_int < MIN_RETRY_ATTEMPTS or retry_int > MAX_RETRY_ATTEMPTS:
                    return f"Max Retry Attempts must be between {MIN_RETRY_ATTEMPTS} and {MAX_RETRY_ATTEMPTS}"
            except (ValueError, TypeError):
                return "Max Retry Attempts must be a valid number"

        base_delay = data.get("base_retry_delay_seconds")
        if base_delay is not None:
            try:
                delay_float = float(base_delay)
                if (
                    delay_float < MIN_BASE_RETRY_DELAY_SECONDS
                    or delay_float > MAX_BASE_RETRY_DELAY_SECONDS
                ):
                    return f"Base Retry Delay must be between {MIN_BASE_RETRY_DELAY_SECONDS} and {MAX_BASE_RETRY_DELAY_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Base Retry Delay must be a valid number"

        max_delay = data.get("max_retry_delay_seconds")
        if max_delay is not None:
            try:
                delay_float = float(max_delay)
                if (
                    delay_float < MIN_MAX_RETRY_DELAY_SECONDS
                    or delay_float > MAX_MAX_RETRY_DELAY_SECONDS
                ):
                    return f"Max Retry Delay must be between {MIN_MAX_RETRY_DELAY_SECONDS} and {MAX_MAX_RETRY_DELAY_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Max Retry Delay must be a valid number"

    elif section == "api_limits":
        # Story #3 Phase 2 AC19-AC24: API limits configuration
        # Story #3 Phase 2 AC35, AC38-AC39: Audit/log limits
        from ..services.constants import (
            MIN_DEFAULT_FILE_READ_LINES,
            MAX_DEFAULT_FILE_READ_LINES,
            MIN_MAX_FILE_READ_LINES,
            MAX_MAX_FILE_READ_LINES,
            MIN_DEFAULT_DIFF_LINES,
            MAX_DEFAULT_DIFF_LINES,
            MIN_MAX_DIFF_LINES,
            MAX_MAX_DIFF_LINES,
            MIN_DEFAULT_LOG_COMMITS,
            MAX_DEFAULT_LOG_COMMITS,
            MIN_MAX_LOG_COMMITS,
            MAX_MAX_LOG_COMMITS,
            MIN_AUDIT_LOG_DEFAULT_LIMIT,
            MAX_AUDIT_LOG_DEFAULT_LIMIT,
            MIN_LOG_PAGE_SIZE_DEFAULT,
            MAX_LOG_PAGE_SIZE_DEFAULT,
            MIN_LOG_PAGE_SIZE_MAX,
            MAX_LOG_PAGE_SIZE_MAX,
        )

        default_file_lines = data.get("default_file_read_lines")
        if default_file_lines is not None:
            try:
                lines_int = int(default_file_lines)
                if (
                    lines_int < MIN_DEFAULT_FILE_READ_LINES
                    or lines_int > MAX_DEFAULT_FILE_READ_LINES
                ):
                    return f"Default File Read Lines must be between {MIN_DEFAULT_FILE_READ_LINES} and {MAX_DEFAULT_FILE_READ_LINES}"
            except (ValueError, TypeError):
                return "Default File Read Lines must be a valid number"

        max_file_lines = data.get("max_file_read_lines")
        if max_file_lines is not None:
            try:
                lines_int = int(max_file_lines)
                if (
                    lines_int < MIN_MAX_FILE_READ_LINES
                    or lines_int > MAX_MAX_FILE_READ_LINES
                ):
                    return f"Max File Read Lines must be between {MIN_MAX_FILE_READ_LINES} and {MAX_MAX_FILE_READ_LINES}"
            except (ValueError, TypeError):
                return "Max File Read Lines must be a valid number"

        default_diff_lines = data.get("default_diff_lines")
        if default_diff_lines is not None:
            try:
                lines_int = int(default_diff_lines)
                if (
                    lines_int < MIN_DEFAULT_DIFF_LINES
                    or lines_int > MAX_DEFAULT_DIFF_LINES
                ):
                    return f"Default Diff Lines must be between {MIN_DEFAULT_DIFF_LINES} and {MAX_DEFAULT_DIFF_LINES}"
            except (ValueError, TypeError):
                return "Default Diff Lines must be a valid number"

        max_diff_lines = data.get("max_diff_lines")
        if max_diff_lines is not None:
            try:
                lines_int = int(max_diff_lines)
                if lines_int < MIN_MAX_DIFF_LINES or lines_int > MAX_MAX_DIFF_LINES:
                    return f"Max Diff Lines must be between {MIN_MAX_DIFF_LINES} and {MAX_MAX_DIFF_LINES}"
            except (ValueError, TypeError):
                return "Max Diff Lines must be a valid number"

        default_log = data.get("default_log_commits")
        if default_log is not None:
            try:
                commits_int = int(default_log)
                if (
                    commits_int < MIN_DEFAULT_LOG_COMMITS
                    or commits_int > MAX_DEFAULT_LOG_COMMITS
                ):
                    return f"Default Log Commits must be between {MIN_DEFAULT_LOG_COMMITS} and {MAX_DEFAULT_LOG_COMMITS}"
            except (ValueError, TypeError):
                return "Default Log Commits must be a valid number"

        max_log = data.get("max_log_commits")
        if max_log is not None:
            try:
                commits_int = int(max_log)
                if (
                    commits_int < MIN_MAX_LOG_COMMITS
                    or commits_int > MAX_MAX_LOG_COMMITS
                ):
                    return f"Max Log Commits must be between {MIN_MAX_LOG_COMMITS} and {MAX_MAX_LOG_COMMITS}"
            except (ValueError, TypeError):
                return "Max Log Commits must be a valid number"

        # P3 settings (AC35, AC38-AC39)
        audit_limit = data.get("audit_log_default_limit")
        if audit_limit is not None:
            try:
                limit_int = int(audit_limit)
                if (
                    limit_int < MIN_AUDIT_LOG_DEFAULT_LIMIT
                    or limit_int > MAX_AUDIT_LOG_DEFAULT_LIMIT
                ):
                    return f"Audit Log Default Limit must be between {MIN_AUDIT_LOG_DEFAULT_LIMIT} and {MAX_AUDIT_LOG_DEFAULT_LIMIT}"
            except (ValueError, TypeError):
                return "Audit Log Default Limit must be a valid number"

        log_page_default = data.get("log_page_size_default")
        if log_page_default is not None:
            try:
                size_int = int(log_page_default)
                if (
                    size_int < MIN_LOG_PAGE_SIZE_DEFAULT
                    or size_int > MAX_LOG_PAGE_SIZE_DEFAULT
                ):
                    return f"Log Page Size Default must be between {MIN_LOG_PAGE_SIZE_DEFAULT} and {MAX_LOG_PAGE_SIZE_DEFAULT}"
            except (ValueError, TypeError):
                return "Log Page Size Default must be a valid number"

        log_page_max = data.get("log_page_size_max")
        if log_page_max is not None:
            try:
                size_int = int(log_page_max)
                if size_int < MIN_LOG_PAGE_SIZE_MAX or size_int > MAX_LOG_PAGE_SIZE_MAX:
                    return f"Log Page Size Max must be between {MIN_LOG_PAGE_SIZE_MAX} and {MAX_LOG_PAGE_SIZE_MAX}"
            except (ValueError, TypeError):
                return "Log Page Size Max must be a valid number"

    elif section == "web_security":
        # Story #3 Phase 2 AC25-AC26: Web security configuration
        from ..services.constants import (
            MIN_CSRF_MAX_AGE_SECONDS,
            MAX_CSRF_MAX_AGE_SECONDS,
            MIN_WEB_SESSION_TIMEOUT_SECONDS,
            MAX_WEB_SESSION_TIMEOUT_SECONDS,
        )

        csrf_max_age = data.get("csrf_max_age_seconds")
        if csrf_max_age is not None:
            try:
                age_int = int(csrf_max_age)
                if (
                    age_int < MIN_CSRF_MAX_AGE_SECONDS
                    or age_int > MAX_CSRF_MAX_AGE_SECONDS
                ):
                    return f"CSRF Max Age must be between {MIN_CSRF_MAX_AGE_SECONDS} and {MAX_CSRF_MAX_AGE_SECONDS} seconds"
            except (ValueError, TypeError):
                return "CSRF Max Age must be a valid number"

        session_timeout = data.get("web_session_timeout_seconds")
        if session_timeout is not None:
            try:
                timeout_int = int(session_timeout)
                if (
                    timeout_int < MIN_WEB_SESSION_TIMEOUT_SECONDS
                    or timeout_int > MAX_WEB_SESSION_TIMEOUT_SECONDS
                ):
                    return f"Web Session Timeout must be between {MIN_WEB_SESSION_TIMEOUT_SECONDS} and {MAX_WEB_SESSION_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Web Session Timeout must be a valid number"

    elif section == "auth":
        # Story #3 Phase 2 AC36: Authentication configuration
        from ..services.constants import (
            MIN_OAUTH_EXTENSION_THRESHOLD_HOURS,
            MAX_OAUTH_EXTENSION_THRESHOLD_HOURS,
        )

        oauth_threshold = data.get("oauth_extension_threshold_hours")
        if oauth_threshold is not None:
            try:
                threshold_int = int(oauth_threshold)
                if (
                    threshold_int < MIN_OAUTH_EXTENSION_THRESHOLD_HOURS
                    or threshold_int > MAX_OAUTH_EXTENSION_THRESHOLD_HOURS
                ):
                    return f"OAuth Extension Threshold must be between {MIN_OAUTH_EXTENSION_THRESHOLD_HOURS} and {MAX_OAUTH_EXTENSION_THRESHOLD_HOURS} hours"
            except (ValueError, TypeError):
                return "OAuth Extension Threshold must be a valid number"

    elif section == "multi_search":
        # Story #25: Multi-search limits configuration
        # Validate worker counts (1-50 range)
        for field in ["multi_search_max_workers", "scip_multi_max_workers"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1 or val_int > 50:
                        return f"{field} must be between 1 and 50"
                except (ValueError, TypeError):
                    return f"{field} must be a valid number"

        # Validate timeout values (5-600 range)
        for field in ["multi_search_timeout_seconds", "scip_multi_timeout_seconds"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 5 or val_int > 600:
                        return f"{field} must be between 5 and 600 seconds"
                except (ValueError, TypeError):
                    return f"{field} must be a valid number"

    elif section == "background_jobs":
        # Story #26: Background jobs configuration
        max_concurrent = data.get("max_concurrent_background_jobs")
        if max_concurrent is not None:
            try:
                val_int = int(max_concurrent)
                if val_int < 1 or val_int > 100:
                    return "Max Concurrent Background Jobs must be between 1 and 100"
            except (ValueError, TypeError):
                return "Max Concurrent Background Jobs must be a valid number"

        # Story #27: Subprocess max workers configuration
        subprocess_workers = data.get("subprocess_max_workers")
        if subprocess_workers is not None:
            try:
                val_int = int(subprocess_workers)
                if val_int < 1 or val_int > 50:
                    return "Subprocess Max Workers must be between 1 and 50"
            except (ValueError, TypeError):
                return "Subprocess Max Workers must be a valid number"

    elif section == "omni_search":
        # Story #28: Omni-search configuration
        max_workers = data.get("max_workers")
        if max_workers is not None:
            try:
                val_int = int(max_workers)
                if val_int < 1 or val_int > 100:
                    return "Max Workers must be between 1 and 100"
            except (ValueError, TypeError):
                return "Max Workers must be a valid number"

        per_repo_timeout = data.get("per_repo_timeout_seconds")
        if per_repo_timeout is not None:
            try:
                val_int = int(per_repo_timeout)
                if val_int < 1 or val_int > 3600:
                    return "Per Repo Timeout must be between 1 and 3600 seconds"
            except (ValueError, TypeError):
                return "Per Repo Timeout must be a valid number"

        cache_max_entries = data.get("cache_max_entries")
        if cache_max_entries is not None:
            try:
                val_int = int(cache_max_entries)
                if val_int < 1 or val_int > 10000:
                    return "Cache Max Entries must be between 1 and 10000"
            except (ValueError, TypeError):
                return "Cache Max Entries must be a valid number"

        cache_ttl = data.get("cache_ttl_seconds")
        if cache_ttl is not None:
            try:
                val_int = int(cache_ttl)
                if val_int < 1 or val_int > 86400:
                    return "Cache TTL must be between 1 and 86400 seconds"
            except (ValueError, TypeError):
                return "Cache TTL must be a valid number"

        default_limit = data.get("default_limit")
        if default_limit is not None:
            try:
                val_int = int(default_limit)
                if val_int < 1 or val_int > 1000:
                    return "Default Limit must be between 1 and 1000"
            except (ValueError, TypeError):
                return "Default Limit must be a valid number"

        max_limit = data.get("max_limit")
        if max_limit is not None:
            try:
                val_int = int(max_limit)
                if val_int < 1 or val_int > 10000:
                    return "Max Limit must be between 1 and 10000"
            except (ValueError, TypeError):
                return "Max Limit must be a valid number"

        aggregation_mode = data.get("default_aggregation_mode")
        if aggregation_mode is not None:
            if aggregation_mode not in ("global", "per_repo"):
                return "Default Aggregation Mode must be 'global' or 'per_repo'"

        max_results_per_repo = data.get("max_results_per_repo")
        if max_results_per_repo is not None:
            try:
                val_int = int(max_results_per_repo)
                if val_int < 1 or val_int > 10000:
                    return "Max Results Per Repo must be between 1 and 10000"
            except (ValueError, TypeError):
                return "Max Results Per Repo must be a valid number"

        max_total_results = data.get("max_total_results_before_aggregation")
        if max_total_results is not None:
            try:
                val_int = int(max_total_results)
                if val_int < 1 or val_int > 100000:
                    return "Max Total Results Before Aggregation must be between 1 and 100000"
            except (ValueError, TypeError):
                return "Max Total Results Before Aggregation must be a valid number"

        # pattern_metacharacters - no validation (string)

    elif section == "content_limits":
        # Story #32: Unified content limits configuration
        chars_per_token = data.get("chars_per_token")
        if chars_per_token is not None:
            try:
                val_int = int(chars_per_token)
                if val_int < 1 or val_int > 10:
                    return "Chars Per Token must be between 1 and 10"
            except (ValueError, TypeError):
                return "Chars Per Token must be a valid number"

        file_content_max_tokens = data.get("file_content_max_tokens")
        if file_content_max_tokens is not None:
            try:
                val_int = int(file_content_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "File Content Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "File Content Max Tokens must be a valid number"

        git_diff_max_tokens = data.get("git_diff_max_tokens")
        if git_diff_max_tokens is not None:
            try:
                val_int = int(git_diff_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Git Diff Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Git Diff Max Tokens must be a valid number"

        git_log_max_tokens = data.get("git_log_max_tokens")
        if git_log_max_tokens is not None:
            try:
                val_int = int(git_log_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Git Log Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Git Log Max Tokens must be a valid number"

        search_result_max_tokens = data.get("search_result_max_tokens")
        if search_result_max_tokens is not None:
            try:
                val_int = int(search_result_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Search Result Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Search Result Max Tokens must be a valid number"

        cache_ttl_seconds = data.get("cache_ttl_seconds")
        if cache_ttl_seconds is not None:
            try:
                val_int = int(cache_ttl_seconds)
                if val_int < 60:
                    return "Cache TTL must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Cache TTL must be a valid number"

        cache_max_entries = data.get("cache_max_entries")
        if cache_max_entries is not None:
            try:
                val_int = int(cache_max_entries)
                if val_int < 100 or val_int > 100000:
                    return "Cache Max Entries must be between 100 and 100000"
            except (ValueError, TypeError):
                return "Cache Max Entries must be a valid number"

    return None


def _create_config_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
    validation_errors: Optional[dict] = None,
) -> HTMLResponse:
    """Create config page response with all necessary context."""
    csrf_token = generate_csrf_token()
    config = _get_current_config()

    # Load API keys status
    token_manager = _get_token_manager()
    api_keys_status = token_manager.list_tokens()

    # Get token data for masking in template
    github_token_data = token_manager.get_token("github")
    gitlab_token_data = token_manager.get_token("gitlab")

    response = templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "config",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": config,
            "success_message": success_message,
            "error_message": error_message,
            "validation_errors": validation_errors or {},
            "api_keys_status": api_keys_status,
            "github_token_data": github_token_data,
            "gitlab_token_data": gitlab_token_data,
            "restart_required_fields": RESTART_REQUIRED_FIELDS,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


# =============================================================================
# Auto-Discovery Routes
# =============================================================================


def _get_gitlab_provider():
    """Create GitLab provider with required dependencies."""
    from ..services.repository_providers.gitlab_provider import GitLabProvider

    token_manager = _get_token_manager()
    golden_repo_manager = _get_golden_repo_manager()

    return GitLabProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _get_github_provider():
    """Create GitHub provider with required dependencies."""
    from ..services.repository_providers.github_provider import GitHubProvider

    token_manager = _get_token_manager()
    golden_repo_manager = _get_golden_repo_manager()

    return GitHubProvider(
        token_manager=token_manager, golden_repo_manager=golden_repo_manager
    )


def _build_gitlab_repos_response(
    request: Request,
    repositories: Optional[list] = None,
    total_count: int = 0,
    page: int = 1,
    page_size: int = 50,
    total_pages: int = 0,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    search_term: Optional[str] = None,
):
    """Build GitLab repos partial template response."""
    # Get existing CSRF token from cookie or generate new one
    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

    response = templates.TemplateResponse(
        "partials/gitlab_repos.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repositories": repositories or [],
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "error_type": error_type,
            "error_message": error_message,
            "search_term": search_term or "",
        },
    )

    # Set CSRF cookie to ensure token is available for form submission
    set_csrf_cookie(response, csrf_token)
    return response


def _build_github_repos_response(
    request: Request,
    repositories: Optional[list] = None,
    total_count: int = 0,
    page: int = 1,
    page_size: int = 50,
    total_pages: int = 0,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    search_term: Optional[str] = None,
):
    """Build GitHub repos partial template response."""
    # Get existing CSRF token from cookie or generate new one
    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()

    response = templates.TemplateResponse(
        "partials/github_repos.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "repositories": repositories or [],
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "error_type": error_type,
            "error_message": error_message,
            "search_term": search_term or "",
        },
    )

    # Set CSRF cookie to ensure token is available for form submission
    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/auto-discovery", response_class=HTMLResponse)
def auto_discovery_page(request: Request):
    """Auto-discovery page - discover repositories from GitLab/GitHub."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    response = templates.TemplateResponse(
        "auto_discovery.html",
        {
            "request": request,
            "current_page": "auto-discovery",
            "show_nav": True,
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/partials/auto-discovery/gitlab", response_class=HTMLResponse)
def gitlab_repos_partial(
    request: Request, page: int = 1, page_size: int = 50, search: Optional[str] = None
):
    """HTMX partial for GitLab repository discovery."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    from ..services.repository_providers.gitlab_provider import GitLabProviderError

    # Normalize search: treat empty string as None
    search_term = search.strip() if search else None

    try:
        provider = _get_gitlab_provider()
        if not provider.is_configured():
            return _build_gitlab_repos_response(
                request,
                error_type="not_configured",
                error_message="GitLab token not configured",
                search_term=search_term,
            )

        result = provider.discover_repositories(
            page=page, page_size=page_size, search=search_term
        )
        return _build_gitlab_repos_response(
            request,
            result.repositories,
            result.total_count,
            result.page,
            result.page_size,
            result.total_pages,
            search_term=search_term,
        )
    except GitLabProviderError as e:
        return _build_gitlab_repos_response(
            request,
            page=page,
            page_size=page_size,
            error_type="api_error",
            error_message=str(e),
            search_term=search_term,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-042",
                f"Unexpected error in GitLab discovery: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _build_gitlab_repos_response(
            request,
            page=page,
            page_size=page_size,
            error_type="api_error",
            error_message=f"Unexpected error: {e}",
            search_term=search_term,
        )


@web_router.get("/partials/auto-discovery/github", response_class=HTMLResponse)
def github_repos_partial(
    request: Request, page: int = 1, page_size: int = 50, search: Optional[str] = None
):
    """HTMX partial for GitHub repository discovery."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    from ..services.repository_providers.github_provider import GitHubProviderError

    # Normalize search: treat empty string as None
    search_term = search.strip() if search else None

    try:
        provider = _get_github_provider()
        if not provider.is_configured():
            return _build_github_repos_response(
                request,
                error_type="not_configured",
                error_message="GitHub token not configured",
                search_term=search_term,
            )

        result = provider.discover_repositories(
            page=page, page_size=page_size, search=search_term
        )
        return _build_github_repos_response(
            request,
            result.repositories,
            result.total_count,
            result.page,
            result.page_size,
            result.total_pages,
            search_term=search_term,
        )
    except GitHubProviderError as e:
        error_msg = str(e)
        error_type = "api_error"
        # Check for rate limit specific error
        if "rate limit" in error_msg.lower():
            error_type = "rate_limit"
        return _build_github_repos_response(
            request,
            page=page,
            page_size=page_size,
            error_type=error_type,
            error_message=error_msg,
            search_term=search_term,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-043",
                f"Unexpected error in GitHub discovery: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _build_github_repos_response(
            request,
            page=page,
            page_size=page_size,
            error_type="api_error",
            error_message=f"Unexpected error: {e}",
            search_term=search_term,
        )


# =============================================================================
# Discovery Branches API Endpoint (Story #21)
# =============================================================================


@web_router.post("/api/discovery/branches")
async def fetch_discovery_branches(request: Request):
    """
    Fetch branches for remote repositories during auto-discovery.

    This endpoint fetches available branches from remote git repositories
    and filters out issue-tracker pattern branches (e.g., SCM-1234, PROJ-567).

    Request body:
        {
            "repos": [
                {"clone_url": "https://github.com/org/repo.git", "platform": "github"},
                {"clone_url": "https://gitlab.com/org/repo.git", "platform": "gitlab"}
            ]
        }

    Response:
        {
            "https://github.com/org/repo.git": {
                "branches": ["main", "develop", "feature/login"],
                "default_branch": "main",
                "error": null
            },
            "https://gitlab.com/org/repo.git": {
                "branches": [],
                "default_branch": null,
                "error": "Repository not found"
            }
        }
    """
    # Require admin authentication
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            status_code=401,
            content={"error": "Authentication required"},
        )

    try:
        # Parse request body
        body = await request.json()
        repos = body.get("repos", [])

        if not isinstance(repos, list):
            return JSONResponse(
                status_code=422,
                content={"error": "repos must be a list"},
            )

        # Import and use RemoteBranchService
        from ..services.remote_branch_service import RemoteBranchService

        service = RemoteBranchService()

        # Get token manager to retrieve stored credentials
        token_manager = _get_token_manager()

        # Build requests and fetch branches
        results = {}
        for repo in repos:
            clone_url = repo.get("clone_url")
            platform = repo.get("platform", "github")

            if not clone_url:
                results[str(repo)] = {
                    "branches": [],
                    "default_branch": None,
                    "error": "Missing clone_url",
                }
                continue

            # Retrieve credentials based on platform
            credentials = None
            if platform == "gitlab":
                token_data = token_manager.get_token("gitlab")
                if token_data:
                    credentials = token_data.token
            elif platform == "github":
                token_data = token_manager.get_token("github")
                if token_data:
                    credentials = token_data.token

            # Fetch branches for this repo with credentials
            result = service.fetch_remote_branches(
                clone_url=clone_url,
                platform=platform,
                credentials=credentials,
            )

            results[clone_url] = {
                "branches": result.branches,
                "default_branch": result.default_branch,
                "error": result.error,
            }

        return JSONResponse(content=results)

    except json.JSONDecodeError:
        return JSONResponse(
            status_code=422,
            content={"error": "Invalid JSON in request body"},
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-044",
                f"Error fetching discovery branches: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal server error: {str(e)}"},
        )


@web_router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    """Configuration management page - view and edit CIDX configuration."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_config_page_response(request, session)


@web_router.post("/config/claude_delegation", response_class=HTMLResponse)
async def update_claude_delegation_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Update Claude Delegation configuration with connectivity validation (Story #721)."""
    from ..services.config_service import get_config_service
    from ..config.delegation_config import ClaudeDelegationConfig

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    form_data = await request.form()
    config_service = get_config_service()
    delegation_manager = config_service.get_delegation_manager()

    # Extract credential, preserving existing if empty
    credential = form_data.get("claude_server_credential", "")
    if not credential:
        existing = delegation_manager.load_config()
        credential = existing.claude_server_credential if existing else ""

    url = form_data.get("claude_server_url", "").strip()
    username = form_data.get("claude_server_username", "").strip()

    if not url or not username or not credential:
        return _create_config_page_response(
            request,
            session,
            error_message="URL, username, and credential are required",
            validation_errors={"claude_delegation": "Missing required fields"},
        )

    # Validate connectivity before saving
    cred_type = form_data.get("claude_server_credential_type", "password")
    result = delegation_manager.validate_connectivity(
        url, username, credential, cred_type
    )

    if not result.success:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Connection failed: {result.error_message}",
            validation_errors={"claude_delegation": result.error_message},
        )

    # Save configuration with encrypted credential
    from ..config.delegation_config import DEFAULT_FUNCTION_REPO_ALIAS

    cidx_callback_url = form_data.get("cidx_callback_url", "").strip()  # Story #720
    skip_ssl_verify = form_data.get("skip_ssl_verify", "false").lower() == "true"
    config = ClaudeDelegationConfig(
        function_repo_alias=form_data.get("function_repo_alias", "").strip()
        or DEFAULT_FUNCTION_REPO_ALIAS,
        claude_server_url=url,
        claude_server_username=username,
        claude_server_credential_type=cred_type,
        claude_server_credential=credential,
        cidx_callback_url=cidx_callback_url,
        skip_ssl_verify=skip_ssl_verify,
    )
    delegation_manager.save_config(config)

    return _create_config_page_response(
        request,
        session,
        success_message="Claude Delegation configuration saved and verified",
    )


# NOTE: This specific route MUST come BEFORE /config/{section} to avoid being
# caught by the parameterized route. FastAPI matches routes in order of definition.
@web_router.post("/config/reset", response_class=HTMLResponse)
def reset_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Reset configuration to defaults."""
    from ..services.config_service import get_config_service

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Reset to defaults using ConfigService
    try:
        config_service = get_config_service()
        # Create a fresh default config and save it
        default_config = config_service.config_manager.create_default_config()
        config_service.config_manager.save_config(default_config)
        config_service._config = default_config  # Update cached config

        return _create_config_page_response(
            request,
            session,
            success_message="Configuration reset to defaults successfully",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-045",
                "Failed to reset config: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to reset configuration: {str(e)}",
        )


# NOTE: This specific route MUST come BEFORE /config/{section} to avoid being
# caught by the parameterized route. FastAPI matches routes in order of definition.
@web_router.post("/config/langfuse_pull", response_class=HTMLResponse)
async def update_langfuse_pull_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Update Langfuse Trace Pull configuration (Story #164)."""
    from ..services.config_service import get_config_service

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    form_data = await request.form()
    config_service = get_config_service()

    try:
        # Update scalar settings
        config_service.update_setting(
            "langfuse", "pull_enabled", form_data.get("pull_enabled", "false")
        )
        config_service.update_setting(
            "langfuse",
            "pull_host",
            form_data.get("pull_host", "https://cloud.langfuse.com"),
        )
        config_service.update_setting(
            "langfuse",
            "pull_sync_interval_seconds",
            form_data.get("pull_sync_interval_seconds", "300"),
        )
        config_service.update_setting(
            "langfuse",
            "pull_trace_age_days",
            form_data.get("pull_trace_age_days", "30"),
        )
        config_service.update_setting(
            "langfuse",
            "pull_max_concurrent_observations",
            form_data.get("pull_max_concurrent_observations", "5"),
        )

        # Update projects from JSON
        projects_json = form_data.get("pull_projects", "[]")
        if projects_json:
            config_service.update_setting("langfuse", "pull_projects", projects_json)

        return _create_config_page_response(
            request,
            session,
            success_message="Langfuse Trace Pull configuration saved",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-046",
                "Failed to update Langfuse pull config: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
            validation_errors={"langfuse_pull": str(e)},
        )


@web_router.post("/config/{section}", response_class=HTMLResponse)
async def update_config_section(
    request: Request,
    section: str,
    csrf_token: Optional[str] = Form(None),
):
    """Update configuration for a specific section."""
    from ..services.config_service import get_config_service

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate section
    valid_sections = [
        "server",
        "cache",
        "timeouts",
        "password_security",
        "oidc",
        "job_queue",
        "telemetry",
        "langfuse",
        "search_limits",
        "file_content_limits",
        "golden_repos",
        "mcp_session",
        "health",
        "scip",
        # Story #3 - Phase 2: P2 settings (AC12-AC26)
        "git_timeouts",
        "error_handling",
        "api_limits",
        "web_security",
        # Story #3 - Phase 2: P3 settings (AC36)
        "auth",
        # Story #25 - Multi-search limits configuration
        "multi_search",
        # Story #26 - Background jobs configuration
        "background_jobs",
        # Story #28 - Omni-search configuration
        "omni_search",
        # Story #32 - Unified content limits configuration
        "content_limits",
        # Story #190 - Claude CLI configuration
        "claude_cli",
        # Story #223 - Indexing configuration
        "indexing",
    ]
    if section not in valid_sections:
        return _create_config_page_response(
            request, session, error_message=f"Invalid section: {section}"
        )

    # Get form data
    form_data = await request.form()
    data = {k: v for k, v in form_data.items() if k != "csrf_token"}

    # Validate configuration
    error = _validate_config_section(section, data)
    if error:
        return _create_config_page_response(
            request,
            session,
            error_message=error,
            validation_errors={section: error},
        )

    # Special handling for job_queue - these are currently read-only defaults from SyncJobConfig
    # Note: BackgroundJobManager doesn't have these attributes. The job queue settings are managed
    # via SyncJobConfig which returns hardcoded defaults. Dynamic updates would require
    # extending SyncJobConfig with persistence support.
    if section == "job_queue":
        logger.warning(
            format_error_log(
                "STORE-GENERAL-046",
                "Job queue configuration save attempted but settings are read-only defaults from SyncJobConfig.",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            success_message="Job Queue settings are read-only defaults (dynamic configuration not currently supported)",
        )

    # Save configuration using ConfigService
    try:
        config_service = get_config_service()

        # Update all settings without validating (batch update)
        for key, value in data.items():
            config_service.update_setting(section, key, value, skip_validation=True)

        # Validate configuration
        config = config_service.get_config()
        config_service.config_manager.validate_config(config)

        # For OIDC: test reload BEFORE saving to file
        if section == "oidc":
            try:
                # Try to reload with new config (don't save yet)
                await _reload_oidc_configuration()
                logger.info(
                    "OIDC configuration validated and reloaded successfully",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                # Reload failed - reload original config from file to restore working state
                logger.error(
                    format_error_log(
                        "STORE-GENERAL-047",
                        f"Failed to reload OIDC configuration: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                config_service.load_config()  # Reload from file to undo in-memory changes
                return _create_config_page_response(
                    request,
                    session,
                    error_message=f"Invalid OIDC configuration: {str(e)}. Changes not saved.",
                )

        # Only save to file after validation and OIDC test (if applicable)
        config_service.config_manager.save_config(config)
        logger.info(
            f"Saved {section} configuration with {len(data)} settings",
            extra={"correlation_id": get_correlation_id()},
        )

        # Story #223: Cascade indexing extensions to all golden repos after save
        if section == "indexing":
            try:
                config_service.cascade_indexable_extensions_to_repos()
            except Exception as e:
                logger.warning("Extension cascade failed: %s", e)

        return _create_config_page_response(
            request,
            session,
            success_message=f"{section.title()} configuration saved successfully",
        )
    except ValueError as e:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-048",
                "Failed to save config section %s: %s",
                section,
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save configuration: {str(e)}",
        )


@web_router.post("/config/reset", response_class=HTMLResponse)
def reset_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """Reset configuration to defaults."""
    from ..services.config_service import get_config_service

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Reset to defaults using ConfigService
    try:
        config_service = get_config_service()
        # Create a fresh default config and save it
        default_config = config_service.config_manager.create_default_config()
        config_service.config_manager.save_config(default_config)
        config_service._config = default_config  # Update cached config

        return _create_config_page_response(
            request,
            session,
            success_message="Configuration reset to defaults successfully",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-049",
                "Failed to reset config: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to reset configuration: {str(e)}",
        )


@web_router.get("/partials/config-section", response_class=HTMLResponse)
def config_section_partial(
    request: Request,
    section: Optional[str] = None,
):
    """
    Partial refresh endpoint for config section.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie instead of generating new one
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        # Fallback: generate new token if cookie missing/invalid
        csrf_token = generate_csrf_token()
    config = _get_current_config()

    # Load API keys status
    token_manager = _get_token_manager()
    api_keys_status = token_manager.list_tokens()
    github_token_data = token_manager.get_token("github")
    gitlab_token_data = token_manager.get_token("gitlab")

    response = templates.TemplateResponse(
        "partials/config_section.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "config": config,
            "validation_errors": {},
            "api_keys_status": api_keys_status,
            "github_token_data": github_token_data,
            "gitlab_token_data": gitlab_token_data,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


# =============================================================================
# API Keys Management
# =============================================================================


@web_router.post("/config/api-keys/{platform}", response_class=HTMLResponse)
def save_api_key(
    request: Request,
    platform: str,
    csrf_token: Optional[str] = Form(None),
    token: str = Form(...),
    api_url: Optional[str] = Form(None),
):
    """Save API key for CI/CD platform (GitHub or GitLab)."""
    # Require admin authentication
    session = _require_admin_session(request)
    if not session:
        return RedirectResponse(
            url="/user/login", status_code=status.HTTP_303_SEE_OTHER
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_config_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate platform
    if platform not in ["github", "gitlab"]:
        return _create_config_page_response(
            request, session, error_message=f"Invalid platform: {platform}"
        )

    # Save token using CITokenManager - use same server_dir as config service
    try:
        token_manager = _get_token_manager()
        # Strip whitespace from token before validation (Issue #716 Bug 2a)
        token = token.strip()
        token_manager.save_token(platform, token, base_url=api_url)

        platform_name = "GitHub" if platform == "github" else "GitLab"
        return _create_config_page_response(
            request,
            session,
            success_message=f"{platform_name} API key saved successfully",
        )
    except TokenValidationError as e:
        return _create_config_page_response(
            request,
            session,
            error_message=f"Invalid token format: {str(e)}",
            validation_errors={"api_keys": str(e)},
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "STORE-GENERAL-050",
                "Failed to save %s API key: %s",
                platform,
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_config_page_response(
            request,
            session,
            error_message=f"Failed to save API key: {str(e)}",
        )


@web_router.delete("/config/api-keys/{platform}", response_class=HTMLResponse)
def delete_api_key(
    request: Request,
    platform: str,
):
    """Delete API key for CI/CD platform."""
    # Require admin authentication
    session = _require_admin_session(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Validate CSRF token from header (HTMX sends it as X-CSRF-Token)
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not validate_login_csrf_token(request, csrf_from_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )

    # Validate platform
    if platform not in ["github", "gitlab"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid platform: {platform}",
        )

    # Delete token using CITokenManager - use same server_dir as config service
    try:
        token_manager = _get_token_manager()
        token_manager.delete_token(platform)

        platform_name = "GitHub" if platform == "github" else "GitLab"
        logger.info(
            f"{platform_name} API key deleted successfully",
            extra={"correlation_id": get_correlation_id()},
        )

        # Return success HTML fragment (HTMX expects HTML response)
        return HTMLResponse(
            content=f'<div class="alert success">{platform_name} API key deleted</div>',
            status_code=200,
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-015",
                "Failed to delete %s API key: %s",
                platform,
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete API key: {str(e)}",
        )


# =============================================================================
# Git Settings
# =============================================================================


@web_router.get("/settings/git", response_class=HTMLResponse)
def git_settings_page(request: Request):
    """
    Git settings page - view and edit git service configuration.

    Admin-only page for configuring git committer settings.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    from code_indexer.config import ConfigManager

    # Get current configuration
    config_manager = ConfigManager()
    config = config_manager.load()
    git_config = config.git_service

    # Generate CSRF token
    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "git_settings.html",
        {
            "username": session.username,
            "current_page": "git-settings",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": git_config,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


# =============================================================================
# File Content Limits Settings
# =============================================================================


@web_router.get("/settings/file-content-limits", response_class=HTMLResponse)
def file_content_limits_page(request: Request):
    """
    File content limits settings page - view and edit token limits configuration.

    Admin-only page for configuring file content token limits.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    from ..services.config_service import get_config_service

    # Get current configuration from ConfigService (Story #3 - Configuration Consolidation)
    config_service = get_config_service()
    config = config_service.get_config().file_content_limits_config

    # Generate CSRF token
    csrf_token = generate_csrf_token()

    # Calculate derived values
    max_chars = config.max_chars_per_request
    estimated_lines = max_chars // 80  # Typical code line length

    response = templates.TemplateResponse(
        "file_content_limits.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "file-content-limits",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": config,
            "max_chars": max_chars,
            "estimated_lines": estimated_lines,
            "success_message": None,
            "error_message": None,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.post("/settings/file-content-limits", response_class=HTMLResponse)
def update_file_content_limits(
    request: Request,
    max_tokens_per_request: int = Form(...),
    chars_per_token: int = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """
    Update file content limits configuration.

    Validates input and persists changes to database.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_file_content_limits_response(
            request, session, error_message="Invalid CSRF token"
        )

    # Validate max_tokens_per_request range (Story #3 AC-M3: 1000-50000)
    if max_tokens_per_request < 1000 or max_tokens_per_request > 50000:
        return _create_file_content_limits_response(
            request,
            session,
            error_message="Max tokens per request must be between 1000 and 50000",
        )

    # Validate chars_per_token range (Story #3 AC-M4: 1-10)
    if chars_per_token < 1 or chars_per_token > 10:
        return _create_file_content_limits_response(
            request,
            session,
            error_message="Chars per token must be between 1 and 10",
        )

    # Update configuration using ConfigService (Story #3 - Configuration Consolidation)
    try:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        # Update settings individually with validation
        config_service.update_setting(
            "file_content_limits", "max_tokens_per_request", max_tokens_per_request
        )
        config_service.update_setting(
            "file_content_limits", "chars_per_token", chars_per_token
        )

        return _create_file_content_limits_response(
            request,
            session,
            success_message="File content limits updated successfully",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-016",
                "Failed to update file content limits: %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_file_content_limits_response(
            request,
            session,
            error_message=f"Failed to update configuration: {str(e)}",
        )


def _create_file_content_limits_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create file content limits page response with messages."""
    from ..services.config_service import get_config_service

    # Get configuration from ConfigService (Story #3 - Configuration Consolidation)
    config_service = get_config_service()
    config = config_service.get_config().file_content_limits_config

    csrf_token = generate_csrf_token()

    # Calculate derived values
    max_chars = config.max_chars_per_request
    estimated_lines = max_chars // 80

    response = templates.TemplateResponse(
        "file_content_limits.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "file-content-limits",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": config,
            "max_chars": max_chars,
            "estimated_lines": estimated_lines,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


# =============================================================================
# API Keys Management
# =============================================================================


@web_router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request):
    """API Keys management page - manage personal API keys."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_api_keys_page_response(request, session)


def _create_api_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create API keys page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "api_keys.html",
        {
            "show_nav": True,
            "current_page": "api-keys",
            "username": username,
            "api_keys": keys,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@web_router.get("/partials/api-keys-list", response_class=HTMLResponse)
def api_keys_list_partial(request: Request):
    """Partial for API keys list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "partials/api_keys_list.html",
        {"api_keys": keys},
    )
    return response


@web_router.get("/mcp-credentials", response_class=HTMLResponse)
def admin_mcp_credentials_page(request: Request):
    """Admin MCP Credentials management page - manage personal MCP credentials."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_admin_mcp_credentials_page_response(request, session)


def _create_admin_mcp_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create admin MCP credentials page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()

    response = templates.TemplateResponse(
        request,
        "admin_mcp_credentials.html",
        {
            "show_nav": True,
            "current_page": "mcp-credentials",
            "username": username,
            "mcp_credentials": credentials,
            "system_credentials": system_credentials,
            "success_message": success_message,
            "error_message": error_message,
        },
    )
    return response


@web_router.get("/partials/mcp-credentials-list", response_class=HTMLResponse)
def admin_mcp_credentials_list_partial(request: Request):
    """Partial for admin MCP credentials list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()

    response = templates.TemplateResponse(
        request,
        "partials/mcp_credentials_list.html",
        {"mcp_credentials": credentials, "system_credentials": system_credentials},
    )
    return response


@web_router.get("/api/system-credentials", response_class=JSONResponse)
def admin_system_credentials_api(request: Request):
    """Return system-managed MCP credentials as JSON (Story #275).

    Credentials owned by the built-in admin user that were created automatically
    by the CIDX server (e.g. cidx-local-auto, cidx-server-auto).
    Requires an active admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Authentication required"},
            status_code=403,
        )

    assert dependencies.user_manager is not None  # Initialized at app startup
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()
    return JSONResponse(content={"system_credentials": system_credentials})


# =============================================================================
# User Self-Service Routes (Any Authenticated User)
# =============================================================================


def _require_authenticated_session(request: Request) -> Optional[SessionData]:
    """Check for valid authenticated session (any role), return None if not authenticated."""
    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return None

    return session


# Old /user/login routes removed - replaced by unified login at root level
# See login_router for unified login implementation


@user_router.get("/api-keys", response_class=HTMLResponse)
def user_api_keys_page(request: Request):
    """User API Keys management page - any authenticated user can manage their own API keys."""
    session = _require_authenticated_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_user_api_keys_page_response(request, session)


def _create_user_api_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create user API keys page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "user_api_keys.html",
        {
            "show_nav": True,
            "current_page": "api-keys",
            "username": username,
            "api_keys": keys,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@user_router.get("/partials/api-keys-list", response_class=HTMLResponse)
def user_api_keys_list_partial(request: Request):
    """Partial for user API keys list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_authenticated_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "partials/api_keys_list.html",
        {"api_keys": keys},
    )
    return response


@user_router.get("/mcp-credentials", response_class=HTMLResponse)
def user_mcp_credentials_page(request: Request):
    """User MCP Credentials management page - any authenticated user can manage their own MCP credentials."""
    session = _require_authenticated_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_user_mcp_credentials_page_response(request, session)


def _create_user_mcp_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create user MCP credentials page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)

    response = templates.TemplateResponse(
        request,
        "user_mcp_credentials.html",
        {
            "show_nav": True,
            "current_page": "mcp-credentials",
            "username": username,
            "mcp_credentials": credentials,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@user_router.get("/partials/mcp-credentials-list", response_class=HTMLResponse)
def user_mcp_credentials_list_partial(request: Request):
    """Partial for user MCP credentials list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_authenticated_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)

    response = templates.TemplateResponse(
        request,
        "partials/mcp_credentials_list.html",
        {"mcp_credentials": credentials},
    )
    return response


@user_router.get("/logout")
def user_logout(request: Request):
    """
    Logout and clear session for user portal.

    Redirects to unified login page after clearing session.
    """
    session_manager = get_session_manager()
    response = RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )

    # Clear the session cookie
    session_manager.clear_session(response)

    return response


# SSH Keys Management Page
@web_router.get("/ssh-keys", response_class=HTMLResponse)
def ssh_keys_page(request: Request):
    """SSH Keys management page - view migration status and manage SSH keys."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Generate fresh CSRF token
    csrf_token = generate_csrf_token()

    # Get migration result from app state (set during server startup)
    migration_result = getattr(request.app.state, "ssh_migration_result", None)

    # Get SSH keys list
    managed_keys = []
    unmanaged_keys = []
    try:
        manager = _get_ssh_key_manager()
        key_list = manager.list_keys()
        managed_keys = key_list.managed
        unmanaged_keys = key_list.unmanaged
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-017",
                f"Failed to list SSH keys: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )

    response = templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "show_nav": True,
            "current_page": "ssh-keys",
            "username": session.username,
            "migration_result": migration_result,
            "managed_keys": managed_keys,
            "unmanaged_keys": unmanaged_keys,
            "csrf_token": csrf_token,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


def _create_ssh_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Response:
    """Helper to create SSH keys page response with messages."""
    # Generate fresh CSRF token
    csrf_token = generate_csrf_token()

    migration_result = getattr(request.app.state, "ssh_migration_result", None)

    managed_keys = []
    unmanaged_keys = []
    try:
        manager = _get_ssh_key_manager()
        key_list = manager.list_keys()
        managed_keys = key_list.managed
        unmanaged_keys = key_list.unmanaged
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-018",
                f"Failed to list SSH keys: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )

    response = templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "show_nav": True,
            "current_page": "ssh-keys",
            "username": session.username,
            "migration_result": migration_result,
            "managed_keys": managed_keys,
            "unmanaged_keys": unmanaged_keys,
            "csrf_token": csrf_token,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


@web_router.post("/ssh-keys/create", response_class=HTMLResponse)
def create_ssh_key(
    request: Request,
    key_name: str = Form(...),
    key_type: str = Form(...),
    email: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Create a new SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        from ..services.ssh_key_generator import (
            InvalidKeyNameError,
            KeyAlreadyExistsError,
        )

        manager = _get_ssh_key_manager()
        manager.create_key(
            name=key_name,
            key_type=key_type,
            email=email if email else None,
            description=description if description else None,
        )

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"SSH key '{key_name}' created successfully. Public key is ready to copy.",
        )
    except InvalidKeyNameError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Invalid key name: {e}"
        )
    except KeyAlreadyExistsError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Key already exists: {e}"
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-019",
                f"Failed to create SSH key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to create key: {e}"
        )


@web_router.post("/ssh-keys/delete", response_class=HTMLResponse)
def delete_ssh_key(
    request: Request,
    key_name: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Delete an SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        manager = _get_ssh_key_manager()
        manager.delete_key(key_name)

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"SSH key '{key_name}' deleted successfully.",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-020",
                f"Failed to delete SSH key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to delete key: {e}"
        )


@web_router.post("/ssh-keys/assign-host", response_class=HTMLResponse)
def assign_host_to_key(
    request: Request,
    key_name: str = Form(...),
    hostname: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Assign a host to an SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        from ..services.ssh_key_manager import HostConflictError

        manager = _get_ssh_key_manager()
        manager.assign_key_to_host(key_name, hostname)

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"Host '{hostname}' assigned to key '{key_name}' successfully.",
        )
    except HostConflictError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Host conflict: {e}"
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-021",
                f"Failed to assign host to SSH key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to assign host: {e}"
        )


# ============================================================================
# Logs Management Routes (Story #664, #665, #667)
# ============================================================================


@web_router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    level: Optional[str] = None,
    logger: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
):
    """
    Logs page - view and filter system logs (Story #664 AC1).

    Args:
        request: FastAPI request object
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        logger: Filter by logger name
        search: Search by message text
        page: Page number for pagination
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Generate CSRF token for forms
    csrf_token = generate_csrf_token()

    # Get log database path from app state
    log_db_path = request.app.state.log_db_path

    # Create LogAggregatorService instance
    from ..services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path)

    # Parse level parameter
    levels = None
    if level:
        levels = [level]

    # Query logs with pagination
    result = service.query(
        page=page,
        page_size=50,
        sort_order="desc",
        search=search,
        levels=levels,
    )

    # Convert to template format
    logs = result["logs"]
    pagination = result["pagination"]

    # Render template
    response = templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "username": session.username,
            "show_nav": True,
            "current_page": "logs",
            "logs": logs,
            "level": level,
            "logger": logger,
            "search": search,
            "page": page,
            "total_count": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/partials/logs-list", response_class=HTMLResponse)
def logs_list_partial(
    request: Request,
    level: Optional[str] = None,
    logger: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
):
    """
    Partial endpoint for logs list - used by HTMX for dynamic updates (Story #664 AC2).

    Args:
        request: FastAPI request object
        level: Filter by log level
        logger: Filter by logger name
        search: Search by message text
        page: Page number for pagination
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        csrf_token = generate_csrf_token()

    # Get log database path from app state
    log_db_path = request.app.state.log_db_path

    # Create LogAggregatorService instance
    from ..services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path)

    # Parse level parameter
    levels = None
    if level:
        levels = [level]

    # Query logs with pagination
    result = service.query(
        page=page,
        page_size=50,
        sort_order="desc",
        search=search,
        levels=levels,
    )

    # Convert to template format
    logs = result["logs"]
    pagination = result["pagination"]

    # Render partial template
    response = templates.TemplateResponse(
        "partials/logs_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "logs": logs,
            "level": level,
            "logger": logger,
            "search": search,
            "page": page,
            "total_count": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get("/logs/export")
def export_logs_web(
    request: Request,
    format: str = "json",
    search: Optional[str] = None,
    level: Optional[str] = None,
):
    """
    Export logs to file in JSON or CSV format (Story #667 AC1).

    Web UI endpoint that triggers browser download of log export file.

    Args:
        request: FastAPI request object
        format: Export format - "json" or "csv" (default: json)
        search: Text search filter
        level: Log level filter
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate format parameter
    if format not in ["json", "csv"]:
        raise HTTPException(
            status_code=400, detail="Invalid format. Must be 'json' or 'csv'"
        )

    # Get log database path from app state
    log_db_path = request.app.state.log_db_path

    # Create LogAggregatorService instance
    from ..services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path)

    # Parse level parameter
    levels = None
    if level:
        levels = [lv.strip() for lv in level.split(",") if lv.strip()]

    # Query all logs (no pagination for export)
    logs = service.query_all(
        search=search,
        levels=levels,
        correlation_id=None,
    )

    # Format output based on requested format
    from ..services.log_export_formatter import LogExportFormatter
    from datetime import datetime, timezone

    formatter = LogExportFormatter()

    if format == "json":
        # JSON export with metadata
        filters = {
            "search": search,
            "level": level,
            "correlation_id": None,
        }
        content = formatter.to_json(logs, filters)
        media_type = "application/json"
    else:
        # CSV export
        content = formatter.to_csv(logs)
        media_type = "text/csv"

    # Generate filename with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"logs_{timestamp}.{format}"

    # Return response with file download headers
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# Unified Login Routes (Phase 2: Login Consolidation)
# ============================================================================


@login_router.get("/login", response_class=HTMLResponse)
def unified_login_page(
    request: Request,
    redirect_to: Optional[str] = None,
    error: Optional[str] = None,
    info: Optional[str] = None,
):
    """
    Unified login page for all contexts (admin, user, OAuth).

    Supports:
    - SSO via OIDC (if enabled)
    - Username/password authentication
    - Smart redirect after login based on redirect_to parameter or user role

    Args:
        request: FastAPI Request object
        redirect_to: Optional URL to redirect after successful login
        error: Optional error message to display
        info: Optional info message to display
    """
    # Bug #715: Try to reuse existing valid CSRF token from cookie
    # This prevents race conditions when HTMX polling refreshes the login page
    # while user is filling out the form
    existing_csrf_token = get_csrf_token_from_cookie(request)
    if existing_csrf_token:
        csrf_token = existing_csrf_token
        need_new_cookie = False
    else:
        csrf_token = generate_csrf_token()
        need_new_cookie = True

    # Check if there's an expired session
    session_manager = get_session_manager()
    if not info and session_manager.is_session_expired(request):
        info = "Session expired, please login again"

    # Check if OIDC is enabled
    from ..auth.oidc import routes as oidc_routes

    sso_enabled = False
    if oidc_routes.oidc_manager and hasattr(oidc_routes.oidc_manager, "is_enabled"):
        sso_enabled = oidc_routes.oidc_manager.is_enabled()

    # Create response with CSRF token in signed cookie
    response = templates.TemplateResponse(
        "unified_login.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "redirect_to": redirect_to,
            "error": error,
            "info": info,
            "sso_enabled": sso_enabled,
        },
    )

    # Bug #715: Only set CSRF cookie if we generated a new token
    # This prevents overwriting valid cookies during HTMX polling
    if need_new_cookie:
        set_csrf_cookie(response, csrf_token, path="/")

    return response


@login_router.post("/login", response_class=HTMLResponse)
def unified_login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: Optional[str] = Form(None),
    redirect_to: Optional[str] = Form(None),
):
    """
    Process unified login form submission.

    Validates credentials and creates session on success.
    Accepts ANY role (normal_user, power_user, admin).
    Redirects based on redirect_to parameter or user role.

    Args:
        request: FastAPI Request object
        response: FastAPI Response object
        username: Username from form
        password: Password from form
        csrf_token: CSRF token from form
        redirect_to: Optional redirect URL from form
    """
    # CSRF validation - validate token against signed cookie
    if not validate_login_csrf_token(request, csrf_token):
        # CSRF validation failed - auto-recover by redirecting with fresh token
        # Bug #714: Instead of showing 403, redirect to login page for better UX
        logger.info(
            "CSRF validation failed, auto-recovering with fresh token",
            extra={"correlation_id": get_correlation_id()},
        )

        # Create redirect response to login page with session_expired message
        redirect_url = "/login?info=session_expired"
        redirect_response = RedirectResponse(
            url=redirect_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )

        # Clear old CSRF cookie and set fresh one
        redirect_response.delete_cookie(CSRF_COOKIE_NAME, path="/")
        new_csrf_token = generate_csrf_token()
        set_csrf_cookie(redirect_response, new_csrf_token, path="/")

        return redirect_response

    # Get user manager from dependencies
    user_manager = dependencies.user_manager
    if not user_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User manager not available",
        )

    # Authenticate user (any role accepted)
    user = user_manager.authenticate_user(username, password)

    if user is None:
        # Invalid credentials - show error with new CSRF token
        new_csrf_token = generate_csrf_token()

        # Check if OIDC is enabled
        from ..auth.oidc import routes as oidc_routes

        sso_enabled = False
        if oidc_routes.oidc_manager and hasattr(oidc_routes.oidc_manager, "is_enabled"):
            sso_enabled = oidc_routes.oidc_manager.is_enabled()

        error_response = templates.TemplateResponse(
            "unified_login.html",
            {
                "request": request,
                "csrf_token": new_csrf_token,
                "redirect_to": redirect_to,
                "error": "Invalid username or password",
                "sso_enabled": sso_enabled,
            },
            status_code=200,
        )
        set_csrf_cookie(error_response, new_csrf_token, path="/")
        return error_response

    # Validate redirect_to URL (prevent open redirect)
    safe_redirect = None
    if redirect_to:
        # Only allow relative URLs starting with /
        if redirect_to.startswith("/") and not redirect_to.startswith("//"):
            safe_redirect = redirect_to

    # Smart redirect logic
    if safe_redirect:
        # Explicit redirect_to parameter takes precedence
        redirect_url = safe_redirect
    elif user.role.value == "admin":
        # Admin users go to admin dashboard
        redirect_url = "/admin/"
    else:
        # Non-admin users go to user interface
        redirect_url = "/user/api-keys"

    # Create session for authenticated user
    session_manager = get_session_manager()
    redirect_response = RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    session_manager.create_session(
        redirect_response,
        username=user.username,
        role=user.role.value,
    )

    return redirect_response


@login_router.get("/login/sso")
async def unified_login_sso(
    request: Request,
    redirect_to: Optional[str] = None,
):
    """
    Initiate OIDC SSO flow from unified login page.

    Preserves redirect_to parameter through OIDC flow by storing
    it in the OIDC state parameter.

    Args:
        request: FastAPI Request object
        redirect_to: Optional URL to redirect after SSO completes
    """
    from ..auth.oidc import routes as oidc_routes

    # Check if OIDC is enabled
    if not oidc_routes.oidc_manager or not oidc_routes.oidc_manager.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO is not enabled on this server",
        )

    # Ensure OIDC provider is initialized
    try:
        await oidc_routes.oidc_manager.ensure_provider_initialized()
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-022",
                f"Failed to initialize OIDC provider: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO provider is currently unavailable",
        )

    # Generate PKCE code verifier and challenge for OAuth 2.1 security
    import hashlib
    import base64

    code_verifier = secrets.token_urlsafe(32)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    # Validate redirect_to parameter (prevent open redirect)
    # Note: redirect_to is URL-encoded by JavaScript's encodeURIComponent, decode it
    from urllib.parse import unquote

    safe_redirect = None
    if redirect_to:
        # URL-decode (JavaScript's encodeURIComponent encoding)
        decoded_redirect = unquote(redirect_to)
        if decoded_redirect.startswith("/") and not decoded_redirect.startswith("//"):
            safe_redirect = decoded_redirect

    # Store state with code_verifier and redirect_to using OIDC state manager
    assert (
        oidc_routes.state_manager is not None
    ), "state_manager must be initialized when oidc_manager is enabled"
    state_data = {
        "code_verifier": code_verifier,
    }
    # Only include redirect_to if explicitly provided (let callback determine based on role otherwise)
    if safe_redirect:
        state_data["redirect_to"] = safe_redirect
    state_token = oidc_routes.state_manager.create_state(state_data)

    # Build OIDC authorization URL
    # Use CIDX_ISSUER_URL if set (for reverse proxy scenarios), otherwise use request.base_url
    issuer_url = os.getenv("CIDX_ISSUER_URL")
    if issuer_url:
        callback_url = f"{issuer_url.rstrip('/')}/auth/sso/callback"
    else:
        callback_url = str(request.base_url).rstrip("/") + "/auth/sso/callback"
    oidc_manager = oidc_routes.oidc_manager
    assert (
        oidc_manager is not None
    ), "oidc_manager must be initialized when SSO login is invoked"
    provider = oidc_manager.provider
    assert (
        provider is not None
    ), "oidc provider must be initialized when SSO login is invoked"
    oidc_auth_url = provider.get_authorization_url(
        state=state_token, redirect_uri=callback_url, code_challenge=code_challenge
    )

    return RedirectResponse(url=oidc_auth_url)


# ==============================================================================
# Self-Monitoring Routes (Story #74 - Epic #71)
# ==============================================================================


def _load_self_monitoring_data(
    db_path: Path, session: SessionData
) -> Tuple[List[Dict], List[Dict]]:
    """
    Load self-monitoring scans and issues from database (Story #74 AC4, AC5).

    Args:
        db_path: Path to SQLite database
        session: Current user session (for logging)

    Returns:
        Tuple of (scans, issues) as lists of dicts
    """
    scans = []
    issues = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Load scans (most recent first)
            cursor.execute(
                """
                SELECT scan_id, started_at, completed_at, status,
                       log_id_start, log_id_end, issues_created, error_message
                FROM self_monitoring_scans
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (SCAN_HISTORY_LIMIT,),
            )
            scans = [dict(row) for row in cursor.fetchall()]

            # Add duration calculation for each scan
            from datetime import datetime

            for scan in scans:
                if scan["completed_at"] and scan["started_at"]:
                    # Parse timestamps and calculate duration
                    try:
                        started = datetime.fromisoformat(scan["started_at"])
                        completed = datetime.fromisoformat(scan["completed_at"])
                        duration_seconds = (completed - started).total_seconds()

                        # Format as "Xm Ys"
                        minutes = int(duration_seconds // 60)
                        seconds = int(duration_seconds % 60)
                        scan["duration"] = f"{minutes}m {seconds}s"
                    except (ValueError, TypeError):
                        scan["duration"] = "N/A"
                elif scan["status"] == "RUNNING":
                    scan["duration"] = "In progress"
                else:
                    scan["duration"] = "N/A"

            # Load issues (most recent first)
            cursor.execute(
                """
                SELECT id, scan_id, github_issue_number, github_issue_url,
                       classification, title, fingerprint,
                       source_log_ids, source_files, created_at
                FROM self_monitoring_issues
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (ISSUES_HISTORY_LIMIT,),
            )
            issues = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-001",
                f"Failed to load self-monitoring data: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-001"),
        )

    return scans, issues


def _load_default_prompt() -> str:
    """
    Load default self-monitoring prompt template (Story #74).

    Returns:
        Default prompt text, or empty string if file not found
    """
    default_prompt_path = (
        Path(__file__).parent.parent
        / "self_monitoring"
        / "prompts"
        / "default_analysis_prompt.md"
    )
    if default_prompt_path.exists():
        return default_prompt_path.read_text()
    return ""


def _get_last_scan_time(db_path: Path) -> Optional[str]:
    """
    Get timestamp of most recent scan from database (Bug #129 Fix - Problem 1).

    Args:
        db_path: Path to SQLite database

    Returns:
        started_at timestamp of most recent scan, or None if no scans exist
    """
    try:
        import sqlite3

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT started_at
                FROM self_monitoring_scans
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            return row["started_at"] if row else None
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-002",
                f"Failed to get last scan time: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-002"),
        )
        return None


def _calculate_next_scan_time(
    last_scan_time: Optional[str], cadence_minutes: int
) -> Optional[str]:
    """
    Calculate next scan time based on last scan + cadence (Bug #129 Fix - Problem 2).

    Args:
        last_scan_time: ISO timestamp of last scan (or None)
        cadence_minutes: Scan cadence in minutes

    Returns:
        ISO timestamp of next scan, or None if cannot calculate
    """
    if not last_scan_time:
        return None

    try:
        from datetime import datetime, timedelta

        last_scan_dt = datetime.fromisoformat(last_scan_time)
        next_scan_dt = last_scan_dt + timedelta(minutes=cadence_minutes)
        return next_scan_dt.isoformat()
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-003",
                f"Failed to calculate next scan time: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-003"),
        )
        return None


def _get_scan_status(db_path: Path) -> str:
    """
    Get current scan status by checking background jobs table (Bug #129 Fix - Problem 3).

    Args:
        db_path: Path to SQLite database

    Returns:
        "Running..." if self_monitoring job is running, "Idle" otherwise
    """
    try:
        import sqlite3

        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM self_monitoring_scans
                WHERE completed_at IS NULL
                """
            )
            count = cursor.fetchone()[0]
            return "Running..." if count > 0 else "Idle"
    except Exception as e:
        logger.error(
            format_error_log(
                "WEB-SELF-MONITORING-004",
                f"Failed to get scan status: {e}",
            ),
            extra=get_log_extra("WEB-SELF-MONITORING-004"),
        )
        return "Idle"


def _create_self_monitoring_page_response(
    request: Request,
    session: SessionData,
    self_monitoring_config,
    default_prompt: str,
    current_prompt: str,
    scans: List[Dict],
    issues: List[Dict],
    db_path: Path,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
):
    """
    Build self-monitoring page response with template context (Story #74, Bug #129).

    Args:
        request: FastAPI request
        session: Current user session
        self_monitoring_config: SelfMonitoringConfig instance
        default_prompt: Default prompt template text
        current_prompt: Current prompt (config or default)
        scans: Scan history list
        issues: Issues history list
        db_path: Path to database (for status calculations - Bug #129)
        success_message: Optional success message to display
        error_message: Optional error message to display

    Returns:
        TemplateResponse with CSRF cookie set
    """
    # Bug #129 Fix: Calculate status values from database
    last_scan = _get_last_scan_time(db_path)
    next_scan = _calculate_next_scan_time(
        last_scan, self_monitoring_config.cadence_minutes
    )
    scan_status = _get_scan_status(db_path)

    # Format values for display
    last_scan_display = last_scan if last_scan else "Never"
    next_scan_display = next_scan if next_scan else "N/A"

    csrf_token = generate_csrf_token()
    response = templates.TemplateResponse(
        "self_monitoring.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "self-monitoring",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": self_monitoring_config,
            "default_prompt": default_prompt,
            "current_prompt": current_prompt,
            "scans": scans,
            "issues": issues,
            "success_message": success_message,
            "error_message": error_message,
            # Bug #129 Fix: Pass calculated status values to template
            "last_scan": last_scan_display,
            "next_scan": next_scan_display,
            "scan_status": scan_status,
        },
    )
    set_csrf_cookie(response, csrf_token, path="/")
    return response


@web_router.get("/self-monitoring", response_class=HTMLResponse)
def self_monitoring_page(request: Request):
    """
    Self-Monitoring configuration and monitoring page (Story #74).

    Displays:
    - AC2: Status section (enabled/disabled, last scan, next scan)
    - AC3: Configuration section (enable toggle, cadence, model, prompt editor)
    - AC4: Scan history from self_monitoring_scans table
    - AC5: Created issues from self_monitoring_issues table

    Requires authenticated admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    # Get configuration
    config_service = get_config_service()
    config = config_service.get_config()
    self_monitoring_config = config.self_monitoring_config

    # Load default prompt template
    default_prompt = _load_default_prompt()

    # Get current prompt (use default if empty)
    current_prompt = self_monitoring_config.prompt_template or default_prompt

    # Load scan history and issues from database
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    scans, issues = _load_self_monitoring_data(db_path, session)

    return _create_self_monitoring_page_response(
        request,
        session,
        self_monitoring_config,
        default_prompt,
        current_prompt,
        scans,
        issues,
        db_path,
    )


@web_router.post("/self-monitoring", response_class=HTMLResponse)
async def save_self_monitoring_config(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """
    Save self-monitoring configuration (Story #74 AC6).

    Updates SelfMonitoringConfig from form data including enable/disable,
    cadence, model, and prompt template. Sets prompt_user_modified flag
    when prompt differs from default.

    Requires authenticated admin session and valid CSRF token.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    config_service = get_config_service()
    config = config_service.get_config()
    default_prompt = _load_default_prompt()

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        server_dir = config_service.config_manager.server_dir
        db_path = server_dir / "data" / "cidx_server.db"
        scans, issues = _load_self_monitoring_data(db_path, session)
        current_prompt = config.self_monitoring_config.prompt_template or default_prompt
        return _create_self_monitoring_page_response(
            request,
            session,
            config.self_monitoring_config,
            default_prompt,
            current_prompt,
            scans,
            issues,
            db_path,
            error_message="Invalid CSRF token",
        )

    # Parse form data
    form_data = await request.form()

    enabled = form_data.get("enabled") == "on"

    try:
        cadence_minutes = int(form_data.get("cadence_minutes", "60"))
    except ValueError:
        logger.debug("Invalid cadence_minutes value in form, defaulting to 60")
        cadence_minutes = 60

    model = form_data.get("model", "opus").strip()
    prompt_template = form_data.get("prompt_template", "").strip()

    # Determine if prompt was user-modified
    prompt_user_modified = config.self_monitoring_config.prompt_user_modified
    if prompt_template and prompt_template != default_prompt:
        prompt_user_modified = True

    # Update configuration
    config.self_monitoring_config.enabled = enabled
    config.self_monitoring_config.cadence_minutes = cadence_minutes
    config.self_monitoring_config.model = model
    config.self_monitoring_config.prompt_template = prompt_template
    config.self_monitoring_config.prompt_user_modified = prompt_user_modified

    # Save configuration
    config_service.config_manager.save_config(config)

    # Bug #128: Start/stop service based on enabled flag
    service = getattr(request.app.state, "self_monitoring_service", None)

    if service is not None:
        # Critical: Update service's internal _enabled flag BEFORE start/stop
        # This ensures trigger_scan() works after enabling via toggle
        service._enabled = enabled

        if enabled and not service.is_running:
            service.start()
            logger.info("Self-monitoring service started via configuration toggle")
        elif not enabled and service.is_running:
            service.stop()
            logger.info("Self-monitoring service stopped via configuration toggle")

    # Re-render page with success message
    current_prompt = prompt_template or default_prompt
    server_dir = config_service.config_manager.server_dir
    db_path = server_dir / "data" / "cidx_server.db"
    scans, issues = _load_self_monitoring_data(db_path, session)

    return _create_self_monitoring_page_response(
        request,
        session,
        config.self_monitoring_config,
        default_prompt,
        current_prompt,
        scans,
        issues,
        db_path,
        success_message="Self-monitoring configuration saved successfully",
    )


@web_router.post("/self-monitoring/run-now", response_class=JSONResponse)
async def trigger_manual_scan(
    request: Request,
    csrf_token: Optional[str] = Form(None),
):
    """
    Manually trigger a self-monitoring scan (Story #75 AC1, AC2).

    Submits a scan job to the background job queue. Returns immediately with
    queued status - scan executes asynchronously.

    Returns:
        JSON response with status and scan_id:
        - Success: {"status": "queued", "scan_id": "..."}
        - Error: {"status": "error", "error": "..."}

    Requires authenticated admin session and valid CSRF token.
    """
    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Entry - endpoint called")

    session = _require_admin_session(request)
    if not session:
        logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: No admin session found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Invalid CSRF token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )

    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Auth validated, loading config")

    # Get self-monitoring service from app state
    # The service is initialized in app.py during startup
    from code_indexer.server.self_monitoring.service import SelfMonitoringService
    import os
    from pathlib import Path

    config_service = get_config_service()
    config = config_service.get_config()

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Config loaded - enabled={config.self_monitoring_config.enabled}, cadence_minutes={config.self_monitoring_config.cadence_minutes}, model={config.self_monitoring_config.model}"
    )

    # Get background job manager from app state
    job_manager = getattr(request.app.state, "background_job_manager", None)
    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: job_manager={job_manager is not None}"
    )

    # Get database paths from app state and config (Bug #87)
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
    log_db_path = getattr(request.app.state, "log_db_path", None)
    if log_db_path:
        log_db_path = str(log_db_path)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Database paths - db_path={db_path}, log_db_path={log_db_path}"
    )

    # Get repo_root and github_repo from app.state (auto-detected at startup)
    # Bug Fix: MONITOR-GENERAL-011 - CIDX_REPO_ROOT env var ensures reliable detection
    repo_root = getattr(request.app.state, "self_monitoring_repo_root", None)
    github_repo = getattr(request.app.state, "self_monitoring_github_repo", None)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Repo config - repo_root={repo_root}, github_repo={github_repo}"
    )

    if not github_repo:
        logger.debug(
            "[SELF-MON-DEBUG] trigger_manual_scan: github_repo is None - returning error"
        )
        logger.error(
            format_error_log(
                "MONITOR-GENERAL-011",
                "Self-monitoring: github_repo auto-detection failed. "
                "Ensure CIDX_REPO_ROOT environment variable is set correctly in systemd service.",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error": "GitHub repository auto-detection failed. Check server logs.",
            },
        )

    # Get GitHub token for authentication (Bug #87)
    token_manager = _get_token_manager()
    github_token_data = token_manager.get_token("github")
    github_token = github_token_data.token if github_token_data else None

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: GitHub token retrieved - has_token={github_token is not None}"
    )

    # Get server name for issue identification (Bug #87)
    server_name = config.service_display_name or "Neo"

    logger.debug(f"[SELF-MON-DEBUG] trigger_manual_scan: server_name={server_name}")

    # Create service instance with current configuration (Bug #87)
    logger.debug(
        "[SELF-MON-DEBUG] trigger_manual_scan: Creating SelfMonitoringService instance"
    )
    service = SelfMonitoringService(
        enabled=config.self_monitoring_config.enabled,
        cadence_minutes=config.self_monitoring_config.cadence_minutes,
        job_manager=job_manager,
        db_path=db_path,
        log_db_path=log_db_path,
        github_repo=github_repo,
        prompt_template=config.self_monitoring_config.prompt_template,
        model=config.self_monitoring_config.model,
        repo_root=str(repo_root) if repo_root else None,
        github_token=github_token,
        server_name=server_name,
    )

    # Trigger the scan
    logger.debug("[SELF-MON-DEBUG] trigger_manual_scan: Calling service.trigger_scan()")
    result = service.trigger_scan()
    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: trigger_scan returned - result={result}"
    )

    # Return JSON response
    if result["status"] == "error":
        logger.debug(
            f"[SELF-MON-DEBUG] trigger_manual_scan: Returning error response - {result}"
        )
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=result)

    logger.debug(
        f"[SELF-MON-DEBUG] trigger_manual_scan: Returning success response - {result}"
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)


# ==============================================================================
# Backwards Compatibility Redirects (Phase 8: Login Consolidation)
# ==============================================================================


@login_router.get("/admin/login")
def redirect_admin_login(redirect_to: Optional[str] = None):
    """
    Backwards compatibility redirect: /admin/login → /login.

    301 Permanent Redirect to inform clients to update their URLs.
    """
    if redirect_to:
        return RedirectResponse(
            url=f"/login?redirect_to={quote(redirect_to)}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )
    return RedirectResponse(url="/login", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@login_router.get("/user/login")
def redirect_user_login(redirect_to: Optional[str] = None):
    """
    Backwards compatibility redirect: /user/login → /login.

    301 Permanent Redirect to inform clients to update their URLs.
    """
    if redirect_to:
        return RedirectResponse(
            url=f"/login?redirect_to={quote(redirect_to)}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )
    return RedirectResponse(url="/login", status_code=status.HTTP_301_MOVED_PERMANENTLY)


# ============================================================================
# API Router - Public API Endpoints (Story #89)
# ============================================================================

# Create API router for public API endpoints (no auth required)
api_router = APIRouter()


# Rate limiting for restart endpoint (Code Review Issue #6)
_restart_in_progress = False
_restart_lock = threading.Lock()


def _delayed_restart(delay: int = 2) -> None:
    """
    Execute server restart after a delay.

    Story #205: Server Restart from Diagnostics Tab

    This function sleeps for the specified delay to allow the HTTP response
    to complete, then restarts the server using the appropriate method:
    - Systemd mode: Uses systemctl restart cidx-server
    - Dev mode: Uses os.execv to re-exec the current process

    Args:
        delay: Seconds to wait before restarting (default: 2)
    """
    global _restart_in_progress

    # Sleep to allow HTTP response to complete
    time.sleep(delay)

    # Log before restarting
    logger.info("Executing server restart now")

    # Detect if running under systemd
    is_systemd = os.environ.get("INVOCATION_ID") is not None

    if is_systemd:
        # Systemd mode: use systemctl restart (Code Review Issue #3)
        logger.info("Restarting via systemctl (systemd mode)")
        result = subprocess.run(
            ["sudo", "/usr/bin/systemctl", "restart", "cidx-server"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            logger.error(
                "systemctl restart failed (rc=%d): %s",
                result.returncode,
                result.stderr
            )
            # Reset flag on failure (Code Review Finding #1)
            with _restart_lock:
                _restart_in_progress = False
    else:
        # Dev mode: re-exec the current process (Code Review Issue #4)
        logger.info("Restarting via os.execv (dev mode)")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as e:
            logger.error("os.execv failed: %s", e)
            # Reset flag on failure (Code Review Finding #1)
            with _restart_lock:
                _restart_in_progress = False


def _schedule_delayed_restart(delay: int = 2) -> None:
    """
    Schedule a delayed restart on a background daemon thread.

    Story #205: Server Restart from Diagnostics Tab

    Creates a daemon thread that will execute the restart after a delay.
    The daemon thread ensures the restart doesn't block the HTTP response.

    Args:
        delay: Seconds to wait before restarting (default: 2)
    """
    restart_thread = threading.Thread(
        target=_delayed_restart,
        args=(delay,),
        daemon=True
    )
    restart_thread.start()


@web_router.post("/restart", response_class=JSONResponse)
def restart_server(request: Request) -> JSONResponse:
    """
    Restart the CIDX server (admin only).

    Story #205: Server Restart from Diagnostics Tab

    This endpoint allows admin users to restart the CIDX server from the
    Diagnostics tab without requiring SSH access. The restart is delayed
    by 2 seconds to allow the HTTP 202 response to complete.

    Authentication:
        Requires admin session (checked via _require_admin_session)

    Security:
        Requires valid CSRF token in X-CSRF-Token header

    Rate Limiting:
        Only one restart can be in progress at a time (returns 409 if concurrent)

    Returns:
        202 Accepted with message about restart in progress
        409 Conflict if restart already in progress

    Raises:
        HTTPException 403: If user is not admin or CSRF token is invalid
    """
    global _restart_in_progress

    # Check for admin session
    session = _require_admin_session(request)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    # Validate CSRF token (Code Review Issues #1 and #2)
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not validate_login_csrf_token(request, csrf_from_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token"
        )

    # Rate limiting: Check if restart already in progress (Code Review Issue #6)
    with _restart_lock:
        if _restart_in_progress:
            return JSONResponse(
                status_code=409,
                content={
                    "message": "Restart already in progress"
                }
            )
        _restart_in_progress = True

    # Log restart request with username
    username = session.username
    logger.info(f"Server restart requested by {username}")

    # Schedule delayed restart on background thread
    _schedule_delayed_restart(delay=2)

    # Return 202 Accepted immediately
    return JSONResponse(
        status_code=202,
        content={
            "message": "Server is restarting in 2 seconds..."
        }
    )


@api_router.get("/server-time")
def get_server_time() -> Dict[str, str]:
    """
    Get current server time for client clock synchronization.

    Story #89: Server Clock in Navigation

    Returns current server time in ISO 8601 format with timezone information.
    This endpoint is lightweight and does not require authentication,
    allowing clients to synchronize their clock displays with server time.

    Returns:
        Dictionary with 'timestamp' (ISO 8601 UTC) and 'timezone' ('UTC')

    Example Response:
        {
            "timestamp": "2026-02-04T14:32:15Z",
            "timezone": "UTC"
        }
    """
    from datetime import datetime, timezone as tz

    # Get current UTC time
    current_time = datetime.now(tz.utc)

    # Format as ISO 8601 with Z suffix for UTC (preserves microseconds)
    # Replace '+00:00' with 'Z' for standard UTC notation
    timestamp = current_time.isoformat().replace("+00:00", "Z")

    return {"timestamp": timestamp, "timezone": "UTC"}
