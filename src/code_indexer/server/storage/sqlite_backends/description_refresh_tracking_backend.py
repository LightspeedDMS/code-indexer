"""
SQLite backend for description refresh tracking (Story #190).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


def _row_to_tracking_dict(row: Any) -> Dict[str, Any]:
    """Map an 11-column description_refresh_tracking row to its dict shape."""
    return {
        "repo_alias": row[0],
        "last_run": row[1],
        "next_run": row[2],
        "status": row[3],
        "error": row[4],
        "last_known_commit": row[5],
        "last_known_files_processed": row[6],
        "last_known_indexed_at": row[7],
        "created_at": row[8],
        "updated_at": row[9],
        "lifecycle_schema_version": row[10],
    }


class DescriptionRefreshTrackingBackend:
    """
    SQLite backend for description refresh tracking (Story #190).

    Provides CRUD operations for the description_refresh_tracking table,
    which tracks when repositories need their descriptions regenerated.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def get_tracking_record(self, repo_alias: str) -> Optional[Dict[str, Any]]:
        """
        Get tracking record for a repository.

        Args:
            repo_alias: Alias of the repository

        Returns:
            Dictionary with tracking record, or None if not found
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at,
                      lifecycle_schema_version
               FROM description_refresh_tracking WHERE repo_alias = ?""",
            (repo_alias,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return _row_to_tracking_dict(row)

    def get_stale_repos(self, now_iso: str) -> List[Dict[str, Any]]:
        """
        Query repos where next_run <= now AND status != 'queued'.

        Args:
            now_iso: Current time in ISO 8601 format

        Returns:
            List of tracking records for stale repositories
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at,
                      lifecycle_schema_version
               FROM description_refresh_tracking
               WHERE next_run <= ? AND status != 'queued'""",
            (now_iso,),
        )

        return [_row_to_tracking_dict(row) for row in cursor.fetchall()]

    def upsert_tracking(self, repo_alias: str, **fields) -> None:
        """
        Insert or update tracking record.

        Args:
            repo_alias: Alias of the repository (primary key)
            **fields: Fields to set (last_run, next_run, status, error,
                     last_known_commit, last_known_files_processed,
                     last_known_indexed_at, created_at, updated_at)
        """
        # Build list of fields to set
        valid_fields = {
            "last_run",
            "next_run",
            "status",
            "error",
            "last_known_commit",
            "last_known_files_processed",
            "last_known_indexed_at",
            "created_at",
            "updated_at",
            "lifecycle_schema_version",
        }
        set_fields = {k: v for k, v in fields.items() if k in valid_fields}

        if not set_fields:
            return

        # Build INSERT ON CONFLICT query
        all_columns = ["repo_alias"] + list(set_fields.keys())
        placeholders = ["?"] * len(all_columns)
        values = [repo_alias] + list(set_fields.values())

        update_clause = ", ".join(f"{k} = excluded.{k}" for k in set_fields.keys())

        def operation(conn):
            conn.execute(
                f"""INSERT INTO description_refresh_tracking
                   ({", ".join(all_columns)}) VALUES ({", ".join(placeholders)})
                   ON CONFLICT(repo_alias) DO UPDATE SET {update_clause}""",
                values,
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug(f"Upserted tracking record for repo: {repo_alias}")

    def delete_tracking(self, repo_alias: str) -> bool:
        """
        Remove tracking record for a repository.

        Args:
            repo_alias: Alias of the repository to delete

        Returns:
            True if a record was deleted, False if not found
        """

        def operation(conn):
            cursor = conn.execute(
                "DELETE FROM description_refresh_tracking WHERE repo_alias = ?",
                (repo_alias,),
            )
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted tracking record for repo: {repo_alias}")
        return deleted

    def get_all_tracking(self) -> List[Dict[str, Any]]:
        """
        List all tracking records.

        Returns:
            List of all tracking records (for diagnostics)
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT repo_alias, last_run, next_run, status, error,
                      last_known_commit, last_known_files_processed,
                      last_known_indexed_at, created_at, updated_at
               FROM description_refresh_tracking"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "repo_alias": row[0],
                    "last_run": row[1],
                    "next_run": row[2],
                    "status": row[3],
                    "error": row[4],
                    "last_known_commit": row[5],
                    "last_known_files_processed": row[6],
                    "last_known_indexed_at": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                }
            )

        return result

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
