"""
MFA Setup Web UI Routes (Story #559).

Four endpoints:
- GET /admin/mfa/setup — QR code + manual entry key + verification form
- POST /admin/mfa/verify — Verify TOTP code, activate MFA, show recovery codes
- GET /admin/mfa/status — JSON status check
- POST /admin/mfa/disable — Disable MFA (requires TOTP code)

HTML rendered inline via helper functions to keep feature self-contained.
All service return values explicitly null-checked with error responses.
"""

import base64
import html as html_module
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from code_indexer.server.auth.elevated_session_manager import elevated_session_manager
from code_indexer.server.auth.dependencies import CIDX_SESSION_COOKIE

logger = logging.getLogger(__name__)

_LOGIN_ROUTE = "/login"
_ADMIN_ROUTE = "/admin/"
_VERIFY_ROUTE = "/admin/mfa/verify"

# Scope hierarchy: rank 0 = broadest ("full"), rank 1 = narrower ("totp_repair").
# A session satisfies required_scope R when session_rank <= required_rank.
_SCOPE_RANK: Dict[str, int] = {"full": 0, "totp_repair": 1}


def _resolve_session_key(request: Request) -> Optional[str]:
    """Return elevation session key: user_jti from request state, or cidx_session cookie.

    Web UI auth sets request.state.user_jti from the "session" cookie value via
    _hybrid_auth_impl. Bearer-authenticated callers carry user_jti from the JWT jti
    claim. Both paths store elevation windows under this key, so user_jti must be
    checked first. Falls back to the cidx_session cookie for compatibility.
    """
    jti = getattr(getattr(request, "state", None), "user_jti", None)
    if jti:
        return str(jti)
    cookie = request.cookies.get(CIDX_SESSION_COOKIE)
    return str(cookie) if cookie is not None else None


def _check_elevation_window(
    request: Request,
    required_scope: str = "full",
) -> Optional[Dict[str, Any]]:
    """Return error dict if elevation check fails, or None when check passes.

    Story #925 AC5/AC6: enforces TOTP step-up elevation for Web UI endpoints.
    Fails closed: no window -> returns elevation_required error dict.
    Both required_scope and session.scope are validated against _SCOPE_RANK;
    unknown values raise ValueError (programmer error, not a runtime auth failure).
    """
    if required_scope not in _SCOPE_RANK:
        raise ValueError(
            f"required_scope must be one of {sorted(_SCOPE_RANK)}, got {required_scope!r}"
        )

    session_key = _resolve_session_key(request)
    if not session_key:
        return {"error": "elevation_required", "message": "No active elevation window."}

    session = elevated_session_manager.touch_atomic(session_key)
    if session is None:
        return {"error": "elevation_required", "message": "No active elevation window."}

    if session.scope not in _SCOPE_RANK:
        raise ValueError(
            f"Session has unrecognized scope {session.scope!r}; "
            f"expected one of {sorted(_SCOPE_RANK)}"
        )
    session_rank = _SCOPE_RANK[session.scope]
    required_rank = _SCOPE_RANK[required_scope]
    if session_rank > required_rank:
        return {
            "error": "elevation_required",
            "message": (
                f"Scope {required_scope!r} required; current scope is {session.scope!r}."
            ),
        }
    return None


def _cross_user_setup_guard(
    request: Request,
    admin_username: str,
    target_user: str,
) -> Optional[Any]:
    """Enforce AC5 elevation + confirmation for cross-user TOTP setup.

    Returns a JSONResponse (403 or 400) when the guard fails, or None when
    all checks pass. Caller emits audit log only on the success path.
    """
    from fastapi.responses import JSONResponse as _JSONResponse

    elev_err = _check_elevation_window(request, required_scope="full")
    if elev_err is not None:
        return _JSONResponse(content=elev_err, status_code=403)

    confirm = request.query_params.get("confirm_overwrite")
    if confirm != "1":
        return _JSONResponse(
            content={
                "error": "confirm_overwrite_required",
                "message": (
                    "Overwriting another admin's TOTP requires confirm_overwrite=1 "
                    "in the request."
                ),
            },
            status_code=400,
        )
    return None


mfa_router = APIRouter(prefix="/admin/mfa", tags=["mfa"])
user_mfa_router = APIRouter(tags=["user-mfa"])
_totp_service = None


