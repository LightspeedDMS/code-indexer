"""REST group membership is only written for names that have an account.

The groups routers run in a minimal FastAPI app over a real
``GroupAccessManager`` and a real SQLite ``UserManager``; only the admin
identity and elevation dependencies are overridden.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_user,
)
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.routers import groups as groups_router
from code_indexer.server.routers.groups import (
    get_group_manager,
    router,
    users_router,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from tests.unit.server._account_rows import PASSWORD, Stores, build_stores

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app: FastAPI) -> None:
    for rtr in (router, users_router):
        for route in rtr.routes:
            if not isinstance(route, APIRoute):
                continue
            for dep in route.dependencies or []:
                fn = getattr(dep, "dependency", None)
                if fn is not None and getattr(fn, "__qualname__", "") == (
                    _ELEVATION_QUALNAME
                ):
                    app.dependency_overrides[fn] = lambda: None


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    return build_stores(tmp_path)


@pytest.fixture
def client(stores: Stores, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    groups = stores.registry.groups
    assert isinstance(groups, GroupAccessManager)
    admin = SimpleNamespace(username="admin", role="admin")
    monkeypatch.setattr(dependencies, "user_manager", stores.user_manager)
    monkeypatch.setattr(groups_router, "_group_manager", groups)
    app = FastAPI()
    app.include_router(router)
    app.include_router(users_router)
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_group_manager] = lambda: groups
    _bypass_elevation(app)
    yield TestClient(app)


def _group_id(stores: Stores, name: str) -> int:
    group = stores.registry.groups.get_group_by_name(name)
    assert group is not None
    return int(group.id)


def test_assign_refuses_name_without_account(
    client: TestClient, stores: Stores
) -> None:
    response = client.post(
        f"/api/v1/groups/{_group_id(stores, 'admins')}/members",
        json={"user_id": "ghost"},
    )

    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]
    assert stores.registry.groups.get_user_group("ghost") is None


def test_assign_writes_membership_for_existing_account(
    client: TestClient, stores: Stores
) -> None:
    stores.user_manager.create_user("alice", PASSWORD, UserRole.NORMAL_USER)

    response = client.post(
        f"/api/v1/groups/{_group_id(stores, 'admins')}/members",
        json={"user_id": "alice"},
    )

    assert response.status_code == 200
    group = stores.registry.groups.get_user_group("alice")
    assert group is not None and group.name == "admins"


def test_move_refuses_name_whose_account_is_gone(
    client: TestClient, stores: Stores
) -> None:
    """A membership row left without an account cannot be moved into a group."""
    stores.registry.groups.assign_user_to_group(
        "ghost", _group_id(stores, "users"), "admin"
    )

    response = client.put(
        "/api/v1/users/ghost/group", json={"group_id": _group_id(stores, "admins")}
    )

    assert response.status_code == 404
    group = stores.registry.groups.get_user_group("ghost")
    assert group is not None and group.name == "users"
