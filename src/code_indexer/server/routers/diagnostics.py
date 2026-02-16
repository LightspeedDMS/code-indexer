"""
Diagnostics Router for CIDX Server.

Story #90: Diagnostics Tab with Run All

Provides admin endpoints for running diagnostic checks across five categories:
- CLI Tool Dependencies
- SDK Prerequisites
- External API Integrations
- Credential & Connectivity
- Core Infrastructure

Implements HTMX-based polling for async diagnostic execution.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from code_indexer.server.auth.dependencies import get_current_admin_user
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticsService,
)
from code_indexer.server.web.routes import (
    get_csrf_token_from_cookie,
    generate_csrf_token,
    set_csrf_cookie,
)

logger = logging.getLogger(__name__)

# Templates
TEMPLATES_DIR = Path(__file__).parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Register Jinja2 globals for base template compatibility
def _get_server_time_for_template() -> str:
    """Get current server time for Jinja2 templates (required by base.html)."""
    from datetime import datetime, timezone as tz

    current_time = datetime.now(tz.utc)
    return current_time.isoformat().replace("+00:00", "Z")


templates.env.globals["get_server_time"] = _get_server_time_for_template

# Router
router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])

# Service instance (singleton pattern)
diagnostics_service = DiagnosticsService()


@router.get("", response_class=HTMLResponse)
async def get_diagnostics_page(
    request: Request,
    current_user: User = None,  # TODO: Add dependency after auth integration
) -> HTMLResponse:
    """
    Render the diagnostics page.

    Returns HTML page with:
    - Five category sections
    - Run All Diagnostics button
    - Category-specific re-run buttons
    - HTMX polling setup for async updates

    Args:
        request: FastAPI request object
        current_user: Authenticated admin user

    Returns:
        HTML response with diagnostics page
    """
    try:
        # Get current status
        status = diagnostics_service.get_status()
        is_running = diagnostics_service.is_running()

        # Get CSRF token from session cookie, or generate new if none exists (Story #205, Code Review Issues #1 and #2)
        csrf_token = get_csrf_token_from_cookie(request)
        new_token_generated = False
        if not csrf_token:
            csrf_token = generate_csrf_token()
            new_token_generated = True

        response = templates.TemplateResponse(
            request=request,
            name="diagnostics.html",
            context={
                "request": request,
                "show_nav": True,
                "current_page": "diagnostics",
                "status": status,
                "is_running": is_running,
                "categories": DiagnosticCategory,
                "csrf_token": csrf_token,
            },
        )

        # Set CSRF cookie if we generated a new token (Code Review Finding #2)
        if new_token_generated:
            set_csrf_cookie(response, csrf_token)

        return response
    except Exception as e:
        logger.error(f"Error rendering diagnostics page: {e}")
        raise HTTPException(status_code=500, detail="Failed to render diagnostics page")


@router.post("/run-all", response_class=HTMLResponse)
async def run_all_diagnostics(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = None,  # TODO: Add dependency after auth integration
) -> HTMLResponse:
    """
    Trigger diagnostic execution for all categories.

    Runs diagnostics asynchronously and returns HTML partial with polling enabled.
    The HTML partial initiates HTMX polling to /admin/diagnostics/status.

    Args:
        request: FastAPI request object
        background_tasks: FastAPI background tasks for async execution
        current_user: Authenticated admin user

    Returns:
        HTML response with diagnostics status partial (starts polling)
    """
    try:
        # Run diagnostics asynchronously in background
        background_tasks.add_task(diagnostics_service.run_all_diagnostics)

        # Get current status (will be empty/not-run initially)
        status = diagnostics_service.get_status()

        # Return HTML partial with polling enabled
        return templates.TemplateResponse(
            request=request,
            name="partials/diagnostics_status.html",
            context={
                "request": request,
                "status": status,
                "is_running": True,  # Mark as running to enable polling
                "categories": DiagnosticCategory,
            },
        )
    except Exception as e:
        logger.error(f"Error running all diagnostics: {e}")
        raise HTTPException(status_code=500, detail="Failed to start diagnostics")


@router.post("/run/{category}", response_class=HTMLResponse)
async def run_category_diagnostics(
    category: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = None,  # TODO: Add dependency after auth integration
) -> HTMLResponse:
    """
    Trigger diagnostic execution for a single category.

    Runs diagnostics asynchronously for the specified category only.
    Returns HTML partial with polling enabled to show real-time updates.

    Args:
        category: Category to run (e.g., "cli_tools", "sdk_prerequisites")
        request: FastAPI request object
        background_tasks: FastAPI background tasks for async execution
        current_user: Authenticated admin user

    Returns:
        HTML response with diagnostics status partial (starts polling)

    Raises:
        HTTPException: If category is invalid
    """
    try:
        # Validate and convert category
        try:
            category_enum = DiagnosticCategory(category)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category: {category}. Valid categories: {[c.value for c in DiagnosticCategory]}",
            )

        # Run diagnostics for this category in background
        background_tasks.add_task(diagnostics_service.run_category, category_enum)

        # Get current status
        status = diagnostics_service.get_status()

        # Return HTML partial with polling enabled
        return templates.TemplateResponse(
            request=request,
            name="partials/diagnostics_status.html",
            context={
                "request": request,
                "status": status,
                "is_running": True,  # Mark as running to enable polling
                "categories": DiagnosticCategory,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running category diagnostics for {category}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to start diagnostics for category: {category}"
        )


@router.get("/status", response_class=HTMLResponse)
async def get_diagnostics_status(
    request: Request,
    current_user: User = None,  # TODO: Add dependency after auth integration
) -> Response:
    """
    Get current diagnostics status (HTMX polling endpoint).

    Returns HTML partial with current status for all categories.
    Includes HX-Stop-Polling header when diagnostics are complete
    to stop HTMX polling.

    Args:
        request: FastAPI request object
        current_user: Authenticated admin user

    Returns:
        HTML response with status partial
    """
    try:
        # Get current status
        status = diagnostics_service.get_status()
        is_running = diagnostics_service.is_running()

        # Render status partial
        html_content = templates.TemplateResponse(
            request=request,
            name="partials/diagnostics_status.html",
            context={
                "status": status,
                "is_running": is_running,
                "categories": DiagnosticCategory,
            },
        )

        # Add HX-Stop-Polling header if diagnostics are complete
        if not is_running:
            html_content.headers["HX-Stop-Polling"] = "true"

        return html_content

    except Exception as e:
        logger.error(f"Error getting diagnostics status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get diagnostics status")
