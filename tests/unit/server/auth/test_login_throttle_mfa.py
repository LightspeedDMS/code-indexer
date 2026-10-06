"""A login's MFA codes count toward the same throttle as its passwords.

Rules under test:

- every MFA code (TOTP or recovery) at a login challenge is RESERVED on the
  challenge's throttle key before it is checked: the account's login key
  for a password challenge, its own SSO-MFA key for an SSO-started one;
  inside a backoff window it is refused (429 + ``Retry-After``) without
  being checked, and the challenge stays usable;
- only a completed login (first factor and code) clears its key; a correct
  password with MFA pending clears nothing;
- a store error while clearing never fails the already-issued login.

Front doors, through the real app (``isolated_app``) and its real TOTP
service: REST ``/auth/login`` + ``/auth/mfa/verify``, Web ``/login`` +
``/admin/mfa/challenge/verify``, OAuth ``/oauth/authorize`` +
``/oauth/mfa/verify``.  The throttle is the process singleton, wired per
test to a SQLite file or to PostgreSQL (skipped unless TEST_POSTGRES_DSN is
set), with an injected clock (time is advanced, never slept).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import login_rate_limiter as throttle_module
from code_indexer.server.auth.user_manager import UserManager, UserRole
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Throttle-MFA-Passw0rd!"
REDIRECT_URI = "https://client.example.com/callback"
THRESHOLD = throttle_module.DEFAULT_MAX_ATTEMPTS  # shipped policy: 5
BASE_WINDOW = int(throttle_module.DEFAULT_BASE_DELAY_SECONDS)  # 5 s
_CHALLENGE = re.compile(r"name=['\"]challenge_token['\"] value=['\"]([^'\"]+)")
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _Env:
    client: TestClient
    root: Path
    oauth_client_id: str


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Env]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    root = tmp_path_factory.mktemp("throttle-mfa-app")
    from code_indexer.server.auth.totp_service import TOTPService
    from code_indexer.server.web import mfa_routes

    previous_totp = mfa_routes._totp_service
    try:
        with isolated_app(root) as app:
            # The TOTP service start-up wires in production (lifespan).
            mfa_routes.set_totp_service(TOTPService(db_path=str(root / "mfa.db")))
            # Lifespan mounts the SSO callback when OIDC is enabled.
            from code_indexer.server.auth.oidc import routes as oidc_routes

            app.include_router(oidc_routes.router)
            client = TestClient(app, follow_redirects=False)
            registered = client.post(
                "/oauth/register",
                json={"client_name": "example-client", "redirect_uris": [REDIRECT_URI]},
            )
            assert registered.status_code == 201, registered.text
            yield _Env(client, root, registered.json()["client_id"])
    finally:
        mfa_routes._totp_service = previous_totp


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    """One throwaway, fully migrated database per module (dropped after)."""
    if not _DSN:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"throttle_mfa_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        yield dsn
    finally:
        with psycopg.connect(_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(params=["sqlite", "postgres"])
def clock(request, tmp_path: Path, monkeypatch) -> Iterator[FakeClock]:
    """Wire the process throttle to the backend under test, with a clock.

    The tree-wide conftest resets the singleton's store after each test;
    monkeypatch restores its clock and wiring attributes.
    """
    limiter = throttle_module.login_rate_limiter
    fake = FakeClock()
    monkeypatch.setattr(limiter, "_clock", fake)
    monkeypatch.setattr(limiter, "_pool", limiter._pool)
    monkeypatch.setattr(limiter, "_store", limiter._store)
    if request.param == "sqlite":
        limiter.set_sqlite_path(str(tmp_path / "cidx_server.db"))
        yield fake
        return
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool(request.getfixturevalue("pg_dsn"), min_size=1, max_size=4)
    try:
        limiter.set_connection_pool(pool)
        yield fake
    finally:
        pool.close()


def _enrolled_member(env: _Env) -> Tuple[str, pyotp.TOTP]:
    """A fresh account with TOTP enrolled through the real TOTP service."""
    from code_indexer.server.web.mfa_routes import get_totp_service

    users: UserManager = env.client.app.state.user_manager  # type: ignore[attr-defined]
    name = f"member-{uuid.uuid4().hex[:8]}"
    users.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    totp_service = get_totp_service()
    assert totp_service is not None
    totp_service.generate_secret(name)
    uri = totp_service.get_provisioning_uri(name)
    assert uri is not None
    totp = pyotp.TOTP(parse_qs(urlparse(uri).query)["secret"][0])
    assert totp_service.activate_mfa(name, totp.at(int(time.time()) - 30))
    return name, totp


def _wrong_code(totp: pyotp.TOTP) -> str:
    """A 6-digit code no tolerated time step accepts right now."""
    now = int(time.time())
    accepted = {totp.at(now + offset) for offset in (-60, -30, 0, 30, 60)}
    for candidate in range(10**6):
        code = f"{candidate:06d}"
        if code not in accepted:
            return code
    raise AssertionError("unreachable: at most 5 codes are accepted")


@dataclass
class _Door:
    """One login door: its password step and its MFA challenge step."""

    name: str
    password: Callable[[str], Any]  # -> response
    verify: Callable[..., Any]  # (challenge, totp_code=, recovery_code=)
    success_status: int
    bad_code_status: int

    def challenge(self, username: str) -> str:
        response = self.password(username)
        token = _challenge_from(self.name, response)
        assert token, f"{self.name}: no challenge ({response.status_code})"
        return token

    def code_rejected(self, response: Any) -> bool:
        if response.status_code != self.bad_code_status:
            return False
        if self.name == "web":
            return bool(
                response.headers.get("location", "").endswith("info=mfa_failed")
            )
        if self.name == "rest":  # 401 also answers an unusable challenge
            return bool(response.json().get("detail") == "Invalid MFA code")
        return True

    def completed(self, response: Any) -> bool:
        if response.status_code != self.success_status:
            return False
        if self.name == "web":
            return "/login" not in response.headers.get("location", "")
        if self.name == "oauth":
            return "code=" in response.headers.get("location", "")
        return "access_token" in response.json()


def _challenge_from(door: str, response: Any) -> Optional[str]:
    if response.status_code != 200:
        return None
    if door == "rest":
        body = response.json()
        return body.get("mfa_token") if body.get("mfa_required") else None
    match = _CHALLENGE.search(response.text)
    return match.group(1) if match else None


def _rest_door(env: _Env) -> _Door:
    client = env.client

    def password(name: str) -> Any:
        return client.post("/auth/login", json={"username": name, "password": PASSWORD})

    def verify(token: str, totp_code: Optional[str] = None, recovery_code=None):  # type: ignore[no-untyped-def]
        body = {"mfa_token": token, "totp_code": totp_code}
        if recovery_code is not None:
            body = {"mfa_token": token, "recovery_code": recovery_code}
        return client.post("/auth/mfa/verify", json=body)

    return _Door("rest", password, verify, 200, 401)


def _web_door(env: _Env) -> _Door:
    client = env.client

    def password(name: str) -> Any:
        client.cookies.clear()
        match = _CSRF.search(client.get("/login").text)
        assert match, "csrf token not found on the login page"
        return client.post(
            "/login",
            data={"username": name, "password": PASSWORD, "csrf_token": match.group(1)},
        )

    def verify(token: str, totp_code: Optional[str] = None, recovery_code=None):  # type: ignore[no-untyped-def]
        data = {"challenge_token": token}
        if totp_code is not None:
            data["totp_code"] = totp_code
        if recovery_code is not None:
            data["recovery_code"] = recovery_code
        return client.post("/admin/mfa/challenge/verify", data=data)

    return _Door("web", password, verify, 303, 303)


def _oauth_door(env: _Env) -> _Door:
    client = env.client
    verifier = secrets.token_urlsafe(48)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )

    def password(name: str) -> Any:
        client.cookies.clear()
        return client.post(
            "/oauth/authorize",
            json={
                "client_id": env.oauth_client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": code_challenge,
                "username": name,
                "password": PASSWORD,
            },
        )

    def verify(token: str, totp_code: Optional[str] = None, recovery_code=None):  # type: ignore[no-untyped-def]
        data = {"challenge_token": token}
        if totp_code is not None:
            data["totp_code"] = totp_code
        if recovery_code is not None:
            data["recovery_code"] = recovery_code
        return client.post("/oauth/mfa/verify", data=data)

    return _Door("oauth", password, verify, 302, 401)


_BUILDERS = {"rest": _rest_door, "web": _web_door, "oauth": _oauth_door}


@pytest.fixture(params=sorted(_BUILDERS))
def door(request, env: _Env) -> _Door:
    return _BUILDERS[request.param](env)


def _assert_refused(response: Any, retry_after: int) -> None:
    assert response.status_code == 429, response.text[:300]
    assert response.headers["Retry-After"] == str(retry_after)


def test_password_and_wrong_code_cycles_throttle_the_account(env, door, clock):
    name, totp = _enrolled_member(env)
    wrong = _wrong_code(totp)
    # Attempts 1-4: two (correct password, wrong code) cycles.
    for _ in range(2):
        assert door.code_rejected(door.verify(door.challenge(name), totp_code=wrong))
    # Attempt 5 (a correct password) is admitted and starts the window ...
    challenge = door.challenge(name)
    # ... and inside the window every attempt on the key, code or
    # password, is refused unchecked.
    _assert_refused(door.verify(challenge, totp_code=totp.now()), BASE_WINDOW)
    _assert_refused(door.password(name), BASE_WINDOW)


def _cycles(door: _Door, name: str, wrong: str, count: int) -> None:
    """*count* cycles; each is TWO throttle attempts (password, then code)."""
    for _ in range(count):
        assert door.code_rejected(door.verify(door.challenge(name), totp_code=wrong))


def test_refused_challenge_is_usable_after_the_window_and_completion_clears(
    env, door, clock
):
    name, totp = _enrolled_member(env)
    wrong = _wrong_code(totp)
    _cycles(door, name, wrong, 2)  # attempts 1-4
    challenge = door.challenge(name)  # attempt 5: starts the window
    _assert_refused(door.verify(challenge, totp_code=totp.now()), BASE_WINDOW)
    # The refusal did not consume the challenge: answered after the window
    # it completes the login (password and code).
    clock.advance(BASE_WINDOW)
    assert door.completed(door.verify(challenge, totp_code=totp.now()))
    # The completed login cleared the history: a fresh allowance, so
    # attempts 1-4 and the password at attempt 5 are admitted again.
    _cycles(door, name, wrong, 2)
    final = door.challenge(name)
    _assert_refused(door.verify(final, totp_code=wrong), BASE_WINDOW)


def test_recovery_codes_count_like_totp_codes(env, door, clock):
    from code_indexer.server.web.mfa_routes import get_totp_service

    name, _totp = _enrolled_member(env)
    service = get_totp_service()
    assert service is not None
    good = service.generate_recovery_codes(name)[0]
    for _ in range(2):  # attempts 1-4: (password, wrong recovery code) x 2
        rejected = door.verify(
            door.challenge(name), recovery_code="ABCD-EFGH-0000-0000"
        )
        assert door.code_rejected(rejected)
    challenge = door.challenge(name)  # attempt 5: starts the window
    _assert_refused(door.verify(challenge, recovery_code=good), BASE_WINDOW)
    # Refused unchecked, so the recovery code was not spent.
    clock.advance(BASE_WINDOW)
    assert door.completed(door.verify(challenge, recovery_code=good))


_BURST = 16
_BARRIER_TIMEOUT_S = 30
_JOIN_TIMEOUT_S = 120


def test_concurrent_wrong_codes_at_one_challenge_admit_at_most_the_allowance(
    env, door, clock
):
    name, totp = _enrolled_member(env)
    wrong = _wrong_code(totp)
    challenge = door.challenge(name)  # attempt 1
    barrier = threading.Barrier(_BURST)
    responses: List[Any] = []
    guard = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=_BARRIER_TIMEOUT_S)
        response = door.verify(challenge, totp_code=wrong)
        with guard:
            responses.append(response)

    threads = [threading.Thread(target=attempt) for _ in range(_BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)
    assert len(responses) == _BURST
    statuses = [r.status_code for r in responses]
    # 503 = the solo store's 2 s write-lock bound (possible under an I/O
    # stall with 16 simultaneous reservations): refused unchecked, and it
    # reserves nothing.
    busy = statuses.count(503)
    admitted = [r for r in responses if r.status_code not in (429, 503)]
    # Frozen clock: the remaining allowance before the window is attempts
    # 2-5 (THRESHOLD - 1); no burst may admit more.
    assert len(admitted) <= THRESHOLD - 1, statuses
    # Consume-first: at most one admitted attempt checks the code, the
    # others find the challenge gone.
    assert sum(door.code_rejected(r) for r in admitted) <= 1, statuses
    if busy == 0:
        # Every reservation was decided: the allowance is exactly filled,
        # one code was checked, and the window now runs.
        assert len(admitted) == THRESHOLD - 1, statuses
        assert sum(door.code_rejected(r) for r in admitted) == 1
        _assert_refused(door.password(name), BASE_WINDOW)


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[Any]:
    from tests.unit.server.audit._audit_mfa_login_support import bound_audit_store

    yield from bound_audit_store(tmp_path / "groups.db")


def _wrong_password(door: _Door, env: _Env, name: str) -> Any:
    client = env.client
    if door.name == "rest":
        body = {"username": name, "password": "wrong-password-value"}
        return client.post("/auth/login", json=body)
    if door.name == "web":
        client.cookies.clear()
        match = _CSRF.search(client.get("/login").text)
        assert match
        form = {"username": name, "password": "wrong", "csrf_token": match.group(1)}
        return client.post("/login", data=form)
    return client.post(
        "/oauth/authorize",
        json={
            "client_id": env.oauth_client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "username": name,
            "password": "wrong-password-value",
        },
    )


def test_mfa_failures_keep_their_rows_and_a_throttle_start_is_rate_limited(
    env, door, clock, audit
):
    name, totp = _enrolled_member(env)
    wrong = _wrong_code(totp)
    # Attempt 1: a wrong password -> row (credentials, bad_credentials).
    assert _wrong_password(door, env, name).status_code != 429
    # Attempts 2-5: two (correct password, wrong code) cycles.  Correct
    # passwords that only open a challenge write no row; the code at
    # attempt 3 -> (mfa_code, mfa_code_invalid); the code at attempt 5
    # starts the window -> (mfa_code, rate_limited).
    _cycles(door, name, wrong, 2)
    # Refused inside the window: never checked, so no row.
    _assert_refused(door.password(name), BASE_WINDOW)
    reasons = [
        (r.details["stage"], r.details["reason"])
        for r in audit.rows("authentication_failure")
    ]
    assert reasons == [
        ("credentials", "bad_credentials"),
        ("mfa_code", "mfa_code_invalid"),
        ("mfa_code", "rate_limited"),
    ]


_SSO_VERIFY = {"web": "/admin/mfa/challenge/verify", "oauth": "/oauth/mfa/verify"}


def _sso_challenge(env: _Env, monkeypatch, tmp_path: Path, name: str, flow: str) -> str:
    """Sign *name* in through the real SSO callback; return its challenge.

    Only the identity provider is replaced: its network calls (token
    exchange, userinfo) and the claims-to-account match, which returns the
    real account.  ``flow`` is "web" (session login) or "oauth" (the
    callback of an OAuth authorize that went through SSO).
    """
    from unittest.mock import AsyncMock, Mock

    from code_indexer.server.auth.oidc import routes as oidc_routes
    from code_indexer.server.auth.oidc import state_manager as state_module
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider, OIDCUserInfo
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    users: UserManager = env.client.app.state.user_manager  # type: ignore[attr-defined]
    manager = OIDCManager(
        OIDCProviderConfig(
            enabled=True, issuer_url="https://idp.example.com", client_id="example"
        ),
        None,
        None,
    )
    manager.provider = Mock(spec=OIDCProvider)
    manager.provider.exchange_code_for_token = AsyncMock(
        return_value={"access_token": "example-access", "id_token": "example-id"}
    )
    manager.provider.get_user_info = Mock(
        return_value=OIDCUserInfo(
            subject=f"sub-{name}", email=f"{name}@example.com", email_verified=True
        )
    )
    manager.match_or_create_user = AsyncMock(  # type: ignore[method-assign]
        return_value=users.get_user(name)
    )
    monkeypatch.setattr(state_module, "_configured_sqlite_path", None)
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    states = state_module.StateManager()
    monkeypatch.setattr(oidc_routes, "oidc_manager", manager)
    monkeypatch.setattr(oidc_routes, "state_manager", states)
    data: dict = {"oidc_code_verifier": "example-verifier"}
    if flow == "oauth":
        data.update(
            flow="oauth_authorize",
            client_id=env.oauth_client_id,
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            redirect_uri=REDIRECT_URI,
            oauth_state="example-state",
        )
    else:
        data["redirect_to"] = "/user/api-keys"
    env.client.cookies.clear()
    response = env.client.get(
        f"/auth/sso/callback?code=example-code&state={states.create_state(data)}"
    )
    match = _CHALLENGE.search(response.text)
    assert match, f"no SSO challenge ({response.status_code}): {response.text[:200]}"
    return match.group(1)


def _sso_verify(env: _Env, flow: str, token: str, code: str) -> Any:
    form = {"challenge_token": token, "totp_code": code}
    return env.client.post(_SSO_VERIFY[flow], data=form)


def _sso_completed(flow: str, response: Any) -> bool:
    location = response.headers.get("location", "")
    if flow == "oauth":
        return response.status_code == 302 and "code=" in location
    return response.status_code == 303 and "/login" not in location


@pytest.mark.parametrize("flow", sorted(_SSO_VERIFY))
def test_wrong_passwords_do_not_block_an_sso_started_challenge(
    env, clock, monkeypatch, tmp_path: Path, flow: str
):
    name, totp = _enrolled_member(env)
    rest = _rest_door(env)
    # Wrong passwords throttle the account's login key.
    for _ in range(THRESHOLD):
        _wrong_password(rest, env, name)
    _assert_refused(rest.password(name), BASE_WINDOW)
    # An SSO-started challenge uses its own key, so its correct code
    # completes the login.
    token = _sso_challenge(env, monkeypatch, tmp_path, name, flow)
    assert _sso_completed(flow, _sso_verify(env, flow, token, totp.now()))


@pytest.mark.parametrize("flow", sorted(_SSO_VERIFY))
def test_wrong_codes_after_sso_are_throttled_under_their_own_key(
    env, clock, monkeypatch, tmp_path: Path, audit, flow: str
):
    name, totp = _enrolled_member(env)
    wrong = _wrong_code(totp)
    # An SSO login reserves nothing; each wrong code at its challenge does.
    # THRESHOLD checked codes: rows 1-4 mfa_code_invalid, the 5th starts the
    # window and is rate_limited.
    for _ in range(THRESHOLD):
        token = _sso_challenge(env, monkeypatch, tmp_path, name, flow)
        assert _sso_verify(env, flow, token, wrong).status_code in (303, 401)
    # Inside the window even the correct code is REFUSED unchecked, which
    # writes no audit row (so exactly THRESHOLD rows below).
    token = _sso_challenge(env, monkeypatch, tmp_path, name, flow)
    _assert_refused(_sso_verify(env, flow, token, totp.now()), BASE_WINDOW)
    # The password-login key is untouched: the password door still answers.
    assert _rest_door(env).challenge(name)
    rows = audit.rows("authentication_failure")
    assert [(r.details["method"], r.details["reason"]) for r in rows] == [
        ("sso", "mfa_code_invalid")
    ] * (THRESHOLD - 1) + [("sso", "rate_limited")]


@pytest.mark.parametrize("flow", sorted(_SSO_VERIFY))
def test_completed_sso_login_clears_only_its_own_key(
    env, clock, monkeypatch, tmp_path: Path, audit, flow: str
):
    name, totp = _enrolled_member(env)
    rest = _rest_door(env)
    for _ in range(THRESHOLD):
        _wrong_password(rest, env, name)
    token = _sso_challenge(env, monkeypatch, tmp_path, name, flow)
    assert _sso_completed(flow, _sso_verify(env, flow, token, totp.now()))
    # The SSO login did not clear the password-login key.
    _assert_refused(rest.password(name), BASE_WINDOW)
    successes = audit.rows("authentication_success")
    assert [r.details["method"] for r in successes] == ["sso"]


def test_sso_started_challenge_at_rest_verify_uses_its_own_key(
    env, clock, monkeypatch, tmp_path: Path
):
    name, totp = _enrolled_member(env)
    rest = _rest_door(env)
    for _ in range(THRESHOLD):
        _wrong_password(rest, env, name)
    token = _sso_challenge(env, monkeypatch, tmp_path, name, "web")
    response = rest.verify(token, totp_code=totp.now())
    assert rest.completed(response), response.text[:300]
    _assert_refused(rest.password(name), BASE_WINDOW)


@pytest.fixture(params=["memory", "postgres"])
def challenge_store(request) -> Iterator[Tuple[Any, Optional[str]]]:
    """An MfaChallengeManager on each storage mode, plus the PG DSN."""
    from code_indexer.server.auth.mfa_challenge import MfaChallengeManager

    manager = MfaChallengeManager()
    if request.param == "memory":
        yield manager, None
        return
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    dsn = request.getfixturevalue("pg_dsn")
    pool = ConnectionPool(dsn, min_size=1, max_size=2)
    try:
        manager.set_connection_pool(pool)
        yield manager, dsn
    finally:
        pool.close()


def test_mfa_challenge_first_factor_round_trips_on_both_stores(challenge_store):
    from code_indexer.server.auth.login_rate_limiter import SCOPE_LOGIN, SCOPE_SSO_MFA

    manager, dsn = challenge_store
    ip = "192.0.2.1"
    sso = manager.create_challenge("example-user", "admin", ip, first_factor="sso")
    password = manager.create_challenge("example-user", "admin", ip)
    sso_read, password_read = (
        manager.get_challenge(sso),
        manager.get_challenge(password),
    )
    assert sso_read is not None and password_read is not None
    assert (sso_read.first_factor, sso_read.throttle_scope) == ("sso", SCOPE_SSO_MFA)
    assert (password_read.first_factor, password_read.throttle_scope) == (
        "password",
        SCOPE_LOGIN,
    )
    with pytest.raises(ValueError):
        manager.create_challenge("example-user", "admin", ip, first_factor="other")
    if dsn is None:
        return
    # A row written by a node that predates the column reads as password.
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE mfa_challenges SET first_factor = NULL WHERE token = %s", (sso,)
        )
    legacy = manager.consume(sso)
    assert legacy is not None and legacy.first_factor == "password"


def _plain_member(env: _Env) -> str:
    """A fresh account without MFA."""
    users: UserManager = env.client.app.state.user_manager  # type: ignore[attr-defined]
    name = f"plain-{uuid.uuid4().hex[:8]}"
    users.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    return name


def _completed_without_mfa(door: _Door, response: Any) -> bool:
    if door.name == "web":
        return door.completed(response)
    return response.status_code == 200 and (
        "access_token" in response.json() or "code" in response.json()
    )


_STORE_ERROR = "disk I/O error at example-store-detail"


@pytest.mark.parametrize("completion", ["no_mfa", "mfa"])
def test_store_error_on_the_clear_never_fails_a_completed_login(
    env, door, clock, monkeypatch, caplog, completion: str
):
    import logging
    import sqlite3

    def failing_delete(key: str) -> None:
        raise sqlite3.OperationalError(_STORE_ERROR)

    limiter = throttle_module.login_rate_limiter
    monkeypatch.setattr(limiter._store, "delete", failing_delete)
    caplog.set_level(logging.WARNING, logger=throttle_module.__name__)
    if completion == "no_mfa":
        response = door.password(_plain_member(env))
        assert _completed_without_mfa(door, response), response.text[:300]
    else:
        name, totp = _enrolled_member(env)
        response = door.verify(door.challenge(name), totp_code=totp.now())
        assert door.completed(response), response.text[:300]
    warnings = [
        r
        for r in caplog.records
        if r.name == throttle_module.__name__ and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    # The store's own message is not logged (it may carry store details).
    assert _STORE_ERROR not in caplog.text
    assert warnings[0].exc_info is None


def test_busy_store_answers_503_at_the_challenge_and_keeps_it(
    env, door, clock, request, tmp_path: Path
):
    import sqlite3

    if "sqlite" not in request.node.callspec.id:
        pytest.skip("the busy bound is the solo-mode SQLite store's")
    name, totp = _enrolled_member(env)
    challenge = door.challenge(name)
    writer = sqlite3.connect(str(tmp_path / "cidx_server.db"), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        try:
            busy = door.verify(challenge, totp_code=totp.now())
        finally:
            writer.execute("ROLLBACK")
    finally:
        writer.close()
    assert busy.status_code == 503, busy.text[:300]
    assert busy.headers["Retry-After"] == "1"
    assert "Login is busy, try again shortly." in busy.text
    assert door.completed(door.verify(challenge, totp_code=totp.now()))
