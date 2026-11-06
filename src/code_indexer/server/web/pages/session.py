"""
Web UI shell: login, logout, SSO login, login redirects and the public server-time API.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import os
import secrets
from typing import Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from ...auth import dependencies
from ...auth.login_outcome import complete_login, reject_login
from ..auth import get_session_manager
from ...services.config_service import get_config_service
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    CSRF_COOKIE_NAME,
    _get_user_mfa_status,
    generate_csrf_token,
    get_csrf_token_from_cookie,
    logger,
    set_csrf_cookie,
    templates,
    validate_login_csrf_token,
)

api_router = APIRouter()
login_router = APIRouter()
user_router = APIRouter()
web_router = APIRouter()

# Old /admin/login routes removed - replaced by unified login at root level
# See login_router below for unified login implementation


def _extract_jti_from_request(request: Request) -> "Optional[str]":
    """Extract the JWT jti claim from an incoming request.

    Checks the Authorization: Bearer <token> header first, then falls back
    to the CIDX_SESSION_COOKIE cookie.  Returns None (never raises) when:
      - no token is present
      - the token is malformed or expired (expected at logout time)
      - jwt_manager is not initialized
      - any unexpected error occurs (logged at DEBUG)
    """
    from ...auth.dependencies import CIDX_SESSION_COOKIE
    from ...auth import dependencies as _deps
    from ...auth.jwt_manager import TokenExpiredError, InvalidTokenError

    try:
        _jm = _deps.jwt_manager
        if _jm is None:
            return None

        # Try Authorization header first (REST/MCP clients)
        token: Optional[str] = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]

        # Fall back to session cookie (Web UI)
        if not token:
            token = request.cookies.get(CIDX_SESSION_COOKIE)

        if not token:
            return None

        payload = _jm.validate_token(token)
        return payload.get("jti")

    except (TokenExpiredError, InvalidTokenError):
        # Expected: token is expired or malformed; jti cannot be extracted.
        # This is normal — the client's token may already be invalid.
        return None
    except Exception:
        logger.debug(
            "_extract_jti_from_request: unexpected error extracting jti",
            exc_info=True,
        )
        return None


@web_router.get("/logout")
def logout(request: Request):
    """
    Logout and clear session.

    Redirects to unified login page after clearing session.
    Story #1163: also blacklists the JWT jti for cross-worker revocation.
    """
    # Story #923 AC8: revoke any elevation window for this session
    from code_indexer.server.auth.elevated_session_manager import (
        elevated_session_manager,
    )

    session_key = request.cookies.get("cidx_session")
    if session_key:
        try:
            elevated_session_manager.revoke(session_key)
        except Exception:
            logger.warning(
                "Failed to revoke elevation window on logout for session=%.8s",
                session_key,
                exc_info=True,
            )

    # Story #1163 AC1: blacklist the JWT jti so all workers/nodes reject it.
    # Non-fatal — any failure here must not block the logout redirect.
    try:
        jti = _extract_jti_from_request(request)
        if jti:
            from code_indexer.server.app import get_token_blacklist

            get_token_blacklist().add(jti)
        else:
            logger.warning(
                "logout: could not extract jti from request; token revocation skipped"
            )
    except Exception:
        logger.warning(
            "logout: failed to blacklist jti; proceeding with session clear",
            exc_info=True,
        )

    session_manager = get_session_manager()
    response = RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    session_manager.clear_session(response)
    return response


# Login method recorded by the Web login form.
_WEB_LOGIN_METHOD = "password"


def _web_login_mfa_state() -> str:
    """MFA state recorded for a Web login that issues a session directly.

    Only called when the user has no MFA enrolled (an enrolled user gets a
    challenge instead, recorded when it is answered).
    """
    from .. import mfa_routes

    return "not_applicable" if mfa_routes._totp_service is None else "not_enrolled"


@user_router.get("/logout")
def user_logout(request: Request):
    """
    Logout and clear session for user portal.

    Redirects to unified login page after clearing session.
    Story #1163: also blacklists the JWT jti for cross-worker revocation.
    """
    # Story #923 AC8: revoke any elevation window for this session
    from code_indexer.server.auth.elevated_session_manager import (
        elevated_session_manager,
    )

    session_key = request.cookies.get("cidx_session")
    if session_key:
        try:
            elevated_session_manager.revoke(session_key)
        except Exception:
            logger.warning(
                "Failed to revoke elevation window on user logout for session=%.8s",
                session_key,
                exc_info=True,
            )

    # Story #1163 AC1: blacklist the JWT jti so all workers/nodes reject it.
    # Non-fatal — any failure here must not block the logout redirect.
    try:
        jti = _extract_jti_from_request(request)
        if jti:
            from code_indexer.server.app import get_token_blacklist

            get_token_blacklist().add(jti)
        else:
            logger.warning(
                "user_logout: could not extract jti from request; token revocation skipped"
            )
    except Exception:
        logger.warning(
            "user_logout: failed to blacklist jti; proceeding with session clear",
            exc_info=True,
        )

    session_manager = get_session_manager()
    response = RedirectResponse(
        url="/login",
        status_code=status.HTTP_303_SEE_OTHER,
    )

    # Clear the session cookie
    session_manager.clear_session(response)

    return response


# ============================================================================
# Unified Login Routes (Phase 2: Login Consolidation)
# ============================================================================


@login_router.get("/login", response_class=HTMLResponse)
def unified_login_page(
    request: Request,
    redirect_to: Optional[str] = None,
    error: Optional[str] = None,
    info: Optional[str] = None,
):
    """
    Unified login page for all contexts (admin, user, OAuth).

    Supports:
    - SSO via OIDC (if enabled)
    - Username/password authentication
    - Smart redirect after login based on redirect_to parameter or user role

    Args:
        request: FastAPI Request object
        redirect_to: Optional URL to redirect after successful login
        error: Optional error message to display
        info: Optional info message to display
    """
    # Bug #715: Try to reuse existing valid CSRF token from cookie
    # This prevents race conditions when HTMX polling refreshes the login page
    # while user is filling out the form
    existing_csrf_token = get_csrf_token_from_cookie(request)
    if existing_csrf_token:
        csrf_token = existing_csrf_token
        need_new_cookie = False
    else:
        csrf_token = generate_csrf_token()
        need_new_cookie = True

    # Check if there's an expired session
    session_manager = get_session_manager()
    if not info and session_manager.is_session_expired(request):
        info = "Session expired, please login again"

    # Check if OIDC is enabled
    from ...auth.oidc import routes as oidc_routes

    sso_enabled = False
    if oidc_routes.oidc_manager and hasattr(oidc_routes.oidc_manager, "is_enabled"):
        sso_enabled = oidc_routes.oidc_manager.is_enabled()

    # Create response with CSRF token in signed cookie
    response = templates.TemplateResponse(
        request,
        "unified_login.html",
        {
            "request": request,
            "csrf_token": csrf_token,
            "redirect_to": redirect_to,
            "error": error,
            "info": info,
            "sso_enabled": sso_enabled,
        },
    )

    # Bug #715: Only set CSRF cookie if we generated a new token
    # This prevents overwriting valid cookies during HTMX polling
    if need_new_cookie:
        set_csrf_cookie(response, csrf_token, path="/")

    return response


@login_router.post("/login", response_class=HTMLResponse)
def unified_login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: Optional[str] = Form(None),
    redirect_to: Optional[str] = Form(None),
):
    """
    Process unified login form submission.

    Validates credentials and creates session on success.
    Accepts ANY role (normal_user, power_user, admin).
    Redirects based on redirect_to parameter or user role.

    Args:
        request: FastAPI Request object
        response: FastAPI Response object
        username: Username from form
        password: Password from form
        csrf_token: CSRF token from form
        redirect_to: Optional redirect URL from form
    """
    # CSRF validation - validate token against signed cookie
    if not validate_login_csrf_token(request, csrf_token):
        # CSRF validation failed - auto-recover by redirecting with fresh token
        # Bug #714: Instead of showing 403, redirect to login page for better UX
        logger.info(
            "CSRF validation failed, auto-recovering with fresh token",
            extra={"correlation_id": get_correlation_id()},
        )

        # Create redirect response to login page with session_expired message
        redirect_url = "/login?info=session_expired"
        redirect_response = RedirectResponse(
            url=redirect_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )

        # Clear old CSRF cookie and set fresh one
        redirect_response.delete_cookie(CSRF_COOKIE_NAME, path="/")
        new_csrf_token = generate_csrf_token()
        set_csrf_cookie(redirect_response, new_csrf_token, path="/")

        return redirect_response

    # Get user manager from dependencies
    user_manager = dependencies.user_manager
    if not user_manager:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User manager not available",
        )

    def _form_error(
        message: str, status_code: int = 200, headers: Optional[dict] = None
    ) -> Response:
        # Re-render the form with the error and a new CSRF token.
        new_csrf_token = generate_csrf_token()

        # Check if OIDC is enabled
        from ...auth.oidc import routes as oidc_routes

        sso_enabled = False
        if oidc_routes.oidc_manager and hasattr(oidc_routes.oidc_manager, "is_enabled"):
            sso_enabled = oidc_routes.oidc_manager.is_enabled()

        error_response = templates.TemplateResponse(
            request,
            "unified_login.html",
            {
                "request": request,
                "csrf_token": new_csrf_token,
                "redirect_to": redirect_to,
                "error": message,
                "sso_enabled": sso_enabled,
            },
            status_code=status_code,
            headers=headers,
        )
        set_csrf_cookie(error_response, new_csrf_token, path="/")
        return error_response

    # Per-username progressive throttle, shared with REST /auth/login and
    # OAuth authorize.  The attempt is RESERVED before the password is
    # checked (one row-locked transaction), so concurrent requests cannot
    # slip past it; while the backoff window runs every attempt -- a correct
    # password included -- is refused (no audit row, so refusals cannot
    # flood the store).  There is no lock state: a throttled username can
    # still authenticate with an API key, MCP credentials or SSO, and every
    # window ends on its own (cap 120 s).
    import math

    from ...auth import login_rate_limiter as _login_throttle

    throttle = _login_throttle.login_rate_limiter
    try:
        attempt = throttle.begin_attempt(username)
    except _login_throttle.ThrottleStoreBusy:
        return _form_error(
            "Login is busy, try again shortly.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"Retry-After": "1"},
        )
    if not attempt.admitted:
        wait = math.ceil(attempt.retry_after_seconds)
        return _form_error(
            f"Too many attempts, try again in {wait} seconds",
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"Retry-After": str(wait)},
        )

    # Authenticate user (any role accepted)
    user = user_manager.authenticate_user(username, password)

    if user is None:
        account_exists = user_manager.get_user(username) is not None
        if not account_exists:
            # An unknown name costs one password hash too (same helper as
            # REST), so response time does not reveal whether it exists.
            from ...auth.auth_error_handler import auth_error_handler

            auth_error_handler.perform_dummy_password_work()
        # The attempt's one outcome row (the attempt is already counted);
        # the typed name is recorded only when it names an existing account.
        reject_login(
            username,
            account_exists=account_exists,
            method=_WEB_LOGIN_METHOD,
            stage="credentials",
            reason=_login_throttle.failure_reason(attempt),
        )
        return _form_error("Invalid username or password")

    # Every MFA code at a login challenge is reserved on the account's login
    # throttle key before it is checked; only a completed login clears the
    # key (below), so a correct password with MFA pending clears nothing.

    # Story #565: Password expiry check -- before session creation
    config_svc = get_config_service()
    server_config = config_svc.get_config()
    expiry_config = server_config.password_expiry_config
    if expiry_config and user_manager.is_password_expired(user.username, expiry_config):
        # Redirect to change-password page with expiry message
        redirect_url = (
            "/admin/change-password?info=password_expired"
            if user.role.value == "admin"
            else "/user/change-password?info=password_expired"
        )

        # MFA enforcement -- check before creating session (same condition
        # and helper as the standard login branch below).
        if _get_user_mfa_status(user.username):
            from ...auth.mfa_challenge import mfa_challenge_manager
            from ..mfa_routes import render_mfa_challenge_page

            client_ip = request.client.host if request.client else "unknown"
            challenge_token = mfa_challenge_manager.create_challenge(
                username=user.username,
                role=user.role.value,
                client_ip=client_ip,
                redirect_url=redirect_url,
            )
            return render_mfa_challenge_page(challenge_token)

        session_manager = get_session_manager()
        expiry_response = RedirectResponse(
            url=redirect_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )
        complete_login(
            user.username,
            method=_WEB_LOGIN_METHOD,
            mfa=_web_login_mfa_state(),
            flow="web_session",
            issue=lambda: session_manager.create_session(
                expiry_response,
                username=user.username,
                role=user.role.value,
            ),
        )
        throttle.clear_completed_login(user.username)
        return expiry_response

    # Validate redirect_to URL (prevent open redirect)
    safe_redirect = None
    if redirect_to:
        # Only allow relative URLs starting with /
        if redirect_to.startswith("/") and not redirect_to.startswith("//"):
            safe_redirect = redirect_to

    # Smart redirect logic
    if safe_redirect:
        # Explicit redirect_to parameter takes precedence
        redirect_url = safe_redirect
    elif user.role.value == "admin":
        # Admin users go to admin dashboard
        redirect_url = "/admin/"
    else:
        # Non-admin users go to user interface
        redirect_url = "/user/api-keys"

    # Story #560: MFA enforcement — check before creating session
    if _get_user_mfa_status(user.username):
        from ...auth.mfa_challenge import mfa_challenge_manager
        from ..mfa_routes import render_mfa_challenge_page

        client_ip = request.client.host if request.client else "unknown"
        challenge_token = mfa_challenge_manager.create_challenge(
            username=user.username,
            role=user.role.value,
            client_ip=client_ip,
            redirect_url=redirect_url,
        )
        return render_mfa_challenge_page(challenge_token)

    # Create session for authenticated user
    session_manager = get_session_manager()
    redirect_response = RedirectResponse(
        url=redirect_url,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    complete_login(
        user.username,
        method=_WEB_LOGIN_METHOD,
        mfa=_web_login_mfa_state(),
        flow="web_session",
        issue=lambda: session_manager.create_session(
            redirect_response,
            username=user.username,
            role=user.role.value,
        ),
    )
    throttle.clear_completed_login(user.username)

    return redirect_response


@login_router.get("/login/sso")
async def unified_login_sso(
    request: Request,
    redirect_to: Optional[str] = None,
):
    """
    Initiate OIDC SSO flow from unified login page.

    Preserves redirect_to parameter through OIDC flow by storing
    it in the OIDC state parameter.

    Args:
        request: FastAPI Request object
        redirect_to: Optional URL to redirect after SSO completes
    """
    from ...auth.oidc import routes as oidc_routes

    # Check if OIDC is enabled
    if not oidc_routes.oidc_manager or not oidc_routes.oidc_manager.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO is not enabled on this server",
        )

    # Ensure OIDC provider is initialized
    try:
        await oidc_routes.oidc_manager.ensure_provider_initialized()
    except Exception as e:
        logger.error(
            format_error_log(
                "SVC-GENERAL-022",
                f"Failed to initialize OIDC provider: {e}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SSO provider is currently unavailable",
        )

    # Generate PKCE code verifier and challenge for OAuth 2.1 security
    import hashlib
    import base64

    code_verifier = secrets.token_urlsafe(32)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    # Validate redirect_to parameter (prevent open redirect)
    # Note: redirect_to is URL-encoded by JavaScript's encodeURIComponent, decode it
    from urllib.parse import unquote

    safe_redirect = None
    if redirect_to:
        # URL-decode (JavaScript's encodeURIComponent encoding)
        decoded_redirect = unquote(redirect_to)
        if decoded_redirect.startswith("/") and not decoded_redirect.startswith("//"):
            safe_redirect = decoded_redirect

    # Store state with code_verifier and redirect_to using OIDC state manager
    assert oidc_routes.state_manager is not None, (
        "state_manager must be initialized when oidc_manager is enabled"
    )
    state_data = {
        "code_verifier": code_verifier,
    }
    # Only include redirect_to if explicitly provided (let callback determine based on role otherwise)
    if safe_redirect:
        state_data["redirect_to"] = safe_redirect
    state_token = oidc_routes.state_manager.create_state(state_data)

    # Build OIDC authorization URL
    # Use CIDX_ISSUER_URL if set (for reverse proxy scenarios), otherwise use request.base_url
    issuer_url = os.getenv("CIDX_ISSUER_URL")
    if issuer_url:
        callback_url = f"{issuer_url.rstrip('/')}/auth/sso/callback"
    else:
        callback_url = str(request.base_url).rstrip("/") + "/auth/sso/callback"
    oidc_manager = oidc_routes.oidc_manager
    assert oidc_manager is not None, (
        "oidc_manager must be initialized when SSO login is invoked"
    )
    provider = oidc_manager.provider
    assert provider is not None, (
        "oidc provider must be initialized when SSO login is invoked"
    )
    oidc_auth_url = provider.get_authorization_url(
        state=state_token, redirect_uri=callback_url, code_challenge=code_challenge
    )

    return RedirectResponse(url=oidc_auth_url)


# ==============================================================================
# Backwards Compatibility Redirects (Phase 8: Login Consolidation)
# ==============================================================================


@login_router.get("/admin/login")
def redirect_admin_login(redirect_to: Optional[str] = None):
    """
    Backwards compatibility redirect: /admin/login → /login.

    301 Permanent Redirect to inform clients to update their URLs.
    """
    if redirect_to:
        return RedirectResponse(
            url=f"/login?redirect_to={quote(redirect_to)}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )
    return RedirectResponse(url="/login", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@login_router.get("/user/login")
def redirect_user_login(redirect_to: Optional[str] = None):
    """
    Backwards compatibility redirect: /user/login → /login.

    301 Permanent Redirect to inform clients to update their URLs.
    """
    if redirect_to:
        return RedirectResponse(
            url=f"/login?redirect_to={quote(redirect_to)}",
            status_code=status.HTTP_301_MOVED_PERMANENTLY,
        )
    return RedirectResponse(url="/login", status_code=status.HTTP_301_MOVED_PERMANENTLY)


@api_router.get("/server-time")
def get_server_time() -> Dict[str, str]:
    """
    Get current server time for client clock synchronization.

    Story #89: Server Clock in Navigation

    Returns current server time in ISO 8601 format with timezone information.
    This endpoint is lightweight and does not require authentication,
    allowing clients to synchronize their clock displays with server time.

    Returns:
        Dictionary with 'timestamp' (ISO 8601 UTC) and 'timezone' ('UTC')

    Example Response:
        {
            "timestamp": "2026-02-04T14:32:15Z",
            "timezone": "UTC"
        }
    """
    from datetime import datetime, timezone as tz

    # Get current UTC time
    current_time = datetime.now(tz.utc)

    # Format as ISO 8601 with Z suffix for UTC (preserves microseconds)
    # Replace '+00:00' with 'Z' for standard UTC notation
    timestamp = current_time.isoformat().replace("+00:00", "Z")

    return {"timestamp": timestamp, "timezone": "UTC"}