def set_totp_service(service) -> None:  # type: ignore[no-untyped-def]
    """Inject TOTPService instance during server startup."""
    global _totp_service
    _totp_service = service


def get_totp_service():  # type: ignore[no-untyped-def]
    """Public accessor for the TOTPService instance.

    Returns None if the service hasn't been initialized yet.
    """
    return _totp_service


def _get_session_username(request: Request) -> Optional[str]:
    """Extract username from admin session."""
    from ..web.auth import get_session_manager

    try:
        session_mgr = get_session_manager()
    except RuntimeError as e:
        logger.debug("Session manager not initialized: %s", e)
        return None
    session = session_mgr.get_session(request)
    if session is None or session.role != "admin":
        return None
    return str(session.username)


def _get_any_session_username(request: Request) -> Optional[str]:
    """Extract username from any authenticated session (not just admin)."""
    from ..web.auth import get_session_manager

    try:
        session_mgr = get_session_manager()
    except RuntimeError:
        return None
    session = session_mgr.get_session(request)
    if session is None:
        return None
    return str(session.username)


def _render_setup(
    qr_b64: str,
    manual_key: str,
    csrf: str,
    target_user: str,
    error: str = "",
    success: str = "",
    show_mode: bool = False,
    verify_route: str = "/admin/mfa/verify",
    back_link: str = "/admin/users",
    recovery_link_prefix: str = "/admin/mfa",
    re_setup_link: str = "",
) -> str:
    """Render MFA setup or show-QR HTML inline.

    show_mode=False: Setup flow with "Verify and Activate MFA" button.
    show_mode=True:  Show existing QR with "Test Code" and "View Recovery Codes".
    """
    err = (
        f'<div style="color:#ff4444;background:#2a0a0a;padding:10px;border-radius:6px;margin:10px 0">{error}</div>'
        if error
        else ""
    )
    ok = (
        f'<div style="color:#44ff44;background:#0a2a0a;padding:10px;border-radius:6px;margin:10px 0">{success}</div>'
        if success
        else ""
    )
    user_label = (
        f"<p class='info' style='color:#00d4ff'>MFA for: <strong>{target_user}</strong></p>"
        if target_user
        else ""
    )

    if show_mode:
        title = "Two-Factor Authentication (Active)"
        success_msg = "<div style='color:#44ff44;background:#0a2a0a;padding:10px;border-radius:6px;margin:10px 0'>MFA is enabled. Scan this QR code to add to another authenticator device.</div>"
        re_setup_html = ""
        if re_setup_link:
            re_setup_html = (
                "<div style='margin-top:20px;padding-top:15px;border-top:1px solid #333'>"
                "<p class='info' style='color:#ff9900'>Lost your authenticator? Generate a new key:</p>"
                f"<a href='{re_setup_link}' "
                "style='display:block;text-align:center;padding:10px;background:#6b2222;color:#fff;"
                "border-radius:6px;text-decoration:none;font-size:0.9em'>Re-setup MFA (New Key)</a></div>"
            )
        form_section = (
            "<p>Test your authenticator (optional):</p>"
            f"<form method='POST' action='{verify_route}'>"
            f"<input type='hidden' name='csrf_token' value='{csrf}'>"
            f"<input type='hidden' name='target_user' value='{target_user}'>"
            "<input type='hidden' name='test_only' value='1'>"
            "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' placeholder='000000' autocomplete='one-time-code'>"
            "<button type='submit' style='background:#333;color:#fff'>Test Code</button></form>"
            f"<a href='{recovery_link_prefix}/recovery-codes?user={target_user}' style='display:block;text-align:center;padding:12px;margin-top:10px;background:#444;color:#fff;border-radius:6px;text-decoration:none'>View Recovery Codes</a>"
            f"{re_setup_html}"
        )
    else:
        title = "Set Up Two-Factor Authentication"
        success_msg = ""
        form_section = (
            "<p>Enter the 6-digit code from your app to verify:</p>"
            f"<form method='POST' action='{verify_route}'>"
            f"<input type='hidden' name='csrf_token' value='{csrf}'>"
            f"<input type='hidden' name='target_user' value='{target_user}'>"
            "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' placeholder='000000' autocomplete='one-time-code' autofocus required>"
            "<button type='submit'>Verify and Activate MFA</button></form>"
        )

    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><title>{title} - CIDX</title>'
        "<style>body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}"
        ".c{max-width:500px;margin:40px auto;background:#16213e;border-radius:12px;padding:30px}"
        "h1{color:#00d4ff;font-size:1.4em}"
        ".qr{text-align:center;margin:20px 0;background:#fff;padding:20px;border-radius:8px;display:inline-block}"
        ".qr img{width:200px;height:200px}"
        ".mk{background:#0a0a23;padding:12px;border-radius:6px;font-family:monospace;font-size:1.1em;letter-spacing:2px;text-align:center;margin:15px 0;color:#00d4ff}"
        ".info{color:#999;font-size:0.9em;margin:15px 0}"
        "input[type=text]{width:100%;padding:12px;font-size:1.2em;text-align:center;letter-spacing:8px;border:2px solid #333;border-radius:6px;background:#0a0a23;color:#fff;box-sizing:border-box}"
        "button{width:100%;padding:12px;margin-top:15px;background:#00d4ff;color:#000;border:none;border-radius:6px;font-size:1em;cursor:pointer;font-weight:bold}"
        "a.back{color:#00d4ff;text-decoration:none;display:block;margin-top:20px;text-align:center}"
        f"</style></head><body><div class='c'>"
        f"<h1>{title}</h1>"
        f"{user_label}"
        f"{success_msg}"
        f"{err}"
        f"{ok}"
        "<p class='info'>Scan this QR code with your authenticator app</p>"
        f"<div style='text-align:center'><div class='qr'><img src='data:image/png;base64,{qr_b64}' alt='QR'></div></div>"
        "<p class='info'>Or enter this key manually:</p>"
        f"<div class='mk'>{manual_key}</div>"
        f"{form_section}"
        f"<a href='{back_link}' class='back'>Back</a>"
        "</div></body></html>"
    )


