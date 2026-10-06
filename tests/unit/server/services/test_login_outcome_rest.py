"""REST login doors record exactly one outcome row per attempt.

Drives the REAL ``POST /auth/login`` and ``POST /auth/mfa/verify`` routes
through TestClient, with a real UserManager, JWT and refresh-token managers,
a real TOTPService and a real AuditLogService (temporary SQLite file) bound
as the process audit sink.  The legacy authentication-failure writer is
wired to the same store, so a duplicate failure row would be visible.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.audit_logger import password_audit_logger
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter
from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)
from code_indexer.server.services import audit_capture
from code_indexer.server.services.audit_log_service import AuditLogService

_USER = "example-user"
_MFA_USER = "example-mfa-user"
_PASSWORD = "SecureP@ssw0rd!XyZ789"


class _Env:
    def __init__(self, db_path: Path, totp, managers: Dict[str, Any]) -> None:
        self.db_path = db_path
        self.totp = totp
        self.managers = managers
        self.client = _client(managers, LoginRateLimiter())

    def rows(self) -> List[Tuple]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT action_type, admin_id, target_type, target_id, outcome, "
                "source, auth_method, details, event_uuid FROM audit_logs "
                "ORDER BY id"
            ).fetchall()
        finally:
            conn.close()


def _totp_secret(totp_service, username: str) -> str:
    uri = totp_service.get_provisioning_uri(username)
    return str(uri.split("secret=")[1].split("&")[0])


def _enroll_totp(totp_service, username: str) -> None:
    import pyotp

    totp_service.generate_secret(username)
    totp = pyotp.TOTP(_totp_secret(totp_service, username))
    assert totp_service.activate_mfa(username, totp.at(int(time.time()) - 30))


def _client(managers: Dict[str, Any], limiter: LoginRateLimiter) -> TestClient:
    from code_indexer.server.routers.inline_auth import register_auth_routes

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    register_auth_routes(app, login_rate_limiter=limiter, **managers)
    return TestClient(app)


def _rebuild_app(env: _Env, limiter: LoginRateLimiter) -> None:
    env.client = _client(env.managers, limiter)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[_Env]:
    from code_indexer.server.auth.totp_service import TOTPService
    from code_indexer.server.web import mfa_routes

    jwt_manager = JWTManager(secret_key="example-secret-for-login-outcome")
    users = UserManager(users_file_path=str(tmp_path / "users.json"))
    users.create_user(username=_USER, password=_PASSWORD, role=UserRole.ADMIN)
    users.create_user(username=_MFA_USER, password=_PASSWORD, role=UserRole.ADMIN)
    totp = TOTPService(db_path=str(tmp_path / "mfa.db"))
    _enroll_totp(totp, _MFA_USER)
    monkeypatch.setattr(mfa_routes, "_totp_service", totp)

    db_path = tmp_path / "groups.db"
    service = AuditLogService(db_path)
    service.start()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(service, node_id=None)
    monkeypatch.setattr(password_audit_logger, "_audit_service", service)

    managers: Dict[str, Any] = {
        "jwt_manager": jwt_manager,
        "user_manager": users,
        "refresh_token_manager": RefreshTokenManager(
            jwt_manager=jwt_manager, db_path=str(tmp_path / "refresh.db")
        ),
    }
    try:
        yield _Env(db_path, totp, managers)
    finally:
        service.stop()


def _login(env: _Env, username: str, password: str = _PASSWORD):
    return env.client.post(
        "/auth/login", json={"username": username, "password": password}
    )


def test_successful_login_writes_exactly_one_success_row(env) -> None:
    assert _login(env, _USER).status_code == 200
    rows = env.rows()
    assert len(rows) == 1
    action, actor, target_type, target_id, outcome, source, method, details, uid = rows[
        0
    ]
    assert (action, actor, target_type, target_id, outcome) == (
        "authentication_success",
        _USER,
        "auth",
        _USER,
        "success",
    )
    assert (source, method) == ("rest", "jwt")
    assert json.loads(details) == {
        "method": "password",
        "mfa": "not_enrolled",
        "flow": "rest_token",
    }
    assert uid


def test_bad_password_writes_exactly_one_failure_row(env) -> None:
    assert _login(env, _USER, "wrong-password").status_code == 401
    rows = env.rows()
    assert [(r[0], r[1], r[4], r[6]) for r in rows] == [
        ("authentication_failure", _USER, "failure", "none")
    ]
    assert json.loads(rows[0][7]) == {
        "method": "password",
        "stage": "credentials",
        "reason": "bad_credentials",
    }


def test_invalid_username_is_recorded_as_a_placeholder(env) -> None:
    assert _login(env, "bad/name").status_code == 401
    assert [(r[0], r[1], r[3]) for r in env.rows()] == [
        ("authentication_failure", "(unknown)", "(unknown)")
    ]


def test_password_typed_as_username_is_not_stored(env) -> None:
    typed = "Tr0ub4dor&3-horse"  # password-like text, no such account
    assert _login(env, typed).status_code == 401
    rows = env.rows()
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("authentication_failure", "(unknown)", "(unknown)")
    ]
    assert typed not in repr(rows)


def test_throttle_start_writes_one_row_and_throttled_refusals_none(env) -> None:
    # Two bad passwords start the throttle (limit 2): the attempt that
    # starts it is recorded once, as rate_limited.  Refusals while throttled
    # write nothing (they cannot be used to flood the store).
    _rebuild_app(env, LoginRateLimiter(max_attempts=2))
    for _ in range(2):
        assert _login(env, _USER, "wrong-password").status_code == 401
    rows = env.rows()
    assert [json.loads(r[7])["reason"] for r in rows] == [
        "bad_credentials",
        "rate_limited",
    ]
    assert {(r[0], r[1], r[4]) for r in rows} == {
        ("authentication_failure", _USER, "failure")
    }
    for _ in range(4):
        assert _login(env, _USER).status_code == 429
    assert len(env.rows()) == 2


def test_token_bucket_refusals_write_no_rows(env, monkeypatch) -> None:
    from code_indexer.server.auth.token_bucket import TokenBucketManager
    from code_indexer.server.routers import inline_auth

    # A frozen clock: the bucket never refills, so exactly `capacity`
    # attempts reach the credential check however slow each one is.
    monkeypatch.setattr(
        inline_auth,
        "rate_limiter",
        TokenBucketManager(capacity=10, time_fn=lambda: 0.0),
    )
    _rebuild_app(env, LoginRateLimiter(enabled=False))
    statuses = [_login(env, _USER, "wrong-password").status_code for _ in range(13)]
    assert statuses.count(401) == 10 and statuses[-3:] == [429, 429, 429]
    assert len(env.rows()) == 10  # one row per credential check, none per refusal


def test_mfa_challenge_step_writes_nothing(env) -> None:
    response = _login(env, _MFA_USER)
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert env.rows() == []


def _challenge(env: _Env) -> str:
    return str(_login(env, _MFA_USER).json()["mfa_token"])


def test_mfa_code_failure_writes_one_failure_row(env) -> None:
    token = _challenge(env)
    response = env.client.post(
        "/auth/mfa/verify", json={"mfa_token": token, "totp_code": "000000"}
    )
    assert response.status_code == 401
    rows = env.rows()
    assert [(r[0], r[1], r[4]) for r in rows] == [
        ("authentication_failure", _MFA_USER, "failure")
    ]
    assert json.loads(rows[0][7]) == {
        "method": "password",
        "stage": "mfa_code",
        "reason": "mfa_code_invalid",
    }


def test_invalid_challenge_writes_one_failure_row(env) -> None:
    response = env.client.post(
        "/auth/mfa/verify",
        json={"mfa_token": "not-a-challenge", "totp_code": "000000"},
    )
    assert response.status_code == 401
    rows = env.rows()
    assert [(r[0], r[1]) for r in rows] == [("authentication_failure", "(unknown)")]
    assert json.loads(rows[0][7])["reason"] == "challenge_invalid_or_expired"


def test_mfa_success_writes_one_success_row(env) -> None:
    import pyotp

    token = _challenge(env)
    code = pyotp.TOTP(_totp_secret(env.totp, _MFA_USER)).now()
    response = env.client.post(
        "/auth/mfa/verify", json={"mfa_token": token, "totp_code": code}
    )
    assert response.status_code == 200
    rows = env.rows()
    assert [(r[0], r[1], r[4]) for r in rows] == [
        ("authentication_success", _MFA_USER, "success")
    ]
    assert json.loads(rows[0][7]) == {
        "method": "password",
        "mfa": "totp",
        "flow": "rest_token",
    }
