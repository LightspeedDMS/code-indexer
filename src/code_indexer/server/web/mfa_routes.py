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
    qr_b64: str, manual_key: str, csrf: str, target_user: str, error: str = ""
) -> str:
    """Render MFA setup HTML inline."""
    err = (
        f'<div style="color:#ff4444;background:#2a0a0a;padding:10px;border-radius:6px;margin:10px 0">{error}</div>'
        if error
        else ""
    )
    user_label = (
        f"<p class='info' style='color:#00d4ff'>Setting up MFA for: <strong>{target_user}</strong></p>"
        if target_user
        else ""
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Set Up MFA - CIDX</title>'
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
        "</style></head><body><div class='c'>"
        "<h1>Set Up Two-Factor Authentication</h1>"
        f"{user_label}"
        f"{err}"
        "<p class='info'>Scan this QR code with your authenticator app</p>"
        f"<div style='text-align:center'><div class='qr'><img src='data:image/png;base64,{qr_b64}' alt='QR'></div></div>"
        "<p class='info'>Or enter this key manually:</p>"
        f"<div class='mk'>{manual_key}</div>"
        "<p>Enter the 6-digit code from your app to verify:</p>"
        f"<form method='POST' action='{_VERIFY_ROUTE}'>"
        f"<input type='hidden' name='csrf_token' value='{csrf}'>"
        f"<input type='hidden' name='target_user' value='{target_user}'>"
        "<input type='text' name='totp_code' maxlength='6' pattern='[0-9]{6}' placeholder='000000' autocomplete='one-time-code' autofocus required>"
        "<button type='submit'>Verify and Activate MFA</button></form>"
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

    qr_b64 = base64.b64encode(qr_bytes).decode()
    csrf = request.cookies.get("csrf_token", "")
    return HTMLResponse(_render_setup(qr_b64, manual_key, csrf, target_user))


@mfa_router.post("/verify", response_class=HTMLResponse)
def mfa_verify(
    request: Request,
    totp_code: str = Form(...),
    target_user: Optional[str] = Form(None),
):
    """Verify TOTP code and activate MFA. Shows recovery codes on success."""
    admin_username = _get_session_username(request)
    if not admin_username:
        return RedirectResponse(_LOGIN_ROUTE, status_code=303)
    if _totp_service is None:
        return HTMLResponse("MFA service not available", status_code=503)

    # Use target_user if admin is setting up for another user
    username = target_user if target_user else admin_username

    if _totp_service.activate_mfa(username, totp_code):
        codes = _totp_service.generate_recovery_codes(username)
        if codes is None:
            return HTMLResponse("Failed to generate recovery codes", status_code=500)
        logger.info("MFA activated for user %s (by %s)", username, admin_username)
        return HTMLResponse(_render_recovery_codes(codes))

    # Invalid code — re-display setup with error
    uri = _totp_service.get_provisioning_uri(username)
    if uri is None:
        return HTMLResponse("MFA setup expired. Please start over.", status_code=400)
    qr_bytes = _totp_service.generate_qr_code(uri)
    if qr_bytes is None:
        return HTMLResponse("QR generation failed", status_code=500)
    manual_key = _totp_service.get_manual_entry_key(username)
    if manual_key is None:
        return HTMLResponse("Failed to get manual entry key", status_code=500)

    qr_b64 = base64.b64encode(qr_bytes).decode()
    csrf = request.cookies.get("csrf_token", "")
    return HTMLResponse(
        _render_setup(
            qr_b64,
            manual_key,
            csrf,
            username,
            "Invalid verification code. Please try again.",
        )
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
