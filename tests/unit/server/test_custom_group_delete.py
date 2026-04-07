"""
Unit tests for Story #709: Custom Group Management - AC5, AC6, AC7.

TDD Tests covering:
- AC5: Cannot Delete Default Groups
- AC6: Cannot Delete Groups with Users
- AC7: Delete Empty Custom Group (cascade delete repo_group_access)

TDD: These tests are written FIRST, before implementation.
"""

import pytest
import tempfile
import sqlite3
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


class TestAC5CannotDeleteDefaultGroups:
    """AC5: DELETE /api/v1/groups/{id} on default group returns 400."""

    def test_delete_admins_returns_400(self, client, group_manager):
        """Test DELETE on admins group returns 400."""
        admins = group_manager.get_group_by_name("admins")

        response = client.delete(f"/api/v1/groups/{admins.id}")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_powerusers_returns_400(self, client, group_manager):
        """Test DELETE on powerusers group returns 400."""
        powerusers = group_manager.get_group_by_name("powerusers")

        response = client.delete(f"/api/v1/groups/{powerusers.id}")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_users_returns_400(self, client, group_manager):
        """Test DELETE on users group returns 400."""
        users_group = group_manager.get_group_by_name("users")

        response = client.delete(f"/api/v1/groups/{users_group.id}")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_default_group_error_message(self, client, group_manager):
        """Test DELETE error message indicates default groups cannot be deleted."""
        admins = group_manager.get_group_by_name("admins")

        response = client.delete(f"/api/v1/groups/{admins.id}")

        data = response.json()
        detail_lower = data["detail"].lower()
        assert (
            "default" in detail_lower
            and "cannot" in detail_lower
            and "delete" in detail_lower
        )

    def test_delete_default_group_remains_unchanged(self, client, group_manager):
        """Test default group remains in database after delete attempt."""
        admins = group_manager.get_group_by_name("admins")
        original_id = admins.id

        client.delete(f"/api/v1/groups/{admins.id}")

        admins_after = group_manager.get_group_by_name("admins")
        assert admins_after is not None
        assert admins_after.id == original_id


class TestAC6CannotDeleteGroupsWithUsers:
    """AC6: DELETE on group with assigned users returns 400."""

    def test_delete_group_with_users_returns_400(self, client, group_manager):
        """Test DELETE on group with users returns 400."""
        group_id = create_group_via_api(client, "group-with-user", "Has a user")
        group_manager.assign_user_to_group("testuser", group_id, "admin")

        response = client.delete(f"/api/v1/groups/{group_id}")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_delete_group_with_users_error_includes_count(self, client, group_manager):
        """Test error message includes user count."""
        group_id = create_group_via_api(client, "group-with-many-users", "Has users")
        group_manager.assign_user_to_group("user1", group_id, "admin")
        group_manager.assign_user_to_group("user2", group_id, "admin")
        group_manager.assign_user_to_group("user3", group_id, "admin")

        response = client.delete(f"/api/v1/groups/{group_id}")

        data = response.json()
        assert "3" in data["detail"] or "user" in data["detail"].lower()

    def test_delete_group_with_users_group_unchanged(self, client, group_manager):
        """Test group remains unchanged after failed delete."""
        group_id = create_group_via_api(client, "unchanged-group", "Should remain")
        group_manager.assign_user_to_group("keeper", group_id, "admin")

        client.delete(f"/api/v1/groups/{group_id}")

        group = group_manager.get_group(group_id)
        assert group is not None
        assert group.name == "unchanged-group"

    def test_delete_group_manager_raises_error_for_users(self, temp_db_path):
        """Test GroupAccessManager.delete_group() raises error when group has users."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("has-users", "Group with users")
        manager.assign_user_to_group("testuser", group.id, "admin")

        from code_indexer.server.services.group_access_manager import GroupHasUsersError

        with pytest.raises(GroupHasUsersError) as exc_info:
            manager.delete_group(group.id)

        assert "1" in str(exc_info.value) or "user" in str(exc_info.value).lower()


@pytest.mark.slow
class TestAC7DeleteEmptyCustomGroup:
    """AC7: DELETE /api/v1/groups/{id} on empty custom group succeeds."""

    def test_delete_empty_custom_group_returns_204(self, client):
        """Test DELETE on empty custom group returns 204 No Content."""
        group_id = create_group_via_api(client, "empty-deletable", "No users")

        response = client.delete(f"/api/v1/groups/{group_id}")

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_empty_group_removes_from_database(self, client, group_manager):
        """Test deleted group no longer exists in database."""
        group_id = create_group_via_api(client, "to-be-deleted", "Will be deleted")

        client.delete(f"/api/v1/groups/{group_id}")

        group = group_manager.get_group(group_id)
        assert group is None

    def test_delete_cascades_repo_group_access(
        self, client, group_manager, temp_db_path
    ):
        """Test all repo_group_access records for group are cascade deleted."""
        group_id = create_group_via_api(client, "cascade-test", "Has repos")

        group_manager.grant_repo_access("repo-1", group_id, "admin")
        group_manager.grant_repo_access("repo-2", group_id, "admin")
        group_manager.grant_repo_access("repo-3", group_id, "admin")

        conn = sqlite3.connect(str(temp_db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM repo_group_access WHERE group_id = ?", (group_id,)
        )
        count_before = cursor.fetchone()[0]
        assert count_before == 3

        client.delete(f"/api/v1/groups/{group_id}")

        cursor.execute(
            "SELECT COUNT(*) FROM repo_group_access WHERE group_id = ?", (group_id,)
        )
        count_after = cursor.fetchone()[0]
        conn.close()

        assert count_after == 0

    def test_delete_requires_admin(self, non_admin_client, group_manager):
        """Test DELETE /api/v1/groups/{id} requires admin role."""
        group = group_manager.create_group("admin-only-delete", "Test")

        response = non_admin_client.delete(f"/api/v1/groups/{group.id}")

        assert response.status_code == status.HTTP_403_FORBIDDEN
