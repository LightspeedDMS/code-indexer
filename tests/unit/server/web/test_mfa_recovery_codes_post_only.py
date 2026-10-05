"""Recovery codes are generated only by an explicit POST from an elevated session.

Both recovery-code routes (admin ``/admin/mfa/recovery-codes`` and
self-service ``/user/mfa/recovery-codes``):

- GET never changes the stored recovery codes; it renders a page whose only
  action is a POST form carrying the Web UI CSRF token.
- POST requires a valid Web UI CSRF token, then (from an elevated session)
  replaces the codes and shows the new set once.
- The self-service POST requires the caller's own elevation window when
  elevation enforcement is on, and passes through when it is off.
- The admin POST regenerates codes only for an existing account.
- The MFA page offers regeneration as a POST form, never as a link.

Front door: the real routers on a FastAPI app, a real TOTPService and a real
UserManager on temp SQLite DBs, a real signed web session and a real
ElevatedSessionManager.  Only the elevation-enforcement switch is pinned.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, List
from unittest.mock import patch

import pyotp
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME, SessionManager

_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_ADMIN = "example-admin"
_OTHER = "example-other"
_USER = "example-user"
_MISSING = "example-missing"
_PASSWORD = "Example-Passw0rd!x"
_ADMIN_ROUTE = "/admin/mfa/recovery-codes"
_USER_ROUTE = "/user/mfa/recovery-codes"
_RECOVERY_CODE_RE = re.compile(r"[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}")
_CSRF_FIELD_RE = re.compile(r"name=[\"']csrf_token[\"'] value=[\"']([^\"']*)[\"']")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "mfa.db"


@pytest.fixture
def totp(db_path: Path) -> Iterator[TOTPService]:
    svc = TOTPService(db_path=str(db_path))
    mfa_routes.set_totp_service(svc)
    yield svc
    mfa_routes.set_totp_service(None)


@pytest.fixture
def accounts(tmp_path: Path, monkeypatch) -> UserManager:
    users_db = str(tmp_path / "users.db")
    DatabaseSchema(users_db).initialize_database()
    manager = UserManager(use_sqlite=True, db_path=users_db)
    manager.create_user(_ADMIN, _PASSWORD, UserRole.ADMIN)
    manager.create_user(_OTHER, _PASSWORD, UserRole.ADMIN)
    manager.create_user(_USER, _PASSWORD, UserRole.NORMAL_USER)
    monkeypatch.setattr(dependencies, "user_manager", manager)
    return manager


@pytest.fixture
def sessions(monkeypatch) -> SessionManager:
    sm = SessionManager("example-signing-key", SimpleNamespace(host="127.0.0.1"))
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


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mfa_routes.mfa_router)
    app.include_router(mfa_routes.user_mfa_router, prefix="/user/mfa")
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def web(totp, accounts, sessions, esm) -> Iterator[TestClient]:
    with patch(_ENFORCEMENT_PATH, return_value=True):
        yield _client()


@pytest.fixture
def web_enforcement_off(totp, accounts, sessions, esm) -> Iterator[TestClient]:
    with patch(_ENFORCEMENT_PATH, return_value=False):
        yield _client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: TestClient, sm: SessionManager, username: str, role: str) -> str:
    resp = Response()
    sm.create_session(resp, username=username, role=role)
    header = resp.headers["set-cookie"]
    prefix = f"{SESSION_COOKIE_NAME}="
    cookie = header[len(prefix) :].split(";", 1)[0]
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    return cookie


def _enroll_with_codes(svc: TOTPService, username: str) -> List[str]:
    secret = svc.generate_secret(username)
    codes = svc.activate_mfa_and_issue_recovery_codes(
        username, pyotp.TOTP(secret).now(), actor=username
    )
    assert codes, "enrollment must issue recovery codes"
    return list(codes)


def _elevate(esm: ElevatedSessionManager, cookie: str, username: str, scope: str):
    esm.create(
        session_key=cookie, username=username, elevated_from_ip=None, scope=scope
    )


def _stored_hashes(db_path: Path, username: str) -> List[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT code_hash FROM user_recovery_codes WHERE user_id = ? "
            "ORDER BY code_hash",
            (username,),
        ).fetchall()
    return [r[0] for r in rows]


def _page_token(client: TestClient, page: str) -> str:
    """GET *page* (which sets the signed CSRF cookie) and return its form token."""
    resp = client.get(page)
    assert resp.status_code == 200, resp.text
    match = _CSRF_FIELD_RE.search(resp.text)
    assert match is not None and match.group(1), "page must render a CSRF token"
    return match.group(1)


def _form(client: TestClient, page: str, **fields: str) -> Dict[str, str]:
    return {"csrf_token": _page_token(client, page), **fields}


def _has_post_form(html: str, action: str) -> bool:
    pattern = rf"<form method=[\"']POST[\"'] action=[\"']{re.escape(action)}[\"']"
    return re.search(pattern, html) is not None


# ---------------------------------------------------------------------------
# GET never changes the stored codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,scope,query",
    [(_ADMIN, "totp_repair", ""), (_OTHER, "full", f"?user={_OTHER}")],
    ids=["admin-self", "admin-cross-user"],
)
def test_admin_get_leaves_stored_codes_unchanged(
    web, totp, sessions, esm, db_path, target, scope, query
):
    _enroll_with_codes(totp, target)
    cookie = _login(web, sessions, _ADMIN, "admin")
    _elevate(esm, cookie, _ADMIN, scope)
    before = _stored_hashes(db_path, target)

    resp = web.get(f"{_ADMIN_ROUTE}{query}")

    assert resp.status_code == 200, resp.text
    assert _stored_hashes(db_path, target) == before
    assert _RECOVERY_CODE_RE.findall(resp.text) == []
    assert _has_post_form(resp.text, _ADMIN_ROUTE)
    if query:
        assert re.search(rf"name=[\"']user[\"'] value=[\"']{_OTHER}[\"']", resp.text)


def test_user_get_leaves_stored_codes_unchanged(web, totp, sessions, esm, db_path):
    _enroll_with_codes(totp, _USER)
    cookie = _login(web, sessions, _USER, "normal_user")
    _elevate(esm, cookie, _USER, "full")
    before = _stored_hashes(db_path, _USER)

    resp = web.get(_USER_ROUTE)

    assert resp.status_code == 200, resp.text
    assert _stored_hashes(db_path, _USER) == before
    assert _RECOVERY_CODE_RE.findall(resp.text) == []
    assert _has_post_form(resp.text, _USER_ROUTE)


@pytest.mark.parametrize("route", [_ADMIN_ROUTE, _USER_ROUTE])
def test_get_without_session_redirects_to_login(web, route):
    resp = web.get(route)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# POST from an elevated session with a valid token generates new codes once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target,scope,query",
    [(_ADMIN, "totp_repair", ""), (_OTHER, "full", f"?user={_OTHER}")],
    ids=["admin-self", "admin-cross-user"],
)
def test_admin_post_with_elevation_generates_new_codes(
    web, totp, sessions, esm, db_path, target, scope, query
):
    old_codes = _enroll_with_codes(totp, target)
    cookie = _login(web, sessions, _ADMIN, "admin")
    _elevate(esm, cookie, _ADMIN, scope)
    before = _stored_hashes(db_path, target)
    form = _form(web, f"{_ADMIN_ROUTE}{query}")
    if query:
        form["user"] = target

    resp = web.post(_ADMIN_ROUTE, data=form)

    assert resp.status_code == 200, resp.text
    new_codes = _RECOVERY_CODE_RE.findall(resp.text)
    assert len(new_codes) == 10
    assert _stored_hashes(db_path, target) != before
    assert totp.verify_recovery_code(target, new_codes[0]) is True
    assert totp.verify_recovery_code(target, old_codes[0]) is False


def test_admin_post_without_elevation_is_refused(web, totp, sessions, db_path):
    _enroll_with_codes(totp, _ADMIN)
    _login(web, sessions, _ADMIN, "admin")
    before = _stored_hashes(db_path, _ADMIN)

    resp = web.post(_ADMIN_ROUTE, data=_form(web, _ADMIN_ROUTE))

    assert resp.status_code == 403
    assert _stored_hashes(db_path, _ADMIN) == before


def test_admin_post_for_nonexistent_account_is_refused(
    web, totp, sessions, esm, db_path
):
    cookie = _login(web, sessions, _ADMIN, "admin")
    _elevate(esm, cookie, _ADMIN, "full")
    form = _form(web, f"{_ADMIN_ROUTE}?user={_MISSING}", user=_MISSING)

    resp = web.post(_ADMIN_ROUTE, data=form)

    assert resp.status_code == 404, resp.text
    assert _RECOVERY_CODE_RE.findall(resp.text) == []
    assert _stored_hashes(db_path, _MISSING) == []


def test_user_post_with_elevation_generates_new_codes(
    web, totp, sessions, esm, db_path
):
    old_codes = _enroll_with_codes(totp, _USER)
    cookie = _login(web, sessions, _USER, "normal_user")
    _elevate(esm, cookie, _USER, "totp_repair")
    before = _stored_hashes(db_path, _USER)

    resp = web.post(_USER_ROUTE, data=_form(web, _USER_ROUTE))

    assert resp.status_code == 200, resp.text
    new_codes = _RECOVERY_CODE_RE.findall(resp.text)
    assert len(new_codes) == 10
    assert _stored_hashes(db_path, _USER) != before
    assert totp.verify_recovery_code(_USER, new_codes[0]) is True
    assert totp.verify_recovery_code(_USER, old_codes[0]) is False
    assert "href='/user/api-keys'" in resp.text


def test_user_post_without_elevation_is_refused_when_enforced(
    web, totp, sessions, db_path
):
    _enroll_with_codes(totp, _USER)
    _login(web, sessions, _USER, "normal_user")
    before = _stored_hashes(db_path, _USER)

    resp = web.post(_USER_ROUTE, data=_form(web, _USER_ROUTE))

    assert resp.status_code == 303
    assert resp.headers["location"] == (
        "/admin/elevate?next=%2Fuser%2Fmfa%2Frecovery-codes"
    )
    assert _stored_hashes(db_path, _USER) == before


def test_user_post_with_another_users_window_is_refused(
    web, totp, sessions, esm, db_path
):
    _enroll_with_codes(totp, _USER)
    cookie = _login(web, sessions, _USER, "normal_user")
    _elevate(esm, cookie, _OTHER, "full")
    before = _stored_hashes(db_path, _USER)

    resp = web.post(_USER_ROUTE, data=_form(web, _USER_ROUTE))

    assert resp.status_code == 303
    assert _stored_hashes(db_path, _USER) == before


def test_user_post_passes_through_when_enforcement_off(
    web_enforcement_off, totp, sessions, db_path
):
    client = web_enforcement_off
    _enroll_with_codes(totp, _USER)
    _login(client, sessions, _USER, "normal_user")
    before = _stored_hashes(db_path, _USER)

    resp = client.post(_USER_ROUTE, data=_form(client, _USER_ROUTE))

    assert resp.status_code == 200, resp.text
    assert len(_RECOVERY_CODE_RE.findall(resp.text)) == 10
    assert _stored_hashes(db_path, _USER) != before


@pytest.mark.parametrize("route", [_ADMIN_ROUTE, _USER_ROUTE])
def test_post_without_session_redirects_to_login(web, route):
    resp = web.post(route)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# POST without a valid Web UI CSRF token changes nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route,username,role",
    [(_ADMIN_ROUTE, _ADMIN, "admin"), (_USER_ROUTE, _USER, "normal_user")],
    ids=["admin", "user"],
)
@pytest.mark.parametrize("token", [None, "example-wrong-token"], ids=["none", "wrong"])
def test_post_without_valid_csrf_token_is_refused(
    web, totp, sessions, esm, db_path, route, username, role, token
):
    _enroll_with_codes(totp, username)
    cookie = _login(web, sessions, username, role)
    _elevate(esm, cookie, username, "full")
    web.get(route)  # loads the page, which sets the signed CSRF cookie
    before = _stored_hashes(db_path, username)
    form = {} if token is None else {"csrf_token": token}

    resp = web.post(route, data=form)

    assert resp.status_code == 403, resp.text
    assert _RECOVERY_CODE_RE.findall(resp.text) == []
    assert _stored_hashes(db_path, username) == before


# ---------------------------------------------------------------------------
# The MFA page offers regeneration as a POST form, never a link
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup_path,route,username,role",
    [
        ("/admin/mfa/setup?mode=show", _ADMIN_ROUTE, _ADMIN, "admin"),
        ("/user/mfa/setup", _USER_ROUTE, _USER, "normal_user"),
    ],
    ids=["admin", "user"],
)
def test_mfa_page_renders_post_form_whose_token_is_accepted(
    web, totp, sessions, esm, db_path, setup_path, route, username, role
):
    _enroll_with_codes(totp, username)
    cookie = _login(web, sessions, username, role)
    _elevate(esm, cookie, username, "full")
    before = _stored_hashes(db_path, username)

    page = web.get(setup_path)

    assert page.status_code == 200, page.text
    assert _has_post_form(page.text, route)
    assert f"href='{route}" not in page.text
    assert "Generate New Recovery Codes" in page.text
    token = _CSRF_FIELD_RE.search(page.text)
    assert token is not None
    resp = web.post(route, data={"csrf_token": token.group(1)})
    assert resp.status_code == 200, resp.text
    assert len(_RECOVERY_CODE_RE.findall(resp.text)) == 10
    assert _stored_hashes(db_path, username) != before


@pytest.mark.parametrize(
    "verify_path,route,username,role",
    [
        ("/admin/mfa/verify", _ADMIN_ROUTE, _ADMIN, "admin"),
        ("/user/mfa/verify", _USER_ROUTE, _USER, "normal_user"),
    ],
    ids=["admin", "user"],
)
def test_qr_error_page_form_token_is_accepted(
    web, totp, sessions, esm, db_path, verify_path, route, username, role
):
    _enroll_with_codes(totp, username)
    cookie = _login(web, sessions, username, role)
    _elevate(esm, cookie, username, "full")
    before = _stored_hashes(db_path, username)

    page = web.post(verify_path, data={"totp_code": "000000", "test_only": "1"})

    assert _has_post_form(page.text, route)
    token = _CSRF_FIELD_RE.search(page.text)
    assert token is not None
    resp = web.post(route, data={"csrf_token": token.group(1)})
    assert resp.status_code == 200, resp.text
    assert len(_RECOVERY_CODE_RE.findall(resp.text)) == 10
    assert _stored_hashes(db_path, username) != before
