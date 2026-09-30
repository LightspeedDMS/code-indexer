"""Login doors on the Web form and the MCP authenticate tool.

Each door records exactly one outcome row per attempt through the login
outcome entry point; a password step that only returns an MFA challenge
records nothing (the challenge answer is recorded by its own door); a
rate-limited attempt records nothing.

Drives the REAL routes through TestClient with the audit request context
middleware, a real UserManager, real JWT/session managers, a real TOTP
service and a real audit store.  Only the login form's CSRF check and the
password-expiry configuration lookup are replaced.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _audit_accounts_support import AuditStore, bound_audit_store, make_user_manager
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)

_PASSWORD = "SecureP@ssw0rd!XyZ789"
_USER = "example-user"
_MFA_USER = "example-mfa-user"


def _enroll_totp(totp_service, username: str) -> None:
    import pyotp

    totp_service.generate_secret(username)
    uri = totp_service.get_provisioning_uri(username)
    secret = uri.split("secret=")[1].split("&")[0]
    assert totp_service.activate_mfa(
        username, pyotp.TOTP(secret).at(int(time.time()) - 30)
    )


class _Env:
    def __init__(self, store: AuditStore, users, client: TestClient) -> None:
        self.store = store
        self.users = users
        self.client = client


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[_Env]:
    from code_indexer.server.auth import dependencies
    from code_indexer.server.auth.jwt_manager import JWTManager
    from code_indexer.server.auth.totp_service import TOTPService
    from code_indexer.server.mcp.protocol import mcp_router
    from code_indexer.server.web import mfa_routes, routes
    from code_indexer.server.web.auth import SessionManager

    # The MCP dispatcher builds the lazy server app on first use, which
    # rebinds the auth singletons; build it BEFORE they are patched below.
    import code_indexer.server.app as app_module

    assert app_module.app is not None
    users = make_user_manager(tmp_path)
    users.create_user(_USER, _PASSWORD, UserRole.NORMAL_USER)
    users.create_user(_MFA_USER, _PASSWORD, UserRole.NORMAL_USER)
    totp = TOTPService(db_path=str(tmp_path / "mfa.db"))
    _enroll_totp(totp, _MFA_USER)
    monkeypatch.setattr(mfa_routes, "_totp_service", totp)
    monkeypatch.setattr(dependencies, "user_manager", users)
    monkeypatch.setattr(
        dependencies, "jwt_manager", JWTManager(secret_key="example-jwt-secret")
    )
    monkeypatch.setattr(routes, "validate_login_csrf_token", lambda _r, _t: True)
    sessions = SessionManager(
        "example-session-secret", SimpleNamespace(host="127.0.0.1")
    )
    monkeypatch.setattr(routes, "get_session_manager", lambda: sessions)

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(routes.login_router)
    app.include_router(mcp_router)
    for store in bound_audit_store(tmp_path / "groups.db"):
        yield _Env(store, users, TestClient(app))


def _expire_passwords(monkeypatch) -> None:
    from code_indexer.server.web import routes

    config = SimpleNamespace(
        password_expiry_config=SimpleNamespace(enabled=True, max_age_days=-1)
    )
    service = SimpleNamespace(get_config=lambda: config)
    monkeypatch.setattr(routes, "get_config_service", lambda: service)


def _web_login(env: _Env, username: str, password: str = _PASSWORD):
    return env.client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": "x"},
        follow_redirects=False,
    )


def _mcp_authenticate(env: _Env, username: str, api_key: str):
    return env.client.post(
        "/mcp-public",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "authenticate",
                "arguments": {"username": username, "api_key": api_key},
            },
        },
    )


# ------------------------------------------------------- Web login form


def test_web_login_success_writes_one_success_row(env) -> None:
    assert _web_login(env, _USER).status_code == 303
    (row,) = env.store.rows("authentication_")
    assert (row.action_type, row.actor, row.target_id, row.outcome) == (
        "authentication_success",
        _USER,
        _USER,
        "success",
    )
    assert (row.source, row.auth_method) == ("web", "web_session")
    assert row.details == {
        "method": "password",
        "mfa": "not_enrolled",
        "flow": "web_session",
    }


def test_web_login_bad_password_writes_one_failure_row(env) -> None:
    response = _web_login(env, _USER, "wrong-password-value")
    assert response.status_code == 200
    (row,) = env.store.rows("authentication_")
    assert (row.action_type, row.actor, row.outcome) == (
        "authentication_failure",
        _USER,
        "failure",
    )
    assert (row.source, row.auth_method) == ("web", "none")
    assert row.details == {
        "method": "password",
        "stage": "credentials",
        "reason": "bad_credentials",
    }
    assert "wrong-password-value" not in env.store.all_raw_text()


def test_web_login_unknown_account_is_never_named(env) -> None:
    _web_login(env, "Str0ng-typed-in-wrong-field", "x")
    (row,) = env.store.rows("authentication_")
    assert (row.actor, row.target_id) == ("(unknown)", "(unknown)")
    assert "Str0ng-typed-in-wrong-field" not in env.store.all_raw_text()


def test_web_login_mfa_challenge_writes_nothing(env) -> None:
    assert _web_login(env, _MFA_USER).status_code == 200
    assert env.store.rows("authentication_") == []


# ------------------------------------- Web login, password-expired branch


def test_password_expired_web_login_writes_one_success_row(env, monkeypatch) -> None:
    _expire_passwords(monkeypatch)
    response = _web_login(env, _USER)
    assert response.status_code == 303
    assert "password_expired" in response.headers["location"]
    (row,) = env.store.rows("authentication_")
    assert (row.action_type, row.actor, row.source) == (
        "authentication_success",
        _USER,
        "web",
    )
    assert row.details["flow"] == "web_session"


def test_password_expired_mfa_user_gets_challenge_and_no_row(env, monkeypatch) -> None:
    _expire_passwords(monkeypatch)
    assert _web_login(env, _MFA_USER).status_code == 200
    assert env.store.rows("authentication_") == []


# ------------------------------------------------ MCP authenticate tool


def _api_key(env: _Env) -> str:
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    raw_key, _key_id = ApiKeyManager(env.users).generate_key(_USER)
    return raw_key


def test_mcp_authenticate_success_writes_one_success_row(env) -> None:
    raw_key = _api_key(env)
    body = _mcp_authenticate(env, _USER, raw_key).json()
    assert '"success": true' in body["result"]["content"][0]["text"]
    (row,) = env.store.rows("authentication_")
    assert (row.action_type, row.actor, row.outcome, row.source) == (
        "authentication_success",
        _USER,
        "success",
        "mcp",
    )
    assert row.auth_method == "jwt"
    assert row.details == {
        "method": "api_key",
        "mfa": "not_applicable",
        "flow": "mcp_jwt",
    }
    assert raw_key not in env.store.all_raw_text()


def test_mcp_authenticate_bad_key_writes_one_failure_row(env) -> None:
    _api_key(env)
    bad_key = "cidx_sk_0000000000000000000000000000beef"
    _mcp_authenticate(env, _USER, bad_key)
    (row,) = env.store.rows("authentication_")
    assert (row.action_type, row.actor, row.source, row.auth_method) == (
        "authentication_failure",
        _USER,
        "mcp",
        "none",
    )
    assert row.details == {
        "method": "api_key",
        "stage": "credentials",
        "reason": "bad_credentials",
    }
    assert bad_key not in env.store.all_raw_text()


def test_mcp_authenticate_unknown_account_is_never_named(env) -> None:
    _mcp_authenticate(env, "typed-secret-looking-value", "cidx_sk_00")
    (row,) = env.store.rows("authentication_")
    assert row.actor == "(unknown)"


def test_mcp_authenticate_rate_limited_attempt_writes_no_row(env, monkeypatch) -> None:
    from code_indexer.server.auth import token_bucket

    monkeypatch.setattr(
        token_bucket.rate_limiter, "consume", lambda _username: (False, 5.0)
    )
    _mcp_authenticate(env, _USER, "cidx_sk_00")
    assert env.store.rows("authentication_") == []
