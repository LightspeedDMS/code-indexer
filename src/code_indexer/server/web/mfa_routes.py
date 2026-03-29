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
import logging
from typing import List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

_LOGIN_ROUTE = "/login"
_ADMIN_ROUTE = "/admin/"
_VERIFY_ROUTE = "/admin/mfa/verify"

mfa_router = APIRouter(prefix="/admin/mfa", tags=["mfa"])
_totp_service = None


def set_totp_service(service) -> None:  # type: ignore[no-untyped-def]
    """Inject TOTPService instance during server startup."""
    global _totp_service
    _totp_service = service


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


def _render_setup(
    qr_b64: str,
    manual_key: str,
    csrf: str,
    target_user: str,
    error: str = "",
    success: str = "",
    show_mode: bool = False,
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
        form_section = (
            "<p>Test your authenticator (optional):</p>"
            f"<form method='POST' action='{_VERIFY_ROUTE}'>"
            f"<input type='hidden' name='csrf_token' value='{csrf}'>"
            f"<input type='hidden' name='target_user' value='{target_user}'>"
            "<input type='hidden' name='test_only' value='1'>"
            "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' placeholder='000000' autocomplete='one-time-code'>"
            "<button type='submit' style='background:#333;color:#fff'>Test Code</button></form>"
            f"<a href='/admin/mfa/recovery-codes?user={target_user}' style='display:block;text-align:center;padding:12px;margin-top:10px;background:#444;color:#fff;border-radius:6px;text-decoration:none'>View Recovery Codes</a>"
        )
    else:
        title = "Set Up Two-Factor Authentication"
        success_msg = ""
        form_section = (
            "<p>Enter the 6-digit code from your app to verify:</p>"
            f"<form method='POST' action='{_VERIFY_ROUTE}'>"
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
        f"<a href='{_ADMIN_ROUTE}users' class='back'>Back to Users</a>"
        "</div></body></html>"
    )


def _render_recovery_codes(codes: List[str]) -> str:
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
        f"<a href='{_ADMIN_ROUTE}' class='done'>I Have Saved These Codes</a>"
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
    """
    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    # Determine target user (self or another user)
    target_user = user if user else admin_username

    if mode == "show":
        # Re-display existing QR (no regeneration)
        uri = _totp_service.get_provisioning_uri(target_user)
        if uri is None:
            return HTMLResponse(f"No MFA configured for {target_user}", status_code=404)
    else:
        # New setup — generate fresh secret
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
    """Regenerate and display recovery codes for a user."""
    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    target = user if user else admin_username
    codes = _totp_service.generate_recovery_codes(target)
    if codes is None:
        return HTMLResponse("Failed to generate recovery codes", status_code=500)
    logger.info("Recovery codes regenerated for %s (by %s)", target, admin_username)
    return HTMLResponse(_render_recovery_codes(codes))


def _render_qr_error(username: str, error_msg: str, show_mode: bool) -> HTMLResponse:
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
            qr_b64, manual_key, "", username, error=error_msg, show_mode=show_mode
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
    """Disable MFA. Requires valid TOTP code or recovery code."""
    username = _get_session_username(request)
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

    logger.info("MFA disabled for user %s", username)
    return RedirectResponse(_ADMIN_ROUTE, status_code=303)


# ==============================================================================
# MFA Login Challenge (Story #560)
# ==============================================================================


def render_mfa_challenge_page(token: str, error: str = "") -> HTMLResponse:
    """Render the TOTP challenge page shown after password verification.

    Returns HTMLResponse with X-Frame-Options: DENY to prevent clickjacking.
    """
    err = (
        f'<div style="color:#ff4444;background:#2a0a0a;padding:10px;border-radius:6px;margin:10px 0">{error}</div>'
        if error
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
        f"<input type='hidden' name='challenge_token' value='{token}'>"
        "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' "
        "placeholder='000000' autocomplete='one-time-code' autofocus required>"
        "<button type='submit'>Verify</button>"
        "</form>"
        "<span class='toggle' onclick='toggleRecovery()'>Use recovery code instead</span>"
        "</div>"
        f"<div id='recovery-section' class='hidden'>"
        f"<form method='POST' action='/admin/mfa/challenge/verify'>"
        f"<input type='hidden' name='challenge_token' value='{token}'>"
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

    On success: consume challenge, create session, redirect to dashboard.
    On failure: record attempt, re-render challenge with error.
    On expired/exhausted token: redirect to login page.
    """
    from ..auth.mfa_challenge import mfa_challenge_manager

    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    challenge = mfa_challenge_manager.get_challenge(challenge_token)
    if challenge is None:
        return RedirectResponse("/login?info=mfa_expired", status_code=303)

    # Verify TOTP or recovery code
    verified = False
    if recovery_code:
        client_ip = request.client.host if request.client else "unknown"
        verified = _totp_service.verify_recovery_code(
            challenge.username, recovery_code, ip_address=client_ip
        )
        error_msg = "Invalid recovery code"
    elif totp_code:
        verified = _totp_service.verify_code(challenge.username, totp_code)
        error_msg = "Invalid verification code"
    else:
        error_msg = "No code provided"

    if verified:
        # Success: consume token, create session, redirect
        challenge_data = mfa_challenge_manager.consume(challenge_token)
        redirect_url = challenge_data.redirect_url if challenge_data else _ADMIN_ROUTE

        from ..web.auth import get_session_manager

        session_mgr = get_session_manager()
        redirect_response = RedirectResponse(url=redirect_url, status_code=303)
        session_mgr.create_session(
            redirect_response,
            username=challenge.username,
            role="admin",
        )
        logger.info(
            "MFA login verified for %s (method=%s)",
            challenge.username,
            "recovery" if recovery_code else "totp",
        )
        return redirect_response

    # Failure: record attempt, re-render
    mfa_challenge_manager.record_attempt(challenge_token)
    remaining = mfa_challenge_manager.get_challenge(challenge_token)
    if remaining is None:
        # Attempts exhausted
        logger.warning("MFA challenge exhausted for %s", challenge.username)
        return RedirectResponse("/login?info=mfa_exhausted", status_code=303)

    return render_mfa_challenge_page(challenge_token, error=error_msg)
