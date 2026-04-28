"""
Web routes for Dependency Map page (Story #212, #342, #684).

Provides:
  GET  /admin/dependency-map                                -> Full page
  GET  /admin/partials/depmap-job-status                    -> HTMX partial (admin only)
  POST /admin/partials/depmap-job-status/retry              -> Retry failed dashboard job (Story #684)
  GET  /admin/partials/depmap-activity-journal              -> HTMX journal partial (Story #329)
  POST /admin/dependency-map/trigger                        -> Trigger analysis (admin only, JSON)
  GET  /admin/dependency-map/health                         -> Health report JSON (Story #342)
  POST /admin/dependency-map/repair                         -> Trigger repair (Story #342, JSON)
  POST /admin/dependency-map/trigger-refinement             -> Trigger refinement cycle (Bug #371)
"""

import html
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set
from urllib.parse import quote

from code_indexer import __version__ as _cidx_version
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
# Cache busting: version appended to static asset URLs
templates.env.globals["static_version"] = _cidx_version

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


def _get_dashboard_cache_backend():
    """
    Construct and return a DependencyMapDashboardCacheBackend backed by the server DB.

    Follows the same config-resolution pattern as _get_dashboard_service.
    Returns None if the server configuration is unavailable.
    """
    try:
        from ..services.config_service import get_config_service
        from ..storage.sqlite_backends import DependencyMapDashboardCacheBackend

        config_service = get_config_service()
        server_dir = config_service.config_manager.server_dir
        db_path = str(server_dir / "data" / "cidx_server.db")
        return DependencyMapDashboardCacheBackend(db_path)
    except Exception as e:
        logger.warning("_get_dashboard_cache_backend failed: %s", e)
        return None


def _get_job_tracker():
    """
    Return the global JobTracker instance from app module scope.

    Returns None if unavailable (e.g. during testing without full startup).
    """
    try:
        from ..app import job_tracker

        return job_tracker
    except Exception as e:
        logger.debug("_get_job_tracker failed: %s", e)
        return None


def _get_background_job_manager():
    """
    Return the global BackgroundJobManager instance from app module scope.

    Returns None if unavailable.
    """
    try:
        from ..app import background_job_manager

        return background_job_manager
    except Exception as e:
        logger.debug("_get_background_job_manager failed: %s", e)
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
        # Find the dependency map job (full, delta, or repair)
        for job in active_jobs:
            if job.operation_type in (
                "dependency_map_full",
                "dependency_map_delta",
                "dependency_map_repair",
                "dependency_map_refinement",
                "lifecycle_backfill",
            ):
                return job.progress or 0, job.progress_info or ""
    except Exception as e:
        logger.debug("Could not get progress from job tracker: %s", e)

    return 0, ""


# Default dashboard cache TTL in seconds (10 minutes).
# Used directly by _render_complete_response; override by passing ttl explicitly.
_DASHBOARD_CACHE_TTL_DEFAULT = 600

# Operation type registered with BackgroundJobManager for dashboard analysis jobs
_DASHBOARD_OP_TYPE = "dep_map_dashboard"


def _get_repair_journal_dir() -> Path:
    """Return the repair activity journal directory.

    Honors CIDX_DATA_DIR env var (Bug #879 IPC alignment).
    Falls back to ~/.tmp when CIDX_DATA_DIR is unset.
    """
    import os as _os

    raw = _os.environ.get("CIDX_DATA_DIR", "").strip()
    base = Path(raw) if raw else Path.home() / ".tmp"
    return base / "depmap-repair-journal"


class _NullJobTracker:
    """No-op tracker used as fallback when no real JobTracker is available."""

    def update_status(self, job_id: str, **kwargs) -> None:
        pass

    def get_job(self, job_id: str):
        return None


