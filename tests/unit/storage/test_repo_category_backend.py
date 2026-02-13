"""
Tests for RepoCategorySqliteBackend.

Story #180: Repository Category CRUD and Management UI
Tests backend CRUD operations for AC1-AC4.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.repo_category_backend import RepoCategorySqliteBackend


@pytest.fixture
def temp_db():
    """Create a temporary database with schema initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()
        yield str(db_path)


@pytest.fixture
def backend(temp_db):
    """Create RepoCategorySqliteBackend instance."""
    backend = RepoCategorySqliteBackend(temp_db)
    yield backend
    backend.close()


class TestCreateCategory:
    """Test create_category method (AC1)."""

    def test_create_category_returns_valid_id(self, backend):
        """Test that create_category returns a valid integer ID."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        assert isinstance(category_id, int)
        assert category_id > 0

    def test_create_category_persists_to_database(self, backend, temp_db):
        """Test that created category is persisted in database."""
        category_id = backend.create_category("Frontend", "^frontend-.*", 2)

        # Verify in database
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT name, pattern, priority FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "Frontend"
            assert row[1] == "^frontend-.*"
            assert row[2] == 2
        finally:
            conn.close()

    def test_create_category_sets_timestamps(self, backend, temp_db):
        """Test that created_at and updated_at are set."""
        category_id = backend.create_category("Test", "^test-.*", 1)

        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT created_at, updated_at FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            row = cursor.fetchone()
            assert row[0] is not None, "created_at should be set"
            assert row[1] is not None, "updated_at should be set"
        finally:
            conn.close()

    def test_create_category_with_duplicate_name_raises_error(self, backend):
        """Test that creating category with duplicate name raises error (AC7)."""
        backend.create_category("Backend", "^backend-.*", 1)

        with pytest.raises(sqlite3.IntegrityError):
            backend.create_category("Backend", "^other-.*", 2)


class TestListCategories:
    """Test list_categories method."""

    def test_list_categories_returns_empty_list_initially(self, backend):
        """Test that list_categories returns empty list when no categories exist."""
        categories = backend.list_categories()
        assert categories == []

    def test_list_categories_returns_all_categories(self, backend):
        """Test that list_categories returns all created categories."""
        backend.create_category("Backend", "^backend-.*", 1)
        backend.create_category("Frontend", "^frontend-.*", 2)
        backend.create_category("Mobile", "^mobile-.*", 3)

        categories = backend.list_categories()
        assert len(categories) == 3

        names = {cat["name"] for cat in categories}
        assert names == {"Backend", "Frontend", "Mobile"}

    def test_list_categories_ordered_by_priority_asc(self, backend):
        """Test that categories are returned ordered by priority ASC."""
        backend.create_category("Third", "^third-.*", 3)
        backend.create_category("First", "^first-.*", 1)
        backend.create_category("Second", "^second-.*", 2)

        categories = backend.list_categories()

        assert categories[0]["name"] == "First"
        assert categories[0]["priority"] == 1
        assert categories[1]["name"] == "Second"
        assert categories[1]["priority"] == 2
        assert categories[2]["name"] == "Third"
        assert categories[2]["priority"] == 3


class TestGetCategory:
    """Test get_category method."""

    def test_get_category_returns_existing_category(self, backend):
        """Test that get_category returns details for existing category."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        category = backend.get_category(category_id)

        assert category is not None
        assert category["id"] == category_id
        assert category["name"] == "Backend"
        assert category["pattern"] == "^backend-.*"
        assert category["priority"] == 1

    def test_get_category_returns_none_for_nonexistent(self, backend):
        """Test that get_category returns None for non-existent ID."""
        category = backend.get_category(9999)
        assert category is None


class TestUpdateCategory:
    """Test update_category method (AC2)."""

    def test_update_category_changes_name_and_pattern(self, backend, temp_db):
        """Test that update_category changes name and pattern."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        backend.update_category(category_id, "Backend Services", "^backend.*")

        # Verify in database
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT name, pattern FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            row = cursor.fetchone()
            assert row[0] == "Backend Services"
            assert row[1] == "^backend.*"
        finally:
            conn.close()

    def test_update_category_refreshes_updated_at(self, backend, temp_db):
        """Test that update_category refreshes updated_at timestamp (AC2)."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        # Get initial updated_at
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT updated_at FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            initial_updated_at = cursor.fetchone()[0]
        finally:
            conn.close()

        # Update category
        backend.update_category(category_id, "Backend Services", "^backend.*")

        # Verify updated_at changed
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT updated_at FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            new_updated_at = cursor.fetchone()[0]
            assert new_updated_at != initial_updated_at
        finally:
            conn.close()


