"""The Web dependency map shows each caller only their view of the catalog.

The real dependency-map router on a FastAPI app whose state carries the
real fixture services (group store, access filtering, registry); sessions
are real signed Web sessions. Admin means admins-group membership: a
session with role ``admin`` outside the group is restricted, and an admins
member is unrestricted whatever the session role.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Tuple

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from code_indexer.server.web import auth as web_auth
from code_indexer.server.web.dependency_map_routes import dependency_map_router
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    AUDIT_DOMAIN,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)

Web = Tuple[FastAPI, web_auth.SessionManager]


def _build_fixture(tmp_path: Path) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path)
    add_depmap_extension(fixture)
    return fixture


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    return _build_fixture(tmp_path_factory.mktemp("depmap-web"))


def _web(fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch) -> Web:
    # Imported first: importing the server app initialises the global
    # session manager, which the test manager then replaces.
    from code_indexer.server import app as server_app_module

    server_app = server_app_module.app  # constructed lazily on first access
    manager = web_auth.SessionManager("test-secret", SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr(web_auth, "_session_manager", manager)
    app = FastAPI()
    app.include_router(dependency_map_router, prefix="/admin")
    dep_map_service = SimpleNamespace(cidx_meta_read_path=fx.root)
    app.state.dependency_map_service = dep_map_service
    app.state.access_filtering_service = fx.access_service
    app.state.backend_registry = SimpleNamespace(global_repos=fx.registry)
    # In production request.app IS the server app; mirror its state there.
    monkeypatch.setattr(
        server_app.state,
        "dependency_map_service",
        dep_map_service,
        raising=False,
    )
    return app, manager


@pytest.fixture
def web(fx: CidxMetaFixture) -> Iterator[Web]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _web(fx, monkeypatch)


def _client(web: Web, username: str, role: str) -> TestClient:
    app, manager = web
    response = Response()
    manager.create_session(response, username, role)
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    client = TestClient(app)
    client.cookies.set(web_auth.SESSION_COOKIE_NAME, cookie)
    return client


def _restricted(web: Web) -> TestClient:
    # Role admin, but not a member of the admins group: restricted.
    return _client(web, RESTRICTED_USER, "admin")


def _admin(web: Web) -> TestClient:
    # Member of the admins group, whatever the session role.
    return _client(web, ADMIN_USER, "normal_user")


def _assert_hidden_absent(text: str) -> None:
    assert CANARY not in text, text
    assert GAMMA not in text, text


def test_domain_explorer_lists_only_visible_domains(web: Web) -> None:
    response = _restricted(web).get("/admin/partials/depmap-domain-explorer")
    assert response.status_code == 200
    _assert_hidden_absent(response.text)
    assert "payments" in response.text and AUDIT_DOMAIN in response.text
    assert "inventory" not in response.text


def test_hidden_domain_detail_matches_unknown_domain(web: Web) -> None:
    client = _restricted(web)
    hidden = client.get(f"/admin/partials/depmap-domain-detail/{CANARY}")
    inventory = client.get("/admin/partials/depmap-domain-detail/inventory")
    missing = client.get("/admin/partials/depmap-domain-detail/no-such-domain")
    assert hidden.status_code == missing.status_code
    assert hidden.text.replace(CANARY, "<name>") == missing.text.replace(
        "no-such-domain", "<name>"
    )
    assert inventory.text.replace("inventory", "<name>") == missing.text.replace(
        "no-such-domain", "<name>"
    )


def test_visible_domain_detail_renders_projected_documentation(web: Web) -> None:
    response = _restricted(web).get("/admin/partials/depmap-domain-detail/payments")
    assert response.status_code == 200
    _assert_hidden_absent(response.text)
    assert ALPHA in response.text
    assert "<table" in response.text
    assert "[restricted repository]" in response.text


def test_graph_has_no_hidden_node_and_no_edge_of_hidden_rows(web: Web) -> None:
    response = _restricted(web).get("/admin/dependency-map/graph-data")
    assert response.status_code == 200
    data = response.json()
    _assert_hidden_absent(json.dumps(data))
    # reports is hidden: its frontmatter does not parse.
    assert sorted(n["id"] for n in data["nodes"]) == sorted(
        ["payments", "ledger", AUDIT_DOMAIN]
    )
    assert [(e["source"], e["target"]) for e in data["edges"]] == [
        ("payments", "ledger")
    ]


def test_admins_member_sees_the_full_map(web: Web) -> None:
    client = _admin(web)
    explorer = client.get("/admin/partials/depmap-domain-explorer")
    assert CANARY in explorer.text and "inventory" in explorer.text
    detail = client.get(f"/admin/partials/depmap-domain-detail/{CANARY}")
    assert f"{CANARY} stands alone" in detail.text
    graph = client.get("/admin/dependency-map/graph-data").json()
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert (AUDIT_DOMAIN, "payments") in edges
    assert ("payments", CANARY) in edges


def test_grants_are_read_fresh_from_the_access_service(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    with pytest.MonkeyPatch.context() as monkeypatch:
        web = _web(fixture, monkeypatch)
        client = _restricted(web)
        before = client.get("/admin/partials/depmap-domain-explorer").text
        assert "inventory" not in before
        team = fixture.group_manager.get_group_by_name("fixture-team")
        assert team is not None
        fixture.group_manager.grant_repo_access(GAMMA, team.id, granted_by="test")
        after = client.get("/admin/partials/depmap-domain-explorer").text
        assert "inventory" in after
