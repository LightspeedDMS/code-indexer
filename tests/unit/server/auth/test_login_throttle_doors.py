"""Every password login door applies the same progressive throttle.

Drives the REAL REST ``POST /auth/login``, Web ``POST /login`` form and
OAuth ``POST /oauth/authorize`` routes through TestClient with a real
SQLite UserManager, a real audit store and a real DB-backed throttle whose
clock is injected (time is advanced, never slept).  Only the Web form's CSRF
check, its session manager and the REST token bucket (given room for the
attack loops) are replaced.

Semantics under test (max_attempts=3, base window 5 s, cap 120 s):

- failures are counted CONSECUTIVELY per username; waiting does NOT reset
  the count -- only a success (or 15 minutes without failures) does;
- the 3rd consecutive failure is itself answered as an ordinary credential
  failure (401 / form error) and STARTS a 5 s window;
- every attempt during a window -- a correct password included -- is
  refused with 429 + ``Retry-After`` WITHOUT checking the password; it is
  not counted and writes no audit row (no flooding the audit store);
- each later failure (after its window ended) is answered normally and
  starts a doubled window: 10, 20, 40, 80, then 120 s (capped).

Hence ``fail, fail, fail(starts 5 s), 429`` and, after waiting 5 s,
``fail(#4, starts 10 s), 429``.  The failure that crosses the threshold is
audited with the existing reason ``rate_limited``.  No door ever locks an
account permanently.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import login_rate_limiter as throttle_module
from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)
from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService

_ADMIN = "admin"
_PASSWORD = "SecureP@ssw0rd!XyZ789"
_WRONG = "attacker-guess-value"
_MAX = 3
_BASE = 5
_CAP = 120
_REDIRECT_URI = "https://client.example.com/callback"
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _World:
    tmp_path: Path
    users: UserManager
    limiter: LoginRateLimiter
    clock: FakeClock
    audit_db: Path

    def failure_reasons(self) -> List[str]:
        conn = sqlite3.connect(str(self.audit_db))
        try:
            rows = conn.execute(
                "SELECT details FROM audit_logs "
                "WHERE action_type = 'authentication_failure' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(r[0])["reason"] for r in rows]


@dataclass
class _Door:
    name: str
    post: Callable[[str, str], Any]
    success_status: int
    bad_status: int
    client: Any = None  # the door's TestClient, for raw-body requests


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> Iterator[_World]:
    from code_indexer.server.auth.token_bucket import TokenBucketManager
    from code_indexer.server.routers import inline_auth
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.web import mfa_routes

    db = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path=db).initialize_database()
    users = UserManager(use_sqlite=True, db_path=db)
    users.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    clock = FakeClock()
    limiter = LoginRateLimiter(max_attempts=_MAX, clock=clock)
    limiter.set_sqlite_path(db)
    monkeypatch.setattr(throttle_module, "login_rate_limiter", limiter)
    monkeypatch.setattr(
        inline_auth, "rate_limiter", TokenBucketManager(capacity=10_000)
    )
    monkeypatch.setattr(mfa_routes, "_totp_service", None)

    audit_db = tmp_path / "groups.db"
    service = AuditLogService(audit_db)
    service.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(service, node_id=None)
    try:
        yield _World(tmp_path, users, limiter, clock, audit_db)
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        service.stop()


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    return app


def _rest_door(world: _World, monkeypatch) -> _Door:
    from code_indexer.server.auth.jwt_manager import JWTManager
    from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
    from code_indexer.server.routers.inline_auth import register_auth_routes

    jwt = JWTManager(secret_key="example-throttle-jwt-secret")
    app = _app()
    register_auth_routes(
        app,
        jwt_manager=jwt,
        user_manager=world.users,
        refresh_token_manager=RefreshTokenManager(
            jwt_manager=jwt, db_path=str(world.tmp_path / "refresh.db")
        ),
        login_rate_limiter=world.limiter,
    )
    client = TestClient(app)
    return _Door(
        "rest",
        lambda u, p: client.post("/auth/login", json={"username": u, "password": p}),
        200,
        401,
        client=client,
    )


def _web_door(world: _World, monkeypatch) -> _Door:
    from code_indexer.server.auth import dependencies
    from code_indexer.server.web import routes
    from code_indexer.server.web.auth import SessionManager

    monkeypatch.setattr(dependencies, "user_manager", world.users)
    monkeypatch.setattr(routes, "validate_login_csrf_token", lambda _r, _t: True)
    sessions = SessionManager(
        "example-session-secret", SimpleNamespace(host="127.0.0.1")
    )
    monkeypatch.setattr(routes, "get_session_manager", lambda: sessions)
    app = _app()
    app.include_router(routes.login_router)
    client = TestClient(app)
    return _Door(
        "web",
        lambda u, p: client.post(
            "/login",
            data={"username": u, "password": p, "csrf_token": "x"},
            follow_redirects=False,
        ),
        303,
        200,
    )


def _oauth_door(world: _World, monkeypatch) -> _Door:
    from code_indexer.server.auth.oauth import routes as oauth_routes
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

    manager = OAuthManager(
        db_path=str(world.tmp_path / "oauth.db"), issuer="http://localhost:8000"
    )
    client_id = manager.register_client(
        client_name="Throttle Test Client", redirect_uris=[_REDIRECT_URI]
    )["client_id"]
    app = _app()
    app.include_router(oauth_routes.router)
    app.state.oauth_manager = manager
    app.dependency_overrides[oauth_routes.get_user_manager] = lambda: world.users
    client = TestClient(app)
    return _Door(
        "oauth",
        lambda u, p: client.post(
            "/oauth/authorize",
            json={
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "response_type": "code",
                "code_challenge": _CODE_CHALLENGE,
                "state": "client-state",
                "username": u,
                "password": p,
            },
        ),
        200,
        401,
        client=client,
    )


_BUILDERS = {"rest": _rest_door, "web": _web_door, "oauth": _oauth_door}


@pytest.fixture(params=sorted(_BUILDERS))
def door(request, world, monkeypatch) -> _Door:
    return _BUILDERS[request.param](world, monkeypatch)


def _assert_throttled(door: _Door, response: Any, retry_after: int) -> None:
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == str(retry_after)
    if door.name == "web":
        assert f"Too many attempts, try again in {retry_after} seconds" in (
            response.text
        )


def _fail_until_throttled(door: _Door, username: str = _ADMIN) -> None:
    """Three ordinary failures; the third starts the 5 s window."""
    for _ in range(_MAX):
        assert door.post(username, _WRONG).status_code == door.bad_status


def test_failure_burst_is_refused_with_429_and_retry_after(world, door):
    _fail_until_throttled(door)
    _assert_throttled(door, door.post(_ADMIN, _WRONG), _BASE)


def test_correct_password_is_refused_during_the_window(world, door):
    _fail_until_throttled(door)
    _assert_throttled(door, door.post(_ADMIN, _PASSWORD), _BASE)
    world.clock.advance(_BASE - 1)
    _assert_throttled(door, door.post(_ADMIN, _PASSWORD), 1)


def test_success_after_the_window_resets_the_counter(world, door):
    _fail_until_throttled(door)
    world.clock.advance(_BASE)
    assert door.post(_ADMIN, _PASSWORD).status_code == door.success_status
    # Without the reset these failures would be #4 and #5 and the next
    # (correct) attempt would be refused.
    for _ in range(_MAX - 1):
        assert door.post(_ADMIN, _WRONG).status_code == door.bad_status
    assert door.post(_ADMIN, _PASSWORD).status_code == door.success_status


def test_window_grows_and_the_cap_holds(world, door):
    _fail_until_throttled(door)
    retry_afters = [_BASE]
    for _ in range(8):
        # Window over: the next failure is answered normally and starts a
        # doubled window; the attempt right after it is refused.
        world.clock.advance(retry_afters[-1])
        assert door.post(_ADMIN, _WRONG).status_code == door.bad_status
        refused = door.post(_ADMIN, _WRONG)
        assert refused.status_code == 429
        retry_afters.append(int(refused.headers["Retry-After"]))
    assert retry_afters == [5, 10, 20, 40, 80, 120, 120, 120, 120]


def _signature(door: _Door, response: Any) -> Tuple[Any, ...]:
    body: Any = (
        response.json()
        if door.name != "web"
        else (
            "Invalid username or password" in response.text,
            "Too many attempts" in response.text,
        )
    )
    return response.status_code, response.headers.get("Retry-After"), body


def test_unknown_username_is_indistinguishable_from_a_known_one(world, door):
    def run(username: str) -> List[Tuple[Any, ...]]:
        seen = [_signature(door, door.post(username, _WRONG)) for _ in range(4)]
        # Waiting ends the window but keeps the count: the next failure is
        # #4 (answered normally, starts a 10 s window), the one after is
        # refused.
        world.clock.advance(_BASE)
        seen.append(_signature(door, door.post(username, _WRONG)))
        seen.append(_signature(door, door.post(username, _WRONG)))
        return seen

    known = run(_ADMIN)
    unknown = run("ghost-user-that-does-not-exist")
    assert unknown == known
    bad, refused = door.bad_status, 429
    assert [s[0] for s in known] == [bad, bad, bad, refused, bad, refused]
    assert [s[1] for s in known if s[0] == refused] == ["5", "10"]


def test_admin_cannot_be_permanently_locked_by_someone_else(world, door):
    # An attacker hammers the admin account for a long time ...
    for _ in range(25):
        response = door.post(_ADMIN, _WRONG)
        if response.status_code == 429:
            assert int(response.headers["Retry-After"]) <= _CAP
        world.clock.advance(7)
    # ... yet the admin gets in after waiting at most the cap, no unlock.
    world.clock.advance(_CAP)
    assert door.post(_ADMIN, _PASSWORD).status_code == door.success_status


_BURST = 16  # concurrent attempts per burst
_BARRIER_TIMEOUT_S = 30
_JOIN_TIMEOUT_S = 120


def _concurrent_burst(door: _Door) -> List[int]:
    barrier = threading.Barrier(_BURST)
    statuses: List[int] = []
    guard = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=_BARRIER_TIMEOUT_S)
        status = door.post(_ADMIN, _WRONG).status_code
        with guard:
            statuses.append(status)

    threads = [threading.Thread(target=attempt) for _ in range(_BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT_S)
    assert len(statuses) == _BURST
    return statuses


def test_concurrent_wrong_passwords_admit_at_most_threshold_checks(world, door):
    # The clock is frozen: every attempt of a burst is "at the same time".
    # Each attempt must be reserved before its password check, so concurrent
    # requests cannot all slip through a check-then-record gap.  The
    # crossing (3rd) attempt is itself answered as an ordinary failure.
    first = _concurrent_burst(door)
    checked = [s for s in first if s != 429]
    assert checked == [door.bad_status] * _MAX, first

    # Window over: exactly one more check is admitted (it starts the next,
    # doubled window); the rest of the burst is refused.
    world.clock.advance(_BASE)
    second = _concurrent_burst(door)
    assert [s for s in second if s != 429] == [door.bad_status], second


class _RejectingTotp:
    """TOTP service double: every code is wrong."""

    def verify_enabled_code(self, username: str, code: Any) -> bool:
        return False

    def verify_recovery_code(
        self, username: str, code: Any, ip_address: Any = None
    ) -> bool:
        return False


def _step_up_admin(world: _World) -> Any:
    from code_indexer.server.auth.elevation_step_up import step_up

    return step_up(
        _ADMIN,
        totp_code="000000",
        recovery_code=None,
        session_key="example-session-key",
        client_ip="testclient",
        totp_service=_RejectingTotp(),
        sessions=None,
        limiter=world.limiter,
    )


def test_login_and_step_up_throttles_never_share_a_key(world, monkeypatch):
    from code_indexer.server.auth.elevation_step_up import StepUpOutcome

    door = _web_door(world, monkeypatch)
    # The step-up key is the bare username, so the colliding login name is
    # the user's own: failed logins as admin must not throttle admin's
    # step-up (separate namespaces), nor the reverse.
    crafted = _ADMIN
    for _ in range(_MAX + 2):
        door.post(crafted, _WRONG)
    # Admin's step-up is not throttled by those logins: the code is checked.
    assert _step_up_admin(world).outcome is StepUpOutcome.INVALID_CODE

    # Reverse: admin's failed step-ups never throttle that login name.  Let
    # the crafted login's own attempts be forgotten (15 idle minutes) first.
    world.clock.advance(15 * 60 + 1)
    for _ in range(_MAX + 2):
        _step_up_admin(world)
    for _ in range(_MAX - 1):
        assert door.post(crafted, _WRONG).status_code == door.bad_status


def test_step_up_budget_is_per_user_whatever_the_client_address(world):
    from code_indexer.server.auth.elevation_step_up import StepUpOutcome, step_up

    def step_up_from(address: str) -> Any:
        return step_up(
            _ADMIN,
            totp_code="000000",
            recovery_code=None,
            session_key="example-session-key",
            client_ip=address,
            totp_service=_RejectingTotp(),
            sessions=None,
            limiter=world.limiter,
        )

    # Behind the proxy every address is the proxy's; directly, many
    # addresses must not multiply the budget: one budget per user.
    for i in range(1, _MAX + 1):
        assert step_up_from(f"192.0.2.{i}").outcome is StepUpOutcome.INVALID_CODE
    assert step_up_from("192.0.2.99").outcome is StepUpOutcome.LOCKED_OUT


def test_unknown_username_performs_the_dummy_password_work(world, door, monkeypatch):
    # An unknown name must cost a password hash too, or response time tells
    # whether the account exists.  Counted through a delegating wrapper on
    # the shared handler (the real bcrypt work still runs).
    from code_indexer.server.auth.auth_error_handler import auth_error_handler

    calls: List[int] = []
    real = auth_error_handler.perform_dummy_password_work

    def counting() -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(auth_error_handler, "perform_dummy_password_work", counting)
    response = door.post("ghost-user-that-does-not-exist", _WRONG)
    assert response.status_code == door.bad_status
    assert len(calls) == 1


def _raw_surrogate_request(door: _Door) -> Tuple[str, Dict[str, Any]]:
    credentials = {"username": "admin\ud800", "password": _WRONG}
    if door.name == "rest":
        return "/auth/login", credentials
    manager = door.client.app.state.oauth_manager
    client_id = manager.register_client(
        client_name="Surrogate Test Client", redirect_uris=[_REDIRECT_URI]
    )["client_id"]
    return "/oauth/authorize", {
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "code_challenge": _CODE_CHALLENGE,
        **credentials,
    }


# REST's LoginRequest model rejects such a name before the login handler
# runs (rendering that validation error currently answers 500, which predates
# A2 and is filed separately), so the throttle never sees it there.  OAuth's
# authorize door passes the name to the throttle and the user store.
@pytest.mark.parametrize("door_name", ["oauth"])
def test_lone_surrogate_username_is_an_ordinary_failure(world, monkeypatch, door_name):
    # A JSON body may carry "\ud800" (httpx 0.28 cannot encode it, so the
    # body is sent raw, ASCII-escaped).  No account can have such a name: it
    # must be an ordinary refused login, never a 500 with a traceback.
    door = _BUILDERS[door_name](world, monkeypatch)
    path, payload = _raw_surrogate_request(door)
    client = TestClient(door.client.app, raise_server_exceptions=False)
    response = client.post(
        path,
        content=json.dumps(payload).encode("ascii"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401, response.text
    assert world.failure_reasons() == ["bad_credentials"]


def test_known_user_wrong_password_does_no_dummy_work(world, door, monkeypatch):
    # The real hash already ran for an existing account; a dummy one on top
    # would make known names slower than unknown ones (2 hashes vs 1).
    from code_indexer.server.auth.auth_error_handler import auth_error_handler

    calls: List[int] = []
    real = auth_error_handler.perform_dummy_password_work

    def counting() -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(auth_error_handler, "perform_dummy_password_work", counting)
    assert door.post(_ADMIN, _WRONG).status_code == door.bad_status
    assert calls == []


def test_store_busy_answers_503_try_again_shortly(world, door):
    # Another writer holds cidx_server.db's write lock: the reservation gives
    # up after its 2 s bound and the door answers 503 instead of parking the
    # worker (or turning it into a 500).
    import time

    busy_answer_budget_s = 4.0
    db_path = str(world.tmp_path / "cidx_server.db")
    writer = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    try:
        writer.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            response = door.post(_ADMIN, _WRONG)
            elapsed = time.monotonic() - started
        finally:
            writer.execute("ROLLBACK")
    finally:
        writer.close()
    assert response.status_code == 503, response.text
    # One message for the whole login surface (REST, OAuth and Web form).
    assert "Login is busy, try again shortly." in response.text
    assert elapsed < busy_answer_budget_s, elapsed


def test_refusals_write_no_audit_rows_and_the_crossing_failure_is_rate_limited(
    world, door
):
    # Three credential checks -> three rows; the third crossed the threshold.
    _fail_until_throttled(door)
    # Three refusals (429) never reach the credential check -> no rows.
    for _ in range(3):
        assert door.post(_ADMIN, _PASSWORD).status_code == 429
    assert world.failure_reasons() == [
        "bad_credentials",
        "bad_credentials",
        "rate_limited",
    ]
