"""
Web routes for Dependency Map page (Story #212).

Provides:
  GET  /admin/dependency-map                          -> Full page
  GET  /admin/partials/depmap-job-status              -> HTMX partial (admin only)
  GET  /admin/partials/depmap-activity-journal        -> HTMX journal partial (Story #329)
  POST /admin/dependency-map/trigger                  -> Trigger analysis (admin only, JSON)
"""

import html
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .routes import (
    _require_admin_session,
    _create_login_redirect,
)

logger = logging.getLogger(__name__)

# Templates directory (same as main routes)
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _get_server_time_for_template() -> str:
    """Get current server time in ISO format for template."""
    return datetime.now(timezone.utc).isoformat()


templates.env.globals["get_server_time"] = _get_server_time_for_template

# Router for dependency map pages
dependency_map_router = APIRouter(tags=["dependency-map-web"])


def _get_dashboard_service():
    """
    Build DependencyMapDashboardService from live server state.

    Returns a configured DependencyMapDashboardService or None if
    the dependency map service is not available (disabled or not initialized).
    """
    from ..services.dependency_map_dashboard_service import (
        DependencyMapDashboardService,
    )
    from ..services.config_service import get_config_service

    try:
        config_service = get_config_service()
        config_manager = config_service.config_manager

        # Get tracking backend
        server_dir = config_manager.server_dir
        db_path = str(server_dir / "data" / "cidx_server.db")

        from ..storage.sqlite_backends import DependencyMapTrackingBackend

        tracking_backend = DependencyMapTrackingBackend(db_path)

        # Get dependency map service from app state (may be None if disabled)
        dep_map_service = _get_dep_map_service_from_state()

        return DependencyMapDashboardService(
            tracking_backend=tracking_backend,
            config_manager=config_service,
            dependency_map_service=dep_map_service,
        )
    except Exception as e:
        logger.warning("Failed to build DependencyMapDashboardService: %s", e)
        return None


def _get_dep_map_service_from_state():
    """Get DependencyMapService from app state, returning None if unavailable."""
    try:
        from ..app import app  # noqa: F401 - importing app to access state

        dep_map_service = getattr(app.state, "dependency_map_service", None)
        return dep_map_service
    except Exception as e:
        logger.debug("Could not get dep_map_service from state: %s", e)
        return None


def _get_job_status_data() -> dict:
    """
    Get job status dict for template rendering.

    Returns a safe default dict if service is unavailable.
    """
    dashboard_service = _get_dashboard_service()
    if dashboard_service is None:
        return {
            "health": "Disabled",
            "color": "GRAY",
            "status": "unknown",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }

    try:
        return dashboard_service.get_job_status()  # type: ignore[no-any-return]
    except Exception as e:
        logger.warning("Failed to get job status: %s", e)
        return {
            "health": "Unhealthy",
            "color": "RED",
            "status": "error",
            "last_run": None,
            "next_run": None,
            "error_message": str(e),
            "run_history": [],
        }


def _render_journal_html(content: str) -> str:
    """
    Convert journal markdown content to HTML fragments (Story #329).

    Converts each non-empty line into a <div class="journal-entry"> element.
    Markdown conversions applied per line:
      **text** -> <strong>text</strong>
      `text`   -> <code>text</code>

    Args:
        content: Raw journal text content (may contain markdown).

    Returns:
        HTML string with one div per non-empty line, or empty string if no content.
    """
    if not content or not content.strip():
        return ""

    lines = content.strip().split("\n")
    html_parts = []
    for line in lines:
        if not line.strip():
            continue
        # HTML-escape FIRST to prevent XSS - raw content must never reach the browser
        escaped = html.escape(line)
        # Then apply markdown conversions on the already-safe escaped content
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        # Convert `text` to <code>text</code>
        rendered = re.sub(r"`(.+?)`", r"<code>\1</code>", rendered)
        html_parts.append(f'<div class="journal-entry">{rendered}</div>')

    return "\n".join(html_parts)


