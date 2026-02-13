"""
SQLite backend for repository categories (Story #180).

Provides CRUD operations for managing repository categories with atomic
SQLite transactions.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class RepoCategorySqliteBackend:
    """
    SQLite backend for repository category management.

    Provides atomic CRUD operations for categories stored in the repo_categories table.
    Categories are used to organize golden repositories and determine auto-assignment
    via regex pattern matching.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager(db_path)

    def create_category(self, name: str, pattern: str, priority: int) -> int:
        """
        Create a new repository category.

        Args:
            name: Unique category name.
            pattern: Regex pattern for auto-assignment.
            priority: Priority for pattern matching order (lower = higher priority).

        Returns:
            ID of the newly created category.

        Raises:
            sqlite3.IntegrityError: If category name already exists.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                """INSERT INTO repo_categories (name, pattern, priority, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, pattern, priority, now, now)
            )
            return cursor.lastrowid

        category_id = self._conn_manager.execute_atomic(operation)
        logger.info(f"Created category: {name} (id={category_id}, priority={priority})")
        return category_id

    def list_categories(self) -> List[Dict[str, Any]]:
        """
        List all repository categories ordered by priority.

        Returns:
            List of category dictionaries ordered by priority ASC.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT id, name, pattern, priority, created_at, updated_at
               FROM repo_categories
               ORDER BY priority ASC"""
        )

        result = []
        for row in cursor.fetchall():
            result.append({
                "id": row[0],
                "name": row[1],
                "pattern": row[2],
                "priority": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            })

        return result

    def get_category(self, category_id: int) -> Optional[Dict[str, Any]]:
        """
        Get category details by ID.

        Args:
            category_id: ID of the category to retrieve.

        Returns:
            Dictionary with category details, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT id, name, pattern, priority, created_at, updated_at
               FROM repo_categories
               WHERE id = ?""",
            (category_id,)
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "id": row[0],
            "name": row[1],
            "pattern": row[2],
            "priority": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def update_category(self, category_id: int, name: str, pattern: str) -> None:
        """
        Update category name and pattern.

        Args:
            category_id: ID of the category to update.
            name: New category name.
            pattern: New regex pattern.

        Raises:
            sqlite3.IntegrityError: If new name conflicts with existing category.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                """UPDATE repo_categories
                   SET name = ?, pattern = ?, updated_at = ?
                   WHERE id = ?""",
                (name, pattern, now, category_id)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Category with id={category_id} not found")
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Updated category: {name} (id={category_id})")

    def delete_category(self, category_id: int) -> None:
        """
        Delete a category.

        When deleted, all golden_repos_metadata rows with this category_id
        will have category_id set to NULL (ON DELETE SET NULL foreign key).

        Args:
            category_id: ID of the category to delete.
        """
        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM repo_categories WHERE id = ?",
                (category_id,)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Category with id={category_id} not found")
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Deleted category id={category_id}")

    def reorder_categories(self, ordered_ids: List[int]) -> None:
        """
        Reorder categories by reassigning priorities atomically.

        Args:
            ordered_ids: List of category IDs in desired order.
                        First ID gets priority 1, second gets priority 2, etc.
        """
        def operation(conn):
            for new_priority, category_id in enumerate(ordered_ids, start=1):
                conn.execute(
                    "UPDATE repo_categories SET priority = ? WHERE id = ?",
                    (new_priority, category_id)
                )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Reordered {len(ordered_ids)} categories")

    def shift_all_priorities(self) -> None:
        """
        Shift all existing category priorities down by 1 position (increment by 1).

        Used when inserting a new category at priority 1 to make room.
        """
        def operation(conn):
            conn.execute("UPDATE repo_categories SET priority = priority + 1")
            return None

        self._conn_manager.execute_atomic(operation)

    def get_next_priority(self) -> int:
        """
        Get the next available priority value.

        Returns:
            max(priority) + 1, or 1 if no categories exist.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute("SELECT MAX(priority) FROM repo_categories")
        row = cursor.fetchone()

        max_priority = row[0] if row[0] is not None else 0
        return max_priority + 1

    def get_repo_category_map(self) -> Dict[str, Dict[str, Any]]:
        """
        Get mapping of all golden repo aliases to their category information.

        Performs a single LEFT JOIN query for efficiency (Story #182).

        Returns:
            Dict mapping alias -> {category_id, category_name, priority}
            For repos without a category, all category fields are None.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT grm.alias, rc.id, rc.name, rc.priority
               FROM golden_repos_metadata grm
               LEFT JOIN repo_categories rc ON grm.category_id = rc.id
               ORDER BY grm.alias"""
        )

        result = {}
        for row in cursor.fetchall():
            alias = row[0]
            category_id = row[1]  # None if no category
            category_name = row[2]  # None if no category
            priority = row[3]  # None if no category

            result[alias] = {
                "category_id": category_id,
                "category_name": category_name,
                "priority": priority,
            }

        return result

    def close(self) -> None:
        """Close database connections (cleanup for tests)."""
        # DatabaseConnectionManager doesn't have explicit close, connections are thread-local
        pass
