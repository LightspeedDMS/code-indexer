"""
API key, MCP credential and git credential pages (admin and user).

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

import asyncio
from typing import Any, Optional, cast
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import SessionData

from ._shared import (
    _create_login_redirect,
    _require_admin_session,
    _require_authenticated_session,
    generate_csrf_token,
    logger,
    set_csrf_cookie,
    templates,
)

user_router = APIRouter()
web_router = APIRouter()

# =============================================================================
# Git Settings
# =============================================================================


@web_router.get("/settings/git", response_class=HTMLResponse)
def git_settings_page(request: Request):
    """
    Git settings page - view and edit git service configuration.

    Admin-only page for configuring git committer settings.
    """
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    from code_indexer.config import ConfigManager

    # Get current configuration
    config_manager = ConfigManager()
    config = config_manager.load()
    git_config = config.git_service

    # Generate CSRF token
    csrf_token = generate_csrf_token()

    response = templates.TemplateResponse(
        request,
        "git_settings.html",
        {
            "username": session.username,
            "current_page": "git-settings",
            "show_nav": True,
            "csrf_token": csrf_token,
            "config": git_config,
        },
    )

    # Set CSRF cookie
    set_csrf_cookie(response, csrf_token)

    return response


# =============================================================================
# API Keys Management
# =============================================================================


@web_router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request):
    """API Keys management page - manage personal API keys."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_api_keys_page_response(request, session)


