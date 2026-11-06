"""
Admin user management pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from ...auth.user_manager import UserRole, SSOPasswordChangeError
from ...auth import dependencies
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_group_manager,
    _get_user_mfa_status,
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


def _create_users_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create users page response with all necessary context."""
    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "users.html",
        {
            "request": request,
            "username": session.username,
            "current_username": session.username,
            "current_page": "users",
            "show_nav": True,
            "csrf_token": csrf_token,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    set_csrf_cookie(response, csrf_token, path="/")
    return response


_USERS_PAGE_SUCCESS_MESSAGES = {
    "user_deleted": "User '{u}' deleted successfully",
    "user_created": "User '{u}' created successfully",
    "role_updated": "Role for '{u}' updated successfully",
    "email_updated": "Email for '{u}' updated successfully",
    "password_changed": "Password for '{u}' changed successfully",
}

_USERS_PAGE_ERROR_MESSAGES = {
    "invalid_csrf": "Invalid CSRF token",
    "cannot_delete_self": "Cannot delete your own account",
    "user_manager_unavailable": "User manager not available",
    "passwords_mismatch": "Passwords do not match",
    "invalid_role": "Invalid role",
    "cannot_demote_self": "Cannot demote your own admin account",
    "sso_password_change_denied": "Cannot change password for SSO users. Authentication is managed by the identity provider.",
    "user_not_found": "User not found",
    "user_already_exists": "A user with that username already exists.",
    "operation_failed": "Operation failed. Check input and try again.",
}


def _resolve_users_page_messages(
    success: Optional[str], error: Optional[str], u: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Whitelist-map PRG query params to user-facing success/error messages.

    The error banner shows only fixed whitelisted text: no free-text query
    parameter is ever rendered there, so a crafted link cannot put attacker
    text into the admin's error banner.
    """
    success_message: Optional[str] = None
    error_message: Optional[str] = None
    if success and success in _USERS_PAGE_SUCCESS_MESSAGES:
        success_message = _USERS_PAGE_SUCCESS_MESSAGES[success].format(u=u or "")
    if error and error in _USERS_PAGE_ERROR_MESSAGES:
        error_message = _USERS_PAGE_ERROR_MESSAGES[error]
    return success_message, error_message


@web_router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    success: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    u: Optional[str] = Query(None),
):
    """Users management page - list all users with CRUD operations."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    success_message, error_message = _resolve_users_page_messages(success, error, u)
    return _create_users_page_response(
        request, session, success_message=success_message, error_message=error_message
    )


@web_router.post(
    "/users/create",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def create_user(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Create a new user (PRG: redirects to /admin/users with status query)."""
    from urllib.parse import quote

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)

    # Validate password match
    if new_password != confirm_password:
        return RedirectResponse(
            url="/admin/users?error=passwords_mismatch", status_code=303
        )

    # Validate role
    try:
        role_enum = UserRole(role)
    except ValueError:
        return RedirectResponse(url="/admin/users?error=invalid_role", status_code=303)

    # Create user
    user_manager = dependencies.user_manager
    if not user_manager:
        return RedirectResponse(
            url="/admin/users?error=user_manager_unavailable", status_code=303
        )

    try:
        user_manager.create_user_audited(
            new_username, new_password, role_enum, actor=session.username
        )

        # Auto-assign new user to appropriate group based on role.
        # Story #1593 AC7: routed through the shared
        # GroupAccessManager.ensure_user_group_membership() primitive
        # instead of an independent inline assign+audit implementation.
        try:
            from ...services.constants import DEFAULT_GROUP_ADMINS, DEFAULT_GROUP_USERS

            group_manager = _get_group_manager()
            if role_enum == UserRole.ADMIN:
                target_group = group_manager.get_group_by_name(DEFAULT_GROUP_ADMINS)
            else:
                target_group = group_manager.get_group_by_name(DEFAULT_GROUP_USERS)

            if target_group:
                group_manager.ensure_user_group_membership(
                    new_username,
                    target_group,
                    assigned_by=session.username,
                    action_type="user_group_assign",
                    audit_details={
                        "group": target_group.name,
                        "reason": "auto_assign_on_creation",
                    },
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

        return RedirectResponse(
            url=f"/admin/users?success=user_created&u={quote(new_username, safe='')}",
            status_code=303,
        )
    except ValueError as e:
        error_msg = str(e)
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-041",
                f"Failed to create user '{new_username}': {error_msg}",
            )
        )
        if "already exists" in error_msg.lower():
            return RedirectResponse(
                url="/admin/users?error=user_already_exists", status_code=303
            )
        return RedirectResponse(
            url="/admin/users?error=operation_failed", status_code=303
        )


@web_router.post(
    "/users/{username}/role",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def update_user_role(
    request: Request,
    username: str,
    role: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Update a user's role (PRG: redirects to /admin/users with status query)."""
    from urllib.parse import quote

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)

    # Prevent demoting self
    if username == session.username and role != "admin":
        return RedirectResponse(
            url="/admin/users?error=cannot_demote_self", status_code=303
        )

    # Validate role
    try:
        role_enum = UserRole(role)
    except ValueError:
        return RedirectResponse(url="/admin/users?error=invalid_role", status_code=303)

    # Update user
    user_manager = dependencies.user_manager
    if not user_manager:
        return RedirectResponse(
            url="/admin/users?error=user_manager_unavailable", status_code=303
        )

    try:
        if not user_manager.update_user_role_audited(
            username, role_enum, actor=session.username
        ):
            # Unknown user: the same redirect as the password and email doors.
            return RedirectResponse(
                url="/admin/users?error=user_not_found", status_code=303
            )
        return RedirectResponse(
            url=f"/admin/users?success=role_updated&u={quote(username, safe='')}",
            status_code=303,
        )
    except ValueError as e:
        error_msg = str(e)
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-041",
                f"Failed to update role for '{username}': {error_msg}",
            )
        )
        return RedirectResponse(
            url="/admin/users?error=operation_failed", status_code=303
        )


@web_router.post(
    "/users/{username}/password",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def change_user_password(
    request: Request,
    username: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Change a user's password (admin only). PRG: redirects to /admin/users."""
    from urllib.parse import quote

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)

    # Validate password match
    if new_password != confirm_password:
        return RedirectResponse(
            url="/admin/users?error=passwords_mismatch", status_code=303
        )

    # Change password
    user_manager = dependencies.user_manager
    if not user_manager:
        return RedirectResponse(
            url="/admin/users?error=user_manager_unavailable", status_code=303
        )

    try:
        if not user_manager.change_password_audited(
            username, new_password, actor=session.username
        ):
            return RedirectResponse(
                url="/admin/users?error=user_not_found", status_code=303
            )
        return RedirectResponse(
            url=f"/admin/users?success=password_changed&u={quote(username, safe='')}",
            status_code=303,
        )
    except SSOPasswordChangeError:
        # Bug #68: SSO users cannot change passwords locally
        return RedirectResponse(
            url="/admin/users?error=sso_password_change_denied",
            status_code=303,
        )
    except ValueError as e:
        error_msg = str(e)
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-041",
                f"Failed to change password for '{username}': {error_msg}",
            )
        )
        return RedirectResponse(
            url="/admin/users?error=operation_failed", status_code=303
        )


