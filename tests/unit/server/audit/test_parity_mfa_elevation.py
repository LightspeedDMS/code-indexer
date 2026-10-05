"""MFA lifecycle and elevation step-up each record one audit row per action.

MFA (Web is the only door): activation, recovery-code regeneration, disable
and an admin's cross-user secret regeneration each record exactly one row
from the ONE TOTPService entry point every Web route calls.  Activation
never also records a regeneration row.

Elevation (REST, Web form, Web AJAX, MCP): every door goes through the one
step-up function, so each records the same ``elevation_granted`` /
``elevation_failed`` row.  Automatic elevation on credentialed requests
records nothing.

No TOTP code, recovery code or secret ever reaches a row.

Front door: the real routers on a FastAPI app with the real audit request
context middleware, a real TOTPService, a real signed web session, a real
ElevatedSessionManager and a real AuditLogService bound as the audit sink.
Only the elevation-enforcement switch is pinned.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, List
from unittest.mock import patch

import pyotp
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from _audit_mfa_login_support import AuditStore, bound_audit_store
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
)
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME, SessionManager

_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_ADMIN = "audit-admin"
_OTHER = "audit-other"
_USER = "audit-user"
_RECOVERY_CODE_RE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}")

_MFA_TYPES = (
    "mfa_activated",
    "mfa_recovery_codes_regenerated",
    "mfa_disabled",
    "mfa_secret_regenerated_cross_user",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


@pytest.fixture
def totp(tmp_path: Path) -> Iterator[TOTPService]:
    svc = TOTPService(db_path=str(tmp_path / "mfa.db"))
    mfa_routes.set_totp_service(svc)
    yield svc
    mfa_routes.set_totp_service(None)


@pytest.fixture
def sessions(monkeypatch) -> SessionManager:
    sm = SessionManager("audit-mfa-test-signing-key", SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr(web_auth, "_session_manager", sm)
    return sm


@pytest.fixture
def esm(tmp_path: Path, monkeypatch) -> ElevatedSessionManager:
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", manager)
    return manager


@pytest.fixture
def web(store, totp, sessions, esm) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(mfa_routes.mfa_router)
    app.include_router(mfa_routes.user_mfa_router, prefix="/user/mfa")
    with patch(_ENFORCEMENT_PATH, return_value=True):
        yield TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cookie(sm: SessionManager, username: str, role: str) -> str:
    resp = Response()
    sm.create_session(resp, username=username, role=role)
    header = resp.headers["set-cookie"]
    prefix = f"{SESSION_COOKIE_NAME}="
    return header[len(prefix) :].split(";", 1)[0]


def _login(client: TestClient, sm: SessionManager, username: str, role: str) -> str:
    cookie = _cookie(sm, username, role)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    return cookie


def _enroll(svc: TOTPService, username: str) -> str:
    secret = svc.generate_secret(username)
    assert svc.activate_mfa(username, pyotp.TOTP(secret).now())
    return secret


def _next_step_code(secret: str) -> str:
    """A valid code from the NEXT time step (the current one was used to enroll)."""
    return str(pyotp.TOTP(secret).at(int(time.time()) + 30))


def _elevate(
    esm: ElevatedSessionManager, cookie: str, username: str, scope: str
) -> None:
    esm.create(
        session_key=cookie, username=username, elevated_from_ip=None, scope=scope
    )


def _recovery_codes(html: str) -> List[str]:
    return _RECOVERY_CODE_RE.findall(html)


# ---------------------------------------------------------------------------
# MFA activation
# ---------------------------------------------------------------------------


class TestActivation:
    def test_admin_activation_writes_one_activation_row(
        self, web, totp, sessions, store
    ):
        _login(web, sessions, _ADMIN, "admin")
        secret = totp.generate_secret(_ADMIN)
        code = pyotp.TOTP(secret).now()

        resp = web.post("/admin/mfa/verify", data={"totp_code": code})

        assert resp.status_code == 200, resp.text
        assert len(_recovery_codes(resp.text)) == 10
        rows = store.rows(*_MFA_TYPES)
        assert [r.action_type for r in rows] == ["mfa_activated"]
        row = rows[0]
        assert (row.actor, row.target_type, row.target_id) == (_ADMIN, "user", _ADMIN)
        assert row.outcome == "success"
        assert row.source == "web"
        assert row.auth_method == "web_session"
        assert row.details == {"recovery_codes_issued": True}
        raw = store.all_raw_text()
        assert code not in raw
        assert not any(c in raw for c in _recovery_codes(resp.text))

    def test_user_activation_writes_one_activation_row(
        self, web, totp, sessions, store
    ):
        _login(web, sessions, _USER, "normal_user")
        secret = totp.generate_secret(_USER)

        resp = web.post(
            "/user/mfa/verify", data={"totp_code": pyotp.TOTP(secret).now()}
        )

        assert resp.status_code == 200, resp.text
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id) for r in rows] == [
            ("mfa_activated", _USER, _USER)
        ]
        assert rows[0].details == {"recovery_codes_issued": True}

    def test_invalid_activation_code_writes_no_row(self, web, totp, sessions, store):
        _login(web, sessions, _USER, "normal_user")
        totp.generate_secret(_USER)

        resp = web.post("/user/mfa/verify", data={"totp_code": "000000"})

        assert resp.status_code == 200, resp.text
        assert not totp.is_mfa_enabled(_USER)
        assert store.rows(*_MFA_TYPES) == []


# ---------------------------------------------------------------------------
# Recovery-code regeneration
# ---------------------------------------------------------------------------


@pytest.fixture
def accounts(tmp_path: Path, monkeypatch) -> UserManager:
    users_db = str(tmp_path / "users.db")
    DatabaseSchema(users_db).initialize_database()
    manager = UserManager(use_sqlite=True, db_path=users_db)
    manager.create_user(_ADMIN, "Example-Passw0rd!x", UserRole.ADMIN)
    manager.create_user(_OTHER, "Example-Passw0rd!x", UserRole.ADMIN)
    manager.create_user(_USER, "Example-Passw0rd!x", UserRole.NORMAL_USER)
    monkeypatch.setattr(dependencies, "user_manager", manager)
    return manager


def _page_csrf(client: TestClient, page: str) -> str:
    """GET the recovery-code page (sets the signed CSRF cookie); return its token."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(page).text)
    assert match is not None
    return match.group(1)


