"""
Unit tests for per-repo category update route (Story #183).

Tests the Web UI route for manually updating a golden repo's category assignment.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def temp_db():
    """Create a temporary database with schema initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        schema = DatabaseSchema(db_path)
        schema.initialize_database()
        yield db_path


@pytest.fixture
def app_with_db(temp_db):
    """Create FastAPI app with test database."""
    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(Path(temp_db).parent)}):
        # Reset config service singleton to pick up the test environment variable
        reset_config_service()
        app = create_app()
        yield app
        # Clean up: reset again after test
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    """Create test client."""
    return TestClient(app_with_db)


@pytest.fixture
def admin_session_cookie(client):
    """Create admin session and return session cookies."""
    import re

    # Step 1: Get login page to extract CSRF token
    response_get = client.get("/login")
    assert response_get.status_code == 200

    # Extract CSRF token from HTML
    html = response_get.text
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "No CSRF token found in login page HTML"
    csrf_token = match.group(1)

    # Step 2: Login with form data and CSRF token
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        cookies=response_get.cookies,
        follow_redirects=False,
    )

    # Should redirect to /admin/ on success
    assert response.status_code == 303, f"Login failed with status {response.status_code}"
    assert response.headers.get("location") == "/admin/", f"Unexpected redirect: {response.headers.get('location')}"

    # Extract session cookie from response
    session_cookies = response.cookies
    assert "session" in session_cookies, "No session cookie found in login response"

    return session_cookies


@pytest.fixture
def csrf_token(client, admin_session_cookie):
    """Get CSRF token for form submissions."""
    import re

    # Get golden repos page to extract CSRF token
    response = client.get("/admin/golden-repos", cookies=admin_session_cookie)
    assert response.status_code == 200

    # Extract CSRF token from HTML
    html = response.text
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "No CSRF token found in golden repos page HTML"

    return match.group(1)


def test_update_repo_category_requires_admin_session(client):
    """Test that updating category requires admin session."""
    response = client.post(
        "/admin/golden-repos/test-repo/category",
        data={"category_id": "1"},
        follow_redirects=False,
    )
    # Should redirect to login or return 401/403
    assert response.status_code in [302, 303, 401, 403]


def test_update_repo_category_with_valid_data_succeeds(client, admin_session_cookie, csrf_token, temp_db):
    """Test updating repo category with valid data succeeds."""
    # Get the actual database path used by the app
    app_db_path = str(Path(temp_db).parent / "data" / "cidx_server.db")

    # Setup: Create category and repo in the app's database
    service = RepoCategoryService(app_db_path)
    backend = GoldenRepoMetadataSqliteBackend(app_db_path)

    category_id = service.create_category("Backend", "^api-.*")
    backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # Act: Update category via API
    response = client.post(
        "/admin/golden-repos/test-repo/category",
        data={"category_id": str(category_id), "csrf_token": csrf_token},
        cookies=admin_session_cookie,
    )

    # Assert: Success response
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text[:200]}"

    # Verify database update
    repo = backend.get_repo("test-repo")
    assert repo["category_id"] == category_id
    assert repo["category_auto_assigned"] is False  # Manual override


def test_update_repo_category_to_unassigned(client, admin_session_cookie, csrf_token, temp_db):
    """Test updating repo category to Unassigned (NULL)."""
    # Get the actual database path used by the app
    app_db_path = str(Path(temp_db).parent / "data" / "cidx_server.db")

    # Setup: Create category and repo with that category
    service = RepoCategoryService(app_db_path)
    backend = GoldenRepoMetadataSqliteBackend(app_db_path)

    category_id = service.create_category("Backend", "^api-.*")
    backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )
    service.update_repo_category("test-repo", category_id, auto_assigned=False)

    # Act: Set to Unassigned (empty string or "null")
    response = client.post(
        "/admin/golden-repos/test-repo/category",
        data={"category_id": "", "csrf_token": csrf_token},  # Empty string means Unassigned
        cookies=admin_session_cookie,
    )

    # Assert: Success response
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text[:200]}"

    # Verify database update
    repo = backend.get_repo("test-repo")
    assert repo["category_id"] is None


def test_update_repo_category_nonexistent_repo_returns_error(client, admin_session_cookie, csrf_token, temp_db):
    """Test updating category for non-existent repo returns error."""
    # Get the actual database path used by the app
    app_db_path = str(Path(temp_db).parent / "data" / "cidx_server.db")

    # Setup: Create category but no repo
    service = RepoCategoryService(app_db_path)
    category_id = service.create_category("Backend", "^api-.*")

    # Act: Try to update non-existent repo
    response = client.post(
        "/admin/golden-repos/nonexistent-repo/category",
        data={"category_id": str(category_id), "csrf_token": csrf_token},
        cookies=admin_session_cookie,
    )

    # Assert: Error response (400 or 404)
    assert response.status_code in [400, 404, 500]


def test_update_repo_category_invalid_category_id_returns_error(client, admin_session_cookie, csrf_token, temp_db):
    """Test updating repo with invalid category_id returns error."""
    # Get the actual database path used by the app
    app_db_path = str(Path(temp_db).parent / "data" / "cidx_server.db")

    # Setup: Create repo but no category with id 999
    backend = GoldenRepoMetadataSqliteBackend(app_db_path)
    backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # Act: Try to update with invalid category
    response = client.post(
        "/admin/golden-repos/test-repo/category",
        data={"category_id": "999", "csrf_token": csrf_token},
        cookies=admin_session_cookie,
    )

    # Assert: Error response
    assert response.status_code in [400, 500]
