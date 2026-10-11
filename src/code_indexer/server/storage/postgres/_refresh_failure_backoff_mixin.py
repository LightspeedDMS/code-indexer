"""Per-alias refresh failure backoff state for GoldenRepoMetadataPostgresBackend
(Bug #2022 Gap 4) -- the cluster-mode mirror of
``sqlite_backends/_refresh_failure_backoff_mixin.py``. Table created by
migration 058; ``owed_retry_count`` (Bug #2125, an owed retry's own
escalation counter) by migration 070.

Split into its own module because golden_repo_metadata_backend.py is already
over the project's 1,000-line-per-file limit.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..shared.owed_refresh_retry import plan_owed_retry

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
        "pending_trigger, pending_due_at, pending_marked_at, trigger_generation, "
        "owed_retry_count"
    )

    @staticmethod
    def _state_from_row(row: Any) -> Dict[str, Any]:
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": int(row[1]),
            "last_detail": row[2],
            "last_failed_at": float(row[3]),
            "pending_trigger": bool(row[4]),
            "pending_due_at": None if row[5] is None else float(row[5]),
            "pending_marked_at": None if row[6] is None else float(row[6]),
            "trigger_generation": int(row[7]),
            "owed_retry_count": int(row[8]),
        }

    def _update_one(self, sql: str, params: Tuple[Any, ...]) -> bool:
        """Run a single-row conditional UPDATE; True when it changed a row."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                updated: bool = cur.rowcount == 1
            conn.commit()
        return updated

    def mark_refresh_trigger_pending(self, golden_alias: str, due_at: float) -> bool:
        """Remember a system refresh trigger deferred by the backoff, due at
        *due_at* (migrations 059-061). The trigger gets a fresh generation
        from the store-wide sequence in the same statement, so it never
        equals a generation an in-flight cycle captured -- not even after
        the row was recreated. False when no active backoff is left (the row
        was resolved, or re-armed with no failure, concurrently), so nothing
        defers the trigger and the caller submits it normally."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        return self._update_one(
            "UPDATE refresh_failure_backoff_state SET pending_trigger = TRUE, "
            "pending_due_at = %s, pending_marked_at = %s, trigger_generation = "
            "nextval('refresh_trigger_generation_seq') "
            "WHERE golden_alias = %s AND consecutive_failure_count > 0",
            (due_at, time.time(), golden_alias),
        )

    def list_due_refresh_triggers(self, now: float) -> List[Dict[str, Any]]:
        """Deferred triggers due at *now* (partial index from migration 060)."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._STATE_COLUMNS} FROM refresh_failure_backoff_state "
                    "WHERE pending_trigger AND pending_due_at <= %s "
                    "ORDER BY pending_due_at",
                    (now,),
                )
                rows = cur.fetchall()
        return [self._state_from_row(row) for row in rows]

    def lease_pending_refresh_trigger(
        self, golden_alias: str, now: float, until: float
    ) -> bool:
        """Atomically take a due trigger until *until*: True for exactly one
        caller across the cluster. The trigger stays pending, so a crash
        before the refresh completes only delays it to the lease end."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if until <= now:
            raise ValueError("lease end must be later than now")
        return self._update_one(
            "UPDATE refresh_failure_backoff_state SET pending_due_at = %s "
            "WHERE golden_alias = %s AND pending_trigger AND pending_due_at <= %s",
            (until, golden_alias, now),
        )

    def resolve_refresh_failure_backoff(
        self, golden_alias: str, covered_generation: int
    ) -> None:
        """A verified publish resolves the failure and every trigger its
        cycle covered: the row is deleted only while its trigger generation
        still equals *covered_generation* (captured from the store before
        the cycle read its source). A trigger deferred after that advanced
        the generation, whatever any node's clock says: its row survives
        re-armed (no failure and no owed retry left, due now). One
        transaction."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                _resolve_covered_with(
                    cur,
                    golden_alias,
                    covered_generation,
                    time.time(),
                    datetime.now(timezone.utc),
                )
            conn.commit()

    def clear_refresh_trigger(self, golden_alias: str, covered_generation: int) -> bool:
        """Drop the deferred trigger of an alias no refresh can serve (it
        needs an operator), but only while its generation is still the one
        the skip decided on: a trigger deferred since then survives. The
        failure state stays. True when one was dropped."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        return self._update_one(
            "UPDATE refresh_failure_backoff_state SET pending_trigger = FALSE "
            "WHERE golden_alias = %s AND pending_trigger "
            "AND trigger_generation = %s",
            (golden_alias, covered_generation),
        )

    def escalate_refresh_trigger(
        self, golden_alias: str, covered_generation: int
    ) -> Optional[int]:
        """A recoverable skip of a deferred trigger: its retry interval
        escalates -- only while that trigger (generation
        *covered_generation*) is still pending. An owed retry (no failure,
        an owed count) raises its owed count (Bug #2125); any other row one
        more failure. One conditional statement: a row a publish deleted
        meanwhile is never recreated, and the original failure reason
        (last_detail) is kept. Returns the escalated count, or None when
        nothing matched."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # Right-hand sides read the row's values before the update.
                cur.execute(
                    "UPDATE refresh_failure_backoff_state SET "
                    "owed_retry_count = CASE WHEN consecutive_failure_count = 0 "
                    "AND owed_retry_count > 0 THEN owed_retry_count + 1 "
                    "ELSE owed_retry_count END, "
                    "consecutive_failure_count = CASE WHEN "
                    "consecutive_failure_count = 0 AND owed_retry_count > 0 "
                    "THEN 0 ELSE consecutive_failure_count + 1 END, "
                    "last_failed_at = %s, updated_at = %s "
                    "WHERE golden_alias = %s AND pending_trigger "
                    "AND trigger_generation = %s "
                    "RETURNING GREATEST(consecutive_failure_count, owed_retry_count)",
                    (
                        time.time(),
                        datetime.now(timezone.utc),
                        golden_alias,
                        covered_generation,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return None if row is None else int(row[0])

    def mark_volume_alarm_deferral(self, golden_alias: str, due_at: float) -> None:
        """Story #2118: remember a system refresh deferred by the refresh
        volume alarm, due at *due_at*, WITHOUT a strike: a new row has no
        failures (so no failure backoff); an existing row keeps its strikes
        and failure detail. One statement; the trigger gets a fresh
        generation from the store-wide sequence, exactly as a backoff
        deferral does."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        now = time.time()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO refresh_failure_backoff_state "
                    "(golden_alias, consecutive_failure_count, last_detail, "
                    "last_failed_at, updated_at, pending_trigger, pending_due_at, "
                    "pending_marked_at, trigger_generation) "
                    "VALUES (%s, 0, 'refresh volume alarm', %s, %s, TRUE, %s, %s, "
                    "nextval('refresh_trigger_generation_seq')) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET pending_trigger = TRUE, "
                    "pending_due_at = EXCLUDED.pending_due_at, "
                    "pending_marked_at = EXCLUDED.pending_marked_at, "
                    "trigger_generation = EXCLUDED.trigger_generation, "
                    "updated_at = EXCLUDED.updated_at",
                    (golden_alias, now, datetime.now(timezone.utc), due_at, now),
                )
            conn.commit()

    def record_owed_refresh_retry(
        self,
        golden_alias: str,
        detail: str,
        covered_generation: Optional[int],
        min_count: int,
        retry_seconds: Callable[[int], float],
    ) -> Tuple[int, float]:
        """Bug #2125: persist one more OWED retry of *golden_alias* -- a
        pending trigger with a fresh generation, escalated by its own
        ``owed_retry_count`` (see plan_owed_retry); never a failure, so
        ``consecutive_failure_count`` and the failure detail stay as they
        are. *covered_generation* is the trigger generation the finishing
        cycle captured (None: unknown). One transaction holding the row
        lock (see _owe_retry_with). Returns (owed count, due time)."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")
        now = time.time()
        updated_at = datetime.now(timezone.utc)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                planned = _owe_retry_with(
                    cur,
                    golden_alias,
                    detail,
                    covered_generation,
                    min_count,
                    retry_seconds,
                    now,
                    updated_at,
                )
            conn.commit()
        return planned

    def resolve_and_record_owed_retry(
        self,
        golden_alias: str,
        covered_generation: int,
        detail: str,
        retry_seconds: Callable[[int], float],
    ) -> Tuple[int, float]:
        """Bug #2125: a verified publish that still owes files. In ONE
        transaction: resolve exactly as resolve_refresh_failure_backoff does
        (a trigger newer than *covered_generation* survives, re-armed), then
        owe a retry escalated from the owed count the row had before
        (record_owed_refresh_retry). An exception leaves the transaction
        uncommitted (the pool rolls it back), so the covered pending trigger
        is never lost: its lease fires it again. Returns (owed count, due
        time)."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")
        now = time.time()
        updated_at = datetime.now(timezone.utc)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT owed_retry_count FROM refresh_failure_backoff_state "
                    "WHERE golden_alias = %s FOR UPDATE",
                    (golden_alias,),
                )
                row = cur.fetchone()
                prior = 0 if row is None else int(row[0])
                _resolve_covered_with(
                    cur, golden_alias, covered_generation, now, updated_at
                )
                planned = _owe_retry_with(
                    cur,
                    golden_alias,
                    detail,
                    covered_generation,
                    prior + 1,
                    retry_seconds,
                    now,
                    updated_at,
                )
            conn.commit()
        return planned


_LOCK_OWED_ROW_SQL = (
    "SELECT owed_retry_count, pending_trigger, pending_due_at, "
    "trigger_generation FROM refresh_failure_backoff_state "
    "WHERE golden_alias = %s FOR UPDATE"
)


def _resolve_covered_with(
    cur: Any,
    golden_alias: str,
    covered_generation: int,
    now: float,
    updated_at: datetime,
) -> None:
    """The resolution of a verified publish on the caller's cursor (same
    transaction): delete the row while its generation is the covered one;
    a row a newer trigger kept is re-armed (due now) with no failure and no
    owed retry left."""
    cur.execute(
        "DELETE FROM refresh_failure_backoff_state "
        "WHERE golden_alias = %s AND trigger_generation = %s",
        (golden_alias, covered_generation),
    )
    cur.execute(
        "UPDATE refresh_failure_backoff_state SET "
        "consecutive_failure_count = 0, owed_retry_count = 0, "
        "pending_due_at = %s, updated_at = %s WHERE golden_alias = %s",
        (now, updated_at, golden_alias),
    )


def _owe_retry_with(
    cur: Any,
    golden_alias: str,
    detail: str,
    covered_generation: Optional[int],
    min_count: int,
    retry_seconds: Callable[[int], float],
    now: float,
    updated_at: datetime,
) -> Tuple[int, float]:
    """One more owed retry (see plan_owed_retry) on the caller's cursor:
    the row is locked first; the fresh generation comes from the store-wide
    sequence inside the INSERT/UPDATE itself; the failure count is
    untouched. A new row is inserted with ON CONFLICT DO NOTHING; a
    concurrent insert by another node is re-read once under the lock."""
    cur.execute(_LOCK_OWED_ROW_SQL, (golden_alias,))
    row = cur.fetchone()
    if row is None:
        count, due_at = plan_owed_retry(
            None, None, None, covered_generation, min_count, now, retry_seconds
        )
        cur.execute(
            "INSERT INTO refresh_failure_backoff_state "
            "(golden_alias, consecutive_failure_count, last_detail, "
            "last_failed_at, updated_at, pending_trigger, pending_due_at, "
            "pending_marked_at, trigger_generation, owed_retry_count) "
            "VALUES (%s, 0, %s, %s, %s, TRUE, %s, %s, "
            "nextval('refresh_trigger_generation_seq'), %s) "
            "ON CONFLICT (golden_alias) DO NOTHING",
            (golden_alias, detail, now, updated_at, due_at, now, count),
        )
        if cur.rowcount == 1:
            return count, due_at
        # Another node inserted the row meanwhile: lock and update it.
        cur.execute(_LOCK_OWED_ROW_SQL, (golden_alias,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"refresh_failure_backoff_state row of {golden_alias} "
                f"vanished during an owed-retry insert"
            )
    count, due_at = plan_owed_retry(
        int(row[0]),
        None if not row[1] or row[2] is None else float(row[2]),
        int(row[3]),
        covered_generation,
        min_count,
        now,
        retry_seconds,
    )
    cur.execute(
        "UPDATE refresh_failure_backoff_state SET pending_trigger = TRUE, "
        "pending_due_at = %s, pending_marked_at = %s, trigger_generation = "
        "nextval('refresh_trigger_generation_seq'), owed_retry_count = %s, "
        "updated_at = %s WHERE golden_alias = %s",
        (due_at, now, count, updated_at, golden_alias),
    )
    return count, due_at
