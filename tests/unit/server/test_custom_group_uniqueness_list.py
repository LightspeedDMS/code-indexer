"""
Unit tests for Story #709: Custom Group Management - AC8 and AC9.

TDD Tests covering:
- AC8: Group Name Uniqueness (case-insensitive, 409 Conflict)
- AC9: List All Groups Including Custom (sorted: default first, then by name)

TDD: These tests are written FIRST, before implementation.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from code_indexer.server.services.group_access_manager import (
    GroupAccessManager,
)
from code_indexer.server.routers.groups import (
    router,
    set_group_manager,
    get_group_manager,
)
from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_user,
)

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, rtr):
    """Override all require_elevation deps so functional tests can run without TOTP."""
    from fastapi.routing import APIRoute

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


@pytest.fixture
def temp_db_path():
    """Create a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    """Create a GroupAccessManager instance."""
    return GroupAccessManager(temp_db_path)


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user."""
    user = MagicMock()
    user.username = "admin_user"
    user.role = "admin"
    return user


@pytest.fixture
def client(group_manager, mock_admin_user):
    """Create FastAPI test client with admin dependency overrides."""
    app = FastAPI()
    app.include_router(router)
    set_group_manager(group_manager)
    app.dependency_overrides[get_current_admin_user] = lambda: mock_admin_user
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_group_manager] = lambda: group_manager
    _bypass_elevation(app, router)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_group_manager(None)


def create_group_via_api(client: TestClient, name: str, description: str) -> int:
    """Helper to create a group via API and return its id."""
    response = client.post(
        "/api/v1/groups",
        json={"name": name, "description": description},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()["id"]  # type: ignore[no-any-return]


class TestAC8GroupNameUniqueness:
    """AC8: Creating duplicate name returns 409 Conflict (case-insensitive)."""

    def test_duplicate_name_returns_409(self, client):
        """Test creating duplicate name returns 409 Conflict."""
        create_group_via_api(client, "unique-team", "First")

        response = client.post(
            "/api/v1/groups",
            json={"name": "unique-team", "description": "Duplicate"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_duplicate_error_message(self, client):
        """Test error says 'Group name already exists'."""
        create_group_via_api(client, "error-message-test", "First")

        response = client.post(
            "/api/v1/groups",
            json={"name": "error-message-test", "description": "Duplicate"},
        )

        data = response.json()
        assert "already exists" in data["detail"].lower()

    def test_case_insensitive_uniqueness(self, client):
        """Test name uniqueness is case-insensitive."""
        create_group_via_api(client, "case-test", "First")

        response = client.post(
            "/api/v1/groups",
            json={"name": "CASE-TEST", "description": "Should fail"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT

        response = client.post(
            "/api/v1/groups",
            json={"name": "Case-Test", "description": "Should also fail"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_manager_create_group_case_insensitive(self, temp_db_path):
        """Test GroupAccessManager.create_group() enforces case-insensitive uniqueness."""
        manager = GroupAccessManager(temp_db_path)
        manager.create_group("my-group", "First")

        with pytest.raises(ValueError) as exc_info:
            manager.create_group("MY-GROUP", "Duplicate")

        assert "already exists" in str(exc_info.value).lower()

    def test_update_name_uniqueness(self, client):
        """Test updating name also enforces uniqueness."""
        create_group_via_api(client, "existing-name", "First")
        other_id = create_group_via_api(client, "other-name", "Second")

        response = client.put(
            f"/api/v1/groups/{other_id}",
            json={"name": "existing-name"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_update_to_same_name_case_insensitive(self, client):
        """Test updating to a name that differs only in case fails."""
        create_group_via_api(client, "first-group", "First")
        second_id = create_group_via_api(client, "second-group", "Second")

        response = client.put(
            f"/api/v1/groups/{second_id}",
            json={"name": "FIRST-GROUP"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT


class TestAC9ListAllGroupsSorted:
    """AC9: GET /api/v1/groups returns all groups sorted properly."""

    def test_list_includes_default_groups(self, client):
        """Test list includes default groups."""
        response = client.get("/api/v1/groups")

        data = response.json()
        names = [g["name"] for g in data]
        assert "admins" in names
        assert "powerusers" in names
        assert "users" in names

    def test_list_includes_custom_groups(self, client):
        """Test list includes custom groups."""
        create_group_via_api(client, "custom-a", "A")
        create_group_via_api(client, "custom-b", "B")

        response = client.get("/api/v1/groups")

        data = response.json()
        names = [g["name"] for g in data]
        assert "custom-a" in names
        assert "custom-b" in names

    def test_list_sorted_default_groups_first(self, client):
        """Test default groups are listed first."""
        create_group_via_api(client, "aaa-first-alphabetically", "A")

        response = client.get("/api/v1/groups")

        data = response.json()

        default_indices = []
        custom_indices = []
        for i, group in enumerate(data):
            if group["is_default"]:
                default_indices.append(i)
            else:
                custom_indices.append(i)

        if default_indices and custom_indices:
            assert max(default_indices) < min(custom_indices), (
                "Default groups should be listed before custom groups"
            )

    def test_list_custom_groups_sorted_by_name(self, client):
        """Test custom groups are sorted alphabetically by name."""
        create_group_via_api(client, "zebra-team", "Z")
        create_group_via_api(client, "alpha-team", "A")
        create_group_via_api(client, "beta-team", "B")

        response = client.get("/api/v1/groups")

        data = response.json()
        custom_groups = [g for g in data if not g["is_default"]]
        custom_names = [g["name"] for g in custom_groups]

        assert custom_names == sorted(custom_names)

    def test_list_response_includes_all_fields(self, client):
        """Test list response includes required fields (id, name, description, is_default)."""
        response = client.get("/api/v1/groups")

        data = response.json()
        for group in data:
            assert "id" in group
            assert "name" in group
            assert "description" in group
            assert "is_default" in group

    def test_manager_get_all_groups_sorted(self, temp_db_path):
        """Test GroupAccessManager.get_all_groups() returns sorted list."""
        manager = GroupAccessManager(temp_db_path)

        manager.create_group("zebra", "Z group")
        manager.create_group("alpha", "A group")

        groups = manager.get_all_groups()

        default_groups = [g for g in groups if g.is_default]
        custom_groups = [g for g in groups if not g.is_default]

        default_count = len(default_groups)
        for i in range(default_count):
            assert groups[i].is_default, f"Group at index {i} should be default"

        custom_names = [g.name for g in custom_groups]
        assert custom_names == sorted(custom_names), (
            "Custom groups should be sorted alphabetically"
        )
