"""Per-alias refresh failure backoff state for GoldenRepoMetadataPostgresBackend
(Bug #2022 Gap 4) -- the cluster-mode mirror of
``sqlite_backends/_refresh_failure_backoff_mixin.py``. Table created by
migration 058.

Split into its own module because golden_repo_metadata_backend.py is already
over the project's 1,000-line-per-file limit.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .connection_pool import ConnectionPool


def delete_refresh_failure_backoff_for_repo(cur: Any, alias: str) -> None:
    """Bug #2022: drop the backoff of a removed golden repo on the caller's
    cursor (same transaction), under both its bare and ``-global`` alias."""
    cur.execute(
        "DELETE FROM refresh_failure_backoff_state WHERE golden_alias IN (%s, %s)",
        (alias, f"{alias}-global"),
    )


class _RefreshFailureBackoffPostgresMixin:
    """Refresh failure backoff methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataPostgresBackend.__init__.
    _pool: "ConnectionPool"

    def record_refresh_failure_backoff(self, golden_alias: str, detail: str) -> int:
        """Record one non-repairable refresh failure; return the
        consecutive-failure count after recording it. One atomic upsert, so
        concurrent recorders on different nodes never lose an increment."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_failure_backoff_state "
                    "(golden_alias, consecutive_failure_count, last_detail, "
                    "last_failed_at, updated_at) VALUES (%s, 1, %s, %s, %s) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET "
                    "consecutive_failure_count = "
                    "refresh_failure_backoff_state.consecutive_failure_count + 1, "
                    "last_detail = EXCLUDED.last_detail, "
                    "last_failed_at = EXCLUDED.last_failed_at, "
                    "updated_at = EXCLUDED.updated_at "
                    "RETURNING consecutive_failure_count",
                    (golden_alias, detail, time.time(), datetime.now(timezone.utc)),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(
                f"refresh_failure_backoff_state upsert returned no row for {golden_alias}"
            )
        return int(row[0])

    def get_refresh_failure_backoff_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """Return the persisted backoff state, or None if none is recorded."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._STATE_COLUMNS} FROM refresh_failure_backoff_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()
        return None if row is None else self._state_from_row(row)

    _STATE_COLUMNS = (
        "golden_alias, consecutive_failure_count, last_detail, last_failed_at, "
        "pending_trigger"
    )

    @staticmethod
    def _state_from_row(row: Any) -> Dict[str, Any]:
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": int(row[1]),
            "last_detail": row[2],
            "last_failed_at": float(row[3]),
            "pending_trigger": bool(row[4]),
        }

    def reset_refresh_failure_backoff(self, golden_alias: str) -> None:
        """Clear the backoff state (a verified refresh succeeded). A no-op
        for an alias with no recorded state."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM refresh_failure_backoff_state WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()

    def mark_refresh_trigger_pending(self, golden_alias: str) -> None:
        """Remember a system refresh trigger deferred by the backoff, so it
        fires once the backoff ends (migration 059). A no-op when no backoff
        is recorded."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE refresh_failure_backoff_state SET pending_trigger = TRUE "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()

    def list_pending_refresh_triggers(self) -> List[Dict[str, Any]]:
        """Backoff states that carry a deferred trigger (only failing
        aliases, never the whole fleet)."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._STATE_COLUMNS} FROM refresh_failure_backoff_state "
                    "WHERE pending_trigger ORDER BY golden_alias"
                )
                rows = cur.fetchall()
        return [self._state_from_row(row) for row in rows]

    def claim_pending_refresh_trigger(self, golden_alias: str) -> bool:
        """Atomically take the deferred trigger; True for exactly one caller
        across the cluster (single conditional UPDATE)."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE refresh_failure_backoff_state SET pending_trigger = FALSE "
                    "WHERE golden_alias = %s AND pending_trigger",
                    (golden_alias,),
                )
                claimed = bool(cur.rowcount == 1)
            conn.commit()
        return claimed
