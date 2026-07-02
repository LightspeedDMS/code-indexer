"""
PostgreSQL backend for dependency map tracking storage.

Story #414: PostgreSQL Backend for Remaining 6 Backends

Drop-in replacement for DependencyMapTrackingBackend (SQLite) satisfying the
DependencyMapTrackingBackend protocol.
Uses psycopg v3 sync mode with a connection pool.

Uses a singleton row (id=1) pattern identical to the SQLite implementation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# Sentinel for "not provided" — matches the SQLite _UNSET pattern.
_UNSET = object()


class DependencyMapTrackingPostgresBackend:
    """
    PostgreSQL backend for dependency map tracking.

    Satisfies the DependencyMapTrackingBackend protocol.
    Accepts a psycopg v3 connection pool in __init__.
    Uses a singleton row (id=1) for all tracking state.
    """

    def __init__(self, pool: Any) -> None:
        """
        Initialize the backend.

        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def get_tracking(self) -> Dict[str, Any]:
        """
        Get the singleton tracking record, initialising it if absent.

        Returns:
            Dict with keys: id, last_run, next_run, status, commit_hashes,
            error_message, refinement_cursor, refinement_next_run.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                          refinement_cursor, refinement_next_run
                   FROM dependency_map_tracking WHERE id = 1"""
            )
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO dependency_map_tracking (id, status) VALUES (1, 'pending')"
                )
                cursor = conn.execute(
                    """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                              refinement_cursor, refinement_next_run
                       FROM dependency_map_tracking WHERE id = 1"""
                )
                row = cursor.fetchone()
        return {
            "id": row[0],
            "last_run": row[1],
            "next_run": row[2],
            "status": row[3],
            "commit_hashes": row[4],
            "error_message": row[5],
            "refinement_cursor": row[6],
            "refinement_next_run": row[7],
        }

    def update_tracking(
        self,
        last_run: Any = _UNSET,
        next_run: Any = _UNSET,
        status: Any = _UNSET,
        commit_hashes: Any = _UNSET,
        error_message: Any = _UNSET,
        refinement_cursor: Any = _UNSET,
        refinement_next_run: Any = _UNSET,
    ) -> None:
        """
        Update the singleton tracking record.

        Only updates fields explicitly provided (partial updates supported).
        """
        updates: List[str] = []
        params: List[Any] = []

        if last_run is not _UNSET:
            updates.append("last_run = %s")
            params.append(last_run)

        if next_run is not _UNSET:
            updates.append("next_run = %s")
            params.append(next_run)

        if status is not _UNSET:
            updates.append("status = %s")
            params.append(status)

        if commit_hashes is not _UNSET:
            updates.append("commit_hashes = %s")
            params.append(commit_hashes)

        if error_message is not _UNSET:
            updates.append("error_message = %s")
            params.append(error_message)

        if refinement_cursor is not _UNSET:
            updates.append("refinement_cursor = %s")
            params.append(refinement_cursor)

        if refinement_next_run is not _UNSET:
            updates.append("refinement_next_run = %s")
            params.append(refinement_next_run)

        if not updates:
            return

        params.append(1)  # WHERE id = 1
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE dependency_map_tracking SET {', '.join(updates)} WHERE id = %s",
                params,
            )
        logger.debug("Updated dependency map tracking record")

    def cleanup_stale_status_on_startup(self) -> bool:
        """
        Reset stale running/pending status to failed on server startup.

        Returns:
            True if a stale status was cleaned up, False otherwise.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "SELECT status FROM dependency_map_tracking WHERE id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                return False
            if row[0] in ("running", "pending"):
                conn.execute(
                    "UPDATE dependency_map_tracking SET status = 'failed', "
                    "error_message = 'orphaned - server restarted' WHERE id = 1"
                )
                logger.info(
                    "DependencyMapTrackingPostgresBackend: reset stale status to 'failed' on startup"
                )
                return True
        return False

    def record_run_metrics(
        self,
        metrics: Dict[str, Any],
        run_type: Optional[str] = None,
        phase_timings_json: Optional[str] = None,
    ) -> None:
        """
        Store run metrics to dependency_map_run_history.

        Args:
            metrics: Dict with keys: timestamp, domain_count, total_chars, edge_count,
                     zero_char_domains, repos_analyzed, repos_skipped,
                     pass1_duration_s, pass2_duration_s
            run_type: Optional run classification (e.g. "delta", "full").
                      Bug #874 Story B. NULL for legacy rows.
            phase_timings_json: Optional pre-serialized JSON string with per-phase
                      timing breakdown. Bug #874 Story B. NULL for legacy rows.
        """
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO dependency_map_run_history
                   (timestamp, domain_count, total_chars, edge_count, zero_char_domains,
                    repos_analyzed, repos_skipped, pass1_duration_s, pass2_duration_s,
                    run_type, phase_timings_json)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    metrics.get("timestamp"),
                    metrics.get("domain_count"),
                    metrics.get("total_chars"),
                    metrics.get("edge_count"),
                    metrics.get("zero_char_domains"),
                    metrics.get("repos_analyzed"),
                    metrics.get("repos_skipped"),
                    metrics.get("pass1_duration_s"),
                    metrics.get("pass2_duration_s"),
                    run_type,
                    phase_timings_json,
                ),
            )
        logger.debug("Recorded dependency map run metrics")

    def get_run_history(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Retrieve recent run metrics ordered most-recent-first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of metric dicts ordered by run_id DESC.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT run_id, timestamp, domain_count, total_chars, edge_count,
                          zero_char_domains, repos_analyzed, repos_skipped,
                          pass1_duration_s, pass2_duration_s,
                          run_type, phase_timings_json
                   FROM dependency_map_run_history
                   ORDER BY run_id DESC
                   LIMIT %s""",
                (limit,),
            )
            rows = cursor.fetchall()
        return [
            {
                "run_id": row[0],
                "timestamp": row[1],
                "domain_count": row[2],
                "total_chars": row[3],
                "edge_count": row[4],
                "zero_char_domains": row[5],
                "repos_analyzed": row[6],
                "repos_skipped": row[7],
                "pass1_duration_s": row[8],
                "pass2_duration_s": row[9],
                "run_type": row[10],
                "phase_timings_json": row[11],
            }
            for row in rows
        ]

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        """Delete dependency map history records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records before this are deleted.

        Returns:
            Number of rows deleted.
        """
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM dependency_map_tracking WHERE last_run < %s",
                (cutoff_iso,),
            )
            deleted = result.rowcount if result.rowcount else 0
            conn.commit()
        return deleted

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