_ADMIN_RECOVERY = "/admin/mfa/recovery-codes"
_USER_RECOVERY = "/user/mfa/recovery-codes"


@pytest.mark.usefixtures("accounts")
class TestRecoveryCodeRegeneration:
    def test_admin_self_regeneration_writes_one_row(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _ADMIN)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "totp_repair")
        token = _page_csrf(web, _ADMIN_RECOVERY)

        resp = web.post(_ADMIN_RECOVERY, data={"csrf_token": token})

        assert resp.status_code == 200, resp.text
        codes = _recovery_codes(resp.text)
        assert len(codes) == 10
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("mfa_recovery_codes_regenerated", _ADMIN, _ADMIN, "success")
        ]
        assert rows[0].details == {"count": 10}
        raw = store.all_raw_text()
        assert not any(c in raw for c in codes)

    def test_admin_cross_user_regeneration_names_actor_and_target(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _OTHER)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")
        token = _page_csrf(web, f"{_ADMIN_RECOVERY}?user={_OTHER}")

        resp = web.post(_ADMIN_RECOVERY, data={"user": _OTHER, "csrf_token": token})

        assert resp.status_code == 200, resp.text
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id) for r in rows] == [
            ("mfa_recovery_codes_regenerated", _ADMIN, _OTHER)
        ]

    def test_user_self_service_regeneration_writes_one_row(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _USER)
        cookie = _login(web, sessions, _USER, "normal_user")
        _elevate(esm, cookie, _USER, "totp_repair")
        token = _page_csrf(web, _USER_RECOVERY)

        resp = web.post(_USER_RECOVERY, data={"csrf_token": token})

        assert resp.status_code == 200, resp.text
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id) for r in rows] == [
            ("mfa_recovery_codes_regenerated", _USER, _USER)
        ]
        assert rows[0].source == "web"

    def test_refused_regeneration_writes_no_row(self, web, totp, sessions, store):
        _enroll(totp, _ADMIN)
        _login(web, sessions, _ADMIN, "admin")
        token = _page_csrf(web, _ADMIN_RECOVERY)

        # valid form token, no elevation window
        resp = web.post(_ADMIN_RECOVERY, data={"csrf_token": token})

        assert resp.status_code == 403, resp.text
        assert "Elevation Required" in resp.text
        assert store.rows(*_MFA_TYPES) == []


