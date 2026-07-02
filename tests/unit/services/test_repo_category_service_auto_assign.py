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

pytestmark = pytest.mark.slow


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


class TestAutoAssignUrlMatching:
    """Test Story #622: regex patterns match against both alias and repo_url."""

    def test_url_only_match_assigns_category(self, service):
        """Pattern matches repo URL when alias does not match (Story #622 AC1)."""
        cat_id = service.create_category(
            "Backend Team", r".*github\.com:backend-team/.*"
        )

        # alias does NOT match, but URL does
        result = service.auto_assign(
            "my-api", repo_url="git@github.com:backend-team/my-api.git"
        )
        assert result == cat_id

    def test_alias_only_match_assigns_category(self, service):
        """Pattern matches alias when URL does not match (Story #622 AC2 backward compat)."""
        cat_id = service.create_category("Python", r"^python-.*")

        # alias matches, URL does not contain python pattern
        result = service.auto_assign(
            "python-myproject",
            repo_url="git@github.com:other-team/python-myproject.git",
        )
        assert result == cat_id

    def test_both_match_priority_wins(self, service):
        """When both alias and URL can match, highest priority category wins (Story #622 AC4)."""
        # cat2 inserted first (ends up at lower priority), cat1 inserted second (priority 1)
        _ = service.create_category("URL Match", r".*github\.com:backend-team/.*")
        cat1_id = service.create_category("Alias Match", r"^my-api.*")

        # cat1 has priority 1 (highest), cat2 has priority 2
        # alias matches cat1, URL matches cat2 - cat1 wins due to higher priority
        result = service.auto_assign(
            "my-api-service", repo_url="git@github.com:backend-team/my-api-service.git"
        )
        assert result == cat1_id

    def test_neither_match_returns_none(self, service):
        """Pattern matches neither alias nor URL - repo NOT assigned (Story #622 AC3)."""
        service.create_category("Backend Team", r".*github\.com:backend-team/.*")
        service.create_category("Python", r"^python-.*")

        result = service.auto_assign(
            "my-api", repo_url="git@github.com:frontend-team/my-api.git"
        )
        assert result is None

    def test_backward_compat_no_repo_url(self, service):
        """Calling auto_assign without repo_url still works (Story #622 AC5)."""
        cat_id = service.create_category("Python", r"^python-.*")

        # No repo_url - should still match alias
        result = service.auto_assign("python-myproject")
        assert result == cat_id
