"""
PostgreSQL backend for repository category management storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for RepoCategorySqliteBackend satisfying the
RepoCategoryBackend protocol.
Uses psycopg v3 sync mode with a connection pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .pg_utils import sanitize_row

logger = logging.getLogger(__name__)


class RepoCategoryPostgresBackend:
    """
    PostgreSQL backend for repository category management.

    Satisfies the RepoCategoryBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def create_category(self, name: str, pattern: str, priority: int) -> int:
        """
        Create a new repository category.

        Returns:
            ID of the newly created category.

        Raises:
            psycopg.errors.UniqueViolation: If category name already exists.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """INSERT INTO repo_categories (name, pattern, priority, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (name, pattern, priority, now, now),
            )
            row = cursor.fetchone()
        category_id: int = row[0]
        logger.info(f"Created category: {name} (id={category_id}, priority={priority})")
        return category_id

    def list_categories(self) -> List[Dict[str, Any]]:
        """List all repository categories ordered by priority ASC."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT id, name, pattern, priority, created_at, updated_at
                   FROM repo_categories
                   ORDER BY priority ASC"""
            )
            rows = cursor.fetchall()
        return [
            sanitize_row(
                {
                    "id": row[0],
                    "name": row[1],
                    "pattern": row[2],
                    "priority": row[3],
                    "created_at": row[4],
                    "updated_at": row[5],
                }
            )
            for row in rows
        ]

    def get_category(self, category_id: int) -> Optional[Dict[str, Any]]:
        """Get category details by ID."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT id, name, pattern, priority, created_at, updated_at
                   FROM repo_categories WHERE id = %s""",
                (category_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return sanitize_row(
            {
                "id": row[0],
                "name": row[1],
                "pattern": row[2],
                "priority": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
        )

    def update_category(self, category_id: int, name: str, pattern: str) -> None:
        """
        Update category name and pattern.

        Raises:
            ValueError: If no category with the given ID exists.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """UPDATE repo_categories
                   SET name = %s, pattern = %s, updated_at = %s
                   WHERE id = %s""",
                (name, pattern, now, category_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Category with id={category_id} not found")
        logger.info(f"Updated category: {name} (id={category_id})")

    def delete_category(self, category_id: int) -> None:
        """
        Delete a category.

        Raises:
            ValueError: If no category with the given ID exists.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM repo_categories WHERE id = %s", (category_id,)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Category with id={category_id} not found")
        logger.info(f"Deleted category id={category_id}")

    def reorder_categories(self, ordered_ids: List[int]) -> None:
        """Reorder categories by reassigning priorities atomically."""
        with self._pool.connection() as conn:
            for new_priority, cat_id in enumerate(ordered_ids, start=1):
                conn.execute(
                    "UPDATE repo_categories SET priority = %s WHERE id = %s",
                    (new_priority, cat_id),
                )
        logger.info(f"Reordered {len(ordered_ids)} categories")

    def shift_all_priorities(self) -> None:
        """Shift all existing category priorities down by 1 (increment by 1)."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE repo_categories SET priority = priority + 1")

    def get_next_priority(self) -> int:
        """Get the next available priority value (max + 1, or 1 if empty)."""
        with self._pool.connection() as conn:
            cursor = conn.execute("SELECT MAX(priority) FROM repo_categories")
            row = cursor.fetchone()
        max_priority = row[0] if row is not None and row[0] is not None else 0
        return max_priority + 1

    def list_golden_repos(self) -> list:
        """Query golden_repos_metadata for category evaluation.

        Used by RepoCategoryService.re_evaluate_all() and auto_assign_category()
        instead of reaching into a separate SQLite backend.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT alias, repo_url, default_branch FROM golden_repos_metadata"
            ).fetchall()
        return [
            sanitize_row({"alias": r[0], "repo_url": r[1], "default_branch": r[2]})
            for r in rows
        ]

    def get_repo_category_map(self) -> Dict[str, Dict[str, Any]]:
        """
        Get mapping of all golden repo aliases to their category information.

        Returns:
            Dict mapping alias -> {category_id, category_name, priority}.
            For repos without a category, all category fields are None.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT grm.alias, rc.id, rc.name, rc.priority
                   FROM golden_repos_metadata grm
                   LEFT JOIN repo_categories rc ON grm.category_id = rc.id
                   ORDER BY grm.alias"""
            )
            rows = cursor.fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            result[row[0]] = {
                "category_id": row[1],
                "category_name": row[2],
                "priority": row[3],
            }
        return result

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
