"""
SQLite backend for global repository registry.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


def _row_to_repo_dict(row: Any) -> Dict[str, Any]:
    """Map a global_repos row tuple to its dict representation (shared shape)."""
    return {
        "alias_name": row[0],
        "repo_name": row[1],
        "repo_url": row[2],
        "index_path": row[3],
        "created_at": row[4],
        "last_refresh": row[5],
        "enable_temporal": bool(row[6]),
        "temporal_options": json.loads(row[7]) if row[7] else None,
        "enable_scip": bool(row[8]),
        "next_refresh": row[9],
    }


class GlobalReposSqliteBackend:
    """
    SQLite backend for global repository registry.

    Replaces global_registry.json with atomic SQLite operations,
    eliminating race conditions from concurrent instances.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None:
        """
        Register a new repository or update existing one.

        Args:
            alias_name: Unique alias for the repository (primary key).
            repo_name: Name of the repository.
            repo_url: Optional URL of the repository.
            index_path: Path to the repository index.
            enable_temporal: Whether temporal indexing is enabled.
            temporal_options: Optional temporal indexing options (stored as JSON).
            enable_scip: Whether SCIP code intelligence indexing is enabled.
        """
        now = datetime.now(timezone.utc).isoformat()
        temporal_json = json.dumps(temporal_options) if temporal_options else None

        def operation(conn):
            conn.execute(
                """INSERT INTO global_repos
                   (alias_name, repo_name, repo_url, index_path, created_at,
                    last_refresh, enable_temporal, temporal_options, enable_scip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(alias_name) DO UPDATE SET
                    repo_name = excluded.repo_name,
                    repo_url = excluded.repo_url,
                    index_path = excluded.index_path,
                    last_refresh = excluded.last_refresh,
                    enable_temporal = excluded.enable_temporal,
                    temporal_options = excluded.temporal_options,
                    enable_scip = excluded.enable_scip""",
                (
                    alias_name,
                    repo_name,
                    repo_url,
                    index_path,
                    now,
                    now,
                    enable_temporal,
                    temporal_json,
                    enable_scip,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Registered repo: {alias_name}")

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        """
        Get repository details by alias.

        Args:
            alias_name: Alias of the repository to retrieve.

        Returns:
            Dictionary with repository details, or None if not found.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias_name, repo_name, repo_url, index_path, created_at,
                      last_refresh, enable_temporal, temporal_options, enable_scip,
                      next_refresh
               FROM global_repos WHERE alias_name = ?""",
            (alias_name,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return _row_to_repo_dict(row)

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        """
        List all registered repositories.

        Returns:
            Dictionary mapping alias names to repository details.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias_name, repo_name, repo_url, index_path, created_at,
                      last_refresh, enable_temporal, temporal_options, enable_scip,
                      next_refresh
               FROM global_repos"""
        )

        result = {}
        for row in cursor.fetchall():
            result[row[0]] = _row_to_repo_dict(row)

        return result

    def list_due_repos(self, limit: int, now: float) -> list:
        """
        Return repos whose next_refresh is due (i.e. <= now), oldest-first, capped.

        Bug #1063 Part 1: enables capped oldest-first due-query so the
        RefreshScheduler never submits more than `limit` repos in one poll cycle.

        Ordering uses CAST(next_refresh AS REAL) to ensure numeric comparison;
        without the cast, TEXT column ordering is lexicographic and would produce
        wrong results when timestamps differ in leading digit length.

        Args:
            limit: Maximum number of repos to return (0 = empty list).
            now: Current Unix timestamp (float); repos with next_refresh <= now are due.

        Returns:
            List of repo dicts ordered by next_refresh ASC (oldest first).
        """
        if limit <= 0:
            return []

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias_name, repo_name, repo_url, index_path, created_at,
                      last_refresh, enable_temporal, temporal_options, enable_scip,
                      next_refresh
               FROM global_repos
               WHERE next_refresh IS NOT NULL
                 AND CAST(next_refresh AS REAL) <= ?
               ORDER BY CAST(next_refresh AS REAL) ASC
               LIMIT ?""",
            (now, limit),
        )

        return [_row_to_repo_dict(row) for row in cursor.fetchall()]

    def delete_repo(self, alias_name: str) -> bool:
        """
        Delete a repository by alias.

        Args:
            alias_name: Alias of the repository to delete.

        Returns:
            True if a record was deleted, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM global_repos WHERE alias_name = ?",
                (alias_name,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted repo: {alias_name}")
        return deleted

    def update_last_refresh(self, alias_name: str) -> bool:
        """
        Update the last_refresh timestamp for a repository.

        Args:
            alias_name: Alias of the repository to update.

        Returns:
            True if record was updated, False if not found.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET last_refresh = ? WHERE alias_name = ?",
                (now, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated last_refresh for repo: {alias_name}")
        return updated

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> bool:
        """
        Update the enable_temporal flag for a repository.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            enable_temporal: New value for enable_temporal flag

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET enable_temporal = ? WHERE alias_name = ?",
                (1 if enable_temporal else 0, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(
                f"Updated enable_temporal={enable_temporal} for repo: {alias_name}"
            )
        return updated

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool:
        """
        Update the enable_scip flag for a repository.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            enable_scip: New value for enable_scip flag

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET enable_scip = ? WHERE alias_name = ?",
                (1 if enable_scip else 0, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated enable_scip={enable_scip} for repo: {alias_name}")
        return updated

    def update_next_refresh(self, alias_name: str, next_refresh: Optional[str]) -> bool:
        """
        Update the next_refresh timestamp for a repository.

        Story #284: Back-propagating jitter scheduling.

        Args:
            alias_name: Alias of the repository to update (with -global suffix)
            next_refresh: Unix timestamp as string, or None to clear

        Returns:
            True if record was updated, False if not found.
        """

        def operation(conn):
            cursor = conn.execute(
                "UPDATE global_repos SET next_refresh = ? WHERE alias_name = ?",
                (next_refresh, alias_name),
            )
            return cursor.rowcount > 0

        updated: bool = self._conn_manager.execute_atomic(operation)
        if updated:
            logger.debug(f"Updated next_refresh for repo: {alias_name}")
        return updated

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
