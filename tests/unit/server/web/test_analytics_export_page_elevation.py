"""The Web Query Analytics Export page requires TOTP elevation.

The page reads and exports every user's search history through the
elevation-gated ``/api/admin/search-events*`` routes, so the page itself
requires the caller's own elevation window, like the logs page: with
enforcement on and no window, the admin is sent to the elevation page, which
returns here afterwards.  Enforcement off serves the page as before.

Front door: the real app (``create_app`` via ``isolated_app``, never
~/.cidx-server) with a Web session cookie (the elevation key is the session
cookie value).  Elevation windows and TOTP enrolment are real stores on
per-test files; only the elevation-enforcement switch is patched.
"""

from __future__ import annotations

import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator

import pyotp
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-Export-Passw0rd!"
PAGE = "/admin/analytics-export"
# Rendered only on the page itself.
PAGE_MARKER = 'id="export-filter-form"'


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with isolated_app(tmp_path_factory.mktemp("analytics-export-page")) as app:
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


@pytest.fixture
def esm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ElevatedSessionManager:
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(dependencies, "elevated_session_manager", manager)
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", manager)
    return manager


@pytest.fixture
def totp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TOTPService:
    service = TOTPService(db_path=str(tmp_path / "mfa.db"))
    monkeypatch.setattr(mfa_routes, "_totp_service", service)
    return service


def _admin(accounts: UserManager, totp: TOTPService) -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    return name


def _session_cookie(username: str) -> str:
    response = Response()
    web_auth.get_session_manager().create_session(
        response, username, UserRole.ADMIN.value
    )
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


def _get(client: TestClient, cookie: str) -> Any:
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    try:
        return client.get(PAGE)
    finally:
        client.cookies.clear()


def test_page_without_elevation_sends_admin_to_elevate(client, accounts, esm, totp):
    cookie = _session_cookie(_admin(accounts, totp))
    with enforcement(True):
        response = _get(client, cookie)

    assert response.status_code == 303, response.text[:300]
    assert (
        response.headers["location"]
        == "/admin/elevate?next=%2Fadmin%2Fanalytics-export"
    )
    assert PAGE_MARKER not in response.text


def test_page_with_another_admins_window_sends_admin_to_elevate(
    client, accounts, esm, totp
):
    admin, other = _admin(accounts, totp), _admin(accounts, totp)
    cookie = _session_cookie(admin)
    esm.create(session_key=cookie, username=other, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie)

    assert response.status_code == 303, response.text[:300]
    assert PAGE_MARKER not in response.text


def test_page_with_own_window_is_served(client, accounts, esm, totp):
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie)

    assert response.status_code == 200, response.text[:300]
    assert PAGE_MARKER in response.text


def test_page_with_enforcement_off_is_served(client, accounts, esm, totp):
    cookie = _session_cookie(_admin(accounts, totp))
    with enforcement(False):
        response = _get(client, cookie)

    assert response.status_code == 200, response.text[:300]
    assert PAGE_MARKER in response.text
