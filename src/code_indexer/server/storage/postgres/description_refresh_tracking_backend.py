"""
PostgreSQL backend for description refresh tracking storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for DescriptionRefreshTrackingBackend (SQLite) satisfying the
DescriptionRefreshTrackingBackend protocol.
Uses psycopg v3 sync mode with a connection pool.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# Column names valid for upsert_tracking
_VALID_FIELDS = frozenset(
    {
        "last_run",
        "next_run",
        "status",
        "error",
        "last_known_commit",
        "last_known_files_processed",
        "last_known_indexed_at",
        "created_at",
        "updated_at",
    }
)


def _row_to_dict(row: Any) -> Dict[str, Any]:
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
    }


_SELECT_COLUMNS = (
    "repo_alias, last_run, next_run, status, error, "
    "last_known_commit, last_known_files_processed, "
    "last_known_indexed_at, created_at, updated_at"
)


class DescriptionRefreshTrackingPostgresBackend:
    """
    PostgreSQL backend for description refresh tracking.

    Satisfies the DescriptionRefreshTrackingBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def get_tracking_record(self, repo_alias: str) -> Optional[Dict[str, Any]]:
        """Get tracking record for a repository."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM description_refresh_tracking WHERE repo_alias = %s",
                (repo_alias,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def get_stale_repos(self, now_iso: str) -> List[Dict[str, Any]]:
        """Query repos where next_run <= now AND status != 'queued'."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"""SELECT {_SELECT_COLUMNS}
                   FROM description_refresh_tracking
                   WHERE next_run <= %s AND status != 'queued'""",
                (now_iso,),
            )
            rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    def upsert_tracking(self, repo_alias: str, **fields: Any) -> None:
        """Insert or update tracking record for a repository."""
        set_fields = {k: v for k, v in fields.items() if k in _VALID_FIELDS}
        if not set_fields:
            return

        all_columns = ["repo_alias"] + list(set_fields.keys())
        placeholders = ["%s"] * len(all_columns)
        values = [repo_alias] + list(set_fields.values())
        update_clause = ", ".join(f"{k} = EXCLUDED.{k}" for k in set_fields.keys())

        with self._pool.connection() as conn:
            conn.execute(
                f"""INSERT INTO description_refresh_tracking
                   ({", ".join(all_columns)}) VALUES ({", ".join(placeholders)})
                   ON CONFLICT (repo_alias) DO UPDATE SET {update_clause}""",
                values,
            )
        logger.debug(f"Upserted tracking record for repo: {repo_alias}")

    def delete_tracking(self, repo_alias: str) -> bool:
        """Remove tracking record for a repository. Returns True if deleted."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM description_refresh_tracking WHERE repo_alias = %s",
                (repo_alias,),
            )
            deleted: bool = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted tracking record for repo: {repo_alias}")
        return deleted

    def get_all_tracking(self) -> List[Dict[str, Any]]:
        """List all tracking records."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM description_refresh_tracking"
            )
            rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
