"""
Web UI elevation endpoints share the failed-attempt lockout of REST
``POST /auth/elevate`` and MCP ``elevate_session``.

``POST /auth/elevate-ajax`` and ``POST /auth/elevate-form`` use the SAME
login rate limiter instance with the SAME key (``f"{client_ip}:{username}"``)
as REST: a wrong TOTP or recovery code records a failure, a locked-out
caller is refused even with a correct code, and a successful elevation
resets the counter. With the kill switch OFF the web endpoints behave as
before (no code check, no lockout bookkeeping).

Front door: a TestClient over the REAL REST elevation router and the REAL
web elevation router, authenticated with a real login JWT (Bearer).
JWTManager, UserManager, TOTPService and ElevatedSessionManager are real
objects backed by temporary storage; the login rate limiter is the REAL
process-wide singleton (its entry for the test key is cleared before and
after each test). Only the elevation-enforcement config read is pinned.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from unittest.mock import patch

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server import app as real_app_module
from code_indexer.server.auth import dependencies
from code_indexer.server.auth import elevation_routes
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.login_rate_limiter import login_rate_limiter
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import elevation_web_routes, mfa_routes

_PASSWORD = "Front-Door-Pa55word!"
_USERNAME = "alice"
# TestClient reports this as request.client.host.
_LIMITER_KEY = f"testclient:{_USERNAME}"
_MAX_ATTEMPTS = 5
_WEB = "code_indexer.server.web.elevation_web_routes"
_REST = "code_indexer.server.auth.elevation_routes"


class _Door:
    def __init__(self, tmp_path: Path) -> None:
        self.jwt = JWTManager(secret_key="web-lockout-test-secret")
        self.users = UserManager(users_file_path=str(tmp_path / "users.json"))
        self.totp = TOTPService(
            db_path=str(tmp_path / "totp.db"),
            mfa_encryption_key=Fernet.generate_key().decode(),
        )
        self.esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elevation.db"),
        )
        app = FastAPI()
        app.include_router(elevation_routes.router)
        app.include_router(elevation_web_routes.router)
        self.client = TestClient(app)
        self.users.create_user(_USERNAME, _PASSWORD, UserRole.NORMAL_USER)
        self.secret = self.totp.generate_secret(_USERNAME)
        assert self.totp.activate_mfa(_USERNAME, pyotp.TOTP(self.secret).now())
        user = self.users.get_user(_USERNAME)
        assert user is not None
        self.token = self.jwt.create_token(
            {"username": user.username, "role": user.role.value}
        )
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def valid_code(self) -> str:
        return str(pyotp.TOTP(self.secret).now())

    def wrong_code(self) -> str:
        totp = pyotp.TOTP(self.secret)
        now = int(time.time())
        accepted = {totp.at(now + step * 30) for step in (-2, -1, 0, 1, 2)}
        for candidate in ("000000", "111111", "222222", "333333"):
            if candidate not in accepted:
                return candidate
        raise AssertionError("no rejected candidate code found")

    def rest(self, totp_code: str) -> Any:
        return self.client.post(
            "/auth/elevate", json={"totp_code": totp_code}, headers=self.headers
        )

    def ajax(self, **form: str) -> Any:
        return self.client.post("/auth/elevate-ajax", data=form, headers=self.headers)

    def form(self, **form: str) -> Any:
        data: Dict[str, str] = {"next": "/admin/"}
        data.update(form)
        return self.client.post(
            "/auth/elevate-form",
            data=data,
            headers=self.headers,
            follow_redirects=False,
        )

    def window_open(self) -> bool:
        jti = str(self.jwt.validate_token(self.token)["jti"])
        return self.esm.get_status(jti) is not None


def _run(tmp_path: Path, enforcement: bool) -> Iterator[_Door]:
    # Construct the real app first (initialises the web session manager the
    # hybrid auth dependency consults) so its startup wiring cannot
    # overwrite the auth globals patched below.
    assert real_app_module.app.state is not None
    door = _Door(tmp_path)
    previous_totp: Optional[Any] = mfa_routes.get_totp_service()
    mfa_routes.set_totp_service(door.totp)
    login_rate_limiter.record_success(_LIMITER_KEY)
    try:
        with (
            patch.object(dependencies, "jwt_manager", door.jwt),
            patch.object(dependencies, "user_manager", door.users),
            patch.object(dependencies, "oauth_manager", None),
            patch.object(dependencies, "mcp_credential_manager", None),
            patch.object(dependencies, "server_config", None),
            patch(
                f"{_WEB}._is_elevation_enforcement_enabled", return_value=enforcement
            ),
            patch(
                f"{_REST}._is_elevation_enforcement_enabled", return_value=enforcement
            ),
            patch(f"{_WEB}.elevated_session_manager", door.esm),
            patch(f"{_REST}.elevated_session_manager", door.esm),
        ):
            yield door
    finally:
        login_rate_limiter.record_success(_LIMITER_KEY)
        mfa_routes.set_totp_service(previous_totp)


@pytest.fixture
def door(tmp_path: Path) -> Iterator[_Door]:
    yield from _run(tmp_path, enforcement=True)


@pytest.fixture
def door_enforcement_off(tmp_path: Path) -> Iterator[_Door]:
    yield from _run(tmp_path, enforcement=False)


def test_web_routes_use_the_rest_limiter_instance():
    assert (
        elevation_web_routes.login_rate_limiter is elevation_routes.login_rate_limiter
    )


class TestSharedCounterAcrossFrontDoors:
    def test_rest_failures_plus_one_web_failure_lock_out_both(self, door):
        for _ in range(_MAX_ATTEMPTS - 1):
            assert door.rest(door.wrong_code()).status_code == 401
        assert door.ajax(totp_code=door.wrong_code()).status_code == 401

        rest = door.rest(door.valid_code())
        assert rest.status_code == 429, rest.text
        ajax = door.ajax(totp_code=door.valid_code())
        assert ajax.status_code == 429, ajax.text
        assert ajax.json()["success"] is False
        form = door.form(totp_code=door.valid_code())
        assert form.status_code == 429, form.text
        assert not door.window_open()

    def test_web_form_failures_lock_out_rest(self, door):
        for _ in range(_MAX_ATTEMPTS):
            assert door.form(totp_code=door.wrong_code()).status_code == 401

        assert door.rest(door.valid_code()).status_code == 429
        assert not door.window_open()

    def test_wrong_recovery_codes_count_toward_lockout(self, door):
        for _ in range(_MAX_ATTEMPTS):
            resp = door.ajax(recovery_code="NOT-A-REAL-CODE")
            assert resp.status_code == 401, resp.text

        assert door.ajax(totp_code=door.valid_code()).status_code == 429
        assert not door.window_open()


class TestSuccessPath:
    def test_valid_code_elevates_via_ajax(self, door):
        resp = door.ajax(totp_code=door.valid_code())

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"success": True}
        assert door.window_open()

    def test_valid_code_elevates_via_form(self, door):
        resp = door.form(totp_code=door.valid_code())

        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/admin/"
        assert door.window_open()

    def test_success_resets_the_failure_counter(self, door):
        # A TOTP step is single-use, so the second success uses a recovery
        # code.
        recovery_code = door.totp.generate_recovery_codes(_USERNAME)[0]
        for _ in range(_MAX_ATTEMPTS - 1):
            assert door.ajax(totp_code=door.wrong_code()).status_code == 401
        assert door.ajax(totp_code=door.valid_code()).status_code == 200

        for _ in range(_MAX_ATTEMPTS - 1):
            assert door.ajax(totp_code=door.wrong_code()).status_code == 401
        resp = door.ajax(recovery_code=recovery_code)
        assert resp.status_code == 200, resp.text


class TestEnforcementOff:
    def test_web_endpoints_unchanged_and_record_no_failures(self, door_enforcement_off):
        door = door_enforcement_off
        for _ in range(_MAX_ATTEMPTS + 1):
            resp = door.ajax(totp_code=door.wrong_code())
            assert resp.status_code == 200
            assert resp.json() == {"success": True}
        form = door.form(totp_code=door.wrong_code())
        assert form.status_code == 303

        assert login_rate_limiter.is_locked(_LIMITER_KEY)[0] is False
        assert login_rate_limiter._failures.get(_LIMITER_KEY, []) == []
