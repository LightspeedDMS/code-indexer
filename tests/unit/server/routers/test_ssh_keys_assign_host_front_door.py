"""
REST front-door proof for the assign-host 400.

tests/unit/server/routers/test_ssh_keys_hostname_400.py already
proves the router FUNCTION converts InvalidHostnameError to HTTPException(400)
via a direct call with a stub manager. This file instead drives the real
FastAPI app/router/dependency stack through a REAL HTTP request via
TestClient -- proving the 400 actually reaches the wire, through routing,
request validation, and the admin-auth + elevation dependency chain, not
just the bare Python function.

Modeled on the `_bypass_elevation` + `app.dependency_overrides` pattern in
tests/unit/server/routers/test_groups_access_control.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers import ssh_keys
from code_indexer.server.routers.ssh_keys import router as ssh_keys_router
from code_indexer.server.services.ssh_input_validation import InvalidHostnameError

_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app: FastAPI, rtr) -> None:
    """Override all require_elevation deps so the front-door call reaches
    the route handler without a live TOTP window -- this test proves the
    hostname-validation wiring, not the elevation gate (already covered by
    tests/unit/server/routers/test_ssh_keys_elevation.py)."""
    for route in rtr.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


class _StubManagerInvalidHostname:
    """Real SSHKeyManager.assign_key_to_host()'s actual rejection behavior for
    a hostname that breaks its config line -- stubbed here only to avoid
    touching a real ssh_dir
    through the HTTP layer (the manager-level behavior itself is proven
    end-to-end with a REAL manager in
    tests/unit/server/services/test_ssh_key_manager_hostname_validation.py)."""

    def assign_key_to_host(self, key_name: str, hostname: str, force: bool = False):
        raise InvalidHostnameError(f"Invalid hostname: {hostname!r}")


class _StubManagerSuccess:
    def assign_key_to_host(self, key_name: str, hostname: str, force: bool = False):
        from code_indexer.server.services.ssh_key_manager import KeyMetadata

        return KeyMetadata(
            name=key_name,
            fingerprint="SHA256:fakefingerprint000000000000000000000000000",
            key_type="ed25519",
            private_path=f"/tmp/{key_name}",
            public_path=f"/tmp/{key_name}.pub",
            hosts=[hostname],
        )


@pytest.fixture
def admin_user() -> User:
    return User(
        username="admin",
        role=UserRole.ADMIN,
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


def _client(admin_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(ssh_keys_router)
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
    _bypass_elevation(app, ssh_keys_router)
    return TestClient(app, raise_server_exceptions=False)


def test_assign_host_rest_front_door_returns_400_on_invalid_hostname(
    admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HTTP POST through the real router -- not a direct function call.

    Discriminating RED: without the router's InvalidHostnameError handling,
    this HTTP call would 500, not 400.
    """
    monkeypatch.setattr(
        ssh_keys, "get_ssh_key_manager", lambda: _StubManagerInvalidHostname()
    )
    client = _client(admin_user)

    response = client.post(
        "/api/ssh-keys/deploy-key_1.v2/hosts",
        json={"hostname": "example.com\nHost other"},
    )

    assert response.status_code == 400
    assert "hostname" in response.json()["detail"].lower()


def test_assign_host_rest_front_door_returns_200_on_legitimate_hostname(
    admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HTTP POST through the real router with a legitimate hostname
    must still succeed end to end."""
    monkeypatch.setattr(ssh_keys, "get_ssh_key_manager", lambda: _StubManagerSuccess())
    client = _client(admin_user)

    response = client.post(
        "/api/ssh-keys/deploy-key_1.v2/hosts",
        json={"hostname": "github.com"},
    )

    assert response.status_code == 200
    assert response.json()["hosts"] == ["github.com"]
