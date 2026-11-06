"""
Admin group management and group repository access pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import asyncio
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_golden_repo_manager,
    _get_group_manager,
    _get_repo_category_service,
    _get_users_list,
    _require_admin_session,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()

UNASSIGNED_CATEGORY_PRIORITY = (
    999999  # Story #686: sentinel sort priority for uncategorised repos
)


def _format_datetime_display(
    iso_string: Optional[Union[str, "datetime"]], fmt: str = "%Y-%m-%d %H:%M"
) -> str:
    """Format ISO datetime string or datetime object for display.

    Accepts both str (SQLite) and datetime (PG native type).
    """
    if not iso_string:
        return "N/A"
    try:
        from datetime import datetime

        if isinstance(iso_string, datetime):
            return iso_string.strftime(fmt)
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except (ValueError, AttributeError) as exc:
        logger.debug(
            "_format_datetime_display: could not parse %r — %s", iso_string, exc
        )
        return str(iso_string)


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


# The tabs of the Group Management page.
_GROUPS_PAGE_TABS = frozenset({"groups", "users", "repos"})


def _create_groups_page_response(
    request: Request,
    session: SessionData,
    active_tab: str = "groups",
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create groups page response with all necessary context.

    A tab the page does not have (such as the removed audit tab) renders the
    Groups tab; audit reading lives on the Audit Logs page.
    """
    if active_tab not in _GROUPS_PAGE_TABS:
        active_tab = "groups"
    csrf_token = generate_csrf_token()
    group_manager = _get_group_manager()

    # Get groups data with counts
    groups_data = _get_groups_data()

    # Users' group assignments are user data: the User Assignments tab loads
    # them from the elevation-gated groups-users-list partial, never here.

    # Get all groups for the dropdown
    all_groups = [
        {"id": g.id, "name": g.name, "is_default": g.is_default}
        for g in group_manager.get_all_groups()
    ]

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

    # Category enrichment for repo access matrix (Story #686)
    try:
        category_service = _get_repo_category_service()
        repo_map = category_service.get_repo_category_map()
        for repo in golden_repos:
            info = repo_map.get(repo["name"], {})
            repo["category_id"] = info.get("category_id")
            repo["category_name"] = info.get("category_name") or "Unassigned"
            repo["category_priority"] = info.get(
                "priority", UNASSIGNED_CATEGORY_PRIORITY
            )
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-048",
                f"Could not load category information for groups repo access: {e}",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        for repo in golden_repos:
            repo["category_id"] = None
            repo["category_name"] = "Unassigned"
            repo["category_priority"] = UNASSIGNED_CATEGORY_PRIORITY

    response = templates.TemplateResponse(
        request,
        "groups.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "groups",
            "show_nav": True,
            "active_tab": active_tab,
            "csrf_token": csrf_token,
            "groups": groups_data,
            "all_groups": all_groups,
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


@web_router.post(
    "/groups/create",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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
            details={"name": group.name},
        )

        return _create_groups_page_response(
            request, session, success_message=f"Group '{name}' created successfully"
        )
    except ValueError as e:
        return _create_groups_page_response(request, session, error_message=str(e))


@web_router.post(
    "/groups/{group_id}/update",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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
                details={"old_name": old_group.name, "new_name": name},
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


@web_router.post(
    "/groups/{group_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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
                details={"name": group_name},
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


@web_router.post(
    "/groups/users/{user_id:path}/assign",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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

    from code_indexer.server.services.group_access_manager import (
        GroupNotFoundError,
        UnknownAccountError,
    )

    user_manager = dependencies.user_manager
    if user_manager is None:
        return _create_groups_page_response(
            request,
            session,
            active_tab="users",
            error_message="User manager not available",
        )

    try:
        group_manager = _get_group_manager()
        try:
            new_group = group_manager.assign_user_to_group_audited(
                user_id,
                group_id,
                actor=session.username,
                account_exists=lambda name: user_manager.get_user(name) is not None,
            )
        except UnknownAccountError:
            return _create_groups_page_response(
                request,
                session,
                active_tab="users",
                error_message=f"User '{user_id}' not found",
            )
        except GroupNotFoundError:
            return _create_groups_page_response(
                request,
                session,
                active_tab="users",
                error_message=f"Group {group_id} not found",
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
        request,
        "partials/groups_list.html",
        {"request": request, "csrf_token": csrf_token, "groups": groups_data},
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _get_users_with_groups(group_manager: Any) -> List[Dict[str, Any]]:
    """Every system user with their group assignment (None when unassigned)."""
    assigned_users, _ = group_manager.get_all_users_with_groups()
    assigned_map = {u["user_id"]: u for u in assigned_users}

    users_with_groups: List[Dict[str, Any]] = []
    for user in _get_users_list():
        username = user.username
        if username in assigned_map:
            user_data = assigned_map[username]
            user_data["assigned_at"] = _format_datetime_display(
                user_data.get("assigned_at")
            )
            users_with_groups.append(user_data)
        else:
            users_with_groups.append(
                {
                    "user_id": username,
                    "group_id": None,
                    "group_name": None,
                    "assigned_at": None,
                    "assigned_by": None,
                }
            )
    return users_with_groups


@web_router.get(
    "/partials/groups-users-list",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def groups_users_list_partial(request: Request):
    """Partial for the users' group assignments (the groups page's User
    Assignments tab).

    Group membership is user data: elevation-gated like the users-list
    partial, REST ``GET /api/v1/groups/{group_id}`` and MCP ``get_group``.
    Without a window it answers 403 ``elevation_required``, which the shared
    elevation interceptor turns into the TOTP modal.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    csrf_token = get_csrf_token_from_cookie(request) or generate_csrf_token()
    group_manager = _get_group_manager()
    users_with_groups = _get_users_with_groups(group_manager)

    all_groups = [
        {"id": g.id, "name": g.name, "is_default": g.is_default}
        for g in group_manager.get_all_groups()
    ]

    response = templates.TemplateResponse(
        request,
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

    # Category enrichment (Story #686)
    try:
        category_service = _get_repo_category_service()
        repo_map = category_service.get_repo_category_map()
        for repo in golden_repos:
            info = repo_map.get(repo["name"], {})
            repo["category_id"] = info.get("category_id")
            repo["category_name"] = info.get("category_name") or "Unassigned"
            repo["category_priority"] = info.get(
                "priority", UNASSIGNED_CATEGORY_PRIORITY
            )
    except Exception as e:
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-048",
                f"Could not load category information for groups repo access: {e}",
            ),
            extra={"correlation_id": get_correlation_id()},
        )
        for repo in golden_repos:
            repo["category_id"] = None
            repo["category_name"] = "Unassigned"
            repo["category_priority"] = UNASSIGNED_CATEGORY_PRIORITY

    response = templates.TemplateResponse(
        request,
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
            {"success": False, "error": error_msg}, status_code=status_code
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


def _grant_repo_access_recorded(
    group_manager: Any, repo_name: str, group_id: int, actor: str
) -> Tuple[Any, bool]:
    """Grant *repo_name* to a group and record the grant (synchronous).

    Returns ``(group, granted)``; ``group`` is None when it does not exist.
    """
    group = group_manager.get_group(group_id)
    if not group:
        return None, False
    granted = bool(
        group_manager.grant_repo_access(
            repo_name=repo_name, group_id=group_id, granted_by=actor
        )
    )
    if granted:
        group_manager.log_audit(
            admin_id=actor,
            action_type="repo_access_grant",
            target_type="repo",
            target_id=repo_name,
            details={"repo": repo_name, "group": group.name},
        )
    return group, granted


def _revoke_repo_access_recorded(
    group_manager: Any, repo_name: str, group_id: int, actor: str
) -> Tuple[Any, bool]:
    """Revoke *repo_name* from a group and record the revocation (synchronous).

    Returns ``(group, revoked)``; ``group`` is None when it does not exist.
    The audited entry point writes the one row of the attempt.
    """
    from code_indexer.server.services.group_access_manager import GroupNotFoundError

    try:
        revoked = bool(
            group_manager.revoke_repo_access_audited(repo_name, group_id, actor=actor)
        )
    except GroupNotFoundError:
        return None, False
    return group_manager.get_group(group_id), revoked


@web_router.post(
    "/groups/repo-access/grant",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def grant_repo_access(
    request: Request,
    repo_name: Optional[str] = Form(None),
    group_id: Optional[int] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Grant repository access to a group.

    Supports both AJAX (JSON) and form POST requests (Story #199).
    """
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Validate CSRF
    is_valid, error_msg = _validate_repo_access_csrf(request, is_ajax, csrf_token)
    if not is_valid:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 403)  # type: ignore[arg-type]

    # Parse request
    repo_name, group_id, error_msg = await _parse_repo_access_request(
        request, is_ajax, repo_name, group_id
    )
    if error_msg:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 400)

    if not repo_name or group_id is None:
        return _repo_access_error_response(
            is_ajax,
            request,
            session,
            "Missing required parameters: repo_name and group_id",
            400,
        )

    group_manager = _get_group_manager()

    try:
        # The whole synchronous operation (lookup, grant, durable audit row)
        # runs off the event loop.
        group, success = await asyncio.to_thread(
            _grant_repo_access_recorded,
            group_manager,
            repo_name,
            group_id,
            session.username,
        )
        if not group:
            return _repo_access_error_response(
                is_ajax, request, session, "Group not found", 404
            )

        message = (
            f"Granted '{repo_name}' access to '{group.name}'"
            if success
            else f"'{group.name}' already has access to '{repo_name}'"
        )
        return _repo_access_success_response(is_ajax, request, session, message)

    except Exception as e:
        logger.error(
            format_error_log("SCIP-GENERAL-042", f"Failed to grant repo access: {e}")
        )
        return _repo_access_error_response(is_ajax, request, session, str(e), 500)


@web_router.post(
    "/groups/repo-access/revoke",
    dependencies=[Depends(dependencies.require_elevation())],
)
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

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Validate CSRF
    is_valid, error_msg = _validate_repo_access_csrf(request, is_ajax, csrf_token)
    if not is_valid:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 403)  # type: ignore[arg-type]

    # Parse request
    repo_name, group_id, error_msg = await _parse_repo_access_request(
        request, is_ajax, repo_name, group_id
    )
    if error_msg:
        return _repo_access_error_response(is_ajax, request, session, error_msg, 400)

    if not repo_name or group_id is None:
        return _repo_access_error_response(
            is_ajax,
            request,
            session,
            "Missing required parameters: repo_name and group_id",
            400,
        )

    group_manager = _get_group_manager()

    try:
        # The whole synchronous operation (lookup, revoke, durable audit row)
        # runs off the event loop.
        group, success = await asyncio.to_thread(
            _revoke_repo_access_recorded,
            group_manager,
            repo_name,
            group_id,
            session.username,
        )
        if not group:
            return _repo_access_error_response(
                is_ajax, request, session, "Group not found", 404
            )

        message = (
            f"Revoked '{repo_name}' access from '{group.name}'"
            if success
            else f"'{group.name}' did not have access to '{repo_name}'"
        )
        return _repo_access_success_response(is_ajax, request, session, message)

    except CidxMetaCannotBeRevokedError:
        return _repo_access_error_response(
            is_ajax, request, session, "cidx-meta access cannot be revoked", 400
        )
    except Exception as e:
        logger.error(
            format_error_log("SCIP-GENERAL-043", f"Failed to revoke repo access: {e}")
        )
        return _repo_access_error_response(is_ajax, request, session, str(e), 500)
