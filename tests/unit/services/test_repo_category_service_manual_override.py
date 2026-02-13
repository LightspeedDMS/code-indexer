"""
Unit tests for RepoCategoryService.update_repo_category() (Story #183).

Tests manual category override functionality for golden repositories.
"""

import tempfile
from pathlib import Path

import pytest

from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db():
    """Create a temporary database with schema initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")

        # Initialize schema
        schema = DatabaseSchema(db_path)
        schema.initialize_database()

        yield db_path


@pytest.fixture
def category_service(temp_db):
    """Create RepoCategoryService instance."""
    return RepoCategoryService(temp_db)


@pytest.fixture
def repo_backend(temp_db):
    """Create GoldenRepoMetadataSqliteBackend instance."""
    return GoldenRepoMetadataSqliteBackend(temp_db)


def test_update_repo_category_sets_manual_override(category_service, repo_backend):
    """Test update_repo_category sets category_auto_assigned=False for manual override."""
    # Arrange: Create a category and a repo
    category_id = category_service.create_category("Backend", "^api-.*")

    repo_backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # Act: Update repo category with manual override
    category_service.update_repo_category("test-repo", category_id, auto_assigned=False)

    # Assert: Verify category_id and auto_assigned flag
    repo = repo_backend.get_repo("test-repo")
    assert repo is not None
    assert repo["category_id"] == category_id
    assert repo["category_auto_assigned"] is False


def test_update_repo_category_clears_category_with_none(category_service, repo_backend):
    """Test update_repo_category with None clears category (Unassigned)."""
    # Arrange: Create a category and a repo with that category
    category_id = category_service.create_category("Backend", "^api-.*")

    repo_backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # First assign a category
    category_service.update_repo_category("test-repo", category_id, auto_assigned=False)

    # Act: Clear category by setting to None
    category_service.update_repo_category("test-repo", None, auto_assigned=False)

    # Assert: Verify category_id is NULL
    repo = repo_backend.get_repo("test-repo")
    assert repo is not None
    assert repo["category_id"] is None
    assert repo["category_auto_assigned"] is False


def test_update_repo_category_preserves_auto_assigned_true(category_service, repo_backend):
    """Test update_repo_category can set auto_assigned=True for auto-assignment."""
    # Arrange: Create a category and a repo
    category_id = category_service.create_category("Backend", "^api-.*")

    repo_backend.add_repo(
        alias="api-gateway",
        repo_url="https://example.com/api-gateway.git",
        default_branch="main",
        clone_path="/tmp/api-gateway",
        created_at="2024-01-01T00:00:00Z",
    )

    # Act: Update with auto_assigned=True (simulating auto-assignment)
    category_service.update_repo_category("api-gateway", category_id, auto_assigned=True)

    # Assert: Verify auto_assigned is True
    repo = repo_backend.get_repo("api-gateway")
    assert repo is not None
    assert repo["category_id"] == category_id
    assert repo["category_auto_assigned"] is True


def test_update_repo_category_updates_existing_assignment(category_service, repo_backend):
    """Test update_repo_category can change category from one to another."""
    # Arrange: Create two categories and a repo
    backend_id = category_service.create_category("Backend", "^api-.*")
    frontend_id = category_service.create_category("Frontend", "^web-.*")

    repo_backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # First assign to Backend
    category_service.update_repo_category("test-repo", backend_id, auto_assigned=False)

    # Act: Change to Frontend
    category_service.update_repo_category("test-repo", frontend_id, auto_assigned=False)

    # Assert: Verify category changed
    repo = repo_backend.get_repo("test-repo")
    assert repo is not None
    assert repo["category_id"] == frontend_id
    assert repo["category_auto_assigned"] is False


def test_update_repo_category_nonexistent_repo_raises_error(category_service):
    """Test update_repo_category with non-existent repo raises appropriate error."""
    # Arrange: Create a category but no repo
    category_id = category_service.create_category("Backend", "^api-.*")

    # Act & Assert: Should raise error for non-existent repo
    with pytest.raises(Exception):  # Backend will raise specific error
        category_service.update_repo_category("nonexistent-repo", category_id, auto_assigned=False)


def test_update_repo_category_invalid_category_id_raises_error(category_service, repo_backend):
    """Test update_repo_category with invalid category_id raises error."""
    # Arrange: Create a repo but no category with id 999
    repo_backend.add_repo(
        alias="test-repo",
        repo_url="https://example.com/test-repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
    )

    # Act & Assert: Should raise error for non-existent category
    with pytest.raises(Exception):  # Backend will raise foreign key constraint error
        category_service.update_repo_category("test-repo", 999, auto_assigned=False)