def _render_recovery_codes(codes: List[str], done_link: str = "/admin/") -> str:
    """Render recovery codes HTML inline."""
    codes_html = "".join(f"<div style='color:#00d4ff'>{c}</div>" for c in codes)
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Recovery Codes - CIDX</title>"
        "<style>body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}"
        ".c{max-width:500px;margin:40px auto;background:#16213e;border-radius:12px;padding:30px}"
        "h1{color:#44ff44;font-size:1.4em}"
        ".warn{background:#2a2a0a;color:#ffcc00;padding:12px;border-radius:6px;margin:15px 0;border-left:4px solid #ffcc00}"
        ".codes{background:#0a0a23;padding:20px;border-radius:6px;font-family:monospace;font-size:1.1em;line-height:2}"
        f"a.done{{display:block;text-align:center;padding:12px;margin-top:20px;background:#00d4ff;color:#000;border-radius:6px;text-decoration:none;font-weight:bold}}"
        "</style></head><body><div class='c'>"
        "<h1>MFA Activated Successfully</h1>"
        "<div class='warn'>Save these recovery codes in a secure location. Each code can only be used once.</div>"
        f"<div class='codes'>{codes_html}</div>"
        f"<a href='{done_link}' class='done'>I Have Saved These Codes</a>"
        "</div></body></html>"
    )