class TestDeleteCategory:
    """Test delete_category method (AC4)."""

    def test_delete_category_removes_row(self, backend, temp_db):
        """Test that delete_category removes category from database (AC4)."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        backend.delete_category(category_id)

        # Verify removed
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            count = cursor.fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_delete_category_sets_golden_repos_category_id_to_null(self, backend, temp_db):
        """Test that deleting category sets golden_repos_metadata.category_id to NULL (AC4)."""
        category_id = backend.create_category("Backend", "^backend-.*", 1)

        # Create golden repo with this category
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """INSERT INTO golden_repos_metadata
                   (alias, repo_url, default_branch, clone_path, created_at, category_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("test-repo", "https://github.com/test/repo", "main", "/tmp/test", "2024-01-01T00:00:00Z", category_id)
            )
            conn.commit()
        finally:
            conn.close()

        # Delete category
        backend.delete_category(category_id)

        # Verify category_id is NULL (ON DELETE SET NULL)
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT category_id FROM golden_repos_metadata WHERE alias = ?",
                ("test-repo",)
            )
            row = cursor.fetchone()
            assert row[0] is None, "category_id should be NULL after category deletion"
        finally:
            conn.close()


class TestReorderCategories:
    """Test reorder_categories method (AC3)."""

    def test_reorder_categories_swaps_priorities_atomically(self, backend, temp_db):
        """Test that reorder_categories updates priorities atomically (AC3)."""
        id1 = backend.create_category("Backend", "^backend-.*", 1)
        id2 = backend.create_category("Frontend", "^frontend-.*", 2)
        id3 = backend.create_category("Mobile", "^mobile-.*", 3)

        # Reorder: Mobile (1), Backend (2), Frontend (3)
        backend.reorder_categories([id3, id1, id2])

        # Verify new priorities
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT id, priority FROM repo_categories ORDER BY priority"
            )
            rows = cursor.fetchall()
            assert rows[0] == (id3, 1), "Mobile should be priority 1"
            assert rows[1] == (id1, 2), "Backend should be priority 2"
            assert rows[2] == (id2, 3), "Frontend should be priority 3"
        finally:
            conn.close()

    def test_reorder_categories_maintains_order_consistency(self, backend):
        """Test that reordered categories maintain correct order."""
        id1 = backend.create_category("A", "^a-.*", 1)
        id2 = backend.create_category("B", "^b-.*", 2)
        id3 = backend.create_category("C", "^c-.*", 3)

        # Reorder: C, A, B
        backend.reorder_categories([id3, id1, id2])

        categories = backend.list_categories()
        assert categories[0]["name"] == "C"
        assert categories[1]["name"] == "A"
        assert categories[2]["name"] == "B"


class TestShiftAllPriorities:
    """Test shift_all_priorities method."""

    def test_shift_all_priorities_increments_existing(self, backend):
        """Shift all priorities up by 1."""
        backend.create_category("Cat1", "^cat1-.*", 1)
        backend.create_category("Cat2", "^cat2-.*", 2)
        backend.shift_all_priorities()
        categories = backend.list_categories()
        priorities = [c["priority"] for c in categories]
        assert priorities == [2, 3]


class TestGetNextPriority:
    """Test get_next_priority method."""

    def test_get_next_priority_returns_1_for_empty_table(self, backend):
        """Test that get_next_priority returns 1 when no categories exist."""
        next_priority = backend.get_next_priority()
        assert next_priority == 1

    def test_get_next_priority_returns_max_plus_one(self, backend):
        """Test that get_next_priority returns max(priority) + 1."""
        backend.create_category("First", "^first-.*", 1)
        backend.create_category("Second", "^second-.*", 2)
        backend.create_category("Third", "^third-.*", 3)

        next_priority = backend.get_next_priority()
        assert next_priority == 4