# ---------------------------------------------------------------------------
# Disable
# ---------------------------------------------------------------------------


class TestDisable:
    def test_admin_disable_with_totp_writes_one_row(
        self, web, totp, sessions, esm, store
    ):
        secret = _enroll(totp, _ADMIN)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "totp_repair")
        code = _next_step_code(secret)

        resp = web.post("/admin/mfa/disable", data={"totp_code": code})

        assert resp.status_code == 303, resp.text
        assert not totp.is_mfa_enabled(_ADMIN)
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("mfa_disabled", _ADMIN, _ADMIN, "success")
        ]
        assert rows[0].details == {"method": "totp"}
        assert code not in store.all_raw_text()

    def test_user_disable_with_recovery_code_records_the_method(
        self, web, totp, sessions, store
    ):
        _enroll(totp, _USER)
        recovery = totp.generate_recovery_codes(_USER)[0]
        _login(web, sessions, _USER, "normal_user")

        resp = web.post("/user/mfa/disable", data={"totp_code": recovery})

        assert resp.status_code == 303, resp.text
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id) for r in rows] == [
            ("mfa_disabled", _USER, _USER)
        ]
        assert rows[0].details == {"method": "recovery_code"}
        assert recovery not in store.all_raw_text()

    def test_wrong_code_disables_nothing_and_writes_no_row(
        self, web, totp, sessions, store
    ):
        _enroll(totp, _USER)
        _login(web, sessions, _USER, "normal_user")

        resp = web.post("/user/mfa/disable", data={"totp_code": "not-a-code"})

        assert resp.status_code == 400, resp.text
        assert totp.is_mfa_enabled(_USER)
        assert store.rows(*_MFA_TYPES) == []


# ---------------------------------------------------------------------------
# Cross-user secret regeneration
# ---------------------------------------------------------------------------


class TestCrossUserSecretRegeneration:
    def test_admin_resetting_another_users_secret_writes_one_row(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _OTHER)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")

        resp = web.get(f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1")

        assert resp.status_code == 200, resp.text
        assert not totp.is_mfa_enabled(_OTHER)  # a new, unactivated secret
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("mfa_secret_regenerated_cross_user", _ADMIN, _OTHER, "success")
        ]
        assert rows[0].details == {}
        key = totp.get_manual_entry_key(_OTHER)
        assert key is not None
        assert key.replace(" ", "") not in store.all_raw_text()

    def test_own_setup_and_show_mode_write_no_row(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _OTHER)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")

        own = web.get("/admin/mfa/setup")
        show = web.get(f"/admin/mfa/setup?user={_OTHER}&mode=show")

        assert own.status_code == 200, own.text
        assert show.status_code == 200, show.text
        assert store.rows(*_MFA_TYPES) == []

    def test_refused_cross_user_reset_writes_no_row(
        self, web, totp, sessions, esm, store
    ):
        _enroll(totp, _OTHER)
        cookie = _login(web, sessions, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")

        resp = web.get(f"/admin/mfa/setup?user={_OTHER}")  # no confirm_overwrite

        assert resp.status_code == 400, resp.text
        assert totp.is_mfa_enabled(_OTHER)
        assert store.rows(*_MFA_TYPES) == []


# ---------------------------------------------------------------------------
# Entry-point failure paths and fail-open behaviour
# ---------------------------------------------------------------------------


def _break_storage(svc: TOTPService, tmp_path: Path) -> None:
    """Point the service at a directory: every connection now fails."""
    svc._db_path = str(tmp_path)


class TestMfaEntryPointFailures:
    def test_failed_disable_records_failure_and_raises(self, totp, store, tmp_path):
        _enroll(totp, _USER)
        _break_storage(totp, tmp_path)

        with pytest.raises(sqlite3.Error):
            totp.disable_mfa(_USER, actor=_ADMIN, method="totp")

        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("mfa_disabled", _ADMIN, _USER, "failure")
        ]
        assert rows[0].details == {"method": "totp"}

    def test_failed_regeneration_records_failure_and_raises(
        self, totp, store, tmp_path
    ):
        _break_storage(totp, tmp_path)

        with pytest.raises(sqlite3.Error):
            totp.regenerate_recovery_codes(_USER, actor=_USER)

        assert [(r.action_type, r.outcome) for r in store.rows(*_MFA_TYPES)] == [
            ("mfa_recovery_codes_regenerated", "failure")
        ]

    def test_failed_activation_records_failure_and_raises(self, totp, store, tmp_path):
        _break_storage(totp, tmp_path)

        with pytest.raises(sqlite3.Error):
            totp.activate_mfa_and_issue_recovery_codes(_USER, "123456", actor=_USER)

        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("mfa_activated", "failure")
        ]
        # Nothing changed: no partial-state detail.
        assert rows[0].details == {}

    def test_codes_failing_after_activation_records_failure_with_the_partial_state(
        self, totp, store
    ):
        secret = totp.generate_secret(_USER)
        conn = sqlite3.connect(totp._db_path)
        try:
            conn.execute("DROP TABLE user_recovery_codes")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(sqlite3.Error):
            totp.activate_mfa_and_issue_recovery_codes(
                _USER, pyotp.TOTP(secret).now(), actor=_USER
            )

        assert totp.is_mfa_enabled(_USER)  # activated; codes not issued
        rows = store.rows(*_MFA_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("mfa_activated", _USER, _USER, "failure")
        ]
        assert rows[0].details == {"recovery_codes_issued": False}

    def test_failed_cross_user_reset_records_failure_and_raises(
        self, totp, store, tmp_path
    ):
        _break_storage(totp, tmp_path)

        with pytest.raises(sqlite3.Error):
            totp.regenerate_secret_cross_user(_OTHER, actor=_ADMIN)

        assert [
            (r.action_type, r.actor, r.target_id, r.outcome)
            for r in store.rows(*_MFA_TYPES)
        ] == [("mfa_secret_regenerated_cross_user", _ADMIN, _OTHER, "failure")]

    def test_cross_user_reset_of_own_account_is_refused(self, totp, store):
        with pytest.raises(ValueError):
            totp.regenerate_secret_cross_user(_ADMIN, actor=_ADMIN)
        assert store.rows(*_MFA_TYPES) == []

    def test_unwritable_audit_store_never_blocks_the_action(self, totp):
        from code_indexer.server.services import audit_capture

        _enroll(totp, _USER)
        audit_capture.mark_server_process()  # server process, nothing bound
        dropped_before = audit_capture.records_dropped_since_boot()

        totp.disable_mfa(_USER, actor=_USER, method="totp")

        assert not totp.is_mfa_enabled(_USER)
        assert audit_capture.records_dropped_since_boot() == dropped_before + 1