@mfa_router.get("/setup", response_class=HTMLResponse)
def mfa_setup_page(
    request: Request, user: Optional[str] = None, mode: Optional[str] = None
):
    """Show MFA setup page with QR code and verification form.

    Args:
        user: Target username (admin setting up MFA for another user).
              Defaults to the logged-in user if not specified.
        mode: If 'show', re-displays existing QR without regenerating secret.

    Story #925 AC5: Cross-user setup requires active full-scope elevation +
    explicit confirm_overwrite=1. Audit log written immediately before success return.
    """
    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    target_user = user if user else admin_username
    is_cross_user = target_user != admin_username

    # AC5: cross-user guard (elevation + explicit confirmation)
    if is_cross_user:
        guard_err = _cross_user_setup_guard(request, admin_username, target_user)
        if guard_err is not None:
            return guard_err

    if mode == "show":
        uri = _totp_service.get_provisioning_uri(target_user)
        if uri is None:
            return HTMLResponse(f"No MFA configured for {target_user}", status_code=404)
    else:
        secret = _totp_service.generate_secret(target_user)
        if secret is None:
            return HTMLResponse("Failed to generate secret", status_code=500)
        uri = _totp_service.get_provisioning_uri(target_user)
        if uri is None:
            return HTMLResponse("Failed to generate URI", status_code=500)

    qr_bytes = _totp_service.generate_qr_code(uri)
    if qr_bytes is None:
        return HTMLResponse("Failed to generate QR code", status_code=500)
    manual_key = _totp_service.get_manual_entry_key(target_user)
    if manual_key is None:
        return HTMLResponse("Failed to get manual entry key", status_code=500)

    is_show = mode == "show"
    verified = request.query_params.get("verified") == "1"
    qr_b64 = base64.b64encode(qr_bytes).decode()
    csrf = request.cookies.get("csrf_token", "")

    # AC5: accurate audit log immediately before success return
    if is_cross_user:
        action = "viewed existing TOTP QR" if is_show else "regenerated TOTP secret"
        logger.info(
            "Admin %s %s for %s (cross-user setup, elevation confirmed)",
            admin_username,
            action,
            target_user,
        )

    return HTMLResponse(
        _render_setup(
            qr_b64,
            manual_key,
            csrf,
            target_user,
            show_mode=is_show,
            success="Code verified successfully!" if verified else "",
        )
    )


@mfa_router.get("/recovery-codes", response_class=HTMLResponse)
def mfa_recovery_codes_page(request: Request, user: Optional[str] = None):
    """Regenerate and display recovery codes for a user.

    Story #925 AC6: requires active elevation window.
    Cross-user operations require full scope; self-service accepts totp_repair scope.
    """
    from fastapi.responses import JSONResponse as _JSONResponse

    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    target = user if user else admin_username
    is_cross_user = target != admin_username
    required_scope = "full" if is_cross_user else "totp_repair"

    elev_err = _check_elevation_window(request, required_scope=required_scope)
    if elev_err is not None:
        return _JSONResponse(content=elev_err, status_code=403)

    codes = _totp_service.generate_recovery_codes(target)
    if codes is None:
        return HTMLResponse("Failed to generate recovery codes", status_code=500)
    logger.info("Recovery codes regenerated for %s (by %s)", target, admin_username)
    return HTMLResponse(_render_recovery_codes(codes))


def _render_qr_error(
    username: str,
    error_msg: str,
    show_mode: bool,
    verify_route: str = "/admin/mfa/verify",
    back_link: str = "/admin/users",
    recovery_link_prefix: str = "/admin/mfa",
    re_setup_link: str = "",
) -> HTMLResponse:
    """Re-render QR page with error message after failed verification."""
    assert _totp_service is not None
    uri = _totp_service.get_provisioning_uri(username)
    if uri is None:
        return HTMLResponse("MFA not configured. Please start over.", status_code=400)
    qr_bytes = _totp_service.generate_qr_code(uri)
    if qr_bytes is None:
        return HTMLResponse("QR generation failed", status_code=500)
    manual_key = _totp_service.get_manual_entry_key(username)
    if manual_key is None:
        return HTMLResponse("Failed to get key", status_code=500)
    qr_b64 = base64.b64encode(qr_bytes).decode()
    return HTMLResponse(
        _render_setup(
            qr_b64,
            manual_key,
            "",
            username,
            error=error_msg,
            show_mode=show_mode,
            verify_route=verify_route,
            back_link=back_link,
            recovery_link_prefix=recovery_link_prefix,
            re_setup_link=re_setup_link,
        )
    )


@mfa_router.post("/verify", response_class=HTMLResponse)
def mfa_verify(
    request: Request,
    totp_code: str = Form(...),
    target_user: Optional[str] = Form(None),
    test_only: Optional[str] = Form(None),
):
    """Verify TOTP code. Activates MFA on setup flow, or tests code on show flow."""
    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    username = target_user if target_user else admin_username

    if test_only:
        if _totp_service.verify_code(username, totp_code):
            return RedirectResponse(
                f"/admin/mfa/setup?user={username}&mode=show&verified=1",
                status_code=303,
            )
        return _render_qr_error(username, "Invalid code.", show_mode=True)

    # Setup mode: activate MFA
    if _totp_service.activate_mfa(username, totp_code):
        codes = _totp_service.generate_recovery_codes(username)
        if codes is None:
            return HTMLResponse("Failed to generate recovery codes", status_code=500)
        logger.info("MFA activated for user %s (by %s)", username, admin_username)
        return HTMLResponse(_render_recovery_codes(codes))

    return _render_qr_error(
        username, "Invalid verification code. Please try again.", show_mode=False
    )


