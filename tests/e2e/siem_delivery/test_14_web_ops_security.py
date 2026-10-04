"""The Web SIEM operator routes on the live server with elevation enforcement
ON (the LAST scenario of the phase: it changes global state and restores it).

Once admin TOTP is enrolled, both logins answer with an MFA challenge, so
every Web session is minted BEFORE enrollment; enroll, elevate and disable
each use a code from a DISTINCT 30 s window.  The module fixture's finalizer
ALWAYS restores enforcement OFF and the admin's MFA (even after a failed
assertion), each step independently, then proves the log-audit front door
(the gated ``admin_logs_query``) answers, so the session log-audit gate can
run.  A cleanup failure is reported as its own teardown error.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple

import httpx
import pyotp
import pytest

from tests.e2e.siem_delivery.conftest import SiemE2EConfig, admin_provider_for
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.web_ops import WebSession, text_of

_MK = re.compile(r"<div class='mk'>([^<]+)</div>")
_CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_IDLE = re.compile(r'name="elevation_idle_timeout_seconds"[^>]*value="(\d+)"')
_MAX_AGE = re.compile(r'name="elevation_max_age_seconds"[^>]*value="(\d+)"')
_OTP_ROLLOVER_SECONDS = 35.0
RESUME = "resume"
PASSWORD = "Example-Passw0rd-1!"  # front_door.EXAMPLE_PASSWORD


def _fresh_otp(secret: str, avoid: Optional[str]) -> str:
    """A code from a window other than *avoid*'s (bounded wait)."""
    deadline = time.monotonic() + _OTP_ROLLOVER_SECONDS
    code = pyotp.TOTP(secret).now()
    while code == avoid and time.monotonic() < deadline:
        time.sleep(1)
        code = pyotp.TOTP(secret).now()
    assert code != avoid, "no new TOTP window within the bound"
    return code


