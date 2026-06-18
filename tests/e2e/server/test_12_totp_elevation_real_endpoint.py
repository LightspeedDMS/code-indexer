"""Phase 3 — Story #1131: TOTP Step-Up Elevation via REAL /auth/elevate.

Tests the real REST endpoint POST /auth/elevate (NOT the shortcut via
elevated_session_manager.create()).  All assertions go through the HTTP front
door.  No mocks.

Design decisions
----------------
- Enforcement toggle is a function-scoped yield-fixture that uses try/finally
  to ALWAYS restore elevation_enforcement_enabled=False after each test, even
  when a test raises.  This prevents enforcement from leaking into the
  session-scoped log-audit gate (_phase3_log_audit_gate) which calls
  admin_logs_query at teardown — a gated endpoint — so if enforcement leaked
  the gate would demand a TOTP challenge and the entire Phase 3 would break.

- OTP flake guard: each OTP is generated immediately before use.  For the
  replay test, the exact OTP string used in the successful call is captured
  and replayed.  pyotp valid_window=1 is NOT used at the call site — the
  window is consumed on success and the replay must be rejected regardless of
  which 30s window is active.

- Kill-switch semantics: enforcement OFF means POST /auth/elevate returns 503
  (elevation_enforcement_disabled), NOT 200.  See elevation_routes.py:59-66.
  AC3 tests this by explicitly disabling enforcement and verifying 503.

- TOTP cleanup: every test that activates TOTP calls disable_mfa() in its
  finally block so subsequent tests start clean.

- Recovery code test: generate_recovery_codes() must be called AFTER
  activate_mfa() so the codes are bound to an enabled MFA record.

HTTP status constants use the exact values from elevation_routes.py to
make traceability explicit.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Iterator, Optional

import httpx
import jwt as pyjwt
import pyotp
import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import _auth_headers

# ---------------------------------------------------------------------------
# Environment variable names — no hardcoded defaults
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER = "E2E_ADMIN_USER"
_ENV_ADMIN_PASS = "E2E_ADMIN_PASS"

# ---------------------------------------------------------------------------
# HTTP status constants (mirrors elevation_routes.py for traceability)
# ---------------------------------------------------------------------------
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_SERVICE_UNAVAILABLE = 503

# Password that passes the server's strength policy (mirrors test_08 pattern)
_STRONG_PASSWORD = "ElevTestAb1!xyz"

# Role string accepted by POST /api/admin/users
_ROLE_NORMAL = "normal_user"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skip guard: skip entire module when admin credentials are absent
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not os.environ.get(_ENV_ADMIN_USER) or not os.environ.get(_ENV_ADMIN_PASS),
    reason=(
        "E2E_ADMIN_USER / E2E_ADMIN_PASS not set "
        "— skipping TOTP elevation real-endpoint E2E tests"
    ),
)


# ---------------------------------------------------------------------------
# Private helpers (each <= 20 lines, single responsibility)
# ---------------------------------------------------------------------------


def _admin_username() -> str:
    """Return admin username from environment."""
    return os.environ[_ENV_ADMIN_USER]


def _admin_password() -> str:
    """Return admin password from environment."""
    return os.environ[_ENV_ADMIN_PASS]


def _login_admin(client: TestClient, totp_secret: Optional[str] = None) -> str:
    """Login as admin and return the Bearer token string.

    When TOTP is enrolled for admin, POST /auth/login returns HTTP 200 with
    shape {"mfa_required": true, "mfa_token": "..."} instead of the normal
    {"access_token": "..."}.  If ``totp_secret`` is provided, this function
    detects the MFA-challenge shape and completes the two-factor flow via
    POST /auth/mfa/verify to obtain a real access_token.

    Raises AssertionError with a clear message if:
    - Login returns a non-200 status.
    - Login returns 200 but no access_token AND no mfa_token (unexpected shape).
    - MFA challenge is present but no totp_secret was provided.
    - The mfa/verify call fails.
    """
    resp = client.post(
        "/auth/login",
        json={"username": _admin_username(), "password": _admin_password()},
    )
    assert resp.status_code == HTTP_OK, (
        f"Admin login failed: {resp.status_code} — {resp.text[:300]}"
    )
    body = resp.json()

    # Normal path: TOTP not enrolled — login returns access_token directly.
    if "access_token" in body:
        return str(body["access_token"])

    # MFA-challenge path: TOTP enrolled — login returns mfa_token instead.
    assert "mfa_token" in body, (
        f"Admin login returned 200 but unexpected body shape "
        f"(no access_token, no mfa_token): {body}"
    )
    assert totp_secret is not None, (
        "Admin login returned an MFA challenge but no totp_secret was supplied "
        "to _login_admin() — pass the secret returned by _activate_admin_totp()."
    )
    mfa_token = body["mfa_token"]
    # Generate OTP immediately before the verify call (flake guard).
    otp = pyotp.TOTP(totp_secret).now()
    verify_resp = client.post(
        "/auth/mfa/verify",
        json={"mfa_token": mfa_token, "totp_code": otp},
    )
    assert verify_resp.status_code == HTTP_OK, (
        f"MFA verify failed: {verify_resp.status_code} — {verify_resp.text[:300]}"
    )
    verify_body = verify_resp.json()
    assert "access_token" in verify_body, (
        f"MFA verify returned 200 but no access_token: {verify_body}"
    )
    return str(verify_body["access_token"])


def _extract_jti(token: str) -> Optional[str]:
    """Extract jti claim from a JWT without signature verification."""
    try:
        decoded = pyjwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["HS256"],
        )
        jti = decoded.get("jti")
        return str(jti) if jti is not None else None
    except Exception as exc:
        logger.warning("Could not extract jti from token: %s", exc)
        return None


def _get_totp_service():
    """Return the live TOTPService instance from mfa_routes."""
    from code_indexer.server.web.mfa_routes import get_totp_service

    svc = get_totp_service()
    assert svc is not None, "TOTPService not initialised — server not started?"
    return svc


def _get_config():
    """Return the live ServerConfig from config_service."""
    from code_indexer.server.services.config_service import get_config_service

    return get_config_service().get_config()


def _activate_admin_totp(admin_username: str) -> str:
    """Generate and immediately activate a TOTP secret for admin_username.

    Returns the plaintext base32 secret.  Raises AssertionError if the OTP
    rolled over between generate and activate (extremely rare; re-run test).
    """
    totp_service = _get_totp_service()
    secret = totp_service.generate_secret(admin_username)
    totp = pyotp.TOTP(secret)
    # Generate OTP immediately and activate — single 30s window risk
    code = totp.now()
    activated = totp_service.activate_mfa(admin_username, code)
    assert activated, (
        "Failed to activate TOTP — OTP may have rolled over mid-call; re-run test."
    )
    return str(secret)


def _disable_admin_totp(admin_username: str) -> None:
    """Disable TOTP for admin_username (cleanup in finally blocks)."""
    totp_service = _get_totp_service()
    totp_service.disable_mfa(admin_username)


def _set_enforcement(enabled: bool) -> bool:
    """Enable/disable elevation enforcement; return prior value."""
    config = _get_config()
    prior = bool(config.elevation_enforcement_enabled)
    config.elevation_enforcement_enabled = enabled
    return prior


def _restore_enforcement(prior: bool) -> None:
    """Restore enforcement to prior value."""
    config = _get_config()
    config.elevation_enforcement_enabled = prior


def _call_elevate(
    client: TestClient,
    auth_token: str,
    totp_code: Optional[str] = None,
    recovery_code: Optional[str] = None,
) -> httpx.Response:
    """Call POST /auth/elevate with Bearer token and optional OTP or recovery code."""
    body: dict = {}
    if totp_code is not None:
        body["totp_code"] = totp_code
    if recovery_code is not None:
        body["recovery_code"] = recovery_code
    return client.post(
        "/auth/elevate",
        json=body,
        headers=_auth_headers(auth_token),
    )


# ---------------------------------------------------------------------------
# Fixture: function-scoped enforcement toggle with mandatory teardown
#
# CRITICAL: this fixture enables enforcement ON in setup and restores to OFF
# (the prior value) in teardown via yield-finally, so enforcement NEVER leaks
# into the autouse session-level _phase3_log_audit_gate which calls
# admin_logs_query (@require_mcp_elevation) at session teardown.
# ---------------------------------------------------------------------------


@pytest.fixture()
def enforcement_on(test_client: TestClient) -> Iterator[None]:
    """Enable elevation enforcement for one test; always restore on exit."""
    prior = _set_enforcement(True)
    try:
        yield
    finally:
        _restore_enforcement(prior)


# ---------------------------------------------------------------------------
# AC1 — enforcement ON; no-TOTP -> totp_setup_required (403); wrong OTP -> elevation_failed (401)
# ---------------------------------------------------------------------------


class TestAC1EnforcementOnNoTotpAndWrongOtp:
    """AC1: POST /auth/elevate with enforcement ON returns correct error codes."""

    def test_no_totp_enrolled_returns_totp_setup_required(
        self,
        test_client: TestClient,
        admin_token: str,
        enforcement_on: None,
    ) -> None:
        """Admin with NO TOTP enrolled -> totp_setup_required (403)."""
        username = _admin_username()
        # Ensure MFA is disabled (clean slate)
        totp_service = _get_totp_service()
        totp_service.disable_mfa(username)

        # Reuse the session-scoped admin_token (obtained before any TOTP enrollment)
        # so we never re-login and never hit the replay-prevention window.
        token = admin_token
        # Provide a syntactically valid 6-digit OTP code
        resp = _call_elevate(test_client, token, totp_code="123456")

        assert resp.status_code == HTTP_FORBIDDEN, (
            f"Expected 403 totp_setup_required, got {resp.status_code}: {resp.text[:300]}"
        )
        detail = resp.json().get("detail", {})
        assert detail.get("error") == "totp_setup_required", (
            f"Expected error='totp_setup_required', got: {detail}"
        )

    def test_wrong_otp_returns_elevation_failed(
        self,
        test_client: TestClient,
        admin_token: str,
        enforcement_on: None,
    ) -> None:
        """Admin with TOTP enrolled + wrong OTP -> elevation_failed (401)."""
        username = _admin_username()
        try:
            _activate_admin_totp(username)
            # Use the pre-TOTP session token — avoids re-login through the MFA
            # challenge that would hit the replay-prevention window poisoned by
            # activate_mfa() calling verify_code() with the same 30s window OTP.
            token = admin_token
            # Deliberately wrong OTP (000000 never matches a real TOTP)
            resp = _call_elevate(test_client, token, totp_code="000000")

            assert resp.status_code == HTTP_UNAUTHORIZED, (
                f"Expected 401 elevation_failed, got {resp.status_code}: {resp.text[:300]}"
            )
            detail = resp.json().get("detail", {})
            assert detail.get("error") == "elevation_failed", (
                f"Expected error='elevation_failed', got: {detail}"
            )
        finally:
            _disable_admin_totp(username)


# ---------------------------------------------------------------------------
# AC2 — correct OTP -> 200, gated op POST /api/admin/users -> 201, GET -> 200
# ---------------------------------------------------------------------------


class TestAC2CorrectOtpSuccessAndGatedOp:
    """AC2: Correct OTP -> 200; gated POST /api/admin/users -> 201; ungated GET -> 200."""

    def test_correct_otp_elevates_and_gated_op_succeeds(
        self,
        test_client: TestClient,
        admin_token: str,
        enforcement_on: None,
    ) -> None:
        """Correct OTP -> HTTP 200, then POST /api/admin/users -> 201, GET -> 200.

        Demonstrates per-op gating (not blanket): POST requires elevation,
        GET does not.  Both use the SAME Bearer token (window keyed by jti).
        """
        username = _admin_username()
        try:
            secret = _activate_admin_totp(username)
            # Reuse session-scoped admin_token (pre-enrollment) — avoids re-login
            # through the MFA challenge whose verify_code() would reject the code
            # as replay (activate_mfa already consumed the same 30s window).
            token = admin_token
            auth = _auth_headers(token)

            # Generate OTP immediately before the elevate call (flake guard)
            otp = pyotp.TOTP(secret).now()
            resp = _call_elevate(test_client, token, totp_code=otp)

            assert resp.status_code == HTTP_OK, (
                f"Expected 200 from /auth/elevate, got {resp.status_code}: {resp.text[:300]}"
            )
            body = resp.json()
            assert body.get("elevated") is True, f"Expected elevated=True: {body}"
            assert body.get("scope") == "full", f"Expected scope='full': {body}"

            # Gated op: POST /api/admin/users requires elevation -> 201
            # Elevation window is keyed by jti; Bearer token carries the same jti
            tmp_username = f"e2e_elev_{uuid.uuid4().hex[:8]}"
            create_resp = test_client.post(
                "/api/admin/users",
                json={
                    "username": tmp_username,
                    "password": _STRONG_PASSWORD,
                    "role": _ROLE_NORMAL,
                },
                headers=auth,
            )
            assert create_resp.status_code == HTTP_CREATED, (
                f"POST /api/admin/users expected 201, got "
                f"{create_resp.status_code}: {create_resp.text[:300]}"
            )

            # Ungated op: GET /api/admin/users requires only admin auth -> 200
            list_resp = test_client.get("/api/admin/users", headers=auth)
            assert list_resp.status_code == HTTP_OK, (
                f"GET /api/admin/users expected 200, got "
                f"{list_resp.status_code}: {list_resp.text[:300]}"
            )

            # Cleanup: delete the temp user (requires elevation still active)
            del_resp = test_client.delete(
                f"/api/admin/users/{tmp_username}",
                headers=auth,
            )
            if del_resp.status_code not in (HTTP_OK, HTTP_NO_CONTENT):
                logger.warning(
                    "Temp user cleanup returned %d for %r: %s",
                    del_resp.status_code,
                    tmp_username,
                    del_resp.text[:200],
                )
        finally:
            _disable_admin_totp(username)


# ---------------------------------------------------------------------------
# AC3 — replay -> 401; kill-switch -> 503 NOT 403; recovery code -> totp_repair scope
# ---------------------------------------------------------------------------


class TestAC3ReplayKillSwitchRecovery:
    """AC3: Replay -> 401; kill-switch -> 503; recovery code -> totp_repair scope."""

    def test_replay_same_otp_rejected(
        self,
        test_client: TestClient,
        admin_token: str,
        enforcement_on: None,
    ) -> None:
        """Mutation: replay consumed OTP -> 401 (CAS on last_used_otp_counter).
        Control: a fresh valid OTP -> elevation succeeds (200).

        Token strategy: obtain a second plain-login token BEFORE activating
        TOTP so both logins avoid the MFA challenge entirely.  Each login
        produces a distinct jti, giving two independent elevation windows.
        """
        username = _admin_username()
        try:
            # Obtain a second Bearer token BEFORE TOTP is active — plain login,
            # no MFA challenge, distinct jti from admin_token.
            fresh_token = _login_admin(test_client)

            # Now activate TOTP (this consumes one OTP window via verify_code,
            # but only poisons last_used_counter — NOT last_used_otp_counter
            # which is what /auth/elevate uses).
            secret = _activate_admin_totp(username)

            # Use the session-scoped admin_token for the first (replay) leg.
            token = admin_token

            # Generate OTP immediately before first use (flake guard)
            otp = pyotp.TOTP(secret).now()

            # First call: successful elevation
            first_resp = _call_elevate(test_client, token, totp_code=otp)
            assert first_resp.status_code == HTTP_OK, (
                f"First elevation expected 200, got "
                f"{first_resp.status_code}: {first_resp.text[:300]}"
            )

            # Replay the EXACT same OTP string -> 401 (CAS guard)
            replay_resp = _call_elevate(test_client, token, totp_code=otp)
            assert replay_resp.status_code == HTTP_UNAUTHORIZED, (
                f"Replay expected 401 elevation_failed, got "
                f"{replay_resp.status_code}: {replay_resp.text[:300]}"
            )
            detail = replay_resp.json().get("detail", {})
            assert detail.get("error") == "elevation_failed", (
                f"Expected error='elevation_failed' on replay, got: {detail}"
            )

            # Control: a FRESH valid OTP on a different jti (fresh_token, obtained
            # before TOTP activation, has its own clean last_used_otp_counter).
            # Wait for the next TOTP window if we're still in the same 30s slot.
            fresh_otp = pyotp.TOTP(secret).now()
            deadline = time.monotonic() + 35.0
            while fresh_otp == otp and time.monotonic() < deadline:
                time.sleep(1)
                fresh_otp = pyotp.TOTP(secret).now()

            control_resp = _call_elevate(test_client, fresh_token, totp_code=fresh_otp)
            assert control_resp.status_code == HTTP_OK, (
                f"Control elevation (fresh OTP, fresh jti) expected 200, got "
                f"{control_resp.status_code}: {control_resp.text[:300]}"
            )
        finally:
            _disable_admin_totp(username)

    def test_kill_switch_returns_503_not_403(
        self,
        test_client: TestClient,
        admin_token: str,
    ) -> None:
        """Kill switch (enforcement OFF) -> POST /auth/elevate returns 503 NOT 403.

        This test does NOT use the enforcement_on fixture — it explicitly sets
        enforcement to False to verify the kill-switch 503 path.
        """
        username = _admin_username()
        try:
            secret = _activate_admin_totp(username)
            # Reuse session-scoped admin_token — no re-login needed after enrollment.
            token = admin_token

            # Enforce OFF (kill switch active)
            prior = _set_enforcement(False)
            try:
                otp = pyotp.TOTP(secret).now()
                resp = _call_elevate(test_client, token, totp_code=otp)

                assert resp.status_code == HTTP_SERVICE_UNAVAILABLE, (
                    f"Kill switch expected 503, got "
                    f"{resp.status_code}: {resp.text[:300]}"
                )
                detail = resp.json().get("detail", {})
                assert detail.get("error") == "elevation_enforcement_disabled", (
                    f"Expected error='elevation_enforcement_disabled', got: {detail}"
                )
            finally:
                _restore_enforcement(prior)
        finally:
            _disable_admin_totp(username)

    def test_recovery_code_grants_totp_repair_scope_only(
        self,
        test_client: TestClient,
        admin_token: str,
        enforcement_on: None,
    ) -> None:
        """Recovery code -> elevation succeeds with scope='totp_repair' ONLY."""
        username = _admin_username()
        try:
            _activate_admin_totp(username)
            totp_service = _get_totp_service()
            recovery_codes = totp_service.generate_recovery_codes(username)
            assert len(recovery_codes) > 0, "generate_recovery_codes returned no codes"

            # Reuse session-scoped admin_token — no re-login needed after enrollment.
            token = admin_token
            # Use first recovery code
            resp = _call_elevate(test_client, token, recovery_code=recovery_codes[0])

            assert resp.status_code == HTTP_OK, (
                f"Recovery code elevation expected 200, got "
                f"{resp.status_code}: {resp.text[:300]}"
            )
            body = resp.json()
            assert body.get("elevated") is True, f"Expected elevated=True: {body}"
            assert body.get("scope") == "totp_repair", (
                f"Expected scope='totp_repair' (narrow scope only), got: {body}"
            )
        finally:
            _disable_admin_totp(username)