# ---------------------------------------------------------------------------
# Elevation step-up: REST, Web form, Web AJAX and MCP record the same rows
# ---------------------------------------------------------------------------

_ELEVATION_TYPES = ("elevation_granted", "elevation_failed")
_PASSWORD = "Audit-Door-Pa55word!"
_REST_MOD = "code_indexer.server.auth.elevation_routes"
_WEB_MOD = "code_indexer.server.web.elevation_web_routes"
_MCP_MOD = "code_indexer.server.mcp.handlers.admin.elevate_session"
_DECORATOR_MOD = "code_indexer.server.mcp.auth.elevation_decorator"
_DOORS = ("rest", "web_form", "web_ajax", "mcp")


class _ElevationDoors:
    """Real auth stack; a TestClient over the real REST, Web and MCP routers."""

    def __init__(self, tmp_path: Path) -> None:
        from code_indexer.server.auth.elevation_routes import router as rest_router
        from code_indexer.server.auth.jwt_manager import JWTManager
        from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter
        from code_indexer.server.auth.mcp_credential_manager import (
            MCPCredentialManager,
        )
        from code_indexer.server.auth.user_manager import UserManager, UserRole
        from code_indexer.server.mcp.protocol import mcp_router
        from code_indexer.server.web.elevation_web_routes import router as web_router

        self.jwt = JWTManager(secret_key="audit-elevation-test-secret")
        self.users = UserManager(users_file_path=str(tmp_path / "users.json"))
        self.credentials = MCPCredentialManager(user_manager=self.users)
        self.totp = TOTPService(db_path=str(tmp_path / "totp.db"))
        self.esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elevation.db"),
        )
        self.limiter = LoginRateLimiter()
        app = FastAPI()
        app.add_middleware(AuditRequestContextMiddleware)
        app.include_router(mcp_router)
        app.include_router(rest_router)
        app.include_router(web_router)
        self.client = TestClient(app, follow_redirects=False)
        self.users.create_user(_USER, _PASSWORD, UserRole.NORMAL_USER)
        self.secret = self.totp.generate_secret(_USER)
        assert self.totp.activate_mfa(_USER, pyotp.TOTP(self.secret).now())
        self.token = self.jwt.create_token({"username": _USER, "role": "normal_user"})
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def valid_code(self) -> str:
        return _next_step_code(self.secret)

    def wrong_code(self) -> str:
        totp = pyotp.TOTP(self.secret)
        now = int(time.time())
        accepted = {totp.at(now + step * 30) for step in (-2, -1, 0, 1, 2)}
        for candidate in ("000000", "111111", "222222", "333333"):
            if candidate not in accepted:
                return candidate
        raise AssertionError("no rejected candidate code found")

    def elevate(self, door: str, **codes: str) -> object:
        """Submit *codes* through *door*; return the door's raw response."""
        if door == "rest":
            return self.client.post("/auth/elevate", json=codes, headers=self.headers)
        if door == "web_form":
            data = {"next": "/admin/", **codes}
            return self.client.post(
                "/auth/elevate-form", data=data, headers=self.headers
            )
        if door == "web_ajax":
            return self.client.post(
                "/auth/elevate-ajax", data=codes, headers=self.headers
            )
        return self.mcp_call(self.headers, "elevate_session", codes)

    def mcp_call(self, headers: dict, name: str, arguments: dict) -> object:
        return self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=headers,
        )

    def window_open(self) -> bool:
        jti = str(self.jwt.validate_token(self.token)["jti"])
        return self.esm.get_status(jti) is not None