def _get_progress_from_service(dep_map_service) -> tuple:
    """
    Extract current progress and progress_info from the dep_map_service job tracker.

    Returns:
        Tuple of (progress: int, progress_info: str).
        Defaults to (0, "") when service or tracker is unavailable.
    """
    if dep_map_service is None:
        return 0, ""

    try:
        job_tracker = getattr(dep_map_service, "_job_tracker", None)
        if job_tracker is None:
            return 0, ""
        active_jobs = job_tracker.get_active_jobs()
        # Find the dependency map job (full or delta)
        for job in active_jobs:
            if job.operation_type in ("dependency_map_full", "dependency_map_delta"):
                return job.progress or 0, job.progress_info or ""
    except Exception as e:
        logger.debug("Could not get progress from job tracker: %s", e)

    return 0, ""


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@dependency_map_router.get(
    "/partials/depmap-activity-journal", response_class=HTMLResponse
)
def depmap_activity_journal_partial(request: Request, offset: int = 0):
    """
    HTMX partial for activity journal incremental content (Story #329).

    GET /admin/partials/depmap-activity-journal?offset=N

    Admin-only. Returns new journal entries since byte offset as HTML.
    Response headers:
      X-Journal-Offset      - new byte offset for next poll
      X-Journal-Progress    - current progress 0-100
      X-Journal-Progress-Info - human-readable progress description
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dep_map_service = _get_dep_map_service_from_state()

    content = ""
    new_offset = 0
    progress = 0
    progress_info = ""

    if dep_map_service is not None:
        journal = dep_map_service.activity_journal
        content, new_offset = journal.get_content(offset)
        progress, progress_info = _get_progress_from_service(dep_map_service)

    html_content = _render_journal_html(content)

    response = HTMLResponse(content=html_content)
    response.headers["X-Journal-Offset"] = str(new_offset)
    response.headers["X-Journal-Progress"] = str(progress)
    response.headers["X-Journal-Progress-Info"] = (
        quote(progress_info) if progress_info else ""
    )
    return response


@dependency_map_router.get("/dependency-map", response_class=HTMLResponse)
def dependency_map_page(request: Request):
    """
    Dependency Map main page.

    GET /admin/dependency-map

    Requires authenticated session (admin or non-admin).
    Job Status section is only included in response for admin users.
    """
    from .auth import get_session_manager

    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return _create_login_redirect(request)

    is_admin = session.role == "admin"

    return templates.TemplateResponse(
        "dependency_map.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "dependency-map",
            "show_nav": True,
            "is_admin": is_admin,
        },
    )


@dependency_map_router.get("/partials/depmap-job-status", response_class=HTMLResponse)
def depmap_job_status_partial(request: Request):
    """
    HTMX partial for Dependency Map job status section.

    GET /admin/partials/depmap-job-status

    Admin-only. Returns HTML fragment with health badge, timestamps, error banner,
    and Run Now dropdown. HTMX auto-refresh every 5s when status=running.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    job_status = _get_job_status_data()

    return templates.TemplateResponse(
        "partials/depmap_job_status.html",
        {
            "request": request,
            "job_status": job_status,
            "username": session.username,
        },
    )


@dependency_map_router.get(
    "/partials/depmap-repo-coverage", response_class=HTMLResponse
)
def depmap_repo_coverage_partial(request: Request):
    """
    HTMX partial for Dependency Map repository coverage section (Story #213).

    GET /admin/partials/depmap-repo-coverage

    Requires authenticated session (admin or non-admin).
    Admin sees all repos; non-admin sees only accessible repos.
    Returns HTML fragment with coverage table, progress bar, legend, refresh button.
    """
    from .auth import get_session_manager

    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return HTMLResponse(content="", status_code=401)

    is_admin = session.role == "admin"

    # Resolve accessible repos for non-admin users
    accessible_repos = None  # None = admin (all repos)
    if not is_admin:
        try:
            accessible_repos = _get_accessible_repos_for_user(session.username)
        except Exception as e:
            logger.warning(
                "Failed to get accessible repos for %s: %s", session.username, e
            )
            accessible_repos = set()

    # Get coverage data
    coverage_data = _get_repo_coverage_data(accessible_repos)

    return templates.TemplateResponse(
        "partials/depmap_repo_coverage.html",
        {
            "request": request,
            "coverage": coverage_data,
            "username": session.username,
            "is_admin": is_admin,
        },
    )


