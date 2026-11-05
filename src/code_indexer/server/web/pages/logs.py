"""
Admin log viewer pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import HTMLResponse
from ...auth import dependencies

from ._shared import (
    _create_login_redirect,
    _elevation_page_redirect,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    set_csrf_cookie,
    templates,
)

web_router = APIRouter()

# ============================================================================
# Logs Management Routes (Story #664, #665, #667)
# ============================================================================


@web_router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    level: Optional[str] = None,
    logger: Optional[str] = None,
    search: Optional[str] = None,
    node_id: Optional[str] = None,
    page: int = 1,
):
    """
    Logs page - view and filter system logs (Story #664 AC1, Story #501 AC4).

    Args:
        request: FastAPI request object
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        logger: Filter by logger name
        search: Search by message text
        node_id: Filter by cluster node ID (Story #501 AC4)
        page: Page number for pagination

    Reading server logs requires the caller's own elevation window, like
    the MCP twin ``admin_logs_query`` (``@require_mcp_elevation()``): with
    enforcement on and no window, the admin is sent to the elevation page,
    which returns here afterwards (filters kept; the elevate page's
    ``_sanitize_next`` bounds the target). Enforcement off passes through.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    elevate = _elevation_page_redirect(request, session.username)
    if elevate is not None:
        return elevate

    # Generate CSRF token for forms
    csrf_token = generate_csrf_token()

    # Bug #1553: LogAggregatorService is the ONE place that decides whether
    # reads go to the cross-node backend or the local file (via the
    # is_cross_node_backend capability check) -- replaces the duplicated
    # inline branch + fragile "Postgres" in type(x).__name__ string match.
    logs_backend = getattr(request.app.state, "logs_backend", None)
    log_db_path = request.app.state.log_db_path
    from ...services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path, logs_backend=logs_backend)
    levels = [level] if level else None
    result = service.query(
        page=page,
        page_size=50,
        sort_order="desc",
        search=search,
        levels=levels,
        source=logger or None,
        node_id=node_id or None,
    )
    logs = result["logs"]
    pagination = result["pagination"]
    is_cluster_mode = service.is_cluster_mode

    # Render template
    response = templates.TemplateResponse(
        request,
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
            "node_id": node_id,
            "is_cluster_mode": is_cluster_mode,
            "page": page,
            "total_count": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get(
    "/partials/logs-list",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def logs_list_partial(
    request: Request,
    level: Optional[str] = None,
    logger: Optional[str] = None,
    search: Optional[str] = None,
    node_id: Optional[str] = None,
    page: int = 1,
):
    """
    Partial endpoint for logs list - used by HTMX for dynamic updates (Story #664 AC2, Story #501 AC4).

    Elevation-gated (``require_elevation()``) like the logs page and the MCP
    twin ``admin_logs_query``: without a window it answers 403
    ``elevation_required``, which the shared elevation interceptor turns
    into the TOTP modal. Enforcement off passes through.

    Args:
        request: FastAPI request object
        level: Filter by log level
        logger: Filter by logger name
        search: Search by message text
        node_id: Filter by cluster node ID (Story #501 AC4)
        page: Page number for pagination
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Reuse existing CSRF token from cookie
    csrf_token = get_csrf_token_from_cookie(request)
    if not csrf_token:
        csrf_token = generate_csrf_token()

    # Bug #1553: LogAggregatorService is the ONE place that decides whether
    # reads go to the cross-node backend or the local file (via the
    # is_cross_node_backend capability check) -- replaces the duplicated
    # inline branch + fragile "Postgres" in type(x).__name__ string match.
    logs_backend = getattr(request.app.state, "logs_backend", None)
    log_db_path = request.app.state.log_db_path
    from ...services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path, logs_backend=logs_backend)
    levels = [level] if level else None
    result = service.query(
        page=page,
        page_size=50,
        sort_order="desc",
        search=search,
        levels=levels,
        source=logger or None,
        node_id=node_id or None,
    )
    logs = result["logs"]
    pagination = result["pagination"]
    is_cluster_mode = service.is_cluster_mode

    # Render partial template
    response = templates.TemplateResponse(
        request,
        "partials/logs_list.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "logs": logs,
            "level": level,
            "logger": logger,
            "search": search,
            "node_id": node_id,
            "is_cluster_mode": is_cluster_mode,
            "page": page,
            "total_count": pagination["total"],
            "total_pages": pagination["total_pages"],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response


@web_router.get(
    "/logs/export",
    dependencies=[Depends(dependencies.require_elevation())],
)
def export_logs_web(
    request: Request,
    format: str = "json",
    search: Optional[str] = None,
    level: Optional[str] = None,
):
    """
    Export logs to file in JSON or CSV format (Story #667 AC1).

    Web UI endpoint that triggers browser download of log export file.
    Elevation-gated (``require_elevation()``) like the logs page and the MCP
    twin ``admin_logs_query``: without a window it answers 403
    ``elevation_required``. Enforcement off passes through.

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

    # Get log database path from app state. Bug #1553: pass logs_backend so
    # cluster-mode exports follow the same store the writer thread uses --
    # this endpoint previously had NO backend awareness at all.
    log_db_path = request.app.state.log_db_path
    logs_backend = getattr(request.app.state, "logs_backend", None)

    from ...services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path, logs_backend=logs_backend)

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
    from ...services.log_export_formatter import LogExportFormatter
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
