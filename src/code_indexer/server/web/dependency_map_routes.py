"""
Web routes for Dependency Map page (Story #212, #342).

Provides:
  GET  /admin/dependency-map                          -> Full page
  GET  /admin/partials/depmap-job-status              -> HTMX partial (admin only)
  GET  /admin/partials/depmap-activity-journal        -> HTMX journal partial (Story #329)
  POST /admin/dependency-map/trigger                  -> Trigger analysis (admin only, JSON)
  GET  /admin/dependency-map/health                   -> Health report JSON (Story #342)
  POST /admin/dependency-map/repair                   -> Trigger repair (Story #342, JSON)
"""

import html
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set
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
    # Check if analysis is currently running
    is_active = dep_map_service is not None and not dep_map_service.is_available()
    response.headers["X-Journal-Active"] = "1" if is_active else "0"
    return response


@dependency_map_router.get(
    "/partials/depmap-activity-panel", response_class=HTMLResponse
)
def depmap_activity_panel_partial(request: Request):
    """
    HTMX partial for the full Activity Journal panel (Story #329 fix).

    GET /admin/partials/depmap-activity-panel

    Admin-only. Returns the complete journal panel HTML (container + entries div
    + scripts) when analysis is running, or empty content when idle.

    This endpoint is loaded ONCE by the main dependency_map.html page as a sibling
    to the job-status section. The panel is NOT inside the job-status refresh cycle,
    so it is never destroyed by the 5s outerHTML swap.

    The returned panel contains a polling div that accumulates entries via beforeend,
    preserving all accumulated journal entries across job-status refreshes.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dep_map_service = _get_dep_map_service_from_state()

    is_running = False
    progress = 0
    progress_info = ""

    if dep_map_service is not None:
        try:
            is_running = not dep_map_service.is_available()
        except Exception as e:
            logger.debug("Could not check dep_map_service availability: %s", e)
        progress, progress_info = _get_progress_from_service(dep_map_service)

    return templates.TemplateResponse(
        "partials/depmap_activity_panel.html",
        {
            "request": request,
            "is_running": is_running,
            "progress": progress,
            "progress_info": progress_info,
        },
    )


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

    # Story #342: Merge content health into job status (single source of truth)
    # Content health overrides job health when it's worse than job health
    output_dir = _get_dep_map_output_dir()
    if output_dir is not None:
        from ..services.dep_map_health_detector import DepMapHealthDetector

        try:
            detector = DepMapHealthDetector()
            known_repos = _get_known_repo_names()
            content_report = detector.detect(output_dir, known_repos=known_repos)
            if not content_report.is_healthy:
                if content_report.status == "critical":
                    job_status["health"] = "Critical"
                    job_status["color"] = "RED"
                elif content_report.status == "needs_repair":
                    if content_report.repairable_count > 0:
                        job_status["health"] = "Needs Repair"
                    else:
                        job_status["health"] = "Unhealthy"
                    job_status["color"] = "YELLOW"
                job_status["content_anomaly_count"] = len(content_report.anomalies)
                job_status["content_anomalies"] = [
                    a.to_dict() for a in content_report.anomalies
                ]
        except Exception as e:
            logger.debug("Content health check failed in partial: %s", e)

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


def _get_dep_map_output_dir() -> Optional[Path]:
    """
    Get the dependency map output directory path (Story #342).

    Returns the path to golden-repos/cidx-meta/dependency-map/ or None
    if the directory does not exist or the config is unavailable.
    """
    try:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        config_manager = config_service.config_manager
        golden_repos_dir = Path(config_manager.server_dir) / "data" / "golden-repos"
        output_dir = golden_repos_dir / "cidx-meta" / "dependency-map"
        if output_dir.exists():
            return output_dir
        return None
    except Exception as e:
        logger.warning("Failed to get dep map output dir: %s", e)
        return None


def _get_known_repo_names() -> Optional[Set[str]]:
    """
    Return the set of golden repo names from global_repos table (Story #342 Check 6).

    Used to detect repos not covered by any domain in _domains.json.
    Returns None on any error so the caller can skip Check 6 gracefully.
    """
    import sqlite3

    try:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        config_manager = config_service.config_manager
        server_dir = config_manager.server_dir
        db_path = str(server_dir / "data" / "cidx_server.db")

        conn = sqlite3.connect(db_path)
        try:
            # INNER JOIN excludes orphaned global_repos entries (repos removed from
            # golden_repos_metadata but whose global_repos row was never cleaned up).
            rows = conn.execute(
                "SELECT g.repo_name FROM global_repos g"
                " INNER JOIN golden_repos_metadata m ON g.repo_name = m.alias"
            ).fetchall()
            return {row[0] for row in rows}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to get known repo names for Check 6: %s", e)
        return None


@dependency_map_router.get("/dependency-map/health")
def dependency_map_health(request: Request):
    """
    Health check for dependency map output directory (Story #342).

    GET /admin/dependency-map/health

    Admin-only. Returns JSON with health status, anomalies, and repairable_count.
    Used by UI to drive smart health badge and repair button visibility.
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Admin access required"}, status_code=401
        )

    output_dir = _get_dep_map_output_dir()
    if output_dir is None:
        return JSONResponse(
            content={
                "status": "unknown",
                "anomalies": [],
                "repairable_count": 0,
                "error": "Dependency map service not available",
            },
            status_code=200,
        )

    from ..services.dep_map_health_detector import DepMapHealthDetector

    detector = DepMapHealthDetector()
    known_repos = _get_known_repo_names()
    report = detector.detect(output_dir, known_repos=known_repos)

    return JSONResponse(content=report.to_dict())


def _build_domain_analyzer(dep_map_service, output_dir: Path):
    """
    Build a domain_analyzer callable for DepMapRepairExecutor (Story #342).

    Wraps dep_map_service's Pass 2 per-domain analysis.
    Returns a callable matching RepairExecutor's expected signature:
        (output_dir, domain, domain_list, repo_list) -> bool

    Bug fixes (Story #342):
    - Bug 1: DepMapRepairExecutor always passes [] as repo_list. Capture the
      real repo list from dep_map_service at closure-creation time so Claude
      receives actual repo metadata (aliases, paths, file counts, sizes).
    - Bug 2: Pass output_dir as previous_domain_dir so Claude can see the
      existing (partially correct) domain analysis and improve it rather than
      starting from scratch every time.
    """
    # Bug 1 fix: pre-capture real repo_list at closure-creation time.
    # DepMapRepairExecutor._run_phase1() always passes [] as repo_list.
    # We capture the real list here so the analyzer has proper repo metadata.
    captured_repo_list = []
    try:
        captured_repo_list = dep_map_service._get_activated_repos()
        captured_repo_list = dep_map_service._enrich_repo_sizes(captured_repo_list)
    except Exception as e:
        logger.warning("Failed to gather repo metadata for repair analyzer: %s", e)

    def analyzer(out_dir, domain, domain_list, repo_list):
        try:
            analyzer_obj = getattr(dep_map_service, "_analyzer", None)
            if analyzer_obj is None:
                logger.warning("No analyzer available on dep_map_service")
                return False

            config_manager = getattr(dep_map_service, "_config_manager", None)
            max_turns = 25
            if config_manager:
                try:
                    config = (
                        config_manager.get_config()
                        if hasattr(config_manager, "get_config")
                        else config_manager
                    )
                    max_turns = getattr(config, "dependency_map_pass2_max_turns", 25)
                except Exception:
                    pass

            journal = getattr(dep_map_service, "_activity_journal", None) or getattr(
                dep_map_service, "activity_journal", None
            )
            journal_path = getattr(journal, "journal_path", None)

            # Bug 1 fix: use captured_repo_list when executor passes empty list,
            # but honour a non-empty repo_list if the caller provides one.
            effective_repo_list = captured_repo_list if not repo_list else repo_list

            analyzer_obj.run_pass_2_per_domain(
                staging_dir=out_dir,
                domain=domain,
                domain_list=domain_list,
                repo_list=effective_repo_list,
                max_turns=max_turns,
                # Bug 2 fix: pass output_dir so Claude sees existing analysis
                # files and can improve them rather than starting from scratch.
                previous_domain_dir=output_dir,
                journal_path=journal_path,
            )

            domain_file = out_dir / f"{domain['name']}.md"
            return domain_file.exists() and domain_file.stat().st_size > 0
        except Exception as e:
            logger.warning(
                "Domain analyzer failed for %s: %s", domain.get("name", "?"), e
            )
            return False

    return analyzer


@dependency_map_router.post("/dependency-map/repair")
def trigger_dependency_map_repair(request: Request):
    """
    Trigger dependency map repair (Story #342).

    POST /admin/dependency-map/repair

    Admin-only. Runs health detection, then executes repair for detected anomalies.
    Repair runs in background thread. Returns immediate JSON response.
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Admin access required"}, status_code=401
        )

    dep_map_service = _get_dep_map_service_from_state()
    if dep_map_service is None:
        return JSONResponse(
            content={"error": "Dependency map service not available"},
            status_code=503,
        )

    if not dep_map_service.is_available():
        return JSONResponse(
            content={"error": "Analysis already in progress"},
            status_code=409,
        )

    output_dir = _get_dep_map_output_dir()
    if output_dir is None:
        return JSONResponse(
            content={"error": "Output directory not found"},
            status_code=404,
        )

    def _run_repair():
        # H1 fix (Story #342): acquire lock before doing any work to prevent
        # TOCTOU race between is_available() pre-flight check and repair start.
        if not dep_map_service._lock.acquire(blocking=False):
            logger.warning("Repair aborted: analysis lock unavailable")
            return
        try:
            from ..services.dep_map_health_detector import DepMapHealthDetector
            from ..services.dep_map_index_regenerator import IndexRegenerator
            from ..services.dep_map_repair_executor import DepMapRepairExecutor

            detector = DepMapHealthDetector()
            regenerator = IndexRegenerator()
            domain_analyzer = _build_domain_analyzer(dep_map_service, output_dir)

            journal = getattr(dep_map_service, "_activity_journal", None) or getattr(
                dep_map_service, "activity_journal", None
            )
            journal_cb = journal.log if journal else None

            executor = DepMapRepairExecutor(
                health_detector=detector,
                index_regenerator=regenerator,
                domain_analyzer=domain_analyzer,
                journal_callback=journal_cb,
            )

            known_repos = _get_known_repo_names()
            health_report = detector.detect(output_dir, known_repos=known_repos)
            result = executor.execute(output_dir, health_report)
            logger.info("Repair completed: %s", result.status)
        except Exception as e:
            logger.error("Background repair failed: %s", e)
        finally:
            dep_map_service._lock.release()

    thread = threading.Thread(target=_run_repair, daemon=True)
    thread.start()

    return JSONResponse(
        content={"success": True, "message": "Repair analysis triggered"},
        status_code=202,
    )


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