def _get_domain_service():
    """
    Build DependencyMapDomainService from live server state.

    Returns a configured DependencyMapDomainService or None if
    the dependency map service is not available (disabled or not initialized).
    """
    from ..services.dependency_map_domain_service import DependencyMapDomainService
    from ..services.config_service import get_config_service

    try:
        config_service = get_config_service()
        config_manager = config_service.config_manager
        dep_map_service = _get_dep_map_service_from_state()
        if dep_map_service is None:
            return None
        return DependencyMapDomainService(dep_map_service, config_manager)
    except Exception as e:
        logger.warning("Failed to build DependencyMapDomainService: %s", e)
        return None


def _get_accessible_repos_for_user(username: str):
    """Get the set of repos accessible to a non-admin user."""
    from ..services.access_filtering_service import AccessFilteringService
    from ..services.group_access_manager import GroupAccessManager
    from ..services.config_service import get_config_service

    config_service = get_config_service()
    config_manager = config_service.config_manager
    server_dir = config_manager.server_dir
    db_path = str(server_dir / "data" / "cidx_server.db")

    group_manager = GroupAccessManager(db_path)  # type: ignore[arg-type]
    access_service = AccessFilteringService(group_manager)
    return access_service.get_accessible_repos(username)


def _get_repo_coverage_data(accessible_repos) -> dict:
    """
    Get repo coverage dict for template rendering.

    Returns safe default dict if service is unavailable.
    """
    dashboard_service = _get_dashboard_service()
    if dashboard_service is None:
        return {
            "repos": [],
            "coverage_pct": 0.0,
            "covered_count": 0,
            "total_count": 0,
            "coverage_color": "red",
        }

    try:
        return dashboard_service.get_repo_coverage(accessible_repos=accessible_repos)  # type: ignore[no-any-return]
    except Exception as e:
        logger.warning("Failed to get repo coverage: %s", e)
        return {
            "repos": [],
            "coverage_pct": 0.0,
            "covered_count": 0,
            "total_count": 0,
            "coverage_color": "red",
        }


@dependency_map_router.get(
    "/partials/depmap-domain-explorer", response_class=HTMLResponse
)
def depmap_domain_explorer_partial(request: Request):
    """
    HTMX partial for Dependency Map domain explorer section (Story #214).

    GET /admin/partials/depmap-domain-explorer

    Requires authenticated session (admin or non-admin).
    Admin sees all domains; non-admin sees only accessible domains (filtered by repo access).
    Returns HTML fragment with two-panel domain list and detail placeholder.
    """
    from .auth import get_session_manager

    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return HTMLResponse(content="", status_code=401)

    is_admin = session.role == "admin"

    # Resolve accessible repos for non-admin users
    accessible_repos = None  # None = admin (all repos)
    if not is_admin:
        try:
            accessible_repos = _get_accessible_repos_for_user(session.username)
        except Exception as e:
            logger.warning(
                "Failed to get accessible repos for %s: %s", session.username, e
            )
            accessible_repos = set()

    # Get domain list data
    domain_service = _get_domain_service()
    if domain_service is None:
        domain_data = {"domains": [], "total_count": 0}
    else:
        try:
            domain_data = domain_service.get_domain_list(accessible_repos)
        except Exception as e:
            logger.warning("Failed to get domain list: %s", e)
            domain_data = {"domains": [], "total_count": 0}

    return templates.TemplateResponse(
        "partials/depmap_domain_explorer.html",
        {
            "request": request,
            "domains": domain_data,
            "is_admin": is_admin,
        },
    )


