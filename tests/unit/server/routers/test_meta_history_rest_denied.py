"""History of the internal catalog is admin-only on every REST git route.

The real git router on a small FastAPI app, a real git repository with a
few neutral commits for the catalog, a real ActivatedRepoManager holding
activations of the catalog under a custom alias, and the real group store.
The lock-in is
parameterised over the router's routes, so a route added later is covered
automatically. Neutral names only.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers import git as git_router_module
from code_indexer.server.routers import repo_access_http
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

CATALOG_CLONE = "team-notes"
GIT_REFUSAL = CatalogSurface.GIT_HISTORY.refusal
ROUTES: List[APIRoute] = [
    r for r in git_router_module.router.routes if isinstance(r, APIRoute)
]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Sample Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Sample Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )


def _make_catalog(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / f"{ALPHA}.md").write_text(f"# {ALPHA}\n")
    (path / f"{CANARY}.md").write_text(f"# {CANARY} notes\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "describe repositories")
    (path / f"{CANARY}.md").unlink()
    _git(path, "commit", "-q", "-am", "retire descriptions")


def _activate(data_dir: Path, source: Path, username: str) -> None:
    user_dir = data_dir / "activated-repos" / username
    user_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(source), str(user_dir / CATALOG_CLONE)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    metadata = {
        "user_alias": CATALOG_CLONE,
        "golden_repo_alias": "cidx-meta",
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
    }
    (user_dir / f"{CATALOG_CLONE}_metadata.json").write_text(json.dumps(metadata))


@pytest.fixture(scope="module")
def module_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("meta-history-rest")


@pytest.fixture(scope="module")
def fx(module_tmp: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(module_tmp)


@pytest.fixture(scope="module")
def server(module_tmp: Path, fx: CidxMetaFixture) -> Iterator[SimpleNamespace]:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    catalog = module_tmp / "sources" / "cidx-meta"
    _make_catalog(catalog)
    data_dir = module_tmp / "server" / "data"
    for username in (RESTRICTED_USER, ADMIN_USER):
        _activate(data_dir, catalog, username)
    arm = ActivatedRepoManager(data_dir=str(data_dir))
    current: Dict[str, User] = {}
    app = FastAPI()
    app.include_router(git_router_module.router)
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(git_router_module, "_get_activated_repo_manager", lambda: arm)
        mp.setattr(
            repo_access_http,
            "module_app_access_filtering_service",
            lambda: fx.access_service,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield SimpleNamespace(client=client, current=current, arm=arm, fx=fx)


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name, password_hash="hash", role=role, created_at=datetime.now()
    )


def _dummy(schema: Dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "array":
        return [f"{ALPHA}.md"]
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    if kind == "object":
        return {}
    return "sample"


def _body_for(route: APIRoute) -> Optional[Dict[str, Any]]:
    body_params = route.dependant.body_params
    if not body_params:
        return None
    model = body_params[0].field_info.annotation
    assert model is not None
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    return {name: _dummy(props.get(name, {})) for name in schema.get("required", [])}


def _query_for(route: APIRoute) -> Dict[str, Any]:
    return {
        p.name: f"{ALPHA}.md" if p.name == "path" else "HEAD"
        for p in route.dependant.query_params
        if p.field_info.is_required()
    }


def _call(server: SimpleNamespace, route: APIRoute, alias: str, user: User) -> Any:
    server.current["user"] = user
    method = sorted(route.methods)[0]
    path = route.path.replace("{alias}", alias)
    return server.client.request(
        method, path, params=_query_for(route), json=_body_for(route)
    )


def _route_id(route: APIRoute) -> str:
    return f"{sorted(route.methods)[0]} {route.path}"


def test_lock_in_covers_the_git_router() -> None:
    paths = {r.path.rsplit("/", 1)[-1] for r in ROUTES}
    assert {"log", "cat", "file-history", "blame", "diff", "status"} <= paths
    assert len(ROUTES) >= 18


@pytest.mark.parametrize("alias", ["cidx-meta-global", CATALOG_CLONE])
@pytest.mark.parametrize("route", ROUTES, ids=_route_id)
def test_every_git_route_refuses_catalog_history_for_restricted_caller(
    server: SimpleNamespace, route: APIRoute, alias: str
) -> None:
    # Role ADMIN passes every route's permission gate; outside the admins
    # group the caller is still restricted. The activation rule runs first
    # and refuses a catalog copy like an unknown alias; the history refusal
    # answers whatever reaches it.
    response = _call(server, route, alias, _user(RESTRICTED_USER, UserRole.ADMIN))
    unknown = f"Repository '{alias}' not found or not activated"
    assert (response.status_code, response.json()) in (
        (404, {"detail": unknown}),
        (403, {"detail": GIT_REFUSAL}),
    ), response.text
    assert CANARY not in response.text


def test_history_refusal_stands_on_its_own(server: SimpleNamespace) -> None:
    """An admin acting for a user passes the activation rule (admins pass
    it) but is restricted to that user's scope: the history refusal alone
    answers. Without the scope the same admin is unrestricted."""
    from fastapi import HTTPException

    from code_indexer.server.services.cidx_meta_access import acting_scope

    with acting_scope([ALPHA]):
        with pytest.raises(HTTPException) as raised:
            repo_access_http.enforce_activated_history_access(
                server.arm, ADMIN_USER, CATALOG_CLONE
            )
    assert raised.value.status_code == 403
    assert raised.value.detail == GIT_REFUSAL

    # Outside the acting scope: unrestricted, so nothing is raised.
    repo_access_http.enforce_activated_history_access(
        server.arm, ADMIN_USER, CATALOG_CLONE
    )


@pytest.mark.parametrize(
    "suffix, query, expected",
    [
        ("blame", {"path": f"{ALPHA}.md"}, "Sample Author"),
        ("cat", {"path": f"{CANARY}.md", "rev": "HEAD~1"}, f"{CANARY} notes"),
        ("file-history", {"path": f"{CANARY}.md"}, "retire descriptions"),
    ],
)
def test_admin_reads_catalog_history(
    server: SimpleNamespace, suffix: str, query: Dict[str, str], expected: str
) -> None:
    server.current["user"] = _user(ADMIN_USER, UserRole.ADMIN)
    response = server.client.get(
        f"/api/v1/repos/{CATALOG_CLONE}/git/{suffix}", params=query
    )
    assert response.status_code == 200, response.text
    assert expected in response.text


def _rest_temporal(server: SimpleNamespace, alias: str, user: User) -> Any:
    from code_indexer.server.models.query import SemanticQueryRequest
    from code_indexer.server.routers.inline_query import (
        _execute_temporal_via_live_dispatch_rest,
    )

    # No job manager and no golden_repos_dir: a search that passes the
    # catalog policy stops at a 503, before any job exists.
    app = SimpleNamespace(
        state=SimpleNamespace(access_filtering_service=server.fx.access_service)
    )
    request = SemanticQueryRequest.model_validate(
        {"query_text": "retired", "repository_alias": alias, "time_range_all": True}
    )
    # The dispatch reads only app.state, so a stand-in replaces a FastAPI app.
    return _execute_temporal_via_live_dispatch_rest(
        request, user, cast(Any, app), server.arm
    )


@pytest.mark.parametrize("alias", ["cidx-meta-global", CATALOG_CLONE])
def test_rest_temporal_search_on_catalog_is_rejected_before_any_job(
    server: SimpleNamespace, alias: str
) -> None:
    from code_indexer.server.query.semantic_query_manager import SearchRequestError

    with pytest.raises(SearchRequestError) as raised:
        _rest_temporal(server, alias, _user(RESTRICTED_USER, UserRole.ADMIN))
    assert str(raised.value) == CatalogSurface.TEMPORAL.refusal


@pytest.mark.parametrize(
    "alias, username",
    [
        ("cidx-meta-global", ADMIN_USER),
        (CATALOG_CLONE, ADMIN_USER),
        (f"{ALPHA}-global", RESTRICTED_USER),
    ],
)
def test_rest_temporal_search_passes_the_policy_otherwise(
    server: SimpleNamespace, alias: str, username: str
) -> None:
    response = _rest_temporal(server, alias, _user(username, UserRole.ADMIN))
    assert response.status_code == 503, bytes(response.body)


XRAY_REFUSAL = CatalogSurface.XRAY.refusal


@pytest.fixture(scope="module")
def xray_server(fx: CidxMetaFixture) -> Iterator[SimpleNamespace]:
    from code_indexer.server.mcp.handlers import xray_batch
    from code_indexer.server.routes import xray_routes

    current: Dict[str, User] = {}
    app = FastAPI()
    app.include_router(xray_routes.router)
    app.state.access_filtering_service = fx.access_service
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            xray_batch, "_get_access_filtering_service", lambda: fx.access_service
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            yield SimpleNamespace(client=client, current=current)


def _xray_search(xray_server: SimpleNamespace, alias: str, user: User) -> Any:
    xray_server.current["user"] = user
    body = {
        "repository_alias": alias,
        "driver_regex": "describe",
        "evaluator_code": "true",
        "search_target": "content",
    }
    return xray_server.client.post("/api/xray/search", json=body)


@pytest.mark.parametrize("alias", ["cidx-meta-global", "cidx-meta"])
def test_rest_xray_search_on_catalog_is_refused(
    xray_server: SimpleNamespace, alias: str
) -> None:
    response = _xray_search(
        xray_server, alias, _user(RESTRICTED_USER, UserRole.NORMAL_USER)
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {
        "error_code": "xray_not_available",
        "detail": XRAY_REFUSAL,
    }


def test_rest_xray_search_on_catalog_is_not_refused_for_admin(
    xray_server: SimpleNamespace,
) -> None:
    response = _xray_search(
        xray_server, "cidx-meta-global", _user(ADMIN_USER, UserRole.ADMIN)
    )
    assert XRAY_REFUSAL not in response.text


def test_rest_xray_batch_refuses_catalog_aliases_per_alias(
    xray_server: SimpleNamespace,
) -> None:
    xray_server.current["user"] = _user(RESTRICTED_USER, UserRole.NORMAL_USER)
    body = {
        "repository_alias": ["cidx-meta-global", "cidx-meta"],
        "scans": [{"driver_regex": "describe"}],
    }
    response = xray_server.client.post("/api/xray/search/batch", json=body)
    errors = response.json()["detail"]["errors"]
    assert {e["repository_alias"]: e["message"] for e in errors} == {
        "cidx-meta-global": XRAY_REFUSAL,
        "cidx-meta": XRAY_REFUSAL,
    }
    assert "job_id" not in response.text