@mfa_router.get("/status")
def mfa_status(request: Request):
    """Check MFA status for current user (JSON)."""
    username = _get_session_username(request)
    if not username:
        return {"error": "not_authenticated"}
    if _totp_service is None:
        return {"error": "mfa_service_unavailable"}
    return {"username": username, "mfa_enabled": _totp_service.is_mfa_enabled(username)}


@mfa_router.post("/disable", response_class=HTMLResponse)
def mfa_disable(request: Request, totp_code: str = Form(...)):
    """Disable MFA. Requires valid TOTP code or recovery code.

    Story #925 AC6: requires active totp_repair-scope elevation window.
    """
    from fastapi.responses import JSONResponse as _JSONResponse

    username = _get_session_username(request)
    if not username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    elev_err = _check_elevation_window(request, required_scope="totp_repair")
    if elev_err is not None:
        return _JSONResponse(content=elev_err, status_code=403)

    valid = _totp_service.verify_code(
        username, totp_code
    ) or _totp_service.verify_recovery_code(username, totp_code)
    if not valid:
        return HTMLResponse("Invalid code. MFA was NOT disabled.", status_code=400)

    try:
        _totp_service.disable_mfa(username)
    except Exception as e:
        logger.error("Failed to disable MFA for %s: %s", username, e)
        return HTMLResponse("Failed to disable MFA", status_code=500)

    logger.info("MFA disabled for user %s", username)
    return RedirectResponse(_ADMIN_ROUTE, status_code=303)


# ==============================================================================
# User (non-admin) MFA Routes
# ==============================================================================

_USER_MFA_VERIFY_ROUTE = "/user/mfa/verify"
_USER_BACK_LINK = "/user/api-keys"
_USER_RECOVERY_PREFIX = "/user/mfa"


@user_mfa_router.get("/setup", response_class=HTMLResponse)
def user_mfa_setup_page(request: Request, mode: Optional[str] = None):
    """Show MFA setup page for the authenticated user (self-only)."""
    username = _get_any_session_username(request)
    if not username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    mfa_enabled = _totp_service.is_mfa_enabled(username)

    if mfa_enabled and mode != "new":
        # MFA already active -- show existing QR with test option
        uri = _totp_service.get_provisioning_uri(username)
        if uri is None:
            return HTMLResponse(f"No MFA configured for {username}", status_code=404)
        is_show = True
    else:
        # New setup or explicit re-setup
        secret = _totp_service.generate_secret(username)
        if secret is None:
            return HTMLResponse("Failed to generate secret", status_code=500)
        uri = _totp_service.get_provisioning_uri(username)
        if uri is None:
            return HTMLResponse("Failed to generate URI", status_code=500)
        is_show = False

    qr_bytes = _totp_service.generate_qr_code(uri)
    if qr_bytes is None:
        return HTMLResponse("Failed to generate QR code", status_code=500)
    manual_key = _totp_service.get_manual_entry_key(username)
    if manual_key is None:
        return HTMLResponse("Failed to get manual entry key", status_code=500)

    verified = request.query_params.get("verified") == "1"
    qr_b64 = base64.b64encode(qr_bytes).decode()
    csrf = request.cookies.get("csrf_token", "")
    return HTMLResponse(
        _render_setup(
            qr_b64,
            manual_key,
            csrf,
            username,
            show_mode=is_show,
            success="Code verified successfully!" if verified else "",
            verify_route=_USER_MFA_VERIFY_ROUTE,
            back_link=_USER_BACK_LINK,
            recovery_link_prefix=_USER_RECOVERY_PREFIX,
            re_setup_link="/user/mfa/setup?mode=new" if is_show else "",
        )
    )


