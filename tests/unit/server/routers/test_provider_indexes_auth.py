"""Tests verifying provider_indexes router requires admin authentication.

Story #490: All provider index management endpoints must require admin auth.

The app carries the REAL auth components on isolated files
(``self_service_elevation_harness``), so an unauthenticated or non-admin
call is refused by the router's own dependency -- not by an uninitialized
component -- and an admin call gets past authentication (the discriminating
control: removing the dependency turns the non-admin cases red).
"""

from typing import Any, Dict, Iterator, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.routers.provider_indexes import router
from tests.unit.server.self_service_elevation_harness import build_stack, enforcement

_ENDPOINTS: Tuple[Tuple[str, str, Dict[str, Any]], ...] = (
    ("GET", "/api/admin/provider-indexes/providers", {}),
    ("GET", "/api/admin/provider-indexes/status?alias=test", {}),
    (
        "POST",
        "/api/admin/provider-indexes/add",
        {"json": {"provider": "voyage-ai", "alias": "test"}},
    ),
    (
        "POST",
        "/api/admin/provider-indexes/recreate",
        {"json": {"provider": "voyage-ai", "alias": "test"}},
    ),
    (
        "POST",
        "/api/admin/provider-indexes/remove",
        {"json": {"provider": "voyage-ai", "alias": "test"}},
    ),
    (
        "POST",
        "/api/admin/provider-indexes/bulk-add",
        {"json": {"provider": "voyage-ai"}},
    ),
)
_IDS = ["providers", "status", "add", "recreate", "remove", "bulk_add"]


class _Env:
    def __init__(self, client: TestClient, admin_bearer: str, user_bearer: str):
        self.client = client
        self.admin_bearer = admin_bearer
        self.user_bearer = user_bearer

    def call(self, method: str, path: str, bearer: str = "", **kwargs: Any):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        return self.client.request(method, path, headers=headers, **kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[_Env]:
    from code_indexer.server.services import config_service as config_service_module

    stack = build_stack(tmp_path, monkeypatch)
    admin = stack.create_user("provider-auth-admin", UserRole.ADMIN)
    user = stack.create_user("provider-auth-user", UserRole.NORMAL_USER)
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    config_svc = config_service_module.ConfigService(server_dir_path=str(server_dir))
    config_svc.load_config()
    monkeypatch.setattr(config_service_module, "_config_service", config_svc)

    app = FastAPI()
    app.include_router(router)
    with enforcement(False):
        yield _Env(
            TestClient(app, raise_server_exceptions=False),
            stack.bearer(admin)[0],
            stack.bearer(user)[0],
        )


class TestProviderIndexesAuthRequired:
    """All provider index endpoints must require admin authentication."""

    @pytest.mark.parametrize("method,path,kwargs", _ENDPOINTS, ids=_IDS)
    def test_unauthenticated_call_is_refused(self, env, method, path, kwargs):
        response = env.call(method, path, **kwargs)
        assert response.status_code in (401, 403), response.text

    @pytest.mark.parametrize("method,path,kwargs", _ENDPOINTS, ids=_IDS)
    def test_non_admin_call_is_refused(self, env, method, path, kwargs):
        response = env.call(method, path, bearer=env.user_bearer, **kwargs)
        assert response.status_code == 403, response.text

    def test_admin_call_gets_past_authentication(self, env):
        response = env.call(
            "GET", "/api/admin/provider-indexes/providers", bearer=env.admin_bearer
        )
        assert response.status_code == 200, response.text
        assert "providers" in response.json()