@dependency_map_router.get(
    "/partials/depmap-domain-detail/{name}", response_class=HTMLResponse
)
def depmap_domain_detail_partial(request: Request, name: str):
    """
    HTMX partial for Dependency Map domain detail section (Story #214).

    GET /admin/partials/depmap-domain-detail/{name}

    Requires authenticated session (admin or non-admin).
    Returns HTML fragment with domain detail card, or 'Domain not found' fallback.
    """
    from .auth import get_session_manager

    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return HTMLResponse(content="", status_code=401)

    is_admin = session.role == "admin"

    # Resolve accessible repos for non-admin users
    accessible_repos = None  # None = admin (all repos)
    if not is_admin:
        try:
            accessible_repos = _get_accessible_repos_for_user(session.username)
        except Exception as e:
            logger.warning(
                "Failed to get accessible repos for %s: %s", session.username, e
            )
            accessible_repos = set()

    # Get domain detail data
    domain_service = _get_domain_service()
    if domain_service is None:
        detail_data = None
    else:
        try:
            detail_data = domain_service.get_domain_detail(name, accessible_repos)
        except Exception as e:
            logger.warning("Failed to get domain detail for '%s': %s", name, e)
            detail_data = None

    return templates.TemplateResponse(
        "partials/depmap_domain_detail.html",
        {
            "request": request,
            "domain": detail_data,
        },
    )


@dependency_map_router.get("/dependency-map/graph-data")
def depmap_graph_data(request: Request):
    """
    Graph data JSON endpoint for D3.js visualization (Story #215 AC7).

    GET /admin/dependency-map/graph-data

    Requires authenticated session. Returns JSON with nodes and edges,
    filtered by access for non-admin users.
    """
    from .auth import get_session_manager

    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        return JSONResponse(
            content={"error": "Authentication required"}, status_code=401
        )

    is_admin = session.role == "admin"

    accessible_repos = None
    if not is_admin:
        try:
            accessible_repos = _get_accessible_repos_for_user(session.username)
        except Exception as e:
            logger.warning(
                "Failed to get accessible repos for %s: %s", session.username, e
            )
            accessible_repos = set()

    domain_service = _get_domain_service()
    if domain_service is None:
        return JSONResponse(content={"nodes": [], "edges": []})

    try:
        graph_data = domain_service.get_graph_data(accessible_repos)
        return JSONResponse(content=graph_data)
    except Exception as e:
        logger.warning("Failed to get graph data: %s", e)
        return JSONResponse(content={"nodes": [], "edges": []})


@dependency_map_router.post("/dependency-map/trigger")
def trigger_dependency_map(
    request: Request,
    mode: Optional[str] = Form(None),
):
    """
    Trigger dependency map analysis.

    POST /admin/dependency-map/trigger

    Admin-only. Returns JSON with success or error.
    Pre-flight availability check via is_available() prevents concurrent run start.
    Analysis runs in a background daemon thread.

    Args:
        mode: "full" or "delta"
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Admin access required"},
            status_code=401,
        )

    # Validate mode
    if mode not in ("full", "delta"):
        return JSONResponse(
            content={"error": f"Invalid mode '{mode}'. Must be 'full' or 'delta'."},
            status_code=400,
        )

    dep_map_service = _get_dep_map_service_from_state()
    if dep_map_service is None:
        return JSONResponse(
            content={
                "error": "Dependency map service not available (disabled or not initialized)"
            },
            status_code=503,
        )

    # Pre-flight check: prevent concurrent run start using is_available()
    if not dep_map_service.is_available():
        return JSONResponse(
            content={"error": "Analysis already in progress"},
            status_code=409,
        )

    if mode == "full":

        def _run_full():
            try:
                dep_map_service.run_full_analysis()
            except Exception as e:
                logger.error("Background full analysis failed: %s", e)

        thread = threading.Thread(target=_run_full, daemon=True)
        thread.start()
        return JSONResponse(
            content={"success": True, "message": "Full analysis triggered"},
            status_code=202,
        )
    else:  # delta

        def _run_delta():
            try:
                dep_map_service.run_delta_analysis()
            except Exception as e:
                logger.error("Background delta analysis failed: %s", e)

        thread = threading.Thread(target=_run_delta, daemon=True)
        thread.start()
        return JSONResponse(
            content={"success": True, "message": "Delta refresh triggered"},
            status_code=202,
        )
