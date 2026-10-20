"""REST file write routes judge the canonical catalog path before writing.

A restricted caller writing to the catalog gets one fixed refusal for every
path they may not see, identical whether or not the file exists, before any
existence check; nothing on disk changes. Restricted callers may write only
catalog files they see unchanged: descriptions and dependency-map documents
get the same refusal, other visible paths keep today's behaviour. A lock-in
discovers every mutating route that takes a path, so a new one cannot skip a
gate. Real routes on a small FastAPI app, real catalog on disk, real
FileCRUDService and group store. Neutral names only.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Dict, Iterator, Set, Tuple
from unittest.mock import patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services import file_crud_service as crud_module
from code_indexer.server.services.file_crud_service import file_crud_service
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY, MEMORY_CANARY
from tests.unit.server.mcp.catalog_file_door_env import (
    CATALOG_ALIAS,
    CatalogFileDoorEnv,
    user,
)

REFUSAL = "This path is not available for writing in this repository."
BASE = f"/api/v1/repos/{CATALOG_ALIAS}/files"
HIDDEN = [
    (f"{CANARY}.md", f"{CANARY}-other.md"),
    (f"xray-patterns/{CANARY}/p.yaml", f"xray-patterns/{CANARY}/q.yaml"),
    (f"memories/{MEMORY_CANARY}.md", f"memories/{'d4' * 16}.md"),
]


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    return CatalogFileDoorEnv(tmp_path)


@pytest.fixture
def client(env: CatalogFileDoorEnv) -> Iterator[TestClient]:
    from code_indexer.server.routers.files import router

    marker = env.golden_root / ".write_mode" / "cidx-meta.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    app = FastAPI()
    for name, value in vars(env.state).items():
        setattr(app.state, name, value)
    app.include_router(router)
    from code_indexer.server.auth.user_manager import UserRole

    # The routes require repository:write: the caller holds it, the gate decides.
    app.dependency_overrides[get_current_user] = lambda: user(role=UserRole.POWER_USER)
    saved = dict(file_crud_service._global_write_exceptions)
    saved_golden = file_crud_service._golden_repos_dir
    file_crud_service.register_write_exception(CATALOG_ALIAS, env.fx.root)
    file_crud_service.set_golden_repos_dir(env.golden_root)
    try:
        with (
            patch.object(
                crud_module, "_get_activated_repo_manager", lambda: env.activations
            ),
            TestClient(app, raise_server_exceptions=False) as test_client,
        ):
            yield test_client
    finally:
        file_crud_service._global_write_exceptions = saved
        file_crud_service._golden_repos_dir = saved_golden


def _send(client: TestClient, verb: str, path: str) -> Any:
    if verb == "create":
        return client.post(BASE, json={"file_path": path, "content": "probe\n"})
    if verb == "edit":
        body = {"old_string": "a", "new_string": "b", "content_hash": "0" * 64}
        return client.patch(f"{BASE}/{path}", json=body)
    return client.delete(f"{BASE}/{path}")


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.mark.parametrize("verb", ["create", "edit", "delete"])
@pytest.mark.parametrize("existing,missing", HIDDEN)
def test_hidden_catalog_path_gets_one_refusal_whether_or_not_it_exists(
    client: TestClient,
    env: CatalogFileDoorEnv,
    verb: str,
    existing: str,
    missing: str,
) -> None:
    before = _snapshot(env.fx.root)

    for path in (existing, missing):
        response = _send(client, verb, path)
        assert response.status_code == 403, (path, response.text)
        assert response.json() == {"detail": REFUSAL}
    assert _snapshot(env.fx.root) == before


@pytest.mark.parametrize("verb", ["create", "edit", "delete"])
@pytest.mark.parametrize("path", [f"{ALPHA}.md", "dependency-map/payments.md"])
def test_projected_catalog_paths_get_the_fixed_refusal(
    client: TestClient, env: CatalogFileDoorEnv, verb: str, path: str
) -> None:
    before = _snapshot(env.fx.root)

    response = _send(client, verb, path)

    assert response.status_code == 403, (path, response.text)
    assert response.json() == {"detail": REFUSAL}
    assert _snapshot(env.fx.root) == before


@pytest.mark.parametrize(
    "verb,path",
    [
        ("create", ".git/config"),
        ("edit", ".git/config"),
        ("delete", ".git/config"),
        ("create", "../outside.md"),
        ("create", "/srv/example/abs.md"),
    ],
)
def test_invalid_spellings_get_the_catalog_refusal_first(
    client: TestClient, env: CatalogFileDoorEnv, verb: str, path: str
) -> None:
    before = _snapshot(env.fx.root)

    response = _send(client, verb, path)

    assert response.status_code == 403, (path, response.text)
    assert response.json() == {"detail": REFUSAL}
    assert _snapshot(env.fx.root) == before


def test_visible_catalog_path_is_still_writable(
    client: TestClient, env: CatalogFileDoorEnv
) -> None:
    response = _send(client, "create", "xray-patterns/__any__/n.yaml")

    assert response.status_code == 201, response.text
    assert (env.fx.root / "xray-patterns" / "__any__" / "n.yaml").is_file()


Route = Tuple[str, str]
PATH_FIELDS = {"file_path", "file_paths", "path", "paths"}
CATALOG_PATH_GATED: Set[Route] = {
    ("POST", "/api/v1/repos/{alias}/files"),
    ("PATCH", "/api/v1/repos/{alias}/files/{file_path:path}"),
    ("DELETE", "/api/v1/repos/{alias}/files/{file_path:path}"),
}
# The git router refuses the catalog's history (admin-only) before any path
# is used: test_meta_history_rest_denied covers every route under it.
HISTORY_POLICY_GATED: Set[Route] = {
    ("POST", "/api/v1/repos/{alias}/git/stage"),
    ("POST", "/api/v1/repos/{alias}/git/unstage"),
    ("POST", "/api/v1/repos/{alias}/git/checkout-file"),
}
EXEMPT: Dict[Route, str] = {
    ("POST", "/api/regex/search"): "a search; writes nothing",
}


def _body_fields(route: APIRoute) -> Set[str]:
    names: Set[str] = set()
    for param in route.dependant.body_params:
        model = getattr(param.field_info, "annotation", None)
        if model is not None:
            names |= set(getattr(model, "model_fields", None) or {})
    return names


def _all_api_routes() -> list:
    import code_indexer.server.routers as routers_pkg
    import code_indexer.server.routes as routes_pkg
    from code_indexer.server.routers.inline_repos import register_repo_routes
    from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes

    modules = [
        importlib.import_module(f"{pkg.__name__}.{info.name}")
        for pkg in (routers_pkg, routes_pkg)
        for info in pkgutil.iter_modules(pkg.__path__)
    ]
    modules.append(importlib.import_module("code_indexer.server.web.routes"))
    routes = [
        route
        for module in modules
        for value in vars(module).values()
        if isinstance(value, APIRouter)
        for route in value.routes
    ]
    app = FastAPI()
    managers: Dict[str, Any] = dict(
        activated_repo_manager=None,
        repository_listing_manager=None,
        background_job_manager=None,
    )
    register_repo_routes(app, golden_repo_manager=None, **managers)
    register_repos_v2_routes(app, **managers)
    return routes + list(app.routes)


def _mutating_routes_taking_a_path() -> Set[Route]:
    found: Set[Route] = set()
    for route in _all_api_routes():
        if not isinstance(route, APIRoute):
            continue
        params = {p.name for p in route.dependant.path_params}
        params |= {p.name for p in route.dependant.query_params}
        if not (params | _body_fields(route)) & PATH_FIELDS:
            continue
        for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            found.add((method, route.path))
    return found


def test_every_mutating_route_taking_a_path_is_classified() -> None:
    classified = CATALOG_PATH_GATED | HISTORY_POLICY_GATED | set(EXEMPT)
    assert _mutating_routes_taking_a_path() == classified
