"""Per-alias forced-reconcile state for GoldenRepoMetadataPostgresBackend --
the cluster-mode mirror of ``sqlite_backends/_forced_reconcile_state_mixin.py``
(see that module for the purpose). Table created by migration 064.

Split into its own module because golden_repo_metadata_backend.py is already
over the project's 1,000-line-per-file limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .connection_pool import ConnectionPool


def delete_forced_reconcile_state_for_repo(cur: Any, alias: str) -> None:
    """Drop a removed golden repo's state on the caller's cursor (same
    transaction), under both its bare and ``-global`` alias."""
    cur.execute(
        "DELETE FROM forced_reconcile_state WHERE golden_alias IN (%s, %s)",
        (alias, f"{alias}-global"),
    )


def _require(value: str, name: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a non-empty string")


class _ForcedReconcileStatePostgresMixin:
    """Forced-reconcile state methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataPostgresBackend.__init__.
    _pool: "ConnectionPool"

    def record_forced_reconcile(self, golden_alias: str, signal: str) -> int:
        """Record one forced reconcile for ``signal``; return the number of
        consecutive forced reconciles for this same signal (1 when the
        stored signal differs or none is stored). One atomic upsert, so
        concurrent recorders on different nodes never lose an increment."""
        _require(golden_alias, "golden_alias")
        _require(signal, "signal")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO forced_reconcile_state "
                    "(golden_alias, signal, attempt_count, updated_at) "
                    "VALUES (%s, %s, 1, %s) "
                    "ON CONFLICT (golden_alias) DO UPDATE SET "
                    "attempt_count = CASE WHEN forced_reconcile_state.signal = "
                    "EXCLUDED.signal THEN forced_reconcile_state.attempt_count + 1 "
                    "ELSE 1 END, "
                    "signal = EXCLUDED.signal, updated_at = EXCLUDED.updated_at "
                    "RETURNING attempt_count",
                    (golden_alias, signal, datetime.now(timezone.utc)),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(
                f"forced_reconcile_state upsert returned no row for {golden_alias}"
            )
        return int(row[0])

    def get_forced_reconcile_state(self, golden_alias: str) -> Optional[Dict[str, Any]]:
        """Return ``{"signal", "attempt_count"}`` or None when none is stored."""
        _require(golden_alias, "golden_alias")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT signal, attempt_count FROM forced_reconcile_state "
                    "WHERE golden_alias = %s",
                    (golden_alias,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {"signal": row[0], "attempt_count": int(row[1])}

    def clear_forced_reconcile_state(self, golden_alias: str) -> None:
        """Forget the state (the stale signal cleared)."""
        _require(golden_alias, "golden_alias")
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM forced_reconcile_state WHERE golden_alias = %s",
                    (golden_alias,),
                )
            conn.commit()
