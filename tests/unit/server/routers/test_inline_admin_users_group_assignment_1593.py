"""
AC7 RED/GREEN test: REST admin `create_user` must assign a group.

Story #1593 AC7: a user created through the REST admin endpoint
(`POST /api/admin/users`, `routers/inline_admin_users.py::create_user`)
must land in a group membership row, via the shared
`GroupAccessManager.ensure_user_group_membership()` primitive. The story
text explicitly flags this path as a verified gap: "does not assign a
group today."

TDD: written FIRST, asserting the DESIRED behavior (user ends up with a
group membership matching their role). Against unmodified code this
fails -- the created user has NO group membership at all, which is
exactly the DENY-relevant gap AC8's fail-closed enforcement would later
strand them with (denied every tool except `authenticate`) unless this
gap is closed.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers import groups as groups_router
from code_indexer.server.routers.inline_admin_users import register_admin_user_routes
from code_indexer.server.services.group_access_manager import GroupAccessManager


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


class _StubUserManager:
    def create_user(self, username, password, role):
        return User(
            username=username,
            password_hash="x",
            role=role,
            created_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def client(group_manager, monkeypatch):
    app = FastAPI()

    mock_admin = User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_admin

    stub_user_manager = _StubUserManager()
    monkeypatch.setattr(dependencies, "user_manager", stub_user_manager)
    monkeypatch.setattr(groups_router, "_group_manager", group_manager)

    register_admin_user_routes(
        app,
        jwt_manager=None,
        user_manager=stub_user_manager,
        refresh_token_manager=None,
        db_path_str="unused",
    )

    return TestClient(app)


@pytest.mark.parametrize(
    "username,role,expected_group_name",
    [
        ("rest-created-user", "normal_user", "users"),
        ("rest-created-admin", "admin", "admins"),
    ],
)
def test_created_user_is_assigned_to_expected_group(
    client, group_manager, username, role, expected_group_name
):
    response = client.post(
        "/api/admin/users",
        json={"username": username, "password": "StrongPass1!", "role": role},
    )

    assert response.status_code == 201, response.text
    membership = group_manager.get_user_group(username)
    assert membership is not None, (
        "user created via POST /api/admin/users must have a group "
        "membership (Story #1593 AC7)"
    )
    assert membership.name == expected_group_name
