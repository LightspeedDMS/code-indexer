"""
Admin activated repository pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.validation.user_validation import RESERVED_ACTIVATED_REPOS_DIR_NAMES
import html
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from ...auth import dependencies
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_activated_repo_manager,
    _get_background_job_manager,
    _get_repo_category_service,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()


def _get_all_activated_repos() -> list:
    """
    Get all activated repositories across all users.

    Returns:
        List of activated repository dictionaries with username added and temporal_status
    """
    import os
    from ...services.dashboard_service import DashboardService

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
                    f"Could not load category information for activated repos: {e}",
                ),
                extra={"correlation_id": get_correlation_id()},
            )

        # Iterate over all user directories
        for username in os.listdir(activated_repos_dir):
            if username in RESERVED_ACTIVATED_REPOS_DIR_NAMES:
                # A server-owned entry (e.g. '.trash'), not a username.
                continue
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
                        repo["category_name"] = cat_info.get(
                            "category_name", "Unassigned"
                        )
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
                                f"Failed to get temporal status for repo {username}/{repo.get('user_alias', 'unknown')}: {e}",
                            ),
                            exc_info=True,
                            extra={"correlation_id": get_correlation_id()},
                        )
                        # Provide error temporal_status with honest error format
                        repo["temporal_status"] = {
                            "error": str(e),
                            "format": "error",
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
                "STORE-GENERAL-028", f"Failed to get activated repos: {e}"
            ),
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
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


def _build_deactivating_map() -> dict:
    """Build a {(username, user_alias): job_id} map for in-flight deactivations.

    AC2: Used by the repos page to render the 'Deactivating...' badge per row.
    Returns an empty dict when the job manager is unavailable or no active jobs exist.
    """
    result: dict = {}
    job_manager = _get_background_job_manager()
    if not job_manager:
        return result
    try:
        jobs_data = job_manager.list_jobs(
            username="",
            is_admin=True,
            status_filter=None,
            limit=500,
            offset=0,
        )
        for job in jobs_data.get("jobs", []):
            if job.get("operation_type") != "deactivate_repository":
                continue
            if job.get("status") not in ("pending", "running"):
                continue
            username = job.get("username", "")
            repo_alias = job.get("repo_alias") or ""
            if username and repo_alias:
                result[(username, repo_alias)] = job["job_id"]
    except Exception as e:
        # H3: Log at ERROR (not WARNING) with exc_info so traceback is captured
        logger.error("Failed to build deactivating_map: %s", e, exc_info=True)
    return result


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

    # AC2: Build deactivating map for per-row badge rendering
    deactivating_map = _build_deactivating_map()

    response = templates.TemplateResponse(
        request,
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
            "deactivating_map": deactivating_map,
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
        request,
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
        repo = manager.get_repository(username, user_alias, touch=False)

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
            request,
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
    "/repos/{username}/{user_alias}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
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
    from code_indexer.server.services.activated_repo_audited_ops import (
        deactivate_repository_for_user,
    )

    try:
        job_id = deactivate_repository_for_user(
            _get_activated_repo_manager(),
            username,
            user_alias,
            actor=session.username,  # AC12: attribute to admin who clicked
        )
        job_link = f'<a href="/admin/jobs?search={job_id}">{job_id}</a>'
        return _create_repos_page_response(
            request,
            session,
            success_message=f"Repository '{html.escape(user_alias)}' deactivation job submitted (Job ID: {job_link})",
        )
    except Exception as e:
        error_msg = str(e)
        if "not found" in error_msg.lower():
            error_msg = f"Repository '{user_alias}' not found for user '{username}'"
        return _create_repos_page_response(request, session, error_message=error_msg)
