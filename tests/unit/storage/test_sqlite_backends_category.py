"""
Tests for GoldenRepoMetadataSqliteBackend category methods (Story #181).

Test coverage:
- update_category() - Update repo category assignment
- list_repos_with_categories() - List repos with category info
"""

import pytest
import tempfile
import os
import shutil

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.server.storage.repo_category_backend import RepoCategorySqliteBackend


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")

    schema = DatabaseSchema(db_path)
    schema.initialize_database()

    yield db_path

    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


@pytest.fixture
def backend(temp_db):
    """Create a GoldenRepoMetadataSqliteBackend instance."""
    return GoldenRepoMetadataSqliteBackend(temp_db)


@pytest.fixture
def category_backend(temp_db):
    """Create a RepoCategorySqliteBackend instance."""
    return RepoCategorySqliteBackend(temp_db)


class TestUpdateCategory:
    """Test update_category() method."""

    def test_update_category_sets_category_id(self, backend, category_backend):
        """update_category sets category_id for a repo."""
        # Create a category first (to satisfy foreign key)
        cat_id = category_backend.create_category("TestCat", r".*", 1)

        # Add a repo
        backend.add_repo(
            "test-repo", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )

        # Update category
        backend.update_category("test-repo", category_id=cat_id, auto_assigned=True)

        # Verify update
        repo = backend.get_repo("test-repo")
        assert repo["category_id"] == cat_id
        assert repo["category_auto_assigned"] == True

    def test_update_category_sets_auto_assigned_flag(self, backend, category_backend):
        """update_category sets category_auto_assigned flag correctly."""
        # Create a category first
        cat_id = category_backend.create_category("TestCat", r".*", 1)

        backend.add_repo(
            "test-repo", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )

        # Update with manual assignment
        backend.update_category("test-repo", category_id=cat_id, auto_assigned=False)

        repo = backend.get_repo("test-repo")
        assert repo["category_id"] == cat_id
        assert repo["category_auto_assigned"] == False

    def test_update_category_can_set_null(self, backend, category_backend):
        """update_category can set category_id to NULL (Unassigned)."""
        # Create a category first
        cat_id = category_backend.create_category("TestCat", r".*", 1)

        backend.add_repo(
            "test-repo", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )

        # First assign a category
        backend.update_category("test-repo", category_id=cat_id, auto_assigned=True)

        # Then set to NULL
        backend.update_category("test-repo", category_id=None, auto_assigned=True)

        repo = backend.get_repo("test-repo")
        assert repo["category_id"] is None
        assert repo["category_auto_assigned"] == True


class TestListReposWithCategories:
    """Test list_repos_with_categories() method."""

    def test_list_empty_returns_empty_list(self, backend):
        """list_repos_with_categories returns empty list when no repos."""
        result = backend.list_repos_with_categories()
        assert result == []

    def test_list_includes_category_fields(self, backend, category_backend):
        """list_repos_with_categories includes category_id and category_auto_assigned."""
        # Create a category first
        cat_id = category_backend.create_category("TestCat", r".*", 1)

        backend.add_repo(
            "test-repo", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )
        backend.update_category("test-repo", category_id=cat_id, auto_assigned=True)

        repos = backend.list_repos_with_categories()

        assert len(repos) == 1
        assert repos[0]["alias"] == "test-repo"
        assert repos[0]["category_id"] == cat_id
        assert repos[0]["category_auto_assigned"] == True

    def test_list_shows_null_category_as_none(self, backend):
        """list_repos_with_categories shows NULL category_id as None."""
        backend.add_repo(
            "test-repo", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )

        repos = backend.list_repos_with_categories()

        assert repos[0]["category_id"] is None
        assert repos[0]["category_auto_assigned"] == False
