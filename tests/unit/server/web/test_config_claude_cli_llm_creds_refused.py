"""Front door: the generic Config page section save cannot change the LLM
credentials provider settings.

Only ``/api/llm-creds/save-config`` may change ``claude_auth_mode``,
``llm_creds_provider_url`` and ``llm_creds_provider_api_key``: it reuses the
stored provider key only with the provider URL it was saved with.  Without
this, ``POST /admin/config/claude_cli`` could move the URL while keeping the
stored key, and the next Test Connection, save or restart would send that
key to the new URL.

The real app (``create_app`` via ``isolated_app``, never ~/.cidx-server), a
real admin Web login (CSRF from the page) for the section save, and an admin
Bearer JWT for ``/api/llm-creds/test-connection``.  Configuration lives where
a server worker keeps it: a real ``ConfigService`` with its runtime row in
the app's ``cidx_server.db``.  The only stand-in is a recorder in place of
the LLM-creds HTTP client, so no request leaves the host.  Sample values are
neutral and never real credentials.
"""

from __future__ import annotations

import re
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.routers import llm_creds
from code_indexer.server.services.config_service import (
    ConfigService,
    get_config_service,
    set_config_service,
)
from code_indexer.server.utils.config_manager import ServerConfig
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.self_service_elevation_harness import enforcement

PASSWORD = "Example-LlmCreds-Refused-Passw0rd!"
STORED_KEY = "example-stored-provider-key-0000Ab12"
NEW_KEY = "example-new-provider-key-1111Cd34"
OLD_URL = "https://creds-old.example.com"
NEW_URL = "https://creds-new.example.net"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


class _Session:
    def __init__(self, app: FastAPI, web: TestClient, api: TestClient, jwt: str):
        self.app, self.web, self.api = app, web, api
        self.headers = {"Authorization": f"Bearer {jwt}"}


@pytest.fixture(scope="module")
def admin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("claude-cli-llm-creds-refused")
    with isolated_app(root) as app:
        name = f"admin-{uuid.uuid4().hex[:8]}"
        app.state.user_manager.create_user(name, PASSWORD, UserRole.ADMIN)
        api = TestClient(app, follow_redirects=False)
        login = api.post("/auth/login", json={"username": name, "password": PASSWORD})
        assert login.status_code == HTTPStatus.OK, login.text
        web = TestClient(app, follow_redirects=False)
        csrf = _CSRF.search(web.get("/login").text)
        assert csrf, "csrf token not found on the login page"
        web_login = web.post(
            "/login",
            data={"username": name, "password": PASSWORD, "csrf_token": csrf.group(1)},
        )
        assert web_login.status_code == HTTPStatus.SEE_OTHER, web_login.status_code
        yield _Session(app, web, api, login.json()["access_token"])


def _worker_config_service(app: FastAPI) -> ConfigService:
    """A server worker's ConfigService over the app's runtime row."""
    db_path = Path(app.state.user_manager._sqlite_backend._conn_manager.db_path)
    service = ConfigService(server_dir_path=str(db_path.parent.parent))
    service.initialize_runtime_db(str(db_path))
    return service


@pytest.fixture(autouse=True)
def _per_test_config(admin: _Session) -> None:
    set_config_service(_worker_config_service(admin.app))


class _ClientRecorder:
    """Stands in for LlmCredsClient: records every construction."""

    built: List[Tuple[str, str]] = []

    def __init__(self, provider_url: str, api_key: str) -> None:
        type(self).built.append((provider_url, api_key))

    def health(self) -> bool:
        return True


def _seed_llm_creds() -> None:
    def _mutate(c: ServerConfig) -> None:
        integration = c.claude_integration_config
        assert integration is not None
        integration.claude_auth_mode = "subscription"
        integration.llm_creds_provider_url = OLD_URL
        integration.llm_creds_provider_api_key = STORED_KEY

    get_config_service().apply_system_change(_mutate)


def _committed_llm_creds(admin: _Session) -> Tuple[str, str, str]:
    """The LLM-creds settings a fresh worker reads from the committed row."""
    integration = _worker_config_service(admin.app).get_config()
    claude = integration.claude_integration_config
    assert claude is not None
    return (
        claude.claude_auth_mode,
        claude.llm_creds_provider_url,
        claude.llm_creds_provider_api_key,
    )


def _post_claude_cli(admin: _Session, field: str, value: str):
    page = admin.web.get("/admin/config")
    assert page.status_code == HTTPStatus.OK, page.status_code
    csrf = _CSRF.search(page.text)
    assert csrf, "csrf token not found on the config page"
    with enforcement(False):
        return admin.web.post(
            "/admin/config/claude_cli",
            data={"csrf_token": csrf.group(1), field: value},
        )


@pytest.mark.timeout(180)
@pytest.mark.parametrize(
    "field,value",
    [
        ("llm_creds_provider_url", NEW_URL),
        ("llm_creds_provider_api_key", NEW_KEY),
        ("claude_auth_mode", "api_key"),
    ],
)
def test_generic_claude_cli_save_cannot_change_llm_creds_settings(
    admin: _Session, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    _seed_llm_creds()
    _ClientRecorder.built = []
    monkeypatch.setattr(llm_creds, "LlmCredsClient", _ClientRecorder)

    response = _post_claude_cli(admin, field, value)

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.status_code
    assert "/api/llm-creds/save-config" in response.text
    assert STORED_KEY not in response.text
    assert _committed_llm_creds(admin) == ("subscription", OLD_URL, STORED_KEY)

    # A blank-key Test Connection to the new URL must not reuse the stored key.
    probe = admin.api.post(
        "/api/llm-creds/test-connection",
        json={"provider_url": NEW_URL, "api_key": ""},
        headers=admin.headers,
    )
    assert probe.status_code == HTTPStatus.OK, probe.text
    assert _ClientRecorder.built == [], "the stored key was sent to a new URL"
