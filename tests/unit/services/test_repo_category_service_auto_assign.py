"""
Tests for RepoCategoryService.auto_assign() method (Story #181 AC1, AC2, AC5).

Test coverage:
- AC1: Patterns evaluated in priority order, first match wins
- AC2: No match returns None (Unassigned)
- AC5: Regex matched against alias (not repo_url or clone_path)
"""

import pytest
import tempfile
import os
import shutil

from code_indexer.server.services.repo_category_service import RepoCategoryService
from code_indexer.server.storage.database_manager import DatabaseSchema


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


class TestAutoAssignBasic:
    """Test basic auto-assignment functionality."""

    def test_no_categories_returns_none(self, service):
        """When no categories exist, auto_assign returns None."""
        result = service.auto_assign("any-repo-alias")
        assert result is None

    def test_no_match_returns_none(self, service):
        """When no pattern matches, auto_assign returns None (AC2)."""
        # Create categories that won't match
        service.create_category("Python", r"^python-.*")
        service.create_category("Java", r"^java-.*")

        # Try alias that doesn't match any pattern
        result = service.auto_assign("golang-myproject")
        assert result is None

    def test_single_matching_category(self, service):
        """When one category matches, returns its ID."""
        cat_id = service.create_category("Python", r"^python-.*")

        result = service.auto_assign("python-myproject")
        assert result == cat_id

    def test_first_match_wins_priority_order(self, service):
        """When multiple patterns match, first by priority wins (AC1)."""
        # Create categories in reverse order so most specific gets priority 1
        # (new categories are inserted at priority 1, pushing others down)
        cat3_id = service.create_category("All", r".*")
        cat2_id = service.create_category("General", r"^my-.*")
        cat1_id = service.create_category("Specific", r"^my-special-.*")

        # Should match cat1 (priority 1, created last) even though all three match
        result = service.auto_assign("my-special-project")
        assert result == cat1_id

        # Should match cat2 (priority 2) when cat1 doesn't match
        result = service.auto_assign("my-other-project")
        assert result == cat2_id

        # Should match cat3 (priority 3) when neither cat1 nor cat2 match
        result = service.auto_assign("anything-else")
        assert result == cat3_id
