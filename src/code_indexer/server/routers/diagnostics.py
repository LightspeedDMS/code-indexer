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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
)
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.claude_cli_manager import get_claude_cli_manager
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


# ---------------------------------------------------------------------------
# Story #233: Generate Missing Descriptions Endpoint
# ---------------------------------------------------------------------------


class GenerateMissingDescriptionsResponse(BaseModel):
    """Response for POST /admin/diagnostics/generate-missing-descriptions."""

    repos_queued: int
    repos_with_descriptions: int
    total_repos: int


def _make_description_callback(
    repo_alias: str,
    md_path: Path,
    repo_url: str,
    clone_path: str,
    generate_fn,
):
    """
    Build a ClaudeCliManager work callback that writes the generated .md file.

    ClaudeCliManager acts as a thread-pool with concurrency control.  The actual
    description generation happens inside this callback, which runs in a worker
    thread.  submit_work() queues the repo; this callback is invoked when a
    worker slot becomes available.

    Args:
        repo_alias: Repository alias
        md_path: Destination path for the generated .md file
        repo_url: Repository URL (used by generate_fn)
        clone_path: Filesystem path to the repo clone (used by generate_fn)
        generate_fn: Callable(alias, url, path) -> str that produces .md content

    Returns:
        Callback function(success: bool, result: str) -> None
    """

    def _callback(success: bool, result: str) -> None:
        if success:
            try:
                content = generate_fn(repo_alias, repo_url, clone_path)
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(content, encoding="utf-8")
                logger.info(f"Generated description for {repo_alias}: {md_path}")
            except Exception as gen_err:
                logger.warning(
                    f"Description generation failed for {repo_alias}: {gen_err}",
                    exc_info=True,
                )
        else:
            logger.warning(f"CLI work item failed for {repo_alias}: {result}")

    return _callback


def _queue_repo_description(
    alias: str,
    repo_url: str,
    clone_path: str,
    md_file: Path,
    cli_manager,
    generate_fn,
) -> bool:
    """
    Queue description generation for a single repo.

    Returns True if work was successfully submitted, False otherwise.
    Raises on unexpected errors (caller handles per-repo isolation).

    Args:
        alias: Repository alias
        repo_url: Repository URL
        clone_path: Filesystem path to the repo clone
        md_file: Destination .md file path
        cli_manager: ClaudeCliManager instance (may be None)
        generate_fn: Description generation function

    Returns:
        True if work queued, False if cli_manager unavailable
    """
    if cli_manager is None:
        logger.warning(
            f"ClaudeCliManager not initialised; skipping description generation for {alias}"
        )
        return False

    callback = _make_description_callback(alias, md_file, repo_url, clone_path, generate_fn)
    cli_manager.submit_work(repo_path=Path(clone_path), callback=callback)
    return True


@router.post(
    "/generate-missing-descriptions",
    response_model=GenerateMissingDescriptionsResponse,
    responses={
        200: {"description": "Description generation queued successfully"},
        500: {"description": "Server not configured or internal error"},
    },
)
async def generate_missing_descriptions(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> GenerateMissingDescriptionsResponse:
    """
    Queue description generation for all golden repos missing cidx-meta .md files.

    For each golden repo (excluding cidx-meta itself) that does not yet have a
    cidx-meta/{alias}.md file, this endpoint submits an async work item to the
    ClaudeCliManager.  Already-described repos are skipped (idempotent).

    Individual per-repo failures are logged but do not abort processing of the
    remaining repos (AC4).

    Args:
        request: FastAPI request (reads app.state for golden_repos_dir / golden_repo_manager)
        current_user: Authenticated admin user

    Returns:
        GenerateMissingDescriptionsResponse with counts of queued / existing / total

    Raises:
        HTTPException 500: Server not properly initialised
    """
    from code_indexer.global_repos.meta_description_hook import _generate_repo_description

    golden_repos_dir = getattr(request.app.state, "golden_repos_dir", None)
    golden_repo_manager = getattr(request.app.state, "golden_repo_manager", None)

    if not golden_repos_dir or not golden_repo_manager:
        raise HTTPException(
            status_code=500,
            detail="Server not configured: golden_repos_dir or golden_repo_manager not initialised",
        )

    cidx_meta_dir = Path(golden_repos_dir) / "cidx-meta"
    cli_manager = get_claude_cli_manager()

    try:
        all_repos = golden_repo_manager.list_golden_repos()
    except Exception as e:
        logger.error(f"Failed to list golden repos: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list golden repositories: {e}",
        )

    eligible_repos = [r for r in all_repos if r.get("alias") != "cidx-meta"]

    repos_with_descriptions = 0
    repos_queued = 0

    for repo in eligible_repos:
        alias = repo.get("alias", "")
        repo_url = repo.get("repo_url", "")

        # Use get_actual_repo_path() to resolve versioned paths (Epic #211).
        # golden_repos_metadata.clone_path is stale for repos that have been
        # refreshed into .versioned/ directories; this method resolves correctly.
        try:
            clone_path = str(golden_repo_manager.get_actual_repo_path(alias))
        except Exception:
            clone_path = repo.get("clone_path", "")

        # AC3: Skip repos that already have a description file (idempotent)
        md_file = cidx_meta_dir / f"{alias}.md"
        # Prevent path traversal: reject any alias that escapes the cidx-meta dir
        if not md_file.resolve().is_relative_to(cidx_meta_dir.resolve()):
            logger.warning(
                f"Skipping repo with suspicious alias (path traversal): {alias}"
            )
            continue
        if md_file.exists():
            repos_with_descriptions += 1
            continue

        # AC4: Isolate individual failures - one bad repo must not block others
        try:
            queued = _queue_repo_description(
                alias=alias,
                repo_url=repo_url,
                clone_path=clone_path,
                md_file=md_file,
                cli_manager=cli_manager,
                generate_fn=_generate_repo_description,
            )
            if queued:
                repos_queued += 1
        except Exception as e:
            logger.warning(
                f"Failed to queue description generation for {alias}: {e}",
                exc_info=True,
            )

    return GenerateMissingDescriptionsResponse(
        repos_queued=repos_queued,
        repos_with_descriptions=repos_with_descriptions,
        total_repos=len(eligible_repos),
    )