def _submit_dashboard_job(
    cache_backend, bg_job_manager, dashboard_service, job_tracker
) -> Optional[str]:
    """
    Atomically claim the job slot and submit a background dashboard analysis job.

    Uses cache_backend.claim_job_slot() as a compare-and-swap to coalesce
    concurrent requests onto a single job rather than spawning duplicates.
    Clears the claimed slot if submission fails (exception or falsy return value).

    Args:
        cache_backend: DependencyMapDashboardCacheBackend instance.
        bg_job_manager: BackgroundJobManager instance.
        dashboard_service: DependencyMapDashboardService instance.
        job_tracker: JobTracker instance (may be None; _NullJobTracker used as fallback).

    Returns:
        The job_id string on success, the existing job_id if slot was already taken,
        or None if a required dependency was unavailable or submission failed.
    """
    import uuid

    if bg_job_manager is None or dashboard_service is None or cache_backend is None:
        logger.warning(
            "_submit_dashboard_job: required dependency unavailable "
            "(bg_job_manager=%s, dashboard_service=%s, cache_backend=%s)",
            bg_job_manager is not None,
            dashboard_service is not None,
            cache_backend is not None,
        )
        return None

    new_job_id = str(uuid.uuid4())

    # Atomic claim: returns None on success, existing job_id if slot already taken
    existing = cache_backend.claim_job_slot(new_job_id)
    if existing is not None:
        logger.debug("_submit_dashboard_job: slot already taken by %s", existing)
        return str(existing)

    from ..services.dependency_map_dashboard_job_runner import (
        DependencyMapDashboardJobRunner,
    )

    effective_tracker = job_tracker if job_tracker is not None else _NullJobTracker()
    runner = DependencyMapDashboardJobRunner(
        cache_backend=cache_backend,
        dashboard_service=dashboard_service,
        job_tracker=effective_tracker,
    )

    try:
        submitted_id = bg_job_manager.submit_job(
            _DASHBOARD_OP_TYPE,
            runner.run,
            new_job_id,
            submitter_username="system",
            is_admin=True,
            repo_alias=None,
        )
    except Exception as exc:
        logger.warning("_submit_dashboard_job: submit_job raised: %s", exc)
        cache_backend.clear_job_slot()
        return None

    if not submitted_id:
        logger.warning(
            "_submit_dashboard_job: submit_job returned falsy value %r; clearing slot",
            submitted_id,
        )
        cache_backend.clear_job_slot()
        return None

    return str(submitted_id)


def _render_complete_response(request, session, cached_row: dict) -> HTMLResponse:
    """
    Render the complete dashboard state using depmap_job_status.html.

    Parses result_json from cached_row and applies the Story #342 content
    health merge (same logic as the original synchronous endpoint).

    Args:
        request: FastAPI Request.
        session: Authenticated session object (provides username).
        cached_row: Dict from DependencyMapDashboardCacheBackend.get_cached().

    Returns:
        TemplateResponse rendering partials/depmap_job_status.html.
    """
    import json as _json

    result_json = cached_row.get("result_json") or "{}"
    try:
        job_status = _json.loads(result_json)
    except ValueError as exc:
        logger.warning(
            "_render_complete_response: failed to parse result_json: %s", exc
        )
        job_status = {}

    # Story #342: merge content health — same guard as original endpoint
    output_dir = _get_dep_map_output_dir()
    if output_dir is not None and job_status.get("status") != "running":
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
                    job_status["health"] = (
                        "Needs Repair"
                        if content_report.repairable_count > 0
                        else "Unhealthy"
                    )
                    job_status["color"] = "YELLOW"
                job_status["content_anomaly_count"] = len(content_report.anomalies)
                job_status["content_anomalies"] = [
                    a.to_dict() for a in content_report.anomalies
                ]
                job_status["repairable_count"] = content_report.repairable_count
        except Exception as exc:
            logger.debug(
                "_render_complete_response: content health check failed: %s", exc
            )

    # Story D Bug #874: pre-parse phase_timings_json str -> dict for each run row
    # so the template iterates a real dict rather than needing a Jinja filter.
    for row in job_status.get("run_history", []):
        raw = row.get("phase_timings_json")
        if raw is None:
            # Absent value — no warning; expected for legacy or NULL rows.
            row["phase_timings_parsed"] = None
        else:
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "_render_complete_response: malformed phase_timings_json %r: %s",
                    raw,
                    exc,
                )
                row["phase_timings_parsed"] = None
            else:
                if not isinstance(parsed, dict):
                    logger.warning(
                        "_render_complete_response: phase_timings_json parsed to %s "
                        "(expected dict), ignoring: %r",
                        type(parsed).__name__,
                        raw,
                    )
                    row["phase_timings_parsed"] = None
                else:
                    row["phase_timings_parsed"] = parsed

    return templates.TemplateResponse(
        "partials/depmap_job_status.html",
        {
            "request": request,
            "job_status": job_status,
            "username": session.username,
        },
    )