@user_mfa_router.post("/verify", response_class=HTMLResponse)
def user_mfa_verify(
    request: Request,
    totp_code: str = Form(...),
    test_only: Optional[str] = Form(None),
):
    """Verify TOTP code for session user (self-only, no target_user).

    test_only mode: verifies code without activating, redirects to show mode.
    Activation mode: activates MFA, shows recovery codes with done link.
    """
    username = _get_any_session_username(request)
    if not username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    if test_only:
        if _totp_service.verify_code(username, totp_code):
            return RedirectResponse(
                "/user/mfa/setup?mode=show&verified=1",
                status_code=303,
            )
        return _render_qr_error(
            username,
            "Invalid code.",
            show_mode=True,
            verify_route=_USER_MFA_VERIFY_ROUTE,
            back_link=_USER_BACK_LINK,
            recovery_link_prefix=_USER_RECOVERY_PREFIX,
            re_setup_link="/user/mfa/setup?mode=new",
        )

    if _totp_service.activate_mfa(username, totp_code):
        codes = _totp_service.generate_recovery_codes(username)
        if codes is None:
            return HTMLResponse("Failed to generate recovery codes", status_code=500)
        logger.info("MFA activated for user %s (self-service)", username)
        return HTMLResponse(_render_recovery_codes(codes, done_link=_USER_BACK_LINK))

    return _render_qr_error(
        username,
        "Invalid verification code. Please try again.",
        show_mode=False,
        verify_route=_USER_MFA_VERIFY_ROUTE,
        back_link=_USER_BACK_LINK,
        recovery_link_prefix=_USER_RECOVERY_PREFIX,
    )


@user_mfa_router.get("/recovery-codes", response_class=HTMLResponse)
def user_mfa_recovery_codes_page(request: Request):
    """Regenerate and display recovery codes for session user (self-only)."""
    username = _get_any_session_username(request)
    if not username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    codes = _totp_service.generate_recovery_codes(username)
    if codes is None:
        return HTMLResponse("Failed to generate recovery codes", status_code=500)
    logger.info("Recovery codes regenerated for %s (self-service)", username)
    return HTMLResponse(_render_recovery_codes(codes, done_link=_USER_BACK_LINK))


@user_mfa_router.post("/disable", response_class=HTMLResponse)
def user_mfa_disable(request: Request, totp_code: str = Form(...)):
    """Disable MFA for session user (self-only). Requires valid TOTP or recovery code."""
    username = _get_any_session_username(request)
    if not username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    valid = _totp_service.verify_code(
        username, totp_code
    ) or _totp_service.verify_recovery_code(username, totp_code)
    if not valid:
        return HTMLResponse("Invalid code. MFA was NOT disabled.", status_code=400)

    try:
        _totp_service.disable_mfa(username)
    except Exception as e:
        logger.error("Failed to disable MFA for %s: %s", username, e)
        return HTMLResponse("Failed to disable MFA", status_code=500)

    logger.info("MFA disabled for user %s (self-service)", username)
    return RedirectResponse(_USER_BACK_LINK, status_code=303)


@user_mfa_router.get("/status")
def user_mfa_status(request: Request):
    """Check MFA status for current user (JSON, self-only)."""
    username = _get_any_session_username(request)
    if not username:
        return {"error": "not_authenticated"}
    if _totp_service is None:
        return {"error": "mfa_service_unavailable"}
    return {"username": username, "mfa_enabled": _totp_service.is_mfa_enabled(username)}


# ==============================================================================
# MFA Login Challenge (Story #560)
# ==============================================================================