@pytest.fixture
def doors(tmp_path: Path, store) -> Iterator[_ElevationDoors]:
    from code_indexer.server import app as real_app_module
    from code_indexer.server.auth import dependencies

    # Build the real app first so its startup wiring cannot overwrite the
    # auth globals patched below (it also initialises the web session
    # manager that the hybrid auth dependency consults).
    real_state = real_app_module.app.state
    # Building the app marks the process; the audit sink stays the store's.
    from code_indexer.server.services import audit_capture

    audit_capture.bind_audit_service(store.service, node_id=None)
    door = _ElevationDoors(tmp_path)
    previous_totp = mfa_routes.get_totp_service()
    mfa_routes.set_totp_service(door.totp)
    try:
        with (
            patch.object(dependencies, "jwt_manager", door.jwt),
            patch.object(dependencies, "user_manager", door.users),
            patch.object(dependencies, "oauth_manager", None),
            patch.object(dependencies, "mcp_credential_manager", door.credentials),
            patch.object(dependencies, "elevated_session_manager", door.esm),
            patch.object(dependencies, "server_config", None),
            patch.object(real_state, "group_manager", None, create=True),
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                f"{_DECORATOR_MOD}._is_elevation_enforcement_enabled", return_value=True
            ),
            patch(f"{_DECORATOR_MOD}.elevated_session_manager", door.esm),
            patch(f"{_REST_MOD}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_WEB_MOD}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_MCP_MOD}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_REST_MOD}.elevated_session_manager", door.esm),
            patch(f"{_WEB_MOD}.elevated_session_manager", door.esm),
            patch(f"{_MCP_MOD}.elevated_session_manager", door.esm),
            patch(f"{_REST_MOD}.login_rate_limiter", door.limiter),
            patch(f"{_WEB_MOD}.login_rate_limiter", door.limiter),
            patch(f"{_MCP_MOD}.login_rate_limiter", door.limiter),
        ):
            yield door
    finally:
        mfa_routes.set_totp_service(previous_totp)


def _expected_source(door: str) -> str:
    return "mcp" if door == "mcp" else "rest"


