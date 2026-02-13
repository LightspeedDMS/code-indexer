"""
Tests for RepoCategoryService.bulk_re_evaluate() method (Story #181 AC3, AC4).

Test coverage:
- AC3: Re-evaluate all repos, re-running regex matching
- AC4: Respects manual overrides (category_auto_assigned=0)
"""

import pytest
import tempfile
import os
import shutil

from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    # Create temp dir with proper permissions
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")

    # Initialize database schema
    schema = DatabaseSchema(db_path)
    schema.initialize_database()

    yield db_path

    # Cleanup (use shutil.rmtree to handle WAL files)
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


@pytest.fixture
def service(temp_db):
    """Create a RepoCategoryService instance with temp database."""
    return RepoCategoryService(temp_db)


@pytest.fixture
def repo_backend(temp_db):
    """Create a GoldenRepoMetadataSqliteBackend instance."""
    return GoldenRepoMetadataSqliteBackend(temp_db)


class TestBulkReEvaluateBasic:
    """Test basic bulk re-evaluation functionality."""

    def test_no_repos_returns_zero_updated(self, service):
        """When no repos exist, bulk_re_evaluate returns 0 updated (AC3)."""
        result = service.bulk_re_evaluate()

        assert "updated" in result
        assert result["updated"] == 0

    def test_no_categories_leaves_repos_unassigned(self, service, repo_backend):
        """When no categories exist, all repos stay Unassigned (AC3)."""
        # Add repos without categories
        repo_backend.add_repo(
            "test-repo-1", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )
        repo_backend.add_repo(
            "test-repo-2", "http://test.git", "main", "/path/2", "2024-01-01T00:00:00Z"
        )

        result = service.bulk_re_evaluate()

        assert result["updated"] == 0

        # Verify repos remain Unassigned
        repos = repo_backend.list_repos_with_categories()
        assert all(r.get("category_id") is None for r in repos)

    def test_re_evaluates_unassigned_repos(self, service, repo_backend):
        """Re-evaluation assigns categories to previously Unassigned repos (AC3)."""
        # Create category
        cat_id = service.create_category("Python", r"^python-.*")

        # Add unassigned repo that matches pattern
        repo_backend.add_repo(
            "python-myproject", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )

        result = service.bulk_re_evaluate()

        assert result["updated"] == 1

        # Verify repo was assigned
        repos = repo_backend.list_repos_with_categories()
        assert repos[0]["category_id"] == cat_id
        assert repos[0]["category_auto_assigned"] == True

    def test_respects_manual_overrides(self, service, repo_backend):
        """Re-evaluation skips repos with manual category assignment (AC4)."""
        # Create categories
        cat1_id = service.create_category("Python", r"^python-.*")
        cat2_id = service.create_category("Java", r"^java-.*")

        # Add repo with manual override (category_auto_assigned=0)
        repo_backend.add_repo(
            "python-myproject", "http://test.git", "main", "/path/1", "2024-01-01T00:00:00Z"
        )
        # Manually assign to Java category
        repo_backend.update_category("python-myproject", cat2_id, auto_assigned=False)

        result = service.bulk_re_evaluate()

        # Should not update - manual override respected
        assert result["updated"] == 0

        # Verify repo still assigned to Java (manual override preserved)
        repos = repo_backend.list_repos_with_categories()
        assert repos[0]["category_id"] == cat2_id
        assert repos[0]["category_auto_assigned"] == False