def _render_computing_response(request, job_id: str, tracker) -> HTMLResponse:
    """
    Render the in-progress computing partial (depmap_job_status_computing.html).

    Reads current progress from the tracker if available; defaults to 0.

    Args:
        request: FastAPI Request.
        job_id: Running job ID embedded in the partial for HTMX polling.
        tracker: JobTracker instance (may be None).

    Returns:
        TemplateResponse rendering partials/depmap_job_status_computing.html.
    """
    progress = 0
    progress_info = ""
    if tracker is not None:
        try:
            job = tracker.get_job(job_id)
            if job is not None:
                progress = getattr(job, "progress", 0) or 0
                progress_info = getattr(job, "progress_info", "") or ""
        except Exception as exc:
            logger.debug(
                "_render_computing_response: tracker.get_job(%r) raised: %s",
                job_id,
                exc,
            )

    return templates.TemplateResponse(
        "partials/depmap_job_status_computing.html",
        {
            "request": request,
            "job_id": job_id,
            "progress": progress,
            "progress_info": progress_info,
        },
    )


def _render_error_response(request, error_message: str) -> HTMLResponse:
    """
    Render the error partial (depmap_job_status_error.html).

    Args:
        request: FastAPI Request.
        error_message: Human-readable failure description shown to the admin.

    Returns:
        TemplateResponse rendering partials/depmap_job_status_error.html.
    """
    return templates.TemplateResponse(
        "partials/depmap_job_status_error.html",
        {
            "request": request,
            "error_message": error_message,
        },
    )


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
    HTMX partial for Dependency Map job status section (Story #684 async state machine).

    GET /admin/partials/depmap-job-status[?job_id=<id>]

    Admin-only. Routes through four states in order:
      STATE 1: Fresh cache -> renders complete template with cached result.
      STATE 2: job_id param present -> polls that specific job and routes by status.
      STATE 3: Any in-flight job running -> renders computing partial.
      STATE 4: No cache, no job -> submits background job, returns computing partial.

    STATE 2 is evaluated before STATE 3 so an explicit poll is never short-circuited
    by an unrelated in-flight job.  Content health merge (Story #342) is applied
    inside _render_complete_response.  Error partial is returned if STATE 4 fails.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    cache_backend = _get_dashboard_cache_backend()
    tracker = _get_job_tracker()

    # STATE 1: fresh cache available
    if cache_backend is not None and cache_backend.is_fresh(
        _DASHBOARD_CACHE_TTL_DEFAULT
    ):
        cached = cache_backend.get_cached()
        if cached is not None:
            return _render_complete_response(request, session, cached)

    # STATE 2: caller is polling a specific job (must be checked before STATE 3)
    job_id = request.query_params.get("job_id")
    if job_id:
        job = tracker.get_job(job_id) if tracker is not None else None
        if job is not None:
            if job.status == "completed":
                cached = (
                    cache_backend.get_cached() if cache_backend is not None else None
                )
                if cached is not None:
                    return _render_complete_response(request, session, cached)
            elif job.status == "failed":
                error = getattr(job, "error", None) or "Unknown error"
                return _render_error_response(request, error)
            elif job.status in ("running", "pending"):
                return _render_computing_response(request, job_id, tracker)

    # STATE 3: any in-flight job running (generic, no specific job_id from caller)
    running_job_id = (
        cache_backend.get_running_job_id(tracker) if cache_backend is not None else None
    )
    if running_job_id:
        return _render_computing_response(request, running_job_id, tracker)

    # STATE 4: no cache, no in-flight job — submit a new background job
    bg_manager = _get_background_job_manager()
    dashboard_service = _get_dashboard_service()
    new_job_id = _submit_dashboard_job(
        cache_backend, bg_manager, dashboard_service, tracker
    )
    if new_job_id:
        return _render_computing_response(request, new_job_id, tracker)

    # Submission failed: async infrastructure unavailable
    logger.warning(
        "depmap_job_status_partial: failed to submit dashboard job "
        "(cache_backend=%s, bg_manager=%s, dashboard_service=%s)",
        cache_backend is not None,
        bg_manager is not None,
        dashboard_service is not None,
    )
    return _render_error_response(
        request, "Dashboard analysis infrastructure unavailable"
    )


@dependency_map_router.post(
    "/partials/depmap-job-status/retry", response_class=HTMLResponse
)
def depmap_job_status_retry(request: Request):
    """
    Retry endpoint for failed dashboard background jobs (Story #684).

    POST /admin/partials/depmap-job-status/retry

    Admin-only. Clears the failed/stuck job slot, then delegates to the
    GET handler which will submit a new background job.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    cache_backend = _get_dashboard_cache_backend()
    if cache_backend is not None:
        cache_backend.clear_job_slot_for_retry()

    return depmap_job_status_partial(request)


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
    try:
        from ..services.config_service import get_config_service
        from ..storage.database_manager import DatabaseConnectionManager

        config_service = get_config_service()
        config_manager = config_service.config_manager
        server_dir = config_manager.server_dir
        db_path = str(server_dir / "data" / "cidx_server.db")

        conn = DatabaseConnectionManager.get_instance(db_path).get_connection()
        # INNER JOIN excludes orphaned global_repos entries (repos removed from
        # golden_repos_metadata but whose global_repos row was never cleaned up).
        rows = conn.execute(
            "SELECT g.repo_name FROM global_repos g"
            " INNER JOIN golden_repos_metadata m ON g.repo_name = m.alias"
        ).fetchall()
        return {row[0] for row in rows}
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
        return JSONResponse(content={"error": "Admin access required"}, status_code=401)

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
            if config_manager is None:
                logger.warning("Repair analyzer: no config_manager — aborting")
                return False
            try:
                ci_config = config_manager.get_claude_integration_config()
            except Exception as e:
                logger.error(
                    "Repair analyzer: failed to load claude_integration_config: %s", e
                )
                return False
            max_turns = ci_config.dependency_map_pass2_max_turns

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


def _build_repair_executor(dep_map_service, output_dir: Path, activity_journal):
    """
    Build a DepMapRepairExecutor wired to the service's journal and job tracker (Story #352).

    Constructs the executor with:
      - Real DepMapHealthDetector and IndexRegenerator
      - Domain analyzer closure that uses the initialized journal_path (AC4)
      - journal_callback wired to activity_journal.log
      - progress_callback that updates dep_map_service._job_tracker progress

    Args:
        dep_map_service: Live DependencyMapService instance.
        output_dir: Dependency map output directory.
        activity_journal: ActivityJournalService instance (already initialized).

    Returns:
        Configured DepMapRepairExecutor instance.
    """
    from ..services.config_service import get_config_service
    from ..services.dep_map_health_detector import DepMapHealthDetector
    from ..services.dep_map_index_regenerator import IndexRegenerator
    from ..services.dep_map_repair_executor import DepMapRepairExecutor
    from .routes import _get_golden_repo_manager
    from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

    # Read bootstrap flags from ServerConfig (bootstrap-only, never DB).
    _cfg = get_config_service().get_config()
    enable_graph_channel_repair: bool = bool(_cfg.enable_graph_channel_repair)

    detector = DepMapHealthDetector()
    regenerator = IndexRegenerator()
    domain_analyzer = _build_domain_analyzer(dep_map_service, output_dir)

    journal_cb = getattr(activity_journal, "log", None)

    # progress_callback: forward milestone to job tracker's active repair job
    job_tracker = getattr(dep_map_service, "_job_tracker", None)

    def _progress_cb(progress: int, info: str = "") -> None:
        if job_tracker is None:
            return
        try:
            active_jobs = job_tracker.get_active_jobs()
            for job in active_jobs:
                if job.operation_type == "dependency_map_repair":
                    job_tracker.update_status(
                        job.job_id, progress=progress, progress_info=info
                    )
                    break
        except Exception as e:
            logger.debug("progress_cb failed to update job tracker: %s", e)

    golden_repo_manager = _get_golden_repo_manager()

    return DepMapRepairExecutor(
        health_detector=detector,
        index_regenerator=regenerator,
        domain_analyzer=domain_analyzer,
        journal_callback=journal_cb,
        progress_callback=_progress_cb,
        enable_graph_channel_repair=enable_graph_channel_repair,
        invoke_claude_fn=invoke_claude_cli,
        repo_path_resolver=golden_repo_manager.get_actual_repo_path,
        graph_repair_self_loop=_cfg.graph_repair_self_loop,
        graph_repair_malformed_yaml=_cfg.graph_repair_malformed_yaml,
        graph_repair_garbage_domain=_cfg.graph_repair_garbage_domain,
        graph_repair_bidirectional_mismatch=_cfg.graph_repair_bidirectional_mismatch,
    )


def _repair_execute(dep_map_service, output_dir: Path, activity_journal) -> bool:
    """Run repair executor against the health report. Returns True on success."""
    try:
        executor = _build_repair_executor(dep_map_service, output_dir, activity_journal)
        from ..services.dep_map_health_detector import DepMapHealthDetector

        detector = DepMapHealthDetector()
        health_report = detector.detect(output_dir, known_repos=_get_known_repo_names())
        result = executor.execute(output_dir, health_report)
        logger.info("Repair completed: %s", result.status)
        return True
    except Exception as e:
        logger.error("Repair with feedback failed: %s", e)
        return False


def _execute_repair_body(
    job_id: str,
    output_dir: Path,
    tracking_backend,
    job_tracker,
    activity_journal,
    dep_map_service=None,
) -> None:
    """Run repair for a pre-claimed job_id (Story #927).

    Does NOT call register_job — caller must have already claimed job_id.
    Used by _run_repair_with_feedback and the auto-repair path via repair_invoker_fn.
    """
    if job_tracker is not None:
        try:
            job_tracker.update_status(job_id, status="running")
        except Exception as e:
            logger.warning("Failed to transition repair job to running: %s", e)
    if tracking_backend is not None:
        try:
            tracking_backend.update_tracking(status="running", error_message=None)
        except Exception as e:
            logger.warning("Failed to update tracking backend to running: %s", e)
    success = _repair_execute(dep_map_service, output_dir, activity_journal)
    terminal_status = "completed" if success else "failed"
    if tracking_backend is not None:
        try:
            tracking_backend.update_tracking(
                status=terminal_status,
                error_message=None if success else "Repair failed",
            )
        except Exception as e:
            logger.warning(
                "Failed to update tracking backend to %s: %s", terminal_status, e
            )
    if job_tracker is not None:
        try:
            if success:
                job_tracker.complete_job(job_id)
            else:
                job_tracker.fail_job(job_id, error="Repair failed")
        except Exception as e:
            logger.warning("Failed to update job tracker on %s: %s", terminal_status, e)
    if activity_journal is not None:
        try:
            activity_journal.clear()
        except Exception as e:
            logger.warning("Failed to finalize repair activity journal: %s", e)


def _run_repair_with_feedback(
    output_dir: Path,
    tracking_backend,
    job_tracker,
    activity_journal,
    dep_map_service=None,
) -> None:
    """Execute repair with full job feedback (Story #352, AC1-AC5).

    AC2: activity journal initialized in repair-specific directory.
    AC3: job registered then delegated to _execute_repair_body (Story #927).
    AC1/AC4/AC5: handled inside _execute_repair_body.
    """
    import uuid as _uuid

    # AC2: Initialize journal in a repair-specific directory
    journal_dir = _get_repair_journal_dir()
    if activity_journal is not None:
        try:
            journal_dir.mkdir(parents=True, exist_ok=True)
            activity_journal.init(journal_dir)
        except Exception as e:
            logger.warning("Failed to initialize repair activity journal: %s", e)

    # AC3: Register job in job tracker
    job_id = str(_uuid.uuid4())
    if job_tracker is not None:
        try:
            job_tracker.register_job(
                job_id,
                "dependency_map_repair",
                username="system",
                repo_alias="server",
            )
        except Exception as e:
            logger.warning("Failed to register repair job in job tracker: %s", e)

    _execute_repair_body(
        job_id=job_id,
        output_dir=output_dir,
        tracking_backend=tracking_backend,
        job_tracker=job_tracker,
        activity_journal=activity_journal,
        dep_map_service=dep_map_service,
    )


@dependency_map_router.post("/dependency-map/repair")
def trigger_dependency_map_repair(request: Request):
    """
    Trigger dependency map repair (Story #342, #352).

    POST /admin/dependency-map/repair

    Admin-only. Runs health detection, then executes repair for detected anomalies.
    Repair runs in background thread. Returns immediate JSON response.
    Story #352: Provides same job feedback as Full Analysis and Delta Refresh
    (tracking backend status, activity journal, job tracker progress milestones).
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(content={"error": "Admin access required"}, status_code=401)

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

    # Resolve tracking backend and job tracker from dep_map_service
    tracking_backend = getattr(dep_map_service, "_tracking_backend", None)
    job_tracker = getattr(dep_map_service, "_job_tracker", None)
    activity_journal = getattr(dep_map_service, "_activity_journal", None) or getattr(
        dep_map_service, "activity_journal", None
    )

    def _run_repair():
        # H1 fix (Story #342): acquire lock before doing any work to prevent
        # TOCTOU race between is_available() pre-flight check and repair start.
        if not dep_map_service._lock.acquire(blocking=False):
            logger.warning("Repair aborted: analysis lock unavailable")
            return
        try:
            _run_repair_with_feedback(
                output_dir=output_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                activity_journal=activity_journal,
                dep_map_service=dep_map_service,
            )
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


@dependency_map_router.post("/dependency-map/trigger-refinement")
def trigger_refinement(request: Request):
    """
    Trigger a manual refinement cycle (Bug #371).

    POST /admin/dependency-map/trigger-refinement

    Admin-only. Returns JSON with success or error.
    Refinement runs in a background daemon thread with job tracking.
    """
    import uuid as _uuid

    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Admin access required"},
            status_code=401,
        )

    dep_map_service = _get_dep_map_service_from_state()
    if dep_map_service is None:
        return JSONResponse(
            content={
                "error": "Dependency map service not available (disabled or not initialized)"
            },
            status_code=503,
        )

    if not dep_map_service.is_available():
        return JSONResponse(
            content={"error": "Analysis already in progress"},
            status_code=409,
        )

    job_id = f"dep-map-refinement-{_uuid.uuid4().hex[:8]}"

    def _run_refinement():
        try:
            dep_map_service.run_tracked_refinement(job_id)
        except Exception as e:
            logger.error("Background refinement failed: %s", e)

    thread = threading.Thread(target=_run_refinement, daemon=True)
    thread.start()

    return JSONResponse(
        content={"success": True, "message": "Refinement triggered", "job_id": job_id},
        status_code=202,
    )
