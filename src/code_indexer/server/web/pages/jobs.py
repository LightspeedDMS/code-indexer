"""
Admin background job pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_background_job_manager,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
)

web_router = APIRouter()


def _get_job_tracker():
    """Get the JobTracker instance (Bug #736: needed to show dep-map jobs on Jobs tab).

    Returns None gracefully when job_tracker is not set in the app module
    (e.g. in tests or before server lifespan initialises it).
    """
    try:
        from ...app import job_tracker

        return job_tracker
    except Exception as e:
        logger.error(
            format_error_log("STORE-GENERAL-030", f"Failed to get job tracker: {e}"),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        return None


def _apply_job_filters(
    jobs: list,
    status_filter: Optional[str],
    type_filter: Optional[str],
    search: Optional[str],
) -> list:
    """Apply status/type/search filters to a list of job dicts (in-Python).

    Mirrors the filter semantics BackgroundJobManager applies internally so that
    JobTracker-only jobs are subject to the same criteria.
    """
    from code_indexer.server.storage.shared.job_status_filter import (
        statuses_for_filter,
    )

    result = []
    for job in jobs:
        if status_filter and job.get("status") not in statuses_for_filter(
            status_filter
        ):
            continue
        if type_filter and job.get("operation_type") != type_filter:
            continue
        if search:
            search_lower = search.lower()
            repo = (job.get("repo_alias") or "").lower()
            user = (job.get("username") or "").lower()
            op = (job.get("operation_type") or "").lower()
            err = (job.get("error") or "").lower()
            job_id = (job.get("job_id") or "").lower()
            if not (
                search_lower in repo
                or search_lower in user
                or search_lower in op
                or search_lower in err
                or search_lower in job_id
            ):
                continue
        result.append(job)
    return result


def _get_all_jobs(
    status_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    is_admin: bool = False,  # H1: changed from True — callers pass is_admin explicitly
):
    """
    Get all jobs with filters and pagination.

    Story #271: Delegates to BackgroundJobManager.get_jobs_for_display() for the
    primary job list.

    Bug #736: Also merges in JobTracker-only jobs (e.g. dependency_map_full /
    dependency_map_delta) that are not tracked by BackgroundJobManager.
    When a JobTracker instance is available:
    1. Fetch the BG page window for the current page from the BG manager.
    2. Fetch and filter all JobTracker jobs with the same criteria.
    3. Deduplicate: for each tracker job, check if the BG manager knows about
       it via a targeted per-ID existence check (bounded by tracker set size,
       typically < 20 jobs).  BG manager record wins for shared IDs.
    4. Compute total_count = bg_total + len(tracker-only extras).
    5. Paginate: use a unified global ordering (BG jobs first, then extras).
       For pages straddling the BG/extras boundary, combine the partial BG
       page with extra slots filled from the extras list.

    AC11: is_admin parameter — when True (callers must opt-in explicitly,
    default is False), bypasses per-user scoping so admin sees all users'
    jobs. The /jobs page route passes True because it is admin-only.

    Returns jobs from both tracking systems with consistent filtering and
    pagination.
    """
    from code_indexer.server.repositories.background_jobs import (
        MAX_PAGE_SIZE,
    )

    page_size = min(page_size, MAX_PAGE_SIZE)

    job_manager = _get_background_job_manager()
    if not job_manager:
        return [], 0, 1

    job_tracker = _get_job_tracker()
    if job_tracker is None:
        # Fast path: no tracker available, delegate entirely to BG manager.
        return job_manager.get_jobs_for_display(
            status_filter=status_filter,
            type_filter=type_filter,
            search_text=search,
            page=page,
            page_size=page_size,
            is_admin=is_admin,
        )

    # Step 1: fetch the BG page window for the requested page.
    # get_jobs_for_display() uses COUNT(*) for bg_total — exact, no side-effects.
    bg_page_jobs, bg_total, bg_total_pages = job_manager.get_jobs_for_display(
        status_filter=status_filter,
        type_filter=type_filter,
        search_text=search,
        page=page,
        page_size=page_size,
        is_admin=is_admin,
    )

    # Step 2: fetch tracker jobs (small bounded set — recent dep_map jobs only).
    tracker_jobs = _apply_job_filters(
        job_tracker.get_recent_jobs(),
        status_filter,
        type_filter,
        search,
    )

    # Step 3: deduplicate — exclude tracker jobs whose job_id appears in ANY BG
    # row, not just the current page.  Building only the current page's id set
    # causes cross-page duplicates: a tracker job whose id is a BG job on page 1
    # would appear again as an "extra" on page 2.
    #
    # Fix: one bounded id-only query via list_display_job_ids() returns the full
    # set of matching job_ids in a SINGLE DB round-trip, regardless of how many
    # pages the result set spans.  At 14k jobs/day × 30-day retention the table
    # holds ~420k rows; list_display_job_ids applies a 50,000-row safety cap.
    # This is O(1) queries — never O(bg_total_pages).
    #
    # username=None here because _get_all_jobs is called from the admin /jobs
    # route with is_admin=True, so the dedup id set must span all users.  The
    # username passed to get_jobs_for_display above is already scoped via
    # is_admin, ensuring the page window and id set use the same effective scope.
    #
    # We do NOT use job_manager.get_job_status() per tracker job because that
    # method also finds tracker-only rows stored in background_jobs and would
    # wrongly exclude them (Bug #736 / Bug #1063 context).
    bg_id_set = job_manager.list_display_job_ids(
        status_filter=status_filter,
        type_filter=type_filter,
        search_text=search,
        username=None,
    )
    # Union with active/in-memory ids from the current page (bg_page_jobs may
    # include jobs that are still in memory and not yet in the DB id set).
    all_bg_display_ids: set = bg_id_set | {j["job_id"] for j in bg_page_jobs}

    extra_jobs = [j for j in tracker_jobs if j["job_id"] not in all_bg_display_ids]

    # Step 4: compute total and total_pages using the unified global ordering.
    total_count = bg_total + len(extra_jobs)
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    # Step 5: paginate using unified global offset/length.
    # Global ordering: BG jobs occupy positions 0..bg_total-1;
    # tracker-only extras occupy positions bg_total..total_count-1.
    #
    # For the requested page:
    #   global_start = (page-1) * page_size
    #   global_end   = global_start + page_size
    #
    # BG contribution to this page:  positions global_start..min(global_end, bg_total)-1
    #   → already returned by bg_page_jobs (exact window from get_jobs_for_display)
    #
    # Extras contribution to this page: positions max(0, global_start-bg_total)
    #   ..max(0, global_end-bg_total)-1 within the extras list.
    #
    # This handles three cases correctly:
    #   (a) Page is entirely within BG range: extras slice is empty → use bg_page_jobs.
    #   (b) Page straddles BG/extras boundary: bg_page_jobs is partial; fill remaining
    #       slots from extras[0:remaining].
    #   (c) Page is entirely in extras range: bg_page_jobs is empty; slice extras.
    global_start = (page - 1) * page_size
    global_end = global_start + page_size

    extras_start = max(0, global_start - bg_total)
    extras_end = max(0, global_end - bg_total)
    extras_slice = extra_jobs[extras_start:extras_end]

    paginated = list(bg_page_jobs) + extras_slice

    return paginated, total_count, total_pages


def _get_queue_status() -> dict:
    """Get current queue status from job manager."""
    from ...jobs.config import SyncJobConfig

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

    # Get jobs (H1: explicit admin scope — /jobs page is admin-only)
    jobs, total_count, total_pages = _get_all_jobs(
        status_filter=status_filter,
        type_filter=type_filter,
        search=search,
        page=page,
        is_admin=True,
    )

    response = templates.TemplateResponse(
        request,
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

    # Get jobs (H1: explicit admin scope — /jobs page is admin-only)
    jobs, total_count, total_pages = _get_all_jobs(
        status_filter=status_filter,
        type_filter=job_type,
        search=search,
        page=page,
        is_admin=True,
    )

    response = templates.TemplateResponse(
        request,
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


@web_router.post(
    "/jobs/{job_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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
        result = job_manager.cancel_job(job_id, session.username, is_admin=True)
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

    return JSONResponse({"success": True, **queue_status})
