"""Reading an SSH public key requires TOTP elevation on MCP and REST, for
parity with listing keys (``list_ssh_keys`` / ``GET /api/ssh-keys``).

Without a window, neither door returns the key, and neither distinguishes
"not found" from success, so a key name cannot be confirmed.

Front doors, on the real app (``create_app`` via ``isolated_app``, never
~/.cidx-server):

- MCP: ``POST /mcp`` ``tools/call manage_ssh_key`` ``action=show_public``
  with a Bearer JWT (the elevation key is the token's jti);
- REST: ``GET /api/ssh-keys/{name}/public`` with a Bearer JWT, and with the
  Web session cookie -- exactly what the SSH Keys page's own ``fetch()``
  sends -- whose elevation key is the session cookie value, so an elevated
  Web page can still show a key.

Elevation windows live in a real ``ElevatedSessionManager`` and TOTP
enrolment in a real ``TOTPService`` on per-test files; only the
elevation-enforcement switch is patched.  The key is a real key generated
by the real ``SSHKeyManager`` under the isolated home.
"""

from __future__ import annotations

import json
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Iterator

import pyotp
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.mcp.auth import elevation_decorator
from code_indexer.server.mcp.handlers import ssh_keys as mcp_ssh_keys
from code_indexer.server.routers import ssh_keys as rest_ssh_keys
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web import routes as web_routes
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-Show-Public-Passw0rd!"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("show-public-elevation-app")) as app:
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def accounts(client: TestClient) -> UserManager:
    users: UserManager = client.app.state.user_manager  # type: ignore[attr-defined]
    return users


@pytest.fixture
def esm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ElevatedSessionManager:
    """A real elevation-window store on this test's files, at every read point."""
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(dependencies, "elevated_session_manager", manager)
    monkeypatch.setattr(elevation_decorator, "elevated_session_manager", manager)
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", manager)
    return manager


@pytest.fixture
def totp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TOTPService:
    service = TOTPService(db_path=str(tmp_path / "mfa.db"))
    monkeypatch.setattr(mfa_routes, "_totp_service", service)
    return service


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    """A real managed SSH key, created where every front door reads from."""
    # Both door modules cache their manager process-wide; resolve afresh
    # (restored after the test) so they read this app's server home.
    monkeypatch.setattr(mcp_ssh_keys, "_ssh_key_manager", None)
    monkeypatch.setattr(rest_ssh_keys, "_ssh_key_manager", None)
    name = f"example-deploy-key-{uuid.uuid4().hex[:8]}"
    manager = web_routes._get_ssh_key_manager()
    manager.create_key(name, key_type="ed25519")
    return {"name": name, "public_key": manager.get_public_key(name).strip()}


def _admin(accounts: UserManager, totp: TOTPService) -> str:
    name = f"admin-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.ADMIN)
    secret = totp.generate_secret(name)
    assert totp.activate_mfa(name, pyotp.TOTP(secret).now())
    return name


def _member(accounts: UserManager) -> str:
    name = f"member-{uuid.uuid4().hex[:8]}"
    accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    return name


def _bearer(username: str, role: UserRole) -> Dict[str, str]:
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": role.value})
    jti = str(jwt.validate_token(token)["jti"])
    return {"token": token, "jti": jti}


def _session_cookie(username: str, role: UserRole) -> str:
    response = Response()
    web_auth.get_session_manager().create_session(response, username, role.value)
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[web_auth.SESSION_COOKIE_NAME].value


# ---------------------------------------------------------------------------
# MCP: POST /mcp tools/call manage_ssh_key action=show_public
# ---------------------------------------------------------------------------


def _mcp_show_public(client: TestClient, token: str, name: str) -> Dict[str, Any]:
    client.cookies.clear()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "manage_ssh_key",
                "arguments": {"action": "show_public", "name": name},
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if "error" in body:
        return {"jsonrpc_error": body["error"]}
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


