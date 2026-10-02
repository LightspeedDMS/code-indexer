"""Bounded SIEM delivery statistics (never a scan of the queue).

Every count is an index range capped at ``COUNT_CAP + 1`` rows (reported as
"10,000+"); minima are index seeks.  ONE process per refresh interval
computes the persisted fleet snapshot (claimed with a conditional UPDATE),
including backlog samples and the bounded scan of SIEM boundary rows that
counts pilot rows captured after a disable/clear/change/reset.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.services.siem_delivery.capture import (
    CAPTURE_SNAPSHOT_MAX_AGE,
)
from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx

logger = logging.getLogger(__name__)

COUNT_CAP = 10_000
BYTES_PER_PENDING_ROW = 4096  # assumed average row size (to be measured)
SAMPLE_RETENTION_HOURS = 24
SLOPE_SAMPLES = 60
BOUNDARY_SCAN_ROWS = 1000
MAX_KNOWN_DESTINATIONS = 50
_DESTINATION_CHANGE = "destination_change"
_CLOSING_KINDS = ("disable", "clear", _DESTINATION_CHANGE, "reset")
_OPENING_KINDS = ("enable", _DESTINATION_CHANGE)


def capped_count(tx: SiemTx, status: str, destination_key: Optional[str] = None) -> int:
    if destination_key is None:
        row = tx.one(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM siem_delivery_queue "
            "WHERE status = ? LIMIT ?) t",
            (status, COUNT_CAP + 1),
        )
    else:
        row = tx.one(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM siem_delivery_queue "
            "WHERE status = ? AND destination_key = ? LIMIT ?) t",
            (status, destination_key, COUNT_CAP + 1),
        )
    return int(row["n"]) if row else 0


def _min_created(tx: SiemTx, status: str) -> Any:
    row = tx.one(
        "SELECT MIN(created_at) AS m FROM siem_delivery_queue WHERE status = ?",
        (status,),
    )
    return tx.dialect.parse_ts(row["m"]) if row else None


MIN_ID_SQL = "SELECT MIN(id) AS m FROM siem_delivery_queue WHERE status = ?"


def _min_id(tx: SiemTx, status: str) -> Optional[int]:
    """Lowest id with *status* across EVERY destination: a seek on the
    (status, id) index."""
    row = tx.one(MIN_ID_SQL, (status,))
    return int(row["m"]) if row and row["m"] is not None else None


def due_work(tx: SiemTx, destination_key: Optional[str]) -> bool:
    """A due unbatched row or a due open batch exists for the destination."""
    if destination_key is None:
        return False
    now = tx.ts(tx.now())
    row = tx.one(
        "SELECT 1 AS x FROM siem_delivery_queue WHERE status = 'pending' "
        "AND destination_key = ? AND next_attempt_at <= ? AND batch_id IS NULL LIMIT 1",
        (destination_key, now),
    )
    if row is not None:
        return True
    batch = tx.one(
        "SELECT 1 AS x FROM siem_delivery_batches WHERE destination_key = ? "
        "AND state = 'pending_send' AND next_attempt_at <= ? "
        "AND (lease_expires_at IS NULL OR lease_expires_at < ?) LIMIT 1",
        (destination_key, now, now),
    )
    return batch is not None


def live_counts(tx: SiemTx, destination_key: Optional[str]) -> Dict[str, Any]:
    """Bounded counts for the stats endpoint (pending = not yet delivered)."""
    pending = capped_count(tx, "pending")
    batched = capped_count(tx, "batched")
    quarantined = capped_count(tx, "quarantined")
    oldest = [
        t for t in (_min_created(tx, "pending"), _min_created(tx, "batched")) if t
    ]
    oldest_at = min(oldest) if oldest else None
    now = tx.now()
    return {
        "pending": min(pending + batched, COUNT_CAP + 1),
        "pending_capped": pending + batched > COUNT_CAP,
        "batched": batched,
        "quarantined": quarantined,
        "quarantined_capped": quarantined > COUNT_CAP,
        "oldest_pending_at": oldest_at.isoformat() if oldest_at else None,
        "oldest_pending_age_seconds": (
            (now - oldest_at).total_seconds() if oldest_at else 0.0
        ),
        "unconfigured_destinations": _unconfigured(tx, destination_key),
        "due_work": due_work(tx, destination_key),
    }


def _unconfigured(tx: SiemTx, destination_key: Optional[str]) -> List[Dict[str, Any]]:
    """Pending rows still addressed to a destination that is no longer the
    configured one: driven from the (small) known-destinations table, one
    capped index count per key -- never a pass over the pending rows."""
    keys = tx.query(
        "SELECT destination_key FROM siem_destinations WHERE destination_key <> ? "
        "ORDER BY destination_key LIMIT ?",
        (destination_key or "", MAX_KNOWN_DESTINATIONS),
    )
    out = []
    for row in keys:
        key = str(row["destination_key"])
        pending = capped_count(tx, "pending", key)
        if pending:
            out.append({"destination_key": key, "pending": pending})
    return out


def _backlog_estimate(tx: SiemTx) -> int:
    top = tx.one("SELECT MAX(id) AS m FROM siem_delivery_queue")
    lows = [
        i for i in (_min_id(tx, "pending"), _min_id(tx, "batched")) if i is not None
    ]
    if not lows or top is None or top["m"] is None:
        return 0
    return int(top["m"]) - min(lows) + 1


def _slope_per_hour(tx: SiemTx) -> float:
    rows = tx.query(
        "SELECT sampled_at, backlog_rows_estimate FROM siem_backlog_samples "
        "ORDER BY sampled_at DESC LIMIT ?",
        (SLOPE_SAMPLES,),
    )
    if len(rows) < 2:
        return 0.0
    newest, oldest = rows[0], rows[-1]
    t1 = tx.dialect.parse_ts(newest["sampled_at"])
    t0 = tx.dialect.parse_ts(oldest["sampled_at"])
    hours = (t1 - t0).total_seconds() / 3600.0 if t1 and t0 else 0.0
    if hours <= 0:
        return 0.0
    return (
        int(newest["backlog_rows_estimate"]) - int(oldest["backlog_rows_estimate"])
    ) / hours


def _window_count(tx: SiemTx, destination_key: str, start: Any, end: Any) -> int:
    """Rows captured for *destination_key* in (start, end]; index range on
    (destination_key, created_at), capped."""
    row = tx.one(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM siem_delivery_queue WHERE "
        "destination_key = ? AND created_at > ? AND created_at <= ? "
        "AND boundary_kind IS NULL LIMIT ?) t",
        (destination_key, tx.ts(start), tx.ts(end), COUNT_CAP + 1),
    )
    return int(row["n"]) if row else 0


def _closed_keys(tx: SiemTx, boundary: Dict[str, Any]) -> List[str]:
    """Destinations a closing boundary stops capture for: its own, or -- for
    a destination change -- every OTHER known destination."""
    if boundary["boundary_kind"] != _DESTINATION_CHANGE:
        return [str(boundary["destination_key"])]
    rows = tx.query(
        "SELECT destination_key FROM siem_destinations WHERE destination_key <> ? "
        "ORDER BY destination_key LIMIT ?",
        (boundary["destination_key"], MAX_KNOWN_DESTINATIONS),
    )
    return [str(r["destination_key"]) for r in rows]


def _interval_end(tx: SiemTx, boundary: Dict[str, Any]) -> Any:
    """When capture for the closed destination(s) legitimately resumed: the
    next opening boundary (None while the interval is still open)."""
    if boundary["boundary_kind"] == _DESTINATION_CHANGE:
        row = tx.one(
            "SELECT MIN(created_at) AS m FROM siem_delivery_queue "
            "WHERE boundary_kind = ? AND id > ?",
            (_DESTINATION_CHANGE, boundary["id"]),
        )
    else:
        row = tx.one(
            "SELECT MIN(created_at) AS m FROM siem_delivery_queue "
            "WHERE boundary_kind IN (?, ?) AND destination_key = ? AND id > ?",
            (*_OPENING_KINDS, boundary["destination_key"], boundary["id"]),
        )
    return tx.dialect.parse_ts(row["m"]) if row else None


def _scan_boundaries(tx: SiemTx, state: Dict[str, Any]) -> Dict[str, int]:
    """Count pilot rows captured after closing boundaries.

    Each closing boundary opens an interval that ends at the next opening
    boundary for the same destination(s); rows inside the 90 s snapshot bound
    are ``after``, rows beyond it are ``late`` (a defect).  An interval is
    SETTLED -- counted once into the persisted totals and passed by the
    cursor -- only when it has ended and its end is older than the bound;
    open intervals are recounted on every refresh, so rows that arrive later
    stay observable.  Bounded: BOUNDARY_SCAN_ROWS boundary rows per refresh.
    """
    now = tx.now()
    window = timedelta(seconds=CAPTURE_SNAPSHOT_MAX_AGE)
    cursor = int(state.get("last_boundary_scanned_id") or 0)
    rows = tx.query(
        "SELECT id, destination_key, boundary_kind, created_at FROM siem_delivery_queue "
        "WHERE boundary_kind IS NOT NULL AND id > ? ORDER BY id LIMIT ?",
        (cursor, BOUNDARY_SCAN_ROWS),
    )
    out = {"settled_after": 0, "settled_late": 0, "open_after": 0, "open_late": 0}
    settling = True
    for row in rows:
        start = tx.dialect.parse_ts(row["created_at"])
        if row["boundary_kind"] not in _CLOSING_KINDS or start is None:
            cursor = int(row["id"]) if settling else cursor
            continue
        end = _interval_end(tx, row)
        bound = start + window
        after_end = bound if end is None else min(bound, end)
        late_end = now if end is None else end
        keys = _closed_keys(tx, row)
        after = sum(_window_count(tx, k, start, after_end) for k in keys)
        late = (
            sum(_window_count(tx, k, bound, late_end) for k in keys)
            if late_end > bound
            else 0
        )
        if settling and end is not None and end + window <= now:
            out["settled_after"] += after
            out["settled_late"] += late
            cursor = int(row["id"])
            continue
        settling = False
        out["open_after"] += after
        out["open_late"] += late
    out["last"] = cursor
    return out


def maybe_refresh_stats(
    db: SiemDb, *, refresh_seconds: float, destination_key: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Compute and persist the fleet snapshot when this process wins the
    refresh slot; returns the snapshot, or None when not due here."""

    def _claim(tx: SiemTx) -> bool:
        now = tx.now()
        n = tx.execute(
            "UPDATE siem_delivery_state SET stats_refreshed_at = ? WHERE id = 1 AND "
            "(stats_refreshed_at IS NULL OR stats_refreshed_at < ?)",
            (tx.ts(now), tx.ts(now - timedelta(seconds=refresh_seconds))),
        )
        return n == 1

    if not db.write(_claim, phase="stats"):
        return None

    def _compute(tx: SiemTx) -> Dict[str, Any]:
        from code_indexer.server.services.siem_delivery.state_store import state_in

        snap = live_counts(tx, destination_key)
        snap["backlog_rows_estimate"] = _backlog_estimate(tx)
        snap["backlog_bytes_estimate"] = (
            snap["backlog_rows_estimate"] * BYTES_PER_PENDING_ROW
        )
        snap["growth_per_hour"] = _slope_per_hour(tx)
        snap["refreshed_at"] = tx.now().isoformat()
        snap["boundary_scan"] = _scan_boundaries(tx, state_in(tx))
        return snap

    snap = db.read(_compute)
    snap["projected_hours_to_disk_full"] = _hours_to_full(db, snap["growth_per_hour"])
    scan = snap.pop("boundary_scan")

    def _persist(tx: SiemTx) -> None:
        now = tx.now()
        # reported totals = settled (persisted once) + open (recounted); the
        # right-hand sides read the row's values before this UPDATE
        tx.execute(
            "UPDATE siem_delivery_state SET stats_json = ?, "
            "boundary_settled_after_total = boundary_settled_after_total + ?, "
            "boundary_settled_late_total = boundary_settled_late_total + ?, "
            "capture_after_boundary_total = boundary_settled_after_total + ? + ?, "
            "capture_after_boundary_late_total = boundary_settled_late_total + ? + ?, "
            "last_boundary_scanned_id = ? WHERE id = 1",
            (
                json.dumps(snap),
                scan["settled_after"],
                scan["settled_late"],
                scan["settled_after"],
                scan["open_after"],
                scan["settled_late"],
                scan["open_late"],
                scan["last"],
            ),
        )
        tx.execute(
            "INSERT INTO siem_backlog_samples (sampled_at, backlog_rows_estimate) "
            "VALUES (?, ?) ON CONFLICT (sampled_at) DO NOTHING",
            (tx.ts(now), snap["backlog_rows_estimate"]),
        )

    db.write(_persist, phase="stats")
    return snap


def _hours_to_full(db: SiemDb, growth_per_hour: float) -> Optional[float]:
    """SQLite only: free space of the groups.db directory over growth."""
    if db.dialect.name != "sqlite" or not db.groups_db_path or growth_per_hour <= 0:
        return None
    free = shutil.disk_usage(str(Path(db.groups_db_path).parent)).free
    return free / (growth_per_hour * BYTES_PER_PENDING_ROW)


def persisted_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    from code_indexer.server.storage.json_column import parse_json_column

    raw = state.get("stats_json")
    if raw is None:
        return {}
    return parse_json_column(raw, dict, "stats_json") or {}


def list_quarantined(db: SiemDb, limit: int) -> List[Dict[str, Any]]:
    def _q(tx: SiemTx) -> List[Dict[str, Any]]:
        rows = tx.query(
            "SELECT event_uuid, action_type, destination_key, quarantine_reason, "
            "quarantine_signature, created_at FROM siem_delivery_queue "
            "WHERE status = 'quarantined' ORDER BY status, created_at LIMIT ?",
            (limit,),
        )
        for row in rows:
            created = tx.dialect.parse_ts(row["created_at"])
            row["created_at"] = created.isoformat() if created else None
        return rows

    return db.read(_q)