class TestElevationRowsOnEveryDoor:
    @pytest.mark.parametrize("door", _DOORS)
    def test_valid_totp_code_records_one_granted_row(self, doors, store, door):
        code = doors.valid_code()

        doors.elevate(door, totp_code=code)

        assert doors.window_open()
        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("elevation_granted", _USER, _USER, "success")
        ]
        assert rows[0].target_type == "user"
        assert rows[0].details == {"scope": "full", "used_recovery_code": False}
        assert rows[0].source == _expected_source(door)
        assert code not in store.all_raw_text()

    @pytest.mark.parametrize("door", _DOORS)
    def test_wrong_totp_code_records_one_failed_row(self, doors, store, door):
        code = doors.wrong_code()

        doors.elevate(door, totp_code=code)

        assert not doors.window_open()
        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.actor, r.target_id, r.outcome) for r in rows] == [
            ("elevation_failed", _USER, _USER, "failure")
        ]
        assert rows[0].details == {"scope": "full", "used_recovery_code": False}
        assert rows[0].source == _expected_source(door)

    @pytest.mark.parametrize("door", _DOORS)
    def test_recovery_code_records_the_repair_scope_never_the_code(
        self, doors, store, door
    ):
        recovery = doors.totp.generate_recovery_codes(_USER)[0]

        doors.elevate(door, recovery_code=recovery)

        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("elevation_granted", "success")
        ]
        assert rows[0].details == {"scope": "totp_repair", "used_recovery_code": True}
        assert recovery not in store.all_raw_text()

    @pytest.mark.parametrize("door", _DOORS)
    def test_wrong_recovery_code_records_one_failed_row(self, doors, store, door):
        doors.totp.generate_recovery_codes(_USER)

        doors.elevate(door, recovery_code="AAAA-BBBB-CCCC-DDDD")

        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("elevation_failed", "failure")
        ]
        assert rows[0].details == {"scope": "totp_repair", "used_recovery_code": True}
        assert "AAAA-BBBB-CCCC-DDDD" not in store.all_raw_text()

    @pytest.mark.parametrize("door", _DOORS)
    def test_refusal_while_locked_out_records_no_row(self, doors, store, door):
        for _ in range(5):
            doors.elevate("rest", totp_code=doors.wrong_code())
        assert len(store.rows("elevation_failed")) == 5

        doors.elevate(door, totp_code=doors.valid_code())

        assert not doors.window_open()
        assert len(store.rows(*_ELEVATION_TYPES)) == 5

    @pytest.mark.parametrize("door", _DOORS)
    def test_request_without_a_code_records_no_row(self, doors, store, door):
        doors.elevate(door)

        assert store.rows(*_ELEVATION_TYPES) == []


class _UnreadableWindows:
    """Session store double: accepts create(), never returns the window."""

    def __init__(self, raise_on_create: bool = False) -> None:
        self.raise_on_create = raise_on_create

    def create(self, **_kwargs) -> None:
        if self.raise_on_create:
            raise RuntimeError("session store unavailable")

    def get_status(self, _session_key):
        return None


class TestStepUpEdgeCases:
    @pytest.fixture
    def enrolled(self, tmp_path: Path):
        from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

        svc = TOTPService(db_path=str(tmp_path / "step-up-mfa.db"))
        secret = _enroll(svc, _USER)
        return svc, secret, LoginRateLimiter()

    def _step_up(self, enrolled, sessions, limiter=None, **codes):
        from code_indexer.server.auth.elevation_step_up import step_up

        svc, _secret, default_limiter = enrolled
        limiter = limiter if limiter is not None else default_limiter
        return step_up(
            codes.get("username", _USER),
            totp_code=codes.get("totp_code"),
            recovery_code=codes.get("recovery_code"),
            session_key=codes.get("session_key", "edge-session-key"),
            client_ip="192.0.2.10",
            totp_service=svc,
            sessions=sessions,
            limiter=limiter,
        )

    def test_unreadable_window_records_failure_and_keeps_failure_history(
        self, enrolled, store
    ):
        from code_indexer.server.auth.elevation_step_up import StepUpOutcome

        _svc, secret, limiter = enrolled
        limiter.record_failure(f"192.0.2.10:{_USER}")

        result = self._step_up(
            enrolled, _UnreadableWindows(), totp_code=_next_step_code(secret)
        )

        assert result.outcome is StepUpOutcome.WINDOW_NOT_CREATED
        assert result.session is None
        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("elevation_failed", "failure")
        ]
        assert len(limiter._failures[f"192.0.2.10:{_USER}"]) == 1

    def test_window_creation_error_records_failure_and_propagates(
        self, enrolled, store
    ):
        _svc, secret, _limiter = enrolled

        with pytest.raises(RuntimeError):
            self._step_up(
                enrolled,
                _UnreadableWindows(raise_on_create=True),
                totp_code=_next_step_code(secret),
            )

        assert [(r.action_type, r.outcome) for r in store.rows(*_ELEVATION_TYPES)] == [
            ("elevation_failed", "failure")
        ]

    def test_failure_history_reset_error_still_records_the_granted_window(
        self, enrolled, store, tmp_path
    ):
        from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

        class _ResetFails(LoginRateLimiter):
            def record_success(self, username: str) -> None:
                raise RuntimeError("limiter store unavailable")

        windows = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "reset-fails-elevation.db"),
        )
        _svc, secret, _limiter = enrolled

        with pytest.raises(RuntimeError):
            self._step_up(
                enrolled,
                windows,
                limiter=_ResetFails(),
                totp_code=_next_step_code(secret),
            )

        assert windows.get_status("edge-session-key") is not None
        rows = store.rows(*_ELEVATION_TYPES)
        assert [(r.action_type, r.outcome) for r in rows] == [
            ("elevation_granted", "success")
        ]
        assert rows[0].details == {"scope": "full", "used_recovery_code": False}

    @pytest.mark.parametrize(
        "codes",
        [
            {},
            {"totp_code": "123456", "session_key": "  "},
            {"totp_code": "123456", "username": " "},
        ],
        ids=["no-code", "blank-session-key", "blank-username"],
    )
    def test_precondition_violation_raises_and_records_nothing(
        self, enrolled, store, codes
    ):
        with pytest.raises(ValueError):
            self._step_up(enrolled, _UnreadableWindows(), **codes)
        assert store.rows(*_ELEVATION_TYPES) == []