@pytest.mark.parametrize("existing", [True, False])
def test_mcp_show_public_without_elevation_is_refused(
    client, accounts, esm, totp, key, existing
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    name = key["name"] if existing else f"absent-key-{uuid.uuid4().hex[:8]}"
    with enforcement(True):
        payload = _mcp_show_public(client, creds["token"], name)

    # Same refusal whether or not the key exists: no existence oracle.
    assert payload.get("error") == "elevation_required", payload
    assert key["public_key"] not in json.dumps(payload)


def test_mcp_show_public_with_elevation_returns_key(
    client, accounts, esm, totp, key
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    esm.create(
        session_key=creds["jti"], username=admin, elevated_from_ip=None, scope="full"
    )
    with enforcement(True):
        payload = _mcp_show_public(client, creds["token"], key["name"])

    assert payload.get("success") is True, payload
    assert payload["public_key"].strip() == key["public_key"]


def test_mcp_show_public_with_enforcement_off_returns_key(
    client, accounts, esm, totp, key
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    with enforcement(False):
        payload = _mcp_show_public(client, creds["token"], key["name"])

    assert payload.get("success") is True, payload
    assert payload["public_key"].strip() == key["public_key"]


def test_mcp_show_public_by_non_admin_is_refused(
    client, accounts, esm, totp, key
) -> None:
    member = _member(accounts)
    creds = _bearer(member, UserRole.NORMAL_USER)
    with enforcement(False):
        payload = _mcp_show_public(client, creds["token"], key["name"])

    assert payload.get("success") is not True, payload
    assert key["public_key"] not in json.dumps(payload)


# ---------------------------------------------------------------------------
# REST: GET /api/ssh-keys/{name}/public
# ---------------------------------------------------------------------------


def _rest_with_bearer(client: TestClient, token: str, name: str):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    return client.get(
        f"/api/ssh-keys/{name}/public", headers={"Authorization": f"Bearer {token}"}
    )


def _rest_with_session(client: TestClient, cookie: str, name: str):  # type: ignore[no-untyped-def]
    client.cookies.clear()
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    try:
        return client.get(f"/api/ssh-keys/{name}/public")
    finally:
        client.cookies.clear()


@pytest.mark.parametrize("existing", [True, False])
def test_rest_show_public_without_elevation_is_refused(
    client, accounts, esm, totp, key, existing
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    name = key["name"] if existing else f"absent-key-{uuid.uuid4().hex[:8]}"
    with enforcement(True):
        response = _rest_with_bearer(client, creds["token"], name)

    # Same refusal whether or not the key exists: no existence oracle.
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "elevation_required"
    assert key["public_key"] not in response.text


def test_rest_show_public_with_elevation_returns_key(
    client, accounts, esm, totp, key
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    esm.create(
        session_key=creds["jti"], username=admin, elevated_from_ip=None, scope="full"
    )
    with enforcement(True):
        response = _rest_with_bearer(client, creds["token"], key["name"])

    assert response.status_code == 200, response.text
    assert response.text.strip() == key["public_key"]


def test_rest_show_public_with_enforcement_off_returns_key(
    client, accounts, esm, totp, key
) -> None:
    admin = _admin(accounts, totp)
    creds = _bearer(admin, UserRole.ADMIN)
    with enforcement(False):
        response = _rest_with_bearer(client, creds["token"], key["name"])

    assert response.status_code == 200, response.text
    assert response.text.strip() == key["public_key"]


def test_rest_show_public_by_non_admin_is_refused(
    client, accounts, esm, totp, key
) -> None:
    member = _member(accounts)
    creds = _bearer(member, UserRole.NORMAL_USER)
    with enforcement(False):
        response = _rest_with_bearer(client, creds["token"], key["name"])

    assert response.status_code in (401, 403), response.text
    assert key["public_key"] not in response.text


# ---------------------------------------------------------------------------
# REST with the Web session cookie: the SSH Keys page's own fetch()
# ---------------------------------------------------------------------------


def test_web_page_fetch_with_elevated_session_returns_key(
    client, accounts, esm, totp, key
) -> None:
    """The elevated SSH Keys page's fetch() carries the session cookie; the
    REST gate accepts the Web session's own elevation window."""
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    esm.create(session_key=cookie, username=admin, elevated_from_ip=None, scope="full")
    with enforcement(True):
        response = _rest_with_session(client, cookie, key["name"])

    assert response.status_code == 200, response.text
    assert response.text.strip() == key["public_key"]


def test_web_page_fetch_without_elevation_is_refused(
    client, accounts, esm, totp, key
) -> None:
    admin = _admin(accounts, totp)
    cookie = _session_cookie(admin, UserRole.ADMIN)
    with enforcement(True):
        response = _rest_with_session(client, cookie, key["name"])

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "elevation_required"
    assert key["public_key"] not in response.text
