"""
Tests for RepoCategoryService.

Story #180: Repository Category CRUD and Management UI
Tests service layer validation for AC7 (validation requirements).
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.services.repo_category_service import RepoCategoryService


@pytest.fixture
def temp_db():
    """Create a temporary database with schema initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()
        yield str(db_path)


@pytest.fixture
def service(temp_db):
    """Create RepoCategoryService instance."""
    return RepoCategoryService(temp_db)


class TestCreateWithValidation:
    """Test create with validation (AC7)."""

    def test_create_assigns_priority_1_to_new_category(self, service):
        """New categories should be created at priority 1 (highest precedence)."""
        service.create_category("First", "^first-.*")
        service.create_category("Second", "^second-.*")
        categories = service.list_categories()
        # Second should be at priority 1, First shifted to priority 2
        assert categories[0]["name"] == "Second"
        assert categories[0]["priority"] == 1
        assert categories[1]["name"] == "First"
        assert categories[1]["priority"] == 2

    def test_create_with_valid_regex_succeeds(self, service):
        """Test that create with valid regex pattern succeeds."""
        category_id = service.create_category("Backend", "^backend-.*")

        assert isinstance(category_id, int)
        assert category_id > 0

    def test_create_with_invalid_regex_raises_value_error(self, service):
        """Test that create with invalid regex raises ValueError (AC7)."""
        with pytest.raises(ValueError) as exc_info:
            service.create_category("Backend", "[unclosed")

        assert "regex" in str(exc_info.value).lower() or "pattern" in str(exc_info.value).lower()

    def test_create_with_pattern_exceeding_500_chars_raises_value_error(self, service):
        """Test that create with pattern > 500 chars raises ValueError (AC7)."""
        long_pattern = "a" * 501

        with pytest.raises(ValueError) as exc_info:
            service.create_category("Backend", long_pattern)

        assert "500" in str(exc_info.value) or "long" in str(exc_info.value).lower()

    def test_create_with_duplicate_name_raises_appropriate_error(self, service):
        """Test that create with duplicate name raises appropriate error (AC7)."""
        service.create_category("Backend", "^backend-.*")

        # IntegrityError from backend should bubble up or be converted
        with pytest.raises((sqlite3.IntegrityError, ValueError)):
            service.create_category("Backend", "^other-.*")


class TestUpdateWithValidation:
    """Test update with validation (AC7)."""

    def test_update_with_valid_regex_succeeds(self, service):
        """Test that update with valid regex pattern succeeds."""
        category_id = service.create_category("Backend", "^backend-.*")

        # Should not raise
        service.update_category(category_id, "Backend Services", "^backend.*")

    def test_update_with_invalid_regex_raises_value_error(self, service):
        """Test that update with invalid regex raises ValueError (AC7)."""
        category_id = service.create_category("Backend", "^backend-.*")

        with pytest.raises(ValueError) as exc_info:
            service.update_category(category_id, "Backend", "(unclosed")

        assert "regex" in str(exc_info.value).lower() or "pattern" in str(exc_info.value).lower()

    def test_update_with_pattern_exceeding_500_chars_raises_value_error(self, service):
        """Test that update with pattern > 500 chars raises ValueError (AC7)."""
        category_id = service.create_category("Backend", "^backend-.*")
        long_pattern = "b" * 501

        with pytest.raises(ValueError) as exc_info:
            service.update_category(category_id, "Backend", long_pattern)

        assert "500" in str(exc_info.value) or "long" in str(exc_info.value).lower()


class TestListAndDelegate:
    """Test that service delegates list operations to backend."""

    def test_list_returns_categories_in_priority_order(self, service):
        """Test that list returns categories in priority order."""
        service.create_category("Third", "^third-.*")
        service.create_category("First", "^first-.*")
        service.create_category("Second", "^second-.*")

        categories = service.list_categories()

        # Should be ordered by priority (newest categories get priority 1)
        assert len(categories) == 3
        assert categories[0]["name"] == "Second"
        assert categories[0]["priority"] == 1
        assert categories[1]["name"] == "First"
        assert categories[1]["priority"] == 2
        assert categories[2]["name"] == "Third"
        assert categories[2]["priority"] == 3


class TestDeleteDelegate:
    """Test that service delegates delete to backend."""

    def test_delete_removes_category(self, service):
        """Test that delete removes category."""
        category_id = service.create_category("Backend", "^backend-.*")

        service.delete_category(category_id)

        categories = service.list_categories()
        assert len(categories) == 0
