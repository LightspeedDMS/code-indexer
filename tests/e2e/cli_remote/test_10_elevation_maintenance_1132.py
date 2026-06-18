"""Phase 4 (live uvicorn) E2E — Story #1132 (Epic #1121).

Covers two acceptance areas through the REAL HTTP front door against a live
uvicorn subprocess (NOT a FastAPI TestClient — ``require_localhost`` rejects the
TestClient's synthetic ``testserver`` host):

AC1 — CLI elevation retry (#980)
AC2 — Maintenance localhost-only (#924)

----------------------------------------------------------------------------
MANUAL-EXECUTION LEARNINGS (empirically verified before this file was written)
----------------------------------------------------------------------------
The full flow was driven by hand against a live uvicorn subprocess.  Findings
that shape the implementation below:

1. The MFA manual-entry key rendered at ``/admin/mfa/setup`` is formatted as
   space-separated groups of four (``ABCD EFGH ...``).  Whitespace MUST be
   stripped before handing it to ``pyotp`` or base32 decoding raises.

2. Once admin TOTP is enrolled, BOTH the JSON ``POST /auth/login`` and the web
   ``POST /login`` return an MFA *challenge* (``mfa_required``/challenge HTML),
   NOT an access token / 303.  Therefore every Bearer token and the web session
   MUST be minted BEFORE enrollment.

3. The real ``cidx`` binary cannot authenticate as a TOTP-enrolled admin: it
   re-logs-in via JSON ``/auth/login`` on every admin command (no persisted
   ``.token``; only encrypted ``.creds``) and has no ``mfa_required`` handling,
   so ``_authenticate()`` raises ``No valid access token in response`` before
   any admin request is issued.  Consequently the elevation_required happy-path
   (T1) and wrong-OTP path (T2) cannot be driven through the ``cidx`` subprocess
   when the acting admin has TOTP.  They are instead driven through the REAL
   production client function ``with_elevation_retry`` (the exact function the
   CLI wires at cli.py:14491) against the LIVE server with a pre-enrollment
   Bearer token — real client code, real ``/auth/elevate``, real gated endpoint,
   no mocks.  The ``totp_setup_required`` path (T3) IS driven through the real
   ``cidx`` binary, because a non-TOTP admin under enforcement logs in normally
   and the gate raises ``totp_setup_required`` before any prompt.

4. Elevation windows are keyed by JWT ``jti``.  Each elevation test uses its OWN
   pre-enrollment Bearer token (distinct jti -> independent window) so one test's
   open window cannot bleed into another's first call.

5. OTP windows must be serialised: enroll, each elevate, and disable each need a
   code from a DISTINCT 30s window.  ``_fresh_otp(secret, avoid=...)`` waits for
   a rollover when necessary (bounded, Messi #14).

6. Teardown MUST restore global state before the session-scoped log-audit gate
   runs (it calls the gated ``admin_logs_query``): open a fresh web-session
   elevation window -> ``set_enforcement(False)`` -> self-service
   ``POST /user/mfa/disable`` (session-only, no elevation) -> assert a Bearer
   gated call no longer 403s.

7. ``require_elevation()`` is PASSTHROUGH while enforcement is OFF, so flipping
   enforcement ON needs only the web session + CSRF (no prior elevation).  The
   config POST requires all three fields; current values are READ live from
   ``/admin/config`` rather than hardcoded.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx
import pyotp
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import run_cidx

# ---------------------------------------------------------------------------
# Skip guard: skip the entire module when admin credentials are absent
# (mirrors tests/e2e/server/test_12_totp_elevation_real_endpoint.py:81).
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not os.environ.get("E2E_ADMIN_USER") or not os.environ.get("E2E_ADMIN_PASS"),
    reason=(
        "E2E_ADMIN_USER / E2E_ADMIN_PASS not set "
        "— skipping Story #1132 elevation/maintenance E2E tests"
    ),
)

# HTTP status constants (mirror server source for traceability)
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

# Password that passes the server strength policy (mirrors test_08 / test_12).
_STRONG_PASSWORD = "ElevTestAb1!xyz"
_ROLE_NORMAL = "normal_user"

# Bounded wait for a TOTP window rollover (one 30s slot + slack).
_OTP_ROLLOVER_DEADLINE_SECONDS = 35.0

# Regexes for scraping the web front door.
_MK_RE = re.compile(r"<div class='mk'>([^<]+)</div>")
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_IDLE_RE = re.compile(r'name="elevation_idle_timeout_seconds"[^>]*value="(\d+)"')
_MAXAGE_RE = re.compile(r'name="elevation_max_age_seconds"[^>]*value="(\d+)"')


# ---------------------------------------------------------------------------
# Front-door HTTP primitives (each one I/O against the live server; no mocks).
# ---------------------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    """Authorization header for a Bearer token."""
    return {"Authorization": f"Bearer {token}"}


def _mint_bearer(base_url: str, user: str, password: str) -> str:
    """Mint a fresh access token via JSON ``/auth/login`` (pre-enrollment only).

    Raises AssertionError if the response lacks ``access_token`` — which is what
    happens once admin TOTP is enrolled (the response becomes an MFA challenge),
    so this MUST be called before enrollment.
    """
    resp = httpx.post(
        f"{base_url}/auth/login",
        json={"username": user, "password": password},
        timeout=15.0,
    )
    body = resp.json()
    assert "access_token" in body, (
        f"JSON login did not return an access_token (status={resp.status_code}); "
        f"body={body}. Tokens must be minted BEFORE admin TOTP enrollment."
    )
    return str(body["access_token"])


def _web_login(base_url: str, user: str, password: str) -> httpx.Client:
    """Open an authenticated web session (signed ``session`` cookie).

    Must be done BEFORE admin TOTP is enrolled — once enrolled the web ``/login``
    form returns the MFA-challenge HTML instead of the 303 redirect.
    Returns a non-redirect-following client carrying the session cookie.
    """
    client = httpx.Client(base_url=base_url, follow_redirects=False, timeout=15.0)
    page = client.get("/login")
    csrf_match = _CSRF_RE.search(page.text)
    assert csrf_match, "No csrf_token on /login page"
    resp = client.post(
        "/login",
        data={
            "username": user,
            "password": password,
            "csrf_token": csrf_match.group(1),
        },
    )
    assert resp.status_code == 303, (
        f"Web login expected 303, got {resp.status_code}: {resp.text[:200]}. "
        "Was admin TOTP already enrolled before web_login?"
    )
    return client


def _enroll_admin_totp(web: httpx.Client, admin_user: str) -> str:
    """Enrol TOTP for ``admin_user`` via the web front door; return the secret.

    GET /admin/mfa/setup -> scrape the space-grouped manual-entry key from the
    ``.mk`` div (whitespace MUST be stripped) -> POST /admin/mfa/verify (session
    only, no login-CSRF).  Returns the plaintext base32 secret.
    """
    page = web.get("/admin/mfa/setup")
    assert page.status_code == HTTP_OK, (
        f"/admin/mfa/setup expected 200, got {page.status_code}: {page.text[:200]}"
    )
    mk_match = _MK_RE.search(page.text)
    assert mk_match, f"No '.mk' manual-key div on setup page: {page.text[:300]}"
    secret = mk_match.group(1).replace(" ", "").strip()
    verify = web.post(
        "/admin/mfa/verify",
        data={"totp_code": pyotp.TOTP(secret).now(), "target_user": admin_user},
    )
    assert verify.status_code == HTTP_OK, (
        f"/admin/mfa/verify expected 200, got {verify.status_code}: {verify.text[:200]}"
    )
    return secret


def _read_totp_config(web: httpx.Client) -> tuple[str, str, str]:
    """Read live (idle, max_age, csrf) from the admin config page.

    All three totp_elevation fields are required by POST /admin/config/totp_elevation,
    so we READ the current values rather than hardcoding them.
    """
    page = web.get("/admin/config")
    assert page.status_code == HTTP_OK, (
        f"/admin/config expected 200, got {page.status_code}"
    )
    idle = _IDLE_RE.search(page.text)
    max_age = _MAXAGE_RE.search(page.text)
    csrf = _CSRF_RE.search(page.text)
    assert idle and max_age and csrf, (
        "Could not scrape elevation config fields / csrf from /admin/config"
    )
    return idle.group(1), max_age.group(1), csrf.group(1)


def _set_enforcement(web: httpx.Client, *, enabled: bool) -> httpx.Response:
    """Flip elevation enforcement via the web config form.

    While enforcement is OFF the gate is passthrough, so turning it ON needs only
    the session + CSRF.  Turning it OFF while ON is itself a gated write, so the
    caller must hold an open elevation window on this same web session first.
    """
    idle, max_age, csrf = _read_totp_config(web)
    resp = web.post(
        "/admin/config/totp_elevation",
        data={
            "elevation_enforcement_enabled": "true" if enabled else "false",
            "elevation_idle_timeout_seconds": idle,
            "elevation_max_age_seconds": max_age,
            "csrf_token": csrf,
        },
    )
    return resp


def _fresh_otp(secret: str, *, avoid: Optional[str]) -> str:
    """Return a TOTP code distinct from ``avoid`` (waits for a window rollover).

    Bounded by _OTP_ROLLOVER_DEADLINE_SECONDS (Messi #14 — provable termination).
    """
    otp = pyotp.TOTP(secret).now()
    deadline = time.monotonic() + _OTP_ROLLOVER_DEADLINE_SECONDS
    while avoid is not None and otp == avoid and time.monotonic() < deadline:
        time.sleep(1)
        otp = pyotp.TOTP(secret).now()
    return otp


def _make_create_user_fn(
    base_url: str,
    session: httpx.Client,
    token: str,
    username: str,
) -> Callable[[], httpx.Response]:
    """Build the zero-arg ``fn`` that ``with_elevation_retry`` drives.

    Mirrors the production AdminAPIClient contract (admin_client.py
    ``_check_elevation_required``): on a 403 whose detail error is
    ``elevation_required`` / ``totp_setup_required`` it raises
    ``ElevationRequiredError`` so ``with_elevation_retry`` can react.
    """
    from code_indexer.api_clients.elevation import ElevationRequiredError

    def fn() -> httpx.Response:
        resp = session.post(
            f"{base_url}/api/admin/users",
            json={
                "username": username,
                "password": _STRONG_PASSWORD,
                "role": _ROLE_NORMAL,
            },
            headers=_bearer(token),
        )
        if resp.status_code == HTTP_FORBIDDEN:
            detail = resp.json().get("detail", {})
            code = detail.get("error") if isinstance(detail, dict) else None
            if code in ("elevation_required", "totp_setup_required"):
                setup_url = (
                    detail.get("setup_url") if isinstance(detail, dict) else None
                )
                raise ElevationRequiredError(error_code=code, setup_url=setup_url)
        return resp

    return fn


def _user_exists(base_url: str, admin_token: str, username: str) -> bool:
    """Return True iff ``username`` appears in GET /api/admin/users."""
    resp = httpx.get(
        f"{base_url}/api/admin/users", headers=_bearer(admin_token), timeout=15.0
    )
    resp.raise_for_status()
    body = resp.json()
    users = body.get("users", body) if isinstance(body, dict) else body
    return any(
        (u.get("username") if isinstance(u, dict) else u) == username for u in users
    )


def _delete_user(base_url: str, admin_token: str, username: str) -> None:
    """Best-effort delete of a temp user (cleanup; never raises)."""
    try:
        httpx.request(
            "DELETE",
            f"{base_url}/api/admin/users/{username}",
            headers=_bearer(admin_token),
            timeout=15.0,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixtures — function scoped, mandatory try/finally restoration.
#
# Both fixtures restore global state (enforcement OFF, admin TOTP disabled)
# before returning, then assert a Bearer gated call no longer 403s, so the
# session-scoped _phase4_log_audit_gate (which calls the GATED admin_logs_query
# at teardown) never trips on a leaked elevation requirement.
# ---------------------------------------------------------------------------


class _EnrolledContext:
    """Bundle passed to elevation tests: secret + admin token + base url."""

    def __init__(self, base_url: str, admin_user: str, secret: str, admin_token: str):
        self.base_url = base_url
        self.admin_user = admin_user
        self.secret = secret
        self.admin_token = admin_token


def _teardown_enrollment(
    web: httpx.Client,
    base_url: str,
    admin_user: str,
    secret: str,
    admin_token: str,
) -> None:
    """Restore clean global state: enforcement OFF + admin TOTP disabled.

    Order (empirically validated): open a FRESH web-session elevation window
    (needed because turning enforcement OFF while ON is itself a gated write) ->
    set_enforcement(False) -> self-service /user/mfa/disable (session-only) ->
    assert a Bearer gated call is no longer 403 (proves enforcement truly OFF).
    """
    # 1. Open a fresh elevation window on the web session (distinct OTP window).
    elev_otp = _fresh_otp(secret, avoid=pyotp.TOTP(secret).now())
    web.post("/auth/elevate", json={"totp_code": elev_otp})

    # 2. Disable enforcement (authorised by the window just opened).
    off_resp = _set_enforcement(web, enabled=False)
    assert off_resp.status_code == HTTP_OK, (
        f"Teardown set_enforcement(False) failed: {off_resp.status_code} "
        f"{off_resp.text[:200]}"
    )

    # 3. Self-service disable TOTP (needs a valid OTP from a NEW window).
    disable_otp = _fresh_otp(secret, avoid=elev_otp)
    web.post("/user/mfa/disable", data={"totp_code": disable_otp})

    # 4. Post-condition: a gated Bearer call must no longer be 403.
    probe = f"teardown_probe_{int(time.time())}"
    resp = httpx.post(
        f"{base_url}/api/admin/users",
        json={"username": probe, "password": _STRONG_PASSWORD, "role": _ROLE_NORMAL},
        headers=_bearer(admin_token),
        timeout=15.0,
    )
    assert resp.status_code != HTTP_FORBIDDEN, (
        "Teardown failed to clear enforcement — gated call still 403: "
        f"{resp.text[:200]}"
    )
    _delete_user(base_url, admin_token, probe)
    try:
        web.get("/logout")
    finally:
        web.close()


@pytest.fixture()
def elevation_enrolled(
    e2e_server_url: str,
    e2e_config: E2EConfig,
) -> Iterator[_EnrolledContext]:
    """Enrol admin TOTP + enable enforcement; ALWAYS restore on exit.

    Setup ordering is critical: mint the admin Bearer token and open the web
    session BEFORE enrolling TOTP (post-enrollment both logins return MFA
    challenges).  Yields an _EnrolledContext; teardown is unconditional.
    """
    base = e2e_server_url
    admin_user = e2e_config.admin_user
    admin_pass = e2e_config.admin_pass

    # Pre-enrollment: token for REST verification/cleanup + the web session.
    admin_token = _mint_bearer(base, admin_user, admin_pass)
    web = _web_login(base, admin_user, admin_pass)

    secret = _enroll_admin_totp(web, admin_user)
    on_resp = _set_enforcement(web, enabled=True)
    assert on_resp.status_code == HTTP_OK, (
        f"set_enforcement(True) failed: {on_resp.status_code} {on_resp.text[:200]}"
    )

    try:
        yield _EnrolledContext(base, admin_user, secret, admin_token)
    finally:
        _teardown_enrollment(web, base, admin_user, secret, admin_token)


@pytest.fixture()
def enforcement_no_totp(
    e2e_server_url: str,
    e2e_config: E2EConfig,
) -> Iterator[str]:
    """Enable enforcement WITHOUT enrolling admin TOTP (T3 variant).

    The gate is passthrough while OFF, so flipping it ON needs only a web
    session + CSRF.  Yields the base url.

    Teardown: with no admin TOTP, require_elevation raises totp_setup_required
    for any gated write — including the config POST that would flip enforcement
    OFF.  So if the direct OFF flip is itself gated, bootstrap a throwaway TOTP,
    elevate, flip OFF, then disable the throwaway TOTP (same primitives as the
    elevation_enrolled teardown).
    """
    base = e2e_server_url
    admin_user = e2e_config.admin_user
    admin_pass = e2e_config.admin_pass

    web = _web_login(base, admin_user, admin_pass)
    on_resp = _set_enforcement(web, enabled=True)
    assert on_resp.status_code == HTTP_OK, (
        f"set_enforcement(True) failed: {on_resp.status_code} {on_resp.text[:200]}"
    )

    try:
        yield base
    finally:
        off_resp = _set_enforcement(web, enabled=False)
        if off_resp.status_code != HTTP_OK:
            # Gated: bootstrap a throwaway TOTP to authorise the OFF flip.
            secret = _enroll_admin_totp(web, admin_user)
            elev_otp = _fresh_otp(secret, avoid=pyotp.TOTP(secret).now())
            web.post("/auth/elevate", json={"totp_code": elev_otp})
            off2 = _set_enforcement(web, enabled=False)
            assert off2.status_code == HTTP_OK, (
                f"Teardown could not disable enforcement: {off2.status_code} "
                f"{off2.text[:200]}"
            )
            disable_otp = _fresh_otp(secret, avoid=elev_otp)
            web.post("/user/mfa/disable", data={"totp_code": disable_otp})
        try:
            web.get("/logout")
        finally:
            web.close()


# ===========================================================================
# AC1 — CLI elevation retry (#980)
# ===========================================================================


class TestAC1CliElevationRetry:
    """AC1: the elevation-retry contract through the real front door.

    T1/T2 drive the REAL production client function ``with_elevation_retry``
    (the exact function cli.py wires for ``cidx admin users create``) against the
    LIVE server with a pre-enrollment Bearer token — real client code, real
    ``/auth/elevate``, real gated endpoint.  Driving the ``cidx`` subprocess for
    these two is infeasible: the CLI cannot authenticate as a TOTP-enrolled admin
    (see module docstring, learning #3).  T3 DOES drive the real ``cidx`` binary.

    ``ctx.admin_token`` was minted pre-enrollment (no MFA challenge); both T1 and
    T2 reuse it.  Each test opens its OWN elevation window from a clean state
    (the fixture is function-scoped, so the server starts each test with no
    active window for this token's jti).
    """

    def test_t1_correct_otp_elevates_and_retries(
        self, elevation_enrolled: _EnrolledContext
    ) -> None:
        """T1: 403 elevation_required -> prompt -> /auth/elevate 200 -> retry 201."""
        from code_indexer.api_clients.elevation import with_elevation_retry

        ctx = elevation_enrolled
        token = ctx.admin_token  # pre-enrollment token (no MFA challenge)

        username = f"t1_elev_{int(time.time() * 1000)}"
        session = httpx.Client(timeout=15.0)
        try:
            otp = _fresh_otp(ctx.secret, avoid=pyotp.TOTP(ctx.secret).now())
            result = with_elevation_retry(
                fn=_make_create_user_fn(ctx.base_url, session, token, username),
                session=session,
                server_url=ctx.base_url,
                token=token,
                prompt_totp=lambda: otp,
            )
            assert result.status_code == HTTP_CREATED, (
                f"T1 expected 201 after elevation+retry, got {result.status_code}: "
                f"{result.text[:200]}"
            )
            assert _user_exists(ctx.base_url, ctx.admin_token, username), (
                "T1: user was not actually created despite 201"
            )
        finally:
            session.close()
            _delete_user(ctx.base_url, ctx.admin_token, username)

    def test_t2_wrong_otp_exits_without_creating_user(
        self, elevation_enrolled: _EnrolledContext
    ) -> None:
        """T2: 403 -> prompt -> /auth/elevate 401 -> sys.exit(1); user NOT created."""
        from code_indexer.api_clients.elevation import with_elevation_retry

        ctx = elevation_enrolled
        token = ctx.admin_token  # pre-enrollment token (no MFA challenge)

        username = f"t2_elev_{int(time.time() * 1000)}"
        session = httpx.Client(timeout=15.0)
        try:
            with pytest.raises(SystemExit) as exc_info:
                with_elevation_retry(
                    fn=_make_create_user_fn(ctx.base_url, session, token, username),
                    session=session,
                    server_url=ctx.base_url,
                    token=token,
                    prompt_totp=lambda: "000000",
                )
            assert exc_info.value.code == 1, (
                f"T2 expected SystemExit(1), got code={exc_info.value.code}"
            )
            assert not _user_exists(ctx.base_url, ctx.admin_token, username), (
                "T2: wrong OTP must NOT create the user (no retry on elevation_failed)"
            )
        finally:
            session.close()
            _delete_user(ctx.base_url, ctx.admin_token, username)

    def test_t3_totp_setup_required_via_real_cli(
        self,
        enforcement_no_totp: str,
        authenticated_workspace: Path,
        e2e_cli_env: dict[str, str],
    ) -> None:
        """T3: enforcement ON, admin has NO TOTP -> real cidx prints setup msg + exit 1.

        Drives the REAL ``cidx`` binary.  A non-TOTP admin logs in normally, the
        gate raises ``totp_setup_required`` before any prompt, and
        ``with_elevation_retry`` prints the setup message and exits 1 WITHOUT
        consuming stdin or hanging (empty stdin proves no prompt is awaited).
        """
        result = run_cidx(
            "admin",
            "users",
            "create",
            f"t3_cli_{int(time.time())}",
            "--password",
            _STRONG_PASSWORD,
            "--role",
            _ROLE_NORMAL,
            cwd=str(authenticated_workspace),
            env=e2e_cli_env,
            stdin_input="",
        )
        assert result.returncode != 0, (
            f"T3 expected non-zero exit, got rc=0.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = f"{result.stdout}\n{result.stderr}"
        assert "TOTP setup required" in combined, (
            f"T3 expected 'TOTP setup required' message, got:\n{combined}"
        )


# ===========================================================================
# AC2 — Maintenance localhost-only (#924)
# ===========================================================================


def _primary_non_loopback_ipv4() -> Optional[str]:
    """Discover the host's primary non-loopback IPv4 via a UDP connect trick.

    No packets are sent — connect() on a UDP socket just selects the egress
    interface.  Returns None when only loopback is available (caller loud-skips).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    if ip.startswith("127.") or ip == "0.0.0.0":
        return None
    return str(ip)


def _wait_for_health(url: str, *, timeout: float = 30.0) -> bool:
    """Poll ``url`` until it returns any non-5xx (<500) status; bounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code < 500:
                return True
        except httpx.TransportError:
            pass
        time.sleep(1)
    return False


class TestAC2MaintenanceLocalhostOnly:
    """AC2: maintenance write endpoints are loopback-only; reads are unaffected."""

    def test_t4_loopback_enter_status_exit(
        self,
        e2e_server_url: str,
        e2e_admin_token: str,
    ) -> None:
        """T4: loopback POST enter -> 200, GET status -> 200, POST exit -> 200.

        Shared live server: the finally block ALWAYS exits maintenance so later
        tests are never stranded in maintenance mode.
        """
        headers = _bearer(e2e_admin_token)
        entered = False
        try:
            enter = httpx.post(
                f"{e2e_server_url}/api/admin/maintenance/enter",
                headers=headers,
                timeout=15.0,
            )
            assert enter.status_code == HTTP_OK, (
                f"loopback enter expected 200, got {enter.status_code}: "
                f"{enter.text[:200]}"
            )
            entered = True
            assert enter.json().get("maintenance_mode") is True

            status = httpx.get(
                f"{e2e_server_url}/api/admin/maintenance/status",
                headers=headers,
                timeout=15.0,
            )
            assert status.status_code == HTTP_OK, (
                f"status (no require_localhost) expected 200, got "
                f"{status.status_code}: {status.text[:200]}"
            )
            assert status.json().get("maintenance_mode") is True
        finally:
            exit_resp = httpx.post(
                f"{e2e_server_url}/api/admin/maintenance/exit",
                headers=headers,
                timeout=15.0,
            )
            if entered:
                assert exit_resp.status_code == HTTP_OK, (
                    f"loopback exit expected 200, got {exit_resp.status_code}: "
                    f"{exit_resp.text[:200]}"
                )

    def test_t5_non_loopback_enter_forbidden(
        self,
        e2e_config: E2EConfig,
        tmp_path: Path,
    ) -> None:
        """T5: non-loopback POST enter -> 403 'Localhost-only' (dedicated server).

        Spawns a throwaway uvicorn with its OWN temp data dir (never touches the
        shared server's data), admin-logs-in over loopback (its admin has no
        TOTP), then asserts loopback enter -> 200 and non-loopback enter -> 403.
        """
        ip = _primary_non_loopback_ipv4()
        if ip is None:
            pytest.skip("no non-loopback IPv4 address available on this host")

        port = _find_free_port()
        data_dir = tmp_path / "throwaway_server_data"
        data_dir.mkdir()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
        env["CIDX_TEST_FAST_SQLITE"] = "1"
        env["CIDX_SERVER_DATA_DIR"] = str(data_dir)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "code_indexer.server.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--workers",
                "1",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        entered_loopback = False
        try:
            assert _wait_for_health(f"http://127.0.0.1:{port}/health", timeout=30.0), (
                "throwaway uvicorn did not become healthy within 30s"
            )

            login = httpx.post(
                f"http://127.0.0.1:{port}/auth/login",
                json={
                    "username": e2e_config.admin_user,
                    "password": e2e_config.admin_pass,
                },
                timeout=15.0,
            )
            assert login.status_code == HTTP_OK, (
                f"throwaway admin login failed: {login.status_code} {login.text[:200]}"
            )
            token = login.json()["access_token"]
            headers = _bearer(token)

            # Loopback enter -> 200 (then exit to leave it clean).
            loop_enter = httpx.post(
                f"http://127.0.0.1:{port}/api/admin/maintenance/enter",
                headers=headers,
                timeout=15.0,
            )
            assert loop_enter.status_code == HTTP_OK, (
                f"throwaway loopback enter expected 200, got "
                f"{loop_enter.status_code}: {loop_enter.text[:200]}"
            )
            entered_loopback = True
            httpx.post(
                f"http://127.0.0.1:{port}/api/admin/maintenance/exit",
                headers=headers,
                timeout=15.0,
            )
            entered_loopback = False

            # Non-loopback enter -> 403 Localhost-only.
            non_loop = httpx.post(
                f"http://{ip}:{port}/api/admin/maintenance/enter",
                headers=headers,
                timeout=15.0,
            )
            assert non_loop.status_code == HTTP_FORBIDDEN, (
                f"non-loopback enter expected 403, got {non_loop.status_code}: "
                f"{non_loop.text[:200]}"
            )
            assert "Localhost-only" in non_loop.text, (
                f"non-loopback 403 detail should mention 'Localhost-only', got: "
                f"{non_loop.text[:200]}"
            )
        finally:
            if entered_loopback:
                try:
                    httpx.post(
                        f"http://127.0.0.1:{port}/api/admin/maintenance/exit",
                        headers=_bearer(token),
                        timeout=15.0,
                    )
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10.0)


def _find_free_port() -> int:
    """Bind an ephemeral port, close, and return it (race-tolerant enough for E2E)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()
