"""
Tests for Repository Categories REST API Router (Story #182).

Tests the FastAPI router that exposes category CRUD operations via REST API.
"""

import pytest
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.routers.repo_categories import get_category_service
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.auth.dependencies import get_current_user, get_current_admin_user


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        # Initialize database schema
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        yield str(db_path)


@pytest.fixture
def category_service(test_db):
    """Create RepoCategoryService instance and override dependency."""
    service = RepoCategoryService(test_db)

    # Override the dependency to return our test service
    app.dependency_overrides[get_category_service] = lambda: service

    yield service

    # Clean up
    if get_category_service in app.dependency_overrides:
        del app.dependency_overrides[get_category_service]


@pytest.fixture
def user_manager(test_db):
    """Create UserManager instance."""
    return UserManager(test_db)


@pytest.fixture
def mock_admin_user():
    """Create mock admin user."""
    user = Mock()
    user.username = "admin"
    user.role = UserRole.ADMIN
    return user


@pytest.fixture
def mock_normal_user():
    """Create mock normal user."""
    user = Mock()
    user.username = "user"
    user.role = UserRole.NORMAL_USER
    return user


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_auth(mock_admin_user):
    """Override auth to return admin user."""
    def mock_get_admin():
        return mock_admin_user

    app.dependency_overrides[get_current_admin_user] = mock_get_admin
    app.dependency_overrides[get_current_user] = mock_get_admin
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def normal_user_auth(mock_normal_user):
    """Override auth to return normal user (but not admin)."""
    def mock_get_user():
        return mock_normal_user

    def mock_get_admin():
        # Normal users should get 403 when trying to access admin endpoints
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required"
        )

    app.dependency_overrides[get_current_user] = mock_get_user
    app.dependency_overrides[get_current_admin_user] = mock_get_admin
    yield
    app.dependency_overrides.clear()


class TestListCategories:
    """Test GET /api/v1/repo-categories endpoint."""

    def test_list_returns_all_categories_ordered_by_priority(
        self, client, admin_auth, category_service
    ):
        """Test that list endpoint returns categories in priority order."""
        # Create categories
        category_service.create_category("Backend", "^api-.*")
        category_service.create_category("Frontend", "^web-.*")
        category_service.create_category("Langfuse", "^langfuse-.*")

        # List categories
        response = client.get("/api/v1/repo-categories")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        # With priority-1 insertion, last created = priority 1
        assert data[0]["name"] == "Langfuse"
        assert data[0]["priority"] == 1
        assert data[1]["name"] == "Frontend"
        assert data[1]["priority"] == 2
        assert data[2]["name"] == "Backend"
        assert data[2]["priority"] == 3

    def test_list_accessible_by_normal_user(self, client, normal_user_auth):
        """Test that normal users can read categories (AC6: any authenticated)."""
        response = client.get("/api/v1/repo-categories")

        assert response.status_code == 200

    def test_list_requires_authentication(self, client):
        """Test that unauthenticated requests are rejected."""
        response = client.get("/api/v1/repo-categories")
        assert response.status_code == 401


