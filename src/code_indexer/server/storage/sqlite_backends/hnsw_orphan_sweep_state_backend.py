"""
SQLite backend for the HNSW orphan repair fleet sweep durable state
(Story #1360, Epic #1333 S3).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from ..database_manager import DatabaseConnectionManager

# Story #1360 (Epic #1333 S3): outcomes counted per sweep item -- kept in
# sync with SweepOutcome in hnsw_orphan_sweep/repair_executor.py (that enum
# is the source of truth; this is a plain str set so the backend has no
# import dependency on the server-services layer).
_HNSW_SWEEP_OUTCOMES = {"clean", "repaired", "transient_skip", "error"}

# Columns bumped (by +1 each) for each outcome value, alongside
# pass_indexes_checked which always bumps. "error" bumps pass_orphaned_found
# too: process_candidate() only ever returns ERROR after orphans were
# confirmed present under the lock (repair attempted but failed to converge,
# or the post-repair persist/reload verification failed) -- so an ERROR
# outcome IS an "orphan found" outcome, just one that wasn't successfully
# fixed. Code review finding: without this, pass_orphaned_found silently
# undercounted the true number of orphaned indexes discovered this pass.
_HNSW_SWEEP_OUTCOME_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "repaired": ("pass_orphaned_found", "pass_repaired"),
    "error": ("pass_orphaned_found", "pass_errors"),
    "transient_skip": ("pass_transient_skips",),
}


class HNSWOrphanSweepStateSqliteBackend:
    """SQLite backend for the HNSW orphan repair fleet sweep durable state
    (Story #1360, Epic #1333 S3).

    Singleton row (id=1), auto-created on first access. Tracks the current
    pass's stable-key cursor (``last_completed_key``, a STRING -- never a
    numeric offset, per the story's resume-correctness rationale) and
    per-pass / lifetime statistics.
    """

    _ROW_ID = 1

    def __init__(self, db_path: str) -> None:
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def _ensure_row(self, conn: Any) -> None:
        """Create the singleton row on first access. pass_started_at is set
        to "now" at creation time -- pass 1 begins the moment the row first
        exists (code review finding: pass_started_at must reflect real
        pass-start timing, never a dead always-NULL column)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO hnsw_orphan_sweep_state (id, pass_started_at) "
            "VALUES (?, ?)",
            (self._ROW_ID, now_iso),
        )

    def get_state(self) -> Dict[str, Any]:
        """Return the current durable sweep state, creating the default
        singleton row on first access."""

        def operation(conn: Any) -> Dict[str, Any]:
            self._ensure_row(conn)
            row = conn.execute(
                """SELECT pass_id, last_completed_key, pass_started_at,
                          pass_indexes_checked, pass_orphaned_found,
                          pass_repaired, pass_errors, pass_transient_skips,
                          last_full_pass_completed_at,
                          total_orphans_repaired_lifetime
                   FROM hnsw_orphan_sweep_state WHERE id = ?""",
                (self._ROW_ID,),
            ).fetchone()
            return {
                "pass_id": row[0],
                "last_completed_key": row[1],
                "pass_started_at": row[2],
                "pass_indexes_checked": row[3],
                "pass_orphaned_found": row[4],
                "pass_repaired": row[5],
                "pass_errors": row[6],
                "pass_transient_skips": row[7],
                "last_full_pass_completed_at": row[8],
                "total_orphans_repaired_lifetime": row[9],
            }

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def record_item_processed(self, key: str, outcome: str) -> None:
        """Durably record one processed sweep item (AC1: persisted after
        EACH item, not just at tick end).

        Args:
            key: The item's stable sort key -- becomes the new
                ``last_completed_key`` cursor value.
            outcome: One of "clean", "repaired", "transient_skip", "error".

        Raises:
            ValueError: If outcome is not a recognized value.
        """
        if outcome not in _HNSW_SWEEP_OUTCOMES:
            raise ValueError(
                f"Unknown HNSW sweep outcome '{outcome}'; expected one of "
                f"{sorted(_HNSW_SWEEP_OUTCOMES)}"
            )

        extra_columns = _HNSW_SWEEP_OUTCOME_COLUMNS.get(outcome, ())
        now_iso = datetime.now(timezone.utc).isoformat()
        extra_bumps = "".join(f", {col} = {col} + 1" for col in extra_columns)

        def operation(conn: Any) -> None:
            self._ensure_row(conn)
            conn.execute(
                f"""UPDATE hnsw_orphan_sweep_state
                    SET last_completed_key = ?,
                        pass_indexes_checked = pass_indexes_checked + 1,
                        updated_at = ?
                        {extra_bumps}
                    WHERE id = ?""",
                (key, now_iso, self._ROW_ID),
            )

        self._conn_manager.execute_atomic(operation)

    def complete_pass(self) -> None:
        """Record a completed full pass: accrue the lifetime repaired total,
        reset the cursor and per-pass counters, set a fresh pass_started_at
        for the NEW pass (never NULL -- see _ensure_row docstring), and
        increment pass_id."""
        now_iso = datetime.now(timezone.utc).isoformat()

        def operation(conn: Any) -> None:
            self._ensure_row(conn)
            conn.execute(
                """UPDATE hnsw_orphan_sweep_state
                   SET total_orphans_repaired_lifetime =
                           total_orphans_repaired_lifetime + pass_repaired,
                       last_full_pass_completed_at = ?,
                       pass_id = pass_id + 1,
                       last_completed_key = NULL,
                       pass_started_at = ?,
                       pass_indexes_checked = 0,
                       pass_orphaned_found = 0,
                       pass_repaired = 0,
                       pass_errors = 0,
                       pass_transient_skips = 0,
                       updated_at = ?
                   WHERE id = ?""",
                (now_iso, now_iso, now_iso, self._ROW_ID),
            )

        self._conn_manager.execute_atomic(operation)