def _set_enforcement(web: WebSession, enabled: bool) -> None:
    page = web.get("/admin/config").text
    found = [p.search(page) for p in (_IDLE, _MAX_AGE, _CSRF)]
    assert all(found), "could not read the elevation settings from /admin/config"
    idle, max_age, csrf = (m.group(1) for m in found if m)
    resp = web.http.post(
        "/admin/config/totp_elevation",
        data={
            "elevation_enforcement_enabled": "true" if enabled else "false",
            "elevation_idle_timeout_seconds": idle,
            "elevation_max_age_seconds": max_age,
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200, f"enforcement={enabled}: {resp.status_code}"
    # The returned config page re-issues the session's CSRF cookie: adopt it.
    reissued = _CSRF.search(resp.text)
    assert reissued, "the config page carries no CSRF token"
    web.csrf = reissued.group(1)


@dataclass
class Enforced:
    config: SiemE2EConfig
    admin: WebSession  # TOTP enrolled; elevated only when a test elevates it
    admin_no_totp: WebSession
    normal: WebSession
    secret: str
    last_code: str


def _enroll(admin: WebSession, username: str) -> Tuple[str, str]:
    setup = admin.get("/admin/mfa/setup")
    assert setup.status_code == 200, f"mfa setup: {setup.status_code}"
    found = _MK.search(setup.text)
    assert found, "no manual-entry key on the MFA setup page"
    secret = found.group(1).replace(" ", "").strip()
    code = pyotp.TOTP(secret).now()
    verify = admin.http.post(
        "/admin/mfa/verify", data={"totp_code": code, "target_user": username}
    )
    assert verify.status_code == 200, f"mfa verify: {verify.status_code}"
    return secret, code


def _elevate_now(ctx: Enforced) -> None:
    """Open an elevation window on the admin's Web session (the modal's
    call) with a code from a new window; fails loudly if it is refused."""
    code = _fresh_otp(ctx.secret, avoid=ctx.last_code)
    resp = ctx.admin.elevate(code)
    assert (resp.status_code, resp.json()) == (200, {"success": True})
    ctx.last_code = code


def _restore(ctx: Enforced) -> None:
    """Elevate -> enforcement OFF -> disable MFA -> the gated log query.

    Every step runs in its own try (one failure never skips the others);
    all failures are raised together at the end."""
    from tests.e2e.log_audit_gate import query_logs_via_mcp

    failures: List[str] = []

    def step(name: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - every step runs; all reported
            failures.append(f"{name}: {exc!r}")

    def _elevate() -> None:
        code = _fresh_otp(ctx.secret, avoid=ctx.last_code)
        resp = ctx.admin.http.post("/auth/elevate", json={"totp_code": code})
        assert resp.status_code == 200, f"/auth/elevate: {resp.status_code}"
        ctx.last_code = code

    def _disable_mfa() -> None:
        code = _fresh_otp(ctx.secret, avoid=ctx.last_code)
        resp = ctx.admin.http.post("/user/mfa/disable", data={"totp_code": code})
        assert resp.status_code == 303, f"/user/mfa/disable: {resp.status_code}"
        status = ctx.admin.get("/user/mfa/status").json()
        assert status.get("mfa_enabled") is False, status

    def _log_query() -> None:
        cfg = ctx.config  # a JSON login succeeds only once MFA is off
        token = admin_provider_for(cfg.server_url, cfg.admin_user, cfg.admin_pass)
        with httpx.Client(base_url=cfg.server_url, timeout=60.0) as http:
            logs = query_logs_via_mcp(http, token.get_token())
        assert isinstance(logs, list), "the log-audit front door did not answer"

    step("elevate", _elevate)
    step("enforcement OFF", lambda: _set_enforcement(ctx.admin, enabled=False))
    step("disable admin MFA", _disable_mfa)
    step("log-audit front door", _log_query)
    if failures:
        raise AssertionError("test_14 cleanup failed: " + "; ".join(failures))


@pytest.fixture(scope="module")
def enforced(siem_config: SiemE2EConfig, siem_http: httpx.Client) -> Iterator[Enforced]:
    cfg = siem_config
    door = FrontDoor(
        siem_http, admin_provider_for(cfg.server_url, cfg.admin_user, cfg.admin_pass)
    )
    normal_user, other_admin = unique_name("siem-web-user"), unique_name("siem-web-adm")
    door.create_user(normal_user)
    door.create_user(other_admin, role="admin")
    admin = WebSession(cfg.server_url, cfg.admin_user, cfg.admin_pass)
    sessions = [
        admin,
        WebSession(cfg.server_url, other_admin, PASSWORD),
        WebSession(cfg.server_url, normal_user, PASSWORD, admin=False),
    ]
    ctx: Optional[Enforced] = None
    try:
        secret, code = _enroll(admin, cfg.admin_user)
        ctx = Enforced(cfg, admin, sessions[1], sessions[2], secret, code)
        _set_enforcement(admin, enabled=True)
        yield ctx
    finally:
        try:
            if ctx is not None:
                _restore(ctx)
        finally:
            for session in sessions:
                session.close()


def test_writes_are_refused_without_admin_totp_and_elevation(
    enforced: Enforced,
) -> None:
    # (anonymous -> 401 is proven by the unit matrix: on the live server it
    # logs an authentication WARNING the phase log audit rightly rejects)
    normal = enforced.normal.act(RESUME)
    assert (normal.status_code, normal.json()) == (
        403,
        {"detail": "Admin access required"},
    )
    setup = enforced.admin_no_totp.act(RESUME)
    assert setup.status_code == 403
    assert setup.json()["detail"]["error"] == "totp_setup_required"
    gate = enforced.admin.act(RESUME)
    assert gate.status_code == 403
    assert gate.json()["detail"]["error"] == "elevation_required"
    for read in ("arming", "recovery"):  # reads need no elevation
        assert (
            enforced.admin.get(f"/admin/siem-delivery/partials/{read}").status_code
            == 200
        )


def test_the_modal_elevation_replays_the_action(enforced: Enforced) -> None:
    totp = pyotp.TOTP(enforced.secret)
    wrong = enforced.admin.elevate(totp.at(int(time.time()) - 3600))
    assert wrong.status_code == 401
    assert enforced.admin.act(RESUME).status_code == 403
    _elevate_now(enforced)
    replay = enforced.admin.act(RESUME)
    assert replay.status_code == 200 and "resumed:" in text_of(replay.text)


def test_csrf_and_the_abandon_word_on_the_live_server(enforced: Enforced) -> None:
    _elevate_now(enforced)  # this test's own window (order-independent)
    assert enforced.admin.act(RESUME).status_code == 200  # elevation verified
    bad = enforced.admin.act(RESUME, csrf="not-the-token")
    assert bad.status_code == 403 and "Invalid CSRF token" in bad.text
    word = enforced.admin.act(
        "destinations/harness:0000000000000000/abandon", {"confirm_word": "abandon"}
    )
    assert word.status_code == 400
    assert "type ABANDON to confirm" in text_of(word.text)
