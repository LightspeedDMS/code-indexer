"""PostgreSQL backend for HNSW orphan repair fleet sweep durable state.

Story #1360 (Epic #1333 S3): drop-in cluster-mode replacement for
HNSWOrphanSweepStateSqliteBackend, satisfying the same shape (dict returned
by get_state(), same outcome vocabulary for record_item_processed()).
Uses psycopg v3 sync mode with a connection pool and a singleton row (id=1),
identical in spirit to DependencyMapTrackingPostgresBackend.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

_ROW_ID = 1

_HNSW_SWEEP_OUTCOMES = {"clean", "repaired", "transient_skip", "error"}

_HNSW_SWEEP_OUTCOME_COLUMN = {
    "repaired": "pass_orphaned_found",
    "error": "pass_errors",
    "transient_skip": "pass_transient_skips",
}

_SELECT_COLUMNS = (
    "id, pass_id, last_completed_key, pass_started_at, pass_indexes_checked, "
    "pass_orphaned_found, pass_repaired, pass_errors, pass_transient_skips, "
    "last_full_pass_completed_at, total_orphans_repaired_lifetime"
)


def _row_to_state(row: Any) -> Dict[str, Any]:
    return {
        "pass_id": row[1],
        "last_completed_key": row[2],
        "pass_started_at": row[3],
        "pass_indexes_checked": row[4],
        "pass_orphaned_found": row[5],
        "pass_repaired": row[6],
        "pass_errors": row[7],
        "pass_transient_skips": row[8],
        "last_full_pass_completed_at": row[9],
        "total_orphans_repaired_lifetime": row[10],
    }


class HNSWOrphanSweepStatePostgresBackend:
    """PostgreSQL backend for the HNSW orphan repair fleet sweep durable
    state. Satisfies the same interface as
    HNSWOrphanSweepStateSqliteBackend (get_state / record_item_processed /
    complete_pass) over a singleton row (id=1)."""

    def __init__(self, pool: Any) -> None:
        """
        Args:
            pool: A psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def get_state(self) -> Dict[str, Any]:
        """Return the current durable sweep state, creating the default
        singleton row on first access."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM hnsw_orphan_sweep_state WHERE id = %s",
                (_ROW_ID,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO hnsw_orphan_sweep_state (id) VALUES (%s)",
                    (_ROW_ID,),
                )
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM hnsw_orphan_sweep_state WHERE id = %s",
                    (_ROW_ID,),
                ).fetchone()
        return _row_to_state(row)

    def record_item_processed(self, key: str, outcome: str) -> None:
        """Durably record one processed sweep item (AC1: persisted after
        EACH item, not just at tick end).

        Raises:
            ValueError: If outcome is not a recognized value.
        """
        if outcome not in _HNSW_SWEEP_OUTCOMES:
            raise ValueError(
                f"Unknown HNSW sweep outcome '{outcome}'; expected one of "
                f"{sorted(_HNSW_SWEEP_OUTCOMES)}"
            )

        outcome_column = _HNSW_SWEEP_OUTCOME_COLUMN.get(outcome)
        repaired_bump = 1 if outcome == "repaired" else 0
        now = datetime.now(timezone.utc)

        with self._pool.connection() as conn:
            if outcome_column is not None:
                conn.execute(
                    f"""UPDATE hnsw_orphan_sweep_state
                        SET last_completed_key = %s,
                            pass_indexes_checked = pass_indexes_checked + 1,
                            {outcome_column} = {outcome_column} + 1,
                            pass_repaired = pass_repaired + %s,
                            updated_at = %s
                        WHERE id = %s""",
                    (key, repaired_bump, now, _ROW_ID),
                )
            else:
                conn.execute(
                    """UPDATE hnsw_orphan_sweep_state
                       SET last_completed_key = %s,
                           pass_indexes_checked = pass_indexes_checked + 1,
                           updated_at = %s
                       WHERE id = %s""",
                    (key, now, _ROW_ID),
                )

    def complete_pass(self) -> None:
        """Record a completed full pass: accrue the lifetime repaired total,
        reset the cursor and per-pass counters, and increment pass_id."""
        now = datetime.now(timezone.utc)
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE hnsw_orphan_sweep_state
                   SET total_orphans_repaired_lifetime =
                           total_orphans_repaired_lifetime + pass_repaired,
                       last_full_pass_completed_at = %s,
                       pass_id = pass_id + 1,
                       last_completed_key = NULL,
                       pass_started_at = NULL,
                       pass_indexes_checked = 0,
                       pass_orphaned_found = 0,
                       pass_repaired = 0,
                       pass_errors = 0,
                       pass_transient_skips = 0,
                       updated_at = %s
                   WHERE id = %s""",
                (now, now, _ROW_ID),
            )

    def close(self) -> None:
        """Close the connection pool."""
        self._pool.close()
