"""
Unit tests for Story #709: Custom Group Management - AC3 and AC4.

TDD Tests covering:
- AC3: Read Custom Group (GET /api/v1/groups/{id})
- AC4: Update Custom Group (PUT /api/v1/groups/{id})

TDD: These tests are written FIRST, before implementation.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException, status
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


# Constants
NONEXISTENT_GROUP_ID = 99999


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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    set_group_manager(None)


@pytest.fixture
def non_admin_client(group_manager):
    """Create FastAPI test client where admin check raises 403."""
    app = FastAPI()
    app.include_router(router)
    set_group_manager(group_manager)

    def raise_403():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )

    app.dependency_overrides[get_current_admin_user] = raise_403
    app.dependency_overrides[get_group_manager] = lambda: group_manager
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
    return response.json()["id"]


class TestAC3ReadCustomGroup:
    """AC3: GET /api/v1/groups/{id} returns group details."""

    def test_get_custom_group_returns_all_fields(self, client):
        """Test GET returns id, name, description, is_default, created_at."""
        group_id = create_group_via_api(client, "readable-group", "For reading")

        response = client.get(f"/api/v1/groups/{group_id}")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["id"] == group_id
        assert data["name"] == "readable-group"
        assert data["description"] == "For reading"
        assert data["is_default"] is False
        assert "created_at" in data

    def test_get_custom_group_includes_user_count(self, client, group_manager):
        """Test GET returns count of users in the group."""
        group_id = create_group_via_api(client, "group-with-users", "Has users")
        group_manager.assign_user_to_group("user1", group_id, "admin")
        group_manager.assign_user_to_group("user2", group_id, "admin")

        response = client.get(f"/api/v1/groups/{group_id}")
        data = response.json()

        assert data["user_count"] == 2

    def test_get_custom_group_includes_accessible_repos(self, client, group_manager):
        """Test GET returns list of accessible repos."""
        group_id = create_group_via_api(client, "group-with-repos", "Has repos")
        group_manager.grant_repo_access("repo-a", group_id, "admin")
        group_manager.grant_repo_access("repo-b", group_id, "admin")

        response = client.get(f"/api/v1/groups/{group_id}")
        data = response.json()

        assert "cidx-meta" in data["accessible_repos"]
        assert "repo-a" in data["accessible_repos"]
        assert "repo-b" in data["accessible_repos"]

    def test_get_returns_404_for_nonexistent_group(self, client):
        """Test GET returns 404 for nonexistent group ID."""
        response = client.get(f"/api/v1/groups/{NONEXISTENT_GROUP_ID}")

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAC4UpdateCustomGroup:
    """AC4: PUT /api/v1/groups/{id} updates name and/or description."""

    def test_put_updates_group_name(self, client):
        """Test PUT /api/v1/groups/{id} updates group name."""
        group_id = create_group_via_api(client, "original-name", "Original description")

        response = client.put(
            f"/api/v1/groups/{group_id}",
            json={"name": "updated-name"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "updated-name"

    def test_put_updates_group_description(self, client):
        """Test PUT /api/v1/groups/{id} updates group description."""
        group_id = create_group_via_api(client, "desc-update-group", "Original")

        response = client.put(
            f"/api/v1/groups/{group_id}",
            json={"description": "Updated description"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["description"] == "Updated description"

    def test_put_updates_both_name_and_description(self, client):
        """Test PUT /api/v1/groups/{id} updates both fields."""
        group_id = create_group_via_api(client, "both-update-group", "Original")

        response = client.put(
            f"/api/v1/groups/{group_id}",
            json={"name": "new-name", "description": "New description"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["name"] == "new-name"
        assert data["description"] == "New description"

    def test_put_preserves_is_default_false(self, client):
        """Test PUT cannot change is_default (remains FALSE)."""
        group_id = create_group_via_api(client, "is-default-test", "Test")

        response = client.put(
            f"/api/v1/groups/{group_id}",
            json={"name": "updated-is-default-test"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["is_default"] is False

    def test_put_returns_200_with_updated_details(self, client):
        """Test PUT returns 200 OK with updated details."""
        group_id = create_group_via_api(client, "status-test-group", "Test")

        response = client.put(
            f"/api/v1/groups/{group_id}",
            json={"name": "updated-status-test-group"},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "id" in data
        assert "name" in data
        assert "description" in data
        assert "is_default" in data

    def test_put_requires_admin(self, non_admin_client, group_manager):
        """Test PUT /api/v1/groups/{id} requires admin role."""
        group = group_manager.create_group("admin-test-group", "Test")

        response = non_admin_client.put(
            f"/api/v1/groups/{group.id}",
            json={"name": "hacked-name"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_put_returns_404_for_nonexistent_group(self, client):
        """Test PUT returns 404 for nonexistent group ID."""
        response = client.put(
            f"/api/v1/groups/{NONEXISTENT_GROUP_ID}",
            json={"name": "new-name"},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_group_manager_method(self, temp_db_path):
        """Test GroupAccessManager.update_group() method."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("test-update", "Original")

        updated = manager.update_group(
            group.id, name="updated-name", description="Updated"
        )

        assert updated.name == "updated-name"
        assert updated.description == "Updated"
        assert updated.is_default is False

    def test_update_group_partial_update_name_only(self, temp_db_path):
        """Test update_group with only name."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("partial-test", "Original description")

        updated = manager.update_group(group.id, name="new-name-only")

        assert updated.name == "new-name-only"
        assert updated.description == "Original description"

    def test_update_group_partial_update_description_only(self, temp_db_path):
        """Test update_group with only description."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("desc-only-test", "Original")

        updated = manager.update_group(group.id, description="New description only")

        assert updated.name == "desc-only-test"
        assert updated.description == "New description only"