def render_mfa_challenge_page(token: str, error: str = "") -> HTMLResponse:
    """Render the TOTP challenge page shown after password verification.

    Returns HTMLResponse with X-Frame-Options: DENY to prevent clickjacking.
    """
    safe_error = html_module.escape(error) if error else ""
    safe_token = html_module.escape(token)
    err = (
        f'<div style="color:#ff4444;background:#2a0a0a;padding:10px;border-radius:6px;margin:10px 0">{safe_error}</div>'
        if safe_error
        else ""
    )
    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        "<title>Two-Factor Authentication - CIDX</title>"
        "<style>body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}"
        ".c{max-width:400px;margin:60px auto;background:#16213e;border-radius:12px;padding:30px}"
        "h1{color:#00d4ff;font-size:1.3em;text-align:center}"
        ".info{color:#999;font-size:0.9em;margin:15px 0;text-align:center}"
        "input[type=text]{width:100%;padding:14px;font-size:1.4em;text-align:center;letter-spacing:10px;"
        "border:2px solid #333;border-radius:6px;background:#0a0a23;color:#fff;box-sizing:border-box}"
        "button{width:100%;padding:12px;margin-top:15px;background:#00d4ff;color:#000;border:none;"
        "border-radius:6px;font-size:1em;cursor:pointer;font-weight:bold}"
        "button:hover{background:#00b8d4}"
        ".toggle{color:#00d4ff;text-decoration:none;display:block;text-align:center;margin-top:15px;"
        "font-size:0.9em;cursor:pointer}"
        ".hidden{display:none}"
        "a.back{color:#666;text-decoration:none;display:block;margin-top:20px;text-align:center;"
        "font-size:0.85em}"
        "</style>"
        "<script>"
        "function toggleRecovery(){"
        "var t=document.getElementById('totp-section');"
        "var r=document.getElementById('recovery-section');"
        "if(r.classList.contains('hidden')){r.classList.remove('hidden');t.classList.add('hidden');}"
        "else{t.classList.remove('hidden');r.classList.add('hidden');}}"
        "</script>"
        "</head><body><div class='c'>"
        "<h1>Two-Factor Authentication</h1>"
        "<p class='info'>Enter the code from your authenticator app</p>"
        f"{err}"
        f"<div id='totp-section'>"
        f"<form method='POST' action='/admin/mfa/challenge/verify'>"
        f"<input type='hidden' name='challenge_token' value='{safe_token}'>"
        "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' "
        "placeholder='000000' autocomplete='one-time-code' autofocus required>"
        "<button type='submit'>Verify</button>"
        "</form>"
        "<span class='toggle' onclick='toggleRecovery()'>Use recovery code instead</span>"
        "</div>"
        f"<div id='recovery-section' class='hidden'>"
        f"<form method='POST' action='/admin/mfa/challenge/verify'>"
        f"<input type='hidden' name='challenge_token' value='{safe_token}'>"
        "<p class='info'>Enter one of your recovery codes</p>"
        "<input type='text' name='recovery_code' placeholder='XXXX-XXXX-XXXX-XXXX' "
        "style='letter-spacing:2px;font-size:1em' required>"
        "<button type='submit'>Verify Recovery Code</button>"
        "</form>"
        "<span class='toggle' onclick='toggleRecovery()'>Use authenticator code instead</span>"
        "</div>"
        "<a href='/login' class='back'>Cancel and return to login</a>"
        "</div></body></html>"
    )
    response = HTMLResponse(html)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


