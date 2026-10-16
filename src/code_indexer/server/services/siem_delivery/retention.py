"""Paced retention of TERMINAL SIEM rows (called by DataRetentionScheduler).

Each statement deletes at most ``PRUNE_ROWS_PER_TX`` rows in its own short
transaction, selected by an equality on the leading index column plus a
range on the second (an index range scan that stops after 500 entries).
Cutoffs are computed in DATABASE time.  Pending, batched and quarantined
rows are never deleted.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Dict, List, Tuple

from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx

PRUNE_ROWS_PER_TX = 500
PRUNE_YIELD_SECONDS = 0.05
PRUNE_TIME_BUDGET_SECONDS = 120.0
DELIVERED_RETENTION_HOURS = 24
TERMINAL_BATCH_STATES = ("delivered", "split", "dissolved", "retargeted")
_MAX_ROUNDS_PER_TARGET = 1_000_000

# (table, key column, status column, status value, cutoff column, hours or
# None for the audit retention)
Target = Tuple[str, str, str, str, str, object]


def _targets(audit_retention_hours: float) -> List[Target]:
    targets: List[Target] = [
        (
            "siem_delivery_queue",
            "id",
            "status",
            "delivered",
            "delivered_at",
            DELIVERED_RETENTION_HOURS,
        ),
        (
            "siem_delivery_queue",
            "id",
            "status",
            "abandoned",
            "created_at",
            audit_retention_hours,
        ),
        (
            "siem_delivery_queue",
            "id",
            "status",
            "unrecoverable",
            "created_at",
            audit_retention_hours,
        ),
    ]
    for state in TERMINAL_BATCH_STATES:
        targets.append(
            ("siem_delivery_batches", "batch_id", "state", state, "created_at", 24)
        )
    return targets


def _delete_round(db: SiemDb, target: Target) -> int:
    table, key, status_col, status, cutoff_col, hours = target

    def _do(tx: SiemTx) -> int:
        cutoff = tx.ts(tx.now() - timedelta(hours=float(hours)))  # type: ignore[arg-type]
        return tx.execute(
            f"DELETE FROM {table} WHERE {key} IN (SELECT {key} FROM {table} "
            f"WHERE {status_col} = ? AND {cutoff_col} < ? ORDER BY {cutoff_col} LIMIT ?)",
            (status, cutoff, PRUNE_ROWS_PER_TX),
        )

    return int(db.write(_do, phase="retention"))


def _simple_prune(db: SiemDb, table: str, column: str) -> int:
    def _do(tx: SiemTx) -> int:
        cutoff = tx.ts(tx.now() - timedelta(hours=24))
        return tx.execute(
            f"DELETE FROM {table} WHERE {column} IN (SELECT {column} FROM {table} "
            f"WHERE {column} < ? ORDER BY {column} LIMIT ?)",
            (cutoff, PRUNE_ROWS_PER_TX),
        )

    return int(db.write(_do, phase="retention"))


def prune_terminal(
    db: SiemDb, stop_event: threading.Event, *, audit_retention_hours: float
) -> Dict[str, int]:
    """Delete terminal rows; returns counts per target (bounded per run)."""
    deadline = time.monotonic() + PRUNE_TIME_BUDGET_SECONDS
    counts: Dict[str, int] = {}
    for target in _targets(audit_retention_hours):
        name = f"{target[0]}:{target[3]}"
        counts[name] = 0
        for _ in range(_MAX_ROUNDS_PER_TARGET):
            if stop_event.is_set() or time.monotonic() > deadline:
                return counts
            deleted = _delete_round(db, target)
            counts[name] += deleted
            if deleted < PRUNE_ROWS_PER_TX:
                break
            stop_event.wait(PRUNE_YIELD_SECONDS)
    for table, column in (
        ("siem_backlog_samples", "sampled_at"),
        ("siem_process_status", "expires_at"),
    ):
        counts[table] = _simple_prune(db, table, column)
    return counts