def _create_api_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create API keys page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "api_keys.html",
        {
            "show_nav": True,
            "current_page": "api-keys",
            "username": username,
            "api_keys": keys,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@web_router.get("/partials/api-keys-list", response_class=HTMLResponse)
def api_keys_list_partial(request: Request):
    """Partial for API keys list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "partials/api_keys_list.html",
        {"api_keys": keys},
    )
    return response


@web_router.get("/mcp-credentials", response_class=HTMLResponse)
def admin_mcp_credentials_page(request: Request):
    """Admin MCP Credentials management page - manage personal MCP credentials."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_admin_mcp_credentials_page_response(request, session)


def _create_admin_mcp_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create admin MCP credentials page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()

    response = templates.TemplateResponse(
        request,
        "admin_mcp_credentials.html",
        {
            "show_nav": True,
            "current_page": "mcp-credentials",
            "username": username,
            "mcp_credentials": credentials,
            "system_credentials": system_credentials,
            "success_message": success_message,
            "error_message": error_message,
        },
    )
    return response


@web_router.get("/partials/mcp-credentials-list", response_class=HTMLResponse)
def admin_mcp_credentials_list_partial(request: Request):
    """Partial for admin MCP credentials list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()

    response = templates.TemplateResponse(
        request,
        "partials/mcp_credentials_list.html",
        {"mcp_credentials": credentials, "system_credentials": system_credentials},
    )
    return response


@web_router.get("/api/system-credentials", response_class=JSONResponse)
def admin_system_credentials_api(request: Request):
    """Return system-managed MCP credentials as JSON (Story #275).

    Credentials owned by the built-in admin user that were created automatically
    by the CIDX server (e.g. cidx-local-auto, cidx-server-auto).
    Requires an active admin session.
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            content={"error": "Authentication required"},
            status_code=403,
        )

    assert dependencies.user_manager is not None  # Initialized at app startup
    system_credentials = dependencies.user_manager.get_system_mcp_credentials()
    return JSONResponse(content={"system_credentials": system_credentials})


# =============================================================================
# Admin Git Credentials (Story #385 - Admin is a user too)
# =============================================================================


@web_router.get("/git-credentials", response_class=HTMLResponse)
def admin_git_credentials_page(request: Request):
    """Admin Git Credentials management page - manage personal git forge PATs."""
    session = _require_admin_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_admin_git_credentials_page_response(request, session)


def _create_admin_git_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create admin git credentials page response."""
    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = str(server_dir / "data" / "cidx_server.db")
    _cfg = config_service.config_manager.load_config()
    if _cfg is None:
        raise RuntimeError(
            "Server config unavailable; cannot create git credential manager"
        )
    storage_mode = _cfg.storage_mode
    manager = create_git_credential_manager(
        db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
    )
    credentials = manager.list_credentials(session.username)

    response = templates.TemplateResponse(
        request,
        "admin_git_credentials.html",
        {
            "show_nav": True,
            "current_page": "git-credentials",
            "username": session.username,
            "git_credentials": credentials,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@web_router.get("/partials/git-credentials-list", response_class=HTMLResponse)
def admin_git_credentials_list_partial(request: Request):
    """Partial for admin git credentials list (HTMX refresh)."""
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    db_path = str(server_dir / "data" / "cidx_server.db")
    _cfg = config_service.config_manager.load_config()
    if _cfg is None:
        raise RuntimeError(
            "Server config unavailable; cannot create git credential manager"
        )
    storage_mode = _cfg.storage_mode
    manager = create_git_credential_manager(
        db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
    )
    credentials = manager.list_credentials(session.username)

    response = templates.TemplateResponse(
        request,
        "partials/git_credentials_list.html",
        {"git_credentials": credentials},
    )
    return response


def _build_git_credential_manager() -> Any:
    """Build this server's git credential manager (synchronous I/O).

    Loads the bootstrap config and constructs the manager, which may read or
    create the encryption salt file; async routes call this off the loop.
    """
    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    config_service = get_config_service()
    server_dir = config_service.config_manager.server_dir
    # load_config() returns Optional[ServerConfig]; cast to Any so mypy does not
    # flag .storage_mode access — config is always present when server is running.
    storage_mode = cast(Any, config_service.config_manager.load_config()).storage_mode
    return create_git_credential_manager(
        db_path=str(server_dir / "data" / "cidx_server.db"),
        server_dir=str(server_dir),
        storage_mode=storage_mode,
    )


def _stored_secret_write_refused(exc: Exception, door: str) -> JSONResponse:
    """409 for a stored-secret write refused during a cluster upgrade."""
    logger.warning(
        "%s: stored-secret write refused while the cluster upgrade is in progress",
        door,
    )
    return JSONResponse({"success": False, "error": str(exc)}, status_code=409)


@web_router.post(
    "/git-credentials",
    dependencies=[Depends(dependencies.require_elevation())],
)
async def admin_git_credentials_add(request: Request):
    """Add a new git credential via admin form submission."""
    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Session expired"}, status_code=401
        )

    from ...clients.forge_client import ForgeAuthenticationError
    from ...services.token_encryption import StoredSecretLegacyWriteRefused

    try:
        body = await request.json()
        forge_type = body.get("forge_type")
        forge_host = body.get("forge_host")
        token = body.get("token")
        name = body.get("name")

        if not forge_type or not forge_host or not token:
            return JSONResponse(
                {"success": False, "error": "Missing required fields"}, status_code=400
            )

        # Config load and manager construction are synchronous I/O.
        manager = await asyncio.to_thread(_build_git_credential_manager)

        result = await manager.configure_credential_audited(
            session.username,
            forge_type,
            forge_host,
            token,
            name=name,
            actor=session.username,
        )
        return JSONResponse(result)

    except ForgeAuthenticationError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except StoredSecretLegacyWriteRefused as e:
        return _stored_secret_write_refused(e, "admin git credentials")
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@web_router.delete(
    "/git-credentials/{credential_id}",
    dependencies=[Depends(dependencies.require_elevation())],
)
def admin_git_credentials_delete(request: Request, credential_id: str):
    """Delete a git credential from admin interface."""
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Session expired"}, status_code=401
        )

    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    try:
        config_service = get_config_service()
        db_path = str(
            config_service.config_manager.server_dir / "data" / "cidx_server.db"
        )
        server_dir = config_service.config_manager.server_dir
        # load_config() returns Optional[ServerConfig]; cast to Any so mypy does not
        # flag .storage_mode access — config is always present when server is running.
        storage_mode = cast(
            Any, config_service.config_manager.load_config()
        ).storage_mode
        manager = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
        )
        manager.delete_credential_audited(
            session.username, credential_id, actor=session.username
        )

        return JSONResponse({"success": True, "message": "Credential deleted"})

    except PermissionError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=403)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# Old /user/login routes removed - replaced by unified login at root level
# See login_router for unified login implementation


@user_router.get("/api-keys", response_class=HTMLResponse)
def user_api_keys_page(request: Request):
    """User API Keys management page - any authenticated user can manage their own API keys."""
    session = _require_authenticated_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_user_api_keys_page_response(request, session)


def _create_user_api_keys_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create user API keys page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "user_api_keys.html",
        {
            "show_nav": True,
            "current_page": "api-keys",
            "username": username,
            "api_keys": keys,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@user_router.get("/partials/api-keys-list", response_class=HTMLResponse)
def user_api_keys_list_partial(request: Request):
    """Partial for user API keys list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_authenticated_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    keys = dependencies.user_manager.get_api_keys(username)

    response = templates.TemplateResponse(
        request,
        "partials/api_keys_list.html",
        {"api_keys": keys},
    )
    return response


@user_router.get("/mcp-credentials", response_class=HTMLResponse)
def user_mcp_credentials_page(request: Request):
    """User MCP Credentials management page - any authenticated user can manage their own MCP credentials."""
    session = _require_authenticated_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_user_mcp_credentials_page_response(request, session)


def _create_user_mcp_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create user MCP credentials page response."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)

    response = templates.TemplateResponse(
        request,
        "user_mcp_credentials.html",
        {
            "show_nav": True,
            "current_page": "mcp-credentials",
            "username": username,
            "mcp_credentials": credentials,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@user_router.get("/partials/mcp-credentials-list", response_class=HTMLResponse)
def user_mcp_credentials_list_partial(request: Request):
    """Partial for user MCP credentials list (HTMX refresh)."""
    assert dependencies.user_manager is not None  # Initialized at app startup
    session = _require_authenticated_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    username = session.username
    credentials = dependencies.user_manager.get_mcp_credentials(username)

    response = templates.TemplateResponse(
        request,
        "partials/mcp_credentials_list.html",
        {"mcp_credentials": credentials},
    )
    return response


# =============================================================================
# Git Credentials Management (Story #386)
# =============================================================================


@user_router.get("/git-credentials", response_class=HTMLResponse)
def user_git_credentials_page(request: Request):
    """Git Credentials management page - manage personal git forge PATs."""
    session = _require_authenticated_session(request)
    if not session:
        return _create_login_redirect(request)

    return _create_user_git_credentials_page_response(request, session)


def _create_user_git_credentials_page_response(
    request: Request,
    session: SessionData,
    success_message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HTMLResponse:
    """Create user git credentials page response."""
    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")
    server_dir = config_service.config_manager.server_dir
    _cfg = config_service.config_manager.load_config()
    if _cfg is None:
        raise RuntimeError(
            "Server config unavailable; cannot create git credential manager"
        )
    storage_mode = _cfg.storage_mode
    manager = create_git_credential_manager(
        db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
    )
    credentials = manager.list_credentials(session.username)

    response = templates.TemplateResponse(
        request,
        "user_git_credentials.html",
        {
            "show_nav": True,
            "current_page": "git-credentials",
            "username": session.username,
            "git_credentials": credentials,
            "success_message": success_message,
            "error_message": error_message,
            "csrf_token": session.csrf_token,
        },
    )
    return response


@user_router.get("/partials/git-credentials-list", response_class=HTMLResponse)
def user_git_credentials_list_partial(request: Request):
    """Partial for git credentials list (HTMX refresh)."""
    session = _require_authenticated_session(request)
    if not session:
        return HTMLResponse(
            content="<p>Session expired. Please refresh the page.</p>", status_code=401
        )

    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    config_service = get_config_service()
    db_path = str(config_service.config_manager.server_dir / "data" / "cidx_server.db")
    server_dir = config_service.config_manager.server_dir
    _cfg = config_service.config_manager.load_config()
    if _cfg is None:
        raise RuntimeError(
            "Server config unavailable; cannot create git credential manager"
        )
    storage_mode = _cfg.storage_mode
    manager = create_git_credential_manager(
        db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
    )
    credentials = manager.list_credentials(session.username)

    response = templates.TemplateResponse(
        request,
        "partials/git_credentials_list.html",
        {"git_credentials": credentials},
    )
    return response


def _git_credential_self_elevation(request: Request) -> None:
    """Self-service elevation gate for the caller's own git credentials.

    An unauthenticated request is left to the route itself, so it keeps its
    own 401 "Session expired" body. For an authenticated web session the
    shared self-service gate applies: TOTP set up plus the caller's own
    elevation window when enforcement is on. With enforcement off this is a
    no-op.
    """
    if _require_authenticated_session(request) is None:
        return
    if not dependencies._is_elevation_enforcement_enabled():
        return
    user = dependencies.get_current_user_web_or_api(request, None)
    dependencies.require_self_elevation(request, user, None)


@user_router.post(
    "/git-credentials",
    dependencies=[Depends(_git_credential_self_elevation)],
)
async def user_git_credentials_add(request: Request):
    """Add a new git credential via form submission.

    Requires TOTP plus the caller's own elevation window when enforcement is
    on, matching the admin route and the MCP twin configure_git_credential.
    """
    session = await asyncio.to_thread(_require_authenticated_session, request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Session expired"}, status_code=401
        )

    from ...clients.forge_client import ForgeAuthenticationError
    from ...services.token_encryption import StoredSecretLegacyWriteRefused

    try:
        body = await request.json()
        forge_type = body.get("forge_type")
        forge_host = body.get("forge_host")
        token = body.get("token")
        name = body.get("name")

        if not forge_type or not forge_host or not token:
            return JSONResponse(
                {"success": False, "error": "Missing required fields"}, status_code=400
            )

        # Config load and manager construction are synchronous I/O.
        manager = await asyncio.to_thread(_build_git_credential_manager)

        result = await manager.configure_credential_audited(
            session.username,
            forge_type,
            forge_host,
            token,
            name=name,
            actor=session.username,
        )
        return JSONResponse(result)

    except ForgeAuthenticationError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except StoredSecretLegacyWriteRefused as e:
        return _stored_secret_write_refused(e, "git credentials")
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@user_router.delete(
    "/git-credentials/{credential_id}",
    dependencies=[Depends(_git_credential_self_elevation)],
)
def user_git_credentials_delete(request: Request, credential_id: str):
    """Delete a git credential.

    Requires TOTP plus the caller's own elevation window when enforcement is
    on, matching the admin route and the MCP twin delete_git_credential.
    """
    session = _require_authenticated_session(request)
    if not session:
        return JSONResponse(
            {"success": False, "error": "Session expired"}, status_code=401
        )

    from ...services.config_service import get_config_service
    from ...services.git_credential_manager import create_git_credential_manager

    try:
        config_service = get_config_service()
        db_path = str(
            config_service.config_manager.server_dir / "data" / "cidx_server.db"
        )
        server_dir = config_service.config_manager.server_dir
        # load_config() returns Optional[ServerConfig]; cast to Any so mypy does not
        # flag .storage_mode access — config is always present when server is running.
        storage_mode = cast(
            Any, config_service.config_manager.load_config()
        ).storage_mode
        manager = create_git_credential_manager(
            db_path=db_path, server_dir=str(server_dir), storage_mode=storage_mode
        )
        manager.delete_credential_audited(
            session.username, credential_id, actor=session.username
        )

        return JSONResponse({"success": True, "message": "Credential deleted"})

    except PermissionError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=403)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