# Each credentialed MCP request re-creates the credential's elevation window
# through the same code path, with no counter or threshold on it: the first
# request opens the window and every later one refreshes an existing window.
# Three requests cover both cases (each request costs a real credential
# verification, so a larger count only adds wall time).
_CREDENTIALED_REQUEST_COUNT = 3


def test_credentialed_mcp_requests_record_no_elevation_rows(doors, store):
    import base64

    credential = doors.credentials.generate_credential(_USER)
    basic = base64.b64encode(
        f"{credential['client_id']}:{credential['client_secret']}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}

    for _ in range(_CREDENTIALED_REQUEST_COUNT):
        resp = doors.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    # Each credentialed request opened (refreshed) its elevation window ...
    assert doors.esm.get_status(credential["client_id"]) is not None
    # ... and none of them is an audited elevation.
    assert store.rows(*_ELEVATION_TYPES) == []
    assert credential["client_secret"] not in store.all_raw_text()


# ---------------------------------------------------------------------------
# Wiring: the doors call only the audited MFA entry points
# ---------------------------------------------------------------------------

_SERVER_SRC = Path(__file__).resolve().parents[4] / "src" / "code_indexer" / "server"
_DOOR_DIRS = ("web", "routers", "mcp/handlers")
_UNAUDITED_PRIMITIVE_CALL = re.compile(r"\.(activate_mfa|generate_recovery_codes)\(")


def test_no_door_calls_the_unaudited_mfa_primitives():
    offenders = []
    for door_dir in _DOOR_DIRS:
        for path in sorted((_SERVER_SRC / door_dir).rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if _UNAUDITED_PRIMITIVE_CALL.search(line):
                    offenders.append(f"{path.relative_to(_SERVER_SRC)}:{lineno}")
    assert offenders == []


_ELEVATION_DOOR_MODULES = (
    "auth/elevation_routes.py",
    "web/elevation_web_routes.py",
    "mcp/handlers/admin/elevate_session.py",
)
_STEP_UP_ONLY_CALLS = (
    "verify_enabled_code(",
    "verify_recovery_code(",
    "check_and_record_failure(",
    "record_failure(",
    "record_success(",
    ".create(",
)


@pytest.mark.parametrize("module", _ELEVATION_DOOR_MODULES)
def test_elevation_doors_delegate_to_the_one_step_up(module):
    source = (_SERVER_SRC / module).read_text()
    assert "step_up(" in source
    assert [call for call in _STEP_UP_ONLY_CALLS if call in source] == []


def test_every_mfa_entry_point_is_called_from_the_web_door():
    source = (_SERVER_SRC / "web" / "mfa_routes.py").read_text()
    for entry_point in (
        "activate_mfa_and_issue_recovery_codes(",
        "regenerate_recovery_codes(",
        "disable_mfa(",
        "regenerate_secret_cross_user(",
    ):
        assert entry_point in source, entry_point