class TestCreateCategory:
    """Test POST /api/v1/repo-categories endpoint."""

    def test_create_with_valid_data_succeeds(self, client, admin_auth):
        """Test creating category with valid name and pattern (AC6)."""
        # Use UUID-based unique name to avoid conflicts with production database
        unique_name = f"TestCat-{uuid.uuid4().hex[:8]}"

        response = client.post(
            "/api/v1/repo-categories",
            json={"name": unique_name, "pattern": "^test-.*"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == unique_name
        assert data["pattern"] == "^test-.*"
        assert "id" in data
        assert "priority" in data

    def test_create_with_invalid_regex_returns_422(self, client, admin_auth):
        """Test that invalid regex pattern returns 422 validation error (AC6)."""
        response = client.post(
            "/api/v1/repo-categories",
            json={"name": "Bad", "pattern": "[unclosed"},
        )

        assert response.status_code == 422
        assert "regex" in response.text.lower() or "pattern" in response.text.lower()

    def test_create_with_duplicate_name_returns_409(
        self, client, admin_auth, category_service
    ):
        """Test that duplicate category name returns 409 conflict (AC6)."""
        # Create first category
        category_service.create_category("Backend", "^api-.*")

        # Try to create duplicate
        response = client.post(
            "/api/v1/repo-categories",
            json={"name": "Backend", "pattern": "^service-.*"},
        )

        assert response.status_code == 409

    def test_create_requires_admin_role(self, client, normal_user_auth):
        """Test that non-admin users get 403 for write operations (AC6)."""
        response = client.post(
            "/api/v1/repo-categories",
            json={"name": "Test", "pattern": "^test-.*"},
        )

        assert response.status_code == 403


class TestUpdateCategory:
    """Test PUT /api/v1/repo-categories/{id} endpoint."""

    def test_update_category_succeeds(
        self, client, admin_auth, category_service
    ):
        """Test updating category name and pattern (AC6)."""
        # Create category
        cat_id = category_service.create_category("Backend", "^api-.*")

        # Update it
        response = client.put(
            f"/api/v1/repo-categories/{cat_id}",
            json={"name": "Backend Services", "pattern": "^(api|service)-.*"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Backend Services"
        assert data["pattern"] == "^(api|service)-.*"

    def test_update_nonexistent_category_returns_404(self, client, admin_auth):
        """Test updating non-existent category returns 404."""
        response = client.put(
            "/api/v1/repo-categories/999",
            json={"name": "Test", "pattern": "^test-.*"},
        )

        assert response.status_code == 404

    def test_update_requires_admin_role(self, client, normal_user_auth, category_service):
        """Test that non-admin users cannot update categories."""
        cat_id = category_service.create_category("Backend", "^api-.*")

        response = client.put(
            f"/api/v1/repo-categories/{cat_id}",
            json={"name": "Updated", "pattern": "^updated-.*"},
        )

        assert response.status_code == 403


class TestDeleteCategory:
    """Test DELETE /api/v1/repo-categories/{id} endpoint."""

    def test_delete_category_succeeds(self, client, admin_auth, category_service):
        """Test deleting category (AC6)."""
        cat_id = category_service.create_category("Temporary", "^tmp-.*")

        response = client.delete(f"/api/v1/repo-categories/{cat_id}")

        assert response.status_code == 204

    def test_delete_nonexistent_category_returns_404(self, client, admin_auth):
        """Test deleting non-existent category returns 404."""
        response = client.delete("/api/v1/repo-categories/999")

        assert response.status_code == 404

    def test_delete_requires_admin_role(self, client, normal_user_auth, category_service):
        """Test that non-admin users cannot delete categories."""
        cat_id = category_service.create_category("Backend", "^api-.*")

        response = client.delete(f"/api/v1/repo-categories/{cat_id}")

        assert response.status_code == 403


class TestReorderCategories:
    """Test POST /api/v1/repo-categories/reorder endpoint."""

    def test_reorder_updates_priorities(self, client, admin_auth, category_service):
        """Test reordering categories (AC6)."""
        # Create categories in order: Backend(1), Frontend(2), Langfuse(3)
        backend_id = category_service.create_category("Backend", "^api-.*")
        frontend_id = category_service.create_category("Frontend", "^web-.*")
        langfuse_id = category_service.create_category("Langfuse", "^langfuse-.*")

        # Reorder: Langfuse, Backend, Frontend
        response = client.post(
            "/api/v1/repo-categories/reorder",
            json={"ordered_ids": [langfuse_id, backend_id, frontend_id]},
        )

        assert response.status_code == 200

        # Verify new order
        categories = category_service.list_categories()
        assert categories[0]["name"] == "Langfuse"
        assert categories[0]["priority"] == 1
        assert categories[1]["name"] == "Backend"
        assert categories[1]["priority"] == 2
        assert categories[2]["name"] == "Frontend"
        assert categories[2]["priority"] == 3

    def test_reorder_requires_admin_role(self, client, normal_user_auth):
        """Test that non-admin users cannot reorder categories."""
        response = client.post(
            "/api/v1/repo-categories/reorder",
            json={"ordered_ids": [1, 2, 3]},
        )

        assert response.status_code == 403


class TestReEvaluate:
    """Test POST /api/v1/repo-categories/re-evaluate endpoint."""

    def test_re_evaluate_returns_summary(self, client, admin_auth):
        """Test bulk re-evaluate returns summary (AC6)."""
        response = client.post("/api/v1/repo-categories/re-evaluate")

        assert response.status_code == 200
        data = response.json()
        assert "updated" in data
        assert isinstance(data["updated"], int)

    def test_re_evaluate_requires_admin_role(self, client, normal_user_auth):
        """Test that non-admin users cannot trigger re-evaluate."""
        response = client.post("/api/v1/repo-categories/re-evaluate")

        assert response.status_code == 403