@web_router.post(
    "/users/{username}/email",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def update_user_email(
    request: Request,
    username: str,
    new_email: str = Form(""),
    csrf_token: Optional[str] = Form(None),
):
    """Update a user's email (admin only). Empty clears. PRG: redirects to /admin/users."""
    from urllib.parse import quote

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)

    # Update email
    user_manager = dependencies.user_manager
    if not user_manager:
        return RedirectResponse(
            url="/admin/users?error=user_manager_unavailable", status_code=303
        )

    try:
        # Allow empty email to clear it
        email_value = new_email.strip() if new_email else None
        if not user_manager.update_user_email_audited(
            username, email_value if email_value else None, actor=session.username
        ):
            return RedirectResponse(
                url="/admin/users?error=user_not_found", status_code=303
            )

        return RedirectResponse(
            url=f"/admin/users?success=email_updated&u={quote(username, safe='')}",
            status_code=303,
        )
    except ValueError as e:
        error_msg = str(e)
        logger.warning(
            format_error_log(
                "SCIP-GENERAL-041",
                f"Failed to update email for '{username}': {error_msg}",
            )
        )
        return RedirectResponse(
            url="/admin/users?error=operation_failed", status_code=303
        )


@web_router.post(
    "/users/{username}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def delete_user(
    request: Request,
    username: str,
    csrf_token: Optional[str] = Form(None),
):
    """Delete a user (PRG: redirects to /admin/users with status query)."""
    from urllib.parse import quote

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)

    # Prevent deleting self
    if username == session.username:
        return RedirectResponse(
            url="/admin/users?error=cannot_delete_self", status_code=303
        )

    # Delete user
    user_manager = dependencies.user_manager
    if not user_manager:
        return RedirectResponse(
            url="/admin/users?error=user_manager_unavailable", status_code=303
        )

    try:
        # Account write plus its durable audit row: off the event loop.  The
        # shared deletion step deletes the account row first, then removes
        # every row keyed to the name (group membership, SSO links, MFA,
        # tokens, credentials) in the configured store and submits removal of
        # the account's repositories.
        deleted = await asyncio.to_thread(
            user_manager.delete_user_audited, username, actor=session.username
        )
        if not deleted:
            return RedirectResponse(
                url="/admin/users?error=user_not_found", status_code=303
            )

        return RedirectResponse(
            url=f"/admin/users?success=user_deleted&u={quote(username, safe='')}",
            status_code=303,
        )
    except ValueError:
        return RedirectResponse(url="/admin/users?error=invalid_csrf", status_code=303)


@web_router.get(
    "/partials/users-list",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
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
        request,
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
                    "mfa_enabled": _get_user_mfa_status(u.username),
                }
                for u in users
            ],
        },
    )

    set_csrf_cookie(response, csrf_token)
    return response
