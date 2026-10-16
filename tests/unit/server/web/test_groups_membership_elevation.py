"""Web reads of group membership require TOTP elevation.

Which user is in which group is user data, gated like REST
``GET /api/v1/groups/{group_id}`` and MCP ``get_group``.  As on the users
page, the groups page itself renders no membership data: its User
Assignments tab loads ``/admin/partials/groups-users-list``, which requires
the caller's own elevation window (403 ``elevation_required`` without one,
which the shared elevation interceptor turns into the TOTP modal).
Enforcement off serves the partial as before.

Front door: the real app (``create_app`` via ``isolated_app``, never
~/.cidx-server) with a Web session cookie (the elevation key is the session
cookie value).  Users, group assignments, elevation windows and TOTP
enrolment are real stores on per-test files; only the elevation-enforcement
switch is patched.
"""

from __future__ import annotations

import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator, Tuple

import pyotp
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-Groups-Passw0rd!"
PAGE = "/admin/groups"
PARTIAL = "/admin/partials/groups-users-list"
GROUP_NAME = "example-membership-team"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with isolated_app(tmp_path_factory.mktemp("groups-membership")) as app:
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


@pytest.fixture
def member(
    client: TestClient,
    accounts: UserManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A real user assigned to a real group, in the store the app reads."""
    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    groups = GroupAccessManager(tmp_path / "groups.db")
    group = groups.create_group(GROUP_NAME, "Example membership team")
    groups.assign_user_to_group(name, group.id, "example-admin")
    state = client.app.state  # type: ignore[attr-defined]
    monkeypatch.setattr(state, "group_manager", groups, raising=False)
    return name


def _admin(accounts: UserManager, totp: TOTPService) -> Tuple[str, str]:
    """(username, session cookie) of a TOTP-enrolled admin."""
    name = f"admin-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    response = Response()
    web_auth.get_session_manager().create_session(response, name, UserRole.ADMIN.value)
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return name, cookie[web_auth.SESSION_COOKIE_NAME].value


def _get(client: TestClient, cookie: str, url: str) -> Any:
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    try:
        return client.get(url)
    finally:
        client.cookies.clear()


def test_page_renders_no_membership_and_loads_the_partial(
    client, accounts, esm, totp, member
) -> None:
    admin, cookie = _admin(accounts, totp)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie, PAGE)

    assert response.status_code == 200, response.text[:300]
    assert member not in response.text
    assert f'hx-get="{PARTIAL}"' in response.text


def test_partial_without_window_is_refused(client, accounts, esm, totp, member):
    _admin_name, cookie = _admin(accounts, totp)
    with enforcement(True):
        response = _get(client, cookie, PARTIAL)

    assert response.status_code == 403, response.text[:300]
    assert response.json()["detail"]["error"] == "elevation_required"
    assert member not in response.text


def test_partial_with_another_admins_window_is_refused(
    client, accounts, esm, totp, member
) -> None:
    _admin_name, cookie = _admin(accounts, totp)
    other, _other_cookie = _admin(accounts, totp)
    esm.create(session_key=cookie, username=other, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie, PARTIAL)

    assert response.status_code == 403, response.text[:300]
    assert member not in response.text


def test_partial_with_own_window_shows_membership(
    client, accounts, esm, totp, member
) -> None:
    admin, cookie = _admin(accounts, totp)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _get(client, cookie, PARTIAL)

    assert response.status_code == 200, response.text[:300]
    assert member in response.text
    assert GROUP_NAME in response.text


def test_partial_with_enforcement_off_shows_membership(
    client, accounts, esm, totp, member
) -> None:
    _admin_name, cookie = _admin(accounts, totp)
    with enforcement(False):
        response = _get(client, cookie, PARTIAL)

    assert response.status_code == 200, response.text[:300]
    assert member in response.text
