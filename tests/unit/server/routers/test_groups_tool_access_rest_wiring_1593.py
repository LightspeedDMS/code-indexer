"""
Front-door route-wiring and persistence tests for the Story #1593
AC5/AC6 REST tool-access endpoints.

Codex's own RED/GREEN coverage for these endpoints
(`test_groups_tool_access_rest_1593.py`) calls the handler functions
directly with a `Mock()` GroupAccessManager -- it proves the handler
LOGIC is correct but never proves the endpoints are actually reachable
through a real HTTP request with FastAPI's routing/dependency-injection
machinery genuinely engaged, nor that a mutation actually persists to a
real database. This file closes that gap: real `TestClient`, real
`GroupAccessManager` (SQLite), real HTTP requests against
`/api/v1/groups/tool-access/...`.

Both `get_group_manager` and `get_current_user` are overridden via
`app.dependency_overrides` on a per-app instance (never the shared
module-level `groups_router._group_manager` global), so nothing here is
shared mutable state across tests -- no teardown/restoration is needed
and parallel execution is safe.

Scope, stated precisely (these are NOT full end-to-end auth tests):
`get_current_admin_user` (the dependency `groups.py`'s endpoints declare
directly) is left completely real and unmodified -- only its own
sub-dependency `get_current_user` is overridden with a fixed principal,
so `get_current_admin_user`'s actual `has_permission("manage_users")`
check genuinely executes on every request, including the
non-admin-rejection test below. `get_current_admin_user_hybrid` (used
inside `require_elevation()` for session/JWT-hybrid auth) is overridden
directly with a fixed principal rather than exercised through a real
session/JWT flow -- that dependency's own session/token validation has
its own dedicated coverage elsewhere and would require standing up full
auth infrastructure disproportionate to what this file is testing. This
mirrors the identical, pre-existing pattern used by
`test_web_user_creation_auto_group.py` for the same reason.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.routers import groups as groups_router
from code_indexer.server.services.group_access_manager import GroupAccessManager

_NON_AUTH_TOOL = next(name for name in TOOL_REGISTRY if name != "authenticate")


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    return GroupAccessManager(temp_db_path)


@pytest.fixture
def users_group(group_manager):
    group = group_manager.get_group_by_name("users")
    assert group is not None
    return group


def _user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="x",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _client_for(group_manager: GroupAccessManager, principal: User) -> TestClient:
    app = FastAPI()
    app.include_router(groups_router.router)

    app.dependency_overrides[groups_router.get_group_manager] = lambda: group_manager
    # get_current_admin_user itself is left real: only its sub-dependency
    # is overridden, so its own admin-permission check genuinely runs.
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: principal

    return TestClient(app)


@pytest.fixture
def admin_client(group_manager):
    return _client_for(group_manager, _user("admin", UserRole.ADMIN))


@pytest.fixture
def non_admin_client(group_manager):
    return _client_for(group_manager, _user("regular-user", UserRole.NORMAL_USER))


def test_grant_persists_to_real_group_manager(admin_client, group_manager, users_group):
    response = admin_client.post(
        f"/api/v1/groups/tool-access/{users_group.id}/{_NON_AUTH_TOOL}"
    )

    assert response.status_code == 200, response.text
    assert group_manager.is_tool_allowed(_NON_AUTH_TOOL, users_group.id) is True


def test_revoke_persists_to_real_group_manager(
    admin_client, group_manager, users_group
):
    group_manager.set_tool_access(_NON_AUTH_TOOL, users_group.id, True, "admin")

    response = admin_client.delete(
        f"/api/v1/groups/tool-access/{users_group.id}/{_NON_AUTH_TOOL}"
    )

    assert response.status_code == 200, response.text
    assert group_manager.is_tool_allowed(_NON_AUTH_TOOL, users_group.id) is False


def test_get_reflects_real_state(admin_client, group_manager, users_group):
    group_manager.set_tool_access(_NON_AUTH_TOOL, users_group.id, True, "admin")

    response = admin_client.get("/api/v1/groups/tool-access")

    assert response.status_code == 200, response.text
    body = response.json()
    tool_entry = next(t for t in body["tools"] if t["tool_name"] == _NON_AUTH_TOOL)
    group_entry = next(
        g for g in tool_entry["groups"] if g["group_id"] == users_group.id
    )
    assert group_entry["allowed"] is True


def test_bulk_disable_affects_all_groups_and_writes_one_audit_entry_each(
    admin_client, group_manager
):
    all_groups = group_manager.get_all_groups()
    for group in all_groups:
        group_manager.set_tool_access(_NON_AUTH_TOOL, group.id, True, "admin")

    response = admin_client.post(
        f"/api/v1/groups/tool-access/{_NON_AUTH_TOOL}/bulk-disable"
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["affected_group_ids"]) == {g.id for g in all_groups}
    for group in all_groups:
        assert group_manager.is_tool_allowed(_NON_AUTH_TOOL, group.id) is False

    logs, total = group_manager.get_audit_logs(
        action_type="tool_access_bulk_disable", target_type="tool"
    )
    assert total == len(all_groups)


def test_authenticate_cannot_be_mutated_through_real_endpoint(
    admin_client, users_group
):
    response = admin_client.post(
        f"/api/v1/groups/tool-access/{users_group.id}/authenticate"
    )

    assert response.status_code == 400, response.text


def test_non_admin_user_rejected_by_real_admin_dependency(
    non_admin_client, users_group
):
    response = non_admin_client.post(
        f"/api/v1/groups/tool-access/{users_group.id}/{_NON_AUTH_TOOL}"
    )

    assert response.status_code == 403, response.text
