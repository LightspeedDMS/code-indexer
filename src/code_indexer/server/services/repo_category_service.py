"""
Service layer for repository category management (Story #180).

Provides validation and business logic on top of RepoCategorySqliteBackend.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from ..storage.repo_category_backend import RepoCategorySqliteBackend

logger = logging.getLogger(__name__)


class RepoCategoryService:
    """
    Service for managing repository categories with validation.

    Validates regex patterns and enforces business rules before delegating
    to the SQLite backend.
    """

    MAX_PATTERN_LENGTH = 500

    def __init__(self, db_path: str) -> None:
        """
        Initialize the service.

        Args:
            db_path: Path to SQLite database file.
        """
        self._backend = RepoCategorySqliteBackend(db_path)

    def create_category(self, name: str, pattern: str) -> int:
        """
        Create a new repository category with validation.

        Args:
            name: Unique category name.
            pattern: Regex pattern for auto-assignment.

        Returns:
            ID of the newly created category.

        Raises:
            ValueError: If pattern is invalid regex or exceeds max length.
            sqlite3.IntegrityError: If category name already exists.
        """
        self._validate_pattern(pattern)

        # Shift all existing priorities down to make room at priority 1
        self._backend.shift_all_priorities()

        # Create new category at priority 1 (highest precedence)
        return self._backend.create_category(name, pattern, 1)

    def update_category(self, category_id: int, name: str, pattern: str) -> None:
        """
        Update category name and pattern with validation.

        Args:
            category_id: ID of the category to update.
            name: New category name.
            pattern: New regex pattern.

        Raises:
            ValueError: If pattern is invalid regex or exceeds max length.
            sqlite3.IntegrityError: If new name conflicts with existing category.
        """
        self._validate_pattern(pattern)
        self._backend.update_category(category_id, name, pattern)

    def delete_category(self, category_id: int) -> None:
        """
        Delete a category.

        Args:
            category_id: ID of the category to delete.
        """
        self._backend.delete_category(category_id)

    def get_category(self, category_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a single category by ID.

        Args:
            category_id: ID of the category to retrieve.

        Returns:
            Category dictionary if found, None otherwise.
        """
        return self._backend.get_category(category_id)

    def list_categories(self) -> List[Dict[str, Any]]:
        """
        List all repository categories ordered by priority.

        Returns:
            List of category dictionaries ordered by priority ASC.
        """
        return self._backend.list_categories()

    def reorder_categories(self, ordered_ids: List[int]) -> None:
        """
        Reorder categories by reassigning priorities.

        Args:
            ordered_ids: List of category IDs in desired order.
                        First ID gets priority 1, second gets priority 2, etc.

        Raises:
            ValueError: If ordered_ids does not contain exactly all existing category IDs.
        """
        current = self._backend.list_categories()
        current_ids = set(cat["id"] for cat in current)
        submitted_ids = set(ordered_ids)
        if current_ids != submitted_ids:
            raise ValueError("Reorder list must contain exactly all existing category IDs")
        self._backend.reorder_categories(ordered_ids)

    def auto_assign(self, alias: str) -> Optional[int]:
        """
        Auto-assign a category to a repository based on pattern matching.

        Evaluates category regex patterns in priority order (ascending).
        Uses re.match() which anchors at the start of the string.
        First matching pattern wins.

        Args:
            alias: Repository alias to match against patterns.

        Returns:
            Category ID if match found, None otherwise (Unassigned).
        """
        # Get all categories ordered by priority
        categories = self._backend.list_categories()

        # Try each pattern in priority order
        for category in categories:
            pattern = category["pattern"]
            try:
                if re.match(pattern, alias):
                    logger.debug(
                        f"Auto-assigned alias '{alias}' to category '{category['name']}' "
                        f"(id={category['id']}, priority={category['priority']})"
                    )
                    return category["id"]
            except re.error as e:
                # Pattern should have been validated on creation, but log if somehow invalid
                logger.warning(
                    f"Invalid regex pattern in category '{category['name']}' "
                    f"(id={category['id']}): {e}"
                )
                continue

        # No match found
        logger.debug(f"No category match for alias '{alias}' - leaving Unassigned")
        return None

    def bulk_re_evaluate(self) -> Dict[str, Any]:
        """
        Re-evaluate all repository category assignments (Story #181 AC3, AC4).

        Re-runs auto-assignment logic on all repositories, respecting manual overrides.
        Only re-assigns repos that are:
        - Currently Unassigned (category_id = NULL), OR
        - Auto-assigned (category_auto_assigned = True)

        Skips repos with manual category assignment (category_auto_assigned = False).

        Returns:
            Dictionary with re-evaluation statistics:
            - updated: Number of repos with changed category assignments
            - errors: List of error messages (if any)
        """
        from ..storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

        # Get backend instance - assumes same db_path as category backend
        repo_backend = GoldenRepoMetadataSqliteBackend(self._backend._conn_manager.db_path)

        # Get all repos with category information
        repos = repo_backend.list_repos_with_categories()

        updated_count = 0
        errors = []

        for repo in repos:
            alias = repo["alias"]
            current_category_id = repo.get("category_id")
            is_auto_assigned = repo.get("category_auto_assigned", False)

            # Skip manual overrides (AC4)
            if current_category_id is not None and not is_auto_assigned:
                logger.debug(
                    f"Skipping '{alias}' - manual override (category_id={current_category_id})"
                )
                continue

            # Re-evaluate assignment
            try:
                new_category_id = self.auto_assign(alias)

                # Update if category changed
                if new_category_id != current_category_id:
                    repo_backend.update_category(alias, new_category_id, auto_assigned=True)
                    updated_count += 1
                    logger.info(
                        f"Re-evaluated '{alias}': {current_category_id} -> {new_category_id}"
                    )

            except Exception as e:
                error_msg = f"Failed to re-evaluate '{alias}': {e}"
                logger.warning(error_msg)
                errors.append(error_msg)

        logger.info(f"Bulk re-evaluation complete: {updated_count} repos updated")
        return {"updated": updated_count, "errors": errors}

    def get_repo_category_map(self) -> Dict[str, Dict[str, Any]]:
        """
        Get mapping of all golden repo aliases to their category information (Story #182).

        Provides efficient lookup for MCP and REST API responses.
        Uses a single JOIN query for optimal performance.

        Returns:
            Dict mapping alias -> {category_name, priority}
            For repos without a category, category_name and priority are None.

        Example:
            {
                "api-gateway": {"category_name": "Backend", "priority": 1},
                "misc-tool": {"category_name": None, "priority": None}
            }
        """
        return self._backend.get_repo_category_map()


    def update_repo_category(self, alias: str, category_id: Optional[int], auto_assigned: bool = False) -> None:
        """
        Update a golden repository's category assignment (Story #183).

        This method is used for both manual category overrides (from the Web UI)
        and programmatic updates. The auto_assigned flag distinguishes between
        manual admin actions and automatic category assignment.

        Args:
            alias: Golden repository alias to update.
            category_id: Category ID to assign, or None for Unassigned.
            auto_assigned: False for manual override (Web UI), True for auto-assignment.

        Raises:
            Exception: If repo doesn't exist or category_id is invalid (foreign key violation).
        """
        from ..storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

        # Get backend instance - assumes same db_path as category backend
        repo_backend = GoldenRepoMetadataSqliteBackend(self._backend._conn_manager.db_path)

        # Delegate to backend's update_category method
        updated = repo_backend.update_category(alias, category_id, auto_assigned=auto_assigned)

        if not updated:
            raise ValueError(f"Repository '{alias}' not found")

        logger.info(
            f"Updated category for repo '{alias}': category_id={category_id}, "
            f"auto_assigned={auto_assigned}"
        )

    def _validate_pattern(self, pattern: str) -> None:
        """
        Validate regex pattern.

        Args:
            pattern: Regex pattern to validate.

        Raises:
            ValueError: If pattern is invalid regex or exceeds max length.
        """
        # Check length
        if len(pattern) > self.MAX_PATTERN_LENGTH:
            raise ValueError(
                f"Pattern too long (max {self.MAX_PATTERN_LENGTH} characters)"
            )

        # Validate regex compilation
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")
