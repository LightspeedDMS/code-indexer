"""
Admin SSH key pages.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from typing import Optional
from fastapi import APIRouter, Depends, Request, Response, Form
from fastapi.responses import HTMLResponse
from ...auth import dependencies
from ..auth import SessionData
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _elevation_page_redirect,
    _get_ssh_key_manager,
    _require_admin_session,
    generate_csrf_token,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

web_router = APIRouter()


# SSH Keys Management Page
@web_router.get("/ssh-keys", response_class=HTMLResponse)
def ssh_keys_page(request: Request):
    """SSH Keys management page - view migration status and manage SSH keys.

    Listing key metadata requires the caller's own elevation window, like
    the REST twin ``GET /api/ssh-keys`` (``require_elevation()``): with
    enforcement on and no window, the admin is sent to the elevation page,
    which returns here afterwards (query string kept; the elevate page's
    ``_sanitize_next`` bounds the target). Enforcement off passes through.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    elevate = _elevation_page_redirect(request, session.username)
    if elevate is not None:
        return elevate

    # Generate fresh CSRF token
    csrf_token = generate_csrf_token()

    # Get migration result from app state (set during server startup)
    migration_result = getattr(request.app.state, "ssh_migration_result", None)

    # Get SSH keys list
    managed_keys = []
    unmanaged_keys = []
    try:
        manager = _get_ssh_key_manager()
        key_list = manager.list_keys()
        managed_keys = key_list.managed
        unmanaged_keys = key_list.unmanaged
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-017",
                f"Failed to list SSH keys: {e}",
            )
        )

    response = templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "show_nav": True,
            "current_page": "ssh-keys",
            "username": session.username,
            "migration_result": migration_result,
            "managed_keys": managed_keys,
            "unmanaged_keys": unmanaged_keys,
            "csrf_token": csrf_token,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


def _create_ssh_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Response:
    """Helper to create SSH keys page response with messages."""
    # Generate fresh CSRF token
    csrf_token = generate_csrf_token()

    migration_result = getattr(request.app.state, "ssh_migration_result", None)

    managed_keys = []
    unmanaged_keys = []
    try:
        manager = _get_ssh_key_manager()
        key_list = manager.list_keys()
        managed_keys = key_list.managed
        unmanaged_keys = key_list.unmanaged
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-018",
                f"Failed to list SSH keys: {e}",
            )
        )

    response = templates.TemplateResponse(
        request,
        "ssh_keys.html",
        {
            "show_nav": True,
            "current_page": "ssh-keys",
            "username": session.username,
            "migration_result": migration_result,
            "managed_keys": managed_keys,
            "unmanaged_keys": unmanaged_keys,
            "csrf_token": csrf_token,
            "success_message": success_message,
            "error_message": error_message,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


@web_router.post(
    "/ssh-keys/create",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def create_ssh_key(
    request: Request,
    key_name: str = Form(...),
    key_type: str = Form(...),
    email: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    csrf_token: Optional[str] = Form(None),
):
    """Create a new SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        from ...services.ssh_key_generator import (
            InvalidKeyNameError,
            KeyAlreadyExistsError,
        )

        manager = _get_ssh_key_manager()
        manager.create_key_audited(
            key_name,
            key_type=key_type,
            email=email if email else None,
            description=description if description else None,
            actor=session.username,
        )

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"SSH key '{key_name}' created successfully. Public key is ready to copy.",
        )
    except InvalidKeyNameError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Invalid key name: {e}"
        )
    except KeyAlreadyExistsError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Key already exists: {e}"
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-019",
                f"Failed to create SSH key: {e}",
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to create key: {e}"
        )


@web_router.post(
    "/ssh-keys/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def delete_ssh_key(
    request: Request,
    key_name: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Delete an SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        manager = _get_ssh_key_manager()

        # Bug #1521: delete_key() returns False when the Bug #1519 provenance
        # guard refuses to remove a same-named file this server never proved it
        # wrote. That return value used to be discarded, so a refused deletion
        # was rendered as a success -- a silent lie about a safety-critical
        # operation.
        if not manager.delete_key_audited(key_name, actor=session.username):
            return _create_ssh_keys_page_response(
                request,
                session,
                error_message=(
                    f"Refused to delete '{key_name}': an untracked file of that "
                    f"name exists in the SSH directory and this server has no "
                    f"record of creating it. Remove it manually if it is "
                    f"genuinely unwanted."
                ),
            )

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"SSH key '{key_name}' deleted successfully.",
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-020",
                f"Failed to delete SSH key: {e}",
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to delete key: {e}"
        )


@web_router.post(
    "/ssh-keys/assign-host",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def assign_host_to_key(
    request: Request,
    key_name: str = Form(...),
    hostname: str = Form(...),
    csrf_token: Optional[str] = Form(None),
):
    """Assign a host to an SSH key."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Validate CSRF token
    if not validate_login_csrf_token(request, csrf_token):
        return _create_ssh_keys_page_response(
            request, session, error_message="Invalid CSRF token"
        )

    try:
        from ...services.ssh_key_manager import HostConflictError

        manager = _get_ssh_key_manager()
        manager.assign_key_to_host_audited(key_name, hostname, actor=session.username)

        return _create_ssh_keys_page_response(
            request,
            session,
            success_message=f"Host '{hostname}' assigned to key '{key_name}' successfully.",
        )
    except HostConflictError as e:
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Host conflict: {e}"
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-021",
                f"Failed to assign host to SSH key: {e}",
            )
        )
        return _create_ssh_keys_page_response(
            request, session, error_message=f"Failed to assign host: {e}"
        )