def render_oauth_mfa_challenge_page(token: str, error: str = "") -> HTMLResponse:
    """Render the TOTP challenge page for the OAuth authorization flow (Story #562).

    Posts to /oauth/mfa/verify instead of /admin/mfa/challenge/verify so
    the OAuth authorization completes after TOTP verification.
    Returns HTMLResponse with X-Frame-Options: DENY to prevent clickjacking.
    """
    safe_error = html_module.escape(error) if error else ""
    safe_token = html_module.escape(token)
    err = (
        f'<div style="color:#ff4444;background:#2a0a0a;padding:10px;border-radius:6px;margin:10px 0">{safe_error}</div>'
        if safe_error
        else ""
    )
    html = (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        "<title>Two-Factor Authentication - CIDX</title>"
        "<style>body{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px}"
        ".c{max-width:400px;margin:60px auto;background:#16213e;border-radius:12px;padding:30px}"
        "h1{color:#00d4ff;font-size:1.3em;text-align:center}"
        ".info{color:#999;font-size:0.9em;margin:15px 0;text-align:center}"
        "input[type=text]{width:100%;padding:14px;font-size:1.4em;text-align:center;letter-spacing:10px;"
        "border:2px solid #333;border-radius:6px;background:#0a0a23;color:#fff;box-sizing:border-box}"
        "button{width:100%;padding:12px;margin-top:15px;background:#00d4ff;color:#000;border:none;"
        "border-radius:6px;font-size:1em;cursor:pointer;font-weight:bold}"
        "button:hover{background:#00b8d4}"
        ".toggle{color:#00d4ff;text-decoration:none;display:block;text-align:center;margin-top:15px;"
        "font-size:0.9em;cursor:pointer}"
        ".hidden{display:none}"
        "a.back{color:#666;text-decoration:none;display:block;margin-top:20px;text-align:center;"
        "font-size:0.85em}"
        "</style>"
        "<script>"
        "function toggleRecovery(){"
        "var t=document.getElementById('totp-section');"
        "var r=document.getElementById('recovery-section');"
        "if(r.classList.contains('hidden')){r.classList.remove('hidden');t.classList.add('hidden');}"
        "else{t.classList.remove('hidden');r.classList.add('hidden');}}"
        "</script>"
        "</head><body><div class='c'>"
        "<h1>Two-Factor Authentication</h1>"
        "<p class='info'>Enter the code from your authenticator app</p>"
        f"{err}"
        f"<div id='totp-section'>"
        f"<form method='POST' action='/oauth/mfa/verify'>"
        f"<input type='hidden' name='challenge_token' value='{safe_token}'>"
        "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' "
        "placeholder='000000' autocomplete='one-time-code' autofocus required>"
        "<button type='submit'>Verify</button>"
        "</form>"
        "<span class='toggle' onclick='toggleRecovery()'>Use recovery code instead</span>"
        "</div>"
        f"<div id='recovery-section' class='hidden'>"
        f"<form method='POST' action='/oauth/mfa/verify'>"
        f"<input type='hidden' name='challenge_token' value='{safe_token}'>"
        "<p class='info'>Enter one of your recovery codes</p>"
        "<input type='text' name='recovery_code' placeholder='XXXX-XXXX-XXXX-XXXX' "
        "style='letter-spacing:2px;font-size:1em' required>"
        "<button type='submit'>Verify Recovery Code</button>"
        "</form>"
        "<span class='toggle' onclick='toggleRecovery()'>Use authenticator code instead</span>"
        "</div>"
        "<a href='/login' class='back'>Cancel and return to login</a>"
        "</div></body></html>"
    )
    response = HTMLResponse(html)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


@mfa_router.post("/challenge/verify", response_class=HTMLResponse)
def mfa_challenge_verify(
    request: Request,
    challenge_token: str = Form(...),
    totp_code: Optional[str] = Form(None),
    recovery_code: Optional[str] = Form(None),
):
    """Verify TOTP or recovery code against a pending MFA challenge.

    Uses consume-first pattern to prevent TOCTOU race conditions:
    the token is atomically consumed before verification. On failure,
    the user must re-enter their password (token is gone).
    """
    from ..auth.mfa_challenge import mfa_challenge_manager

    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    # Consume-first: atomically remove token before verifying.
    # This prevents duplicate session creation from concurrent requests.
    challenge_data = mfa_challenge_manager.consume(challenge_token)
    if challenge_data is None:
        return RedirectResponse("/login?info=mfa_expired", status_code=303)

    # Validate client IP matches the one from password verification
    client_ip = request.client.host if request.client else "unknown"
    if challenge_data.client_ip != client_ip:
        logger.warning(
            "MFA challenge IP mismatch for %s: expected %s got %s",
            challenge_data.username,
            challenge_data.client_ip,
            client_ip,
        )
        return RedirectResponse("/login?info=mfa_expired", status_code=303)

    # Verify TOTP or recovery code
    verified = False
    method = "totp"
    if recovery_code:
        method = "recovery"
        verified = _totp_service.verify_recovery_code(
            challenge_data.username, recovery_code, ip_address=client_ip
        )
    elif totp_code:
        verified = _totp_service.verify_code(challenge_data.username, totp_code)

    if verified:
        from ..web.auth import get_session_manager

        session_mgr = get_session_manager()
        redirect_response = RedirectResponse(
            url=challenge_data.redirect_url, status_code=303
        )
        session_mgr.create_session(
            redirect_response,
            username=challenge_data.username,
            role=challenge_data.role,
        )
        logger.info(
            "MFA login verified for %s (method=%s)", challenge_data.username, method
        )
        return redirect_response

    # Verification failed — token is consumed, user must restart login
    logger.warning(
        "MFA verification failed for %s (method=%s)", challenge_data.username, method
    )
    return RedirectResponse("/login?info=mfa_failed", status_code=303)
