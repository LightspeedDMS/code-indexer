"""
Unit tests for Story #709: Custom Group Management - AC1 and AC2.

TDD Tests covering:
- AC1: Create Custom Group (POST /api/v1/groups)
- AC2: Custom Groups Start Empty (no repo access except cidx-meta)

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


# Constants
NONEXISTENT_GROUP_ID = 99999
MAX_GROUP_NAME_LENGTH = 100


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


class TestAC1CreateCustomGroup:
    """AC1: POST /api/v1/groups creates new group with name, description."""

    def test_post_groups_creates_custom_group(self, client):
        """Test POST /api/v1/groups creates a new custom group."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "frontend-team", "description": "Frontend developers"},
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["name"] == "frontend-team"
        assert data["description"] == "Frontend developers"
        assert data["is_default"] is False

    def test_post_groups_returns_201_created(self, client):
        """Test POST /api/v1/groups returns 201 Created status."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "backend-team", "description": "Backend developers"},
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_post_groups_returns_group_details(self, client):
        """Test POST /api/v1/groups returns complete group details."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "qa-team", "description": "QA testers"},
        )

        data = response.json()
        assert "id" in data
        assert "name" in data
        assert "description" in data
        assert "is_default" in data
        assert "created_at" in data

    def test_post_groups_is_default_false(self, client):
        """Test that custom groups have is_default=FALSE."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "devops-team", "description": "DevOps engineers"},
        )

        data = response.json()
        assert data["is_default"] is False

    def test_post_groups_requires_admin(self, non_admin_client):
        """Test POST /api/v1/groups requires admin role."""
        response = non_admin_client.post(
            "/api/v1/groups",
            json={"name": "test-group", "description": "Test"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_post_groups_validates_name_not_empty(self, client):
        """Test group name validation: cannot be empty."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "", "description": "Empty name test"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_post_groups_validates_name_max_length(self, client):
        """Test group name validation: max 100 characters."""
        long_name = "a" * (MAX_GROUP_NAME_LENGTH + 1)
        response = client.post(
            "/api/v1/groups",
            json={"name": long_name, "description": "Long name test"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    def test_create_group_manager_sets_is_default_false(self, temp_db_path):
        """Test GroupAccessManager.create_group() sets is_default=FALSE."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("custom-group", "A custom group")

        assert group.is_default is False
        assert group.name == "custom-group"


class TestAC2CustomGroupsStartEmpty:
    """AC2: New custom groups have no repository access (except implicit cidx-meta)."""

    def test_new_custom_group_has_only_cidx_meta_access(self, client):
        """Test new custom group only has cidx-meta access."""
        response = client.post(
            "/api/v1/groups",
            json={"name": "empty-group", "description": "Should start empty"},
        )
        group_id = response.json()["id"]

        detail_response = client.get(f"/api/v1/groups/{group_id}")
        data = detail_response.json()

        assert data["accessible_repos"] == ["cidx-meta"]

    def test_no_repo_group_access_records_for_new_group(self, temp_db_path):
        """Test no repo_group_access records exist for new custom groups."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("test-group", "Test group")

        conn = sqlite3.connect(str(temp_db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM repo_group_access WHERE group_id = ?", (group.id,)
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 0, "New custom group should have no repo_group_access records"

    def test_get_group_repos_returns_only_cidx_meta_for_new_group(self, temp_db_path):
        """Test get_group_repos returns only cidx-meta for new custom group."""
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("new-team", "New team")

        repos = manager.get_group_repos(group.id)

        assert repos == ["cidx-meta"]
