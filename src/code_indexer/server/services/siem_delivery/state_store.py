"""Fleet-wide SIEM delivery state: the single state row, per-process
readiness, destinations, the version fence, ATOMIC arming, the canary
record, and halts.  Never read or written by the audit transaction.

LOCK ORDER (every backend, every transaction):

    1. the state row  -- ``state_in(tx, lock=True)``  (SELECT ... FOR UPDATE)
    2. batch rows     -- siem_delivery_batches
    3. queue rows     -- siem_delivery_queue

A transaction that writes the state row AND any batch or queue row -- or
writes several batch/queue rows that another such transaction may also
write -- MUST take the state row lock FIRST, before its first batch/queue
write.  This serialises every such transaction on the state row, so two of
them can never hold row locks in opposite orders (a PostgreSQL deadlock
aborts one, e.g. losing a halt after a send).

State-first (checked): claim (``claim._phase1``, ``claim._phase4``,
``claim.claim_specific``, including the lease and fence); completion
(``completion._own`` for accepted, retry, rejection and systemic, and
``completion.dissolve_batch``); probes (``probe._clear`` / ``_defer``, and
their sends go through claim/completion); the mapping-version requeue rounds;
admin resume / acknowledge / rebatch / quarantine requeue / retarget and
abandon (batch close and row moves, each re-reading the committed
destination and bounded by an id snapshot taken when the action started).  State row only: arming and the fence,
canary record/confirm, stats refresh.  Exempt (one row, or rows no other
path writes): send start / release (one batch row each), retention (deletes
terminal rows only), and the capture INSERT inside the audit transaction
(new rows only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

NODE_ACTIVE_THRESHOLD_SECONDS = 30  # NodeHeartbeatService default rule


def read_state(db: SiemDb) -> Dict[str, Any]:
    row = db.read(lambda tx: tx.one("SELECT * FROM siem_delivery_state WHERE id = 1"))
    assert row is not None, "siem_delivery_state row missing"
    return row


def state_in(tx: SiemTx, *, lock: bool = False) -> Dict[str, Any]:
    suffix = tx.dialect.for_update if lock else ""
    row = tx.one(f"SELECT * FROM siem_delivery_state WHERE id = 1{suffix}")
    assert row is not None, "siem_delivery_state row missing"
    return row


# --- per-process readiness -----------------------------------------------------


def register_process(
    db: SiemDb, process_id: str, *, node_id: str, ttl_seconds: float
) -> None:
    """The registration barrier: one INSERT (probe 'pending')."""

    def _do(tx: SiemTx) -> None:
        now = tx.now()
        tx.execute(
            "INSERT INTO siem_process_status (process_id, node_id, destination_key, "
            "probe_result, probed_at, last_seen_at, expires_at) "
            "VALUES (?, ?, NULL, 'pending', NULL, ?, ?) "
            "ON CONFLICT (process_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
            "expires_at = excluded.expires_at",
            (
                process_id,
                node_id,
                tx.ts(now),
                tx.ts(now + timedelta(seconds=ttl_seconds)),
            ),
        )

    db.write(_do, phase="process_status")


def refresh_process(
    db: SiemDb,
    process_id: str,
    *,
    node_id: str,
    ttl_seconds: float,
    refresh_when_remaining: float,
) -> bool:
    """Extend liveness only when little time remains; True when written."""

    def _check(tx: SiemTx) -> bool:
        row = tx.one(
            "SELECT expires_at FROM siem_process_status WHERE process_id = ?",
            (process_id,),
        )
        if row is None:
            return True
        expires = tx.dialect.parse_ts(row["expires_at"])
        assert expires is not None
        return expires - tx.now() < timedelta(seconds=refresh_when_remaining)

    if not db.read(_check):
        return False

    def _do(tx: SiemTx) -> bool:
        now = tx.now()
        updated = tx.execute(
            "UPDATE siem_process_status SET last_seen_at = ?, expires_at = ? "
            "WHERE process_id = ?",
            (tx.ts(now), tx.ts(now + timedelta(seconds=ttl_seconds)), process_id),
        )
        if updated == 0:
            tx.execute(
                "INSERT INTO siem_process_status (process_id, node_id, probe_result, "
                "last_seen_at, expires_at) VALUES (?, ?, 'pending', ?, ?)",
                (
                    process_id,
                    node_id,
                    tx.ts(now),
                    tx.ts(now + timedelta(seconds=ttl_seconds)),
                ),
            )
        return True

    return bool(db.write(_do, phase="process_status"))


def record_probe(
    db: SiemDb, process_id: str, *, destination_key: Optional[str], result: str
) -> None:
    def _do(tx: SiemTx) -> None:
        tx.execute(
            "UPDATE siem_process_status SET probe_result = ?, probed_at = ?, "
            "destination_key = ? WHERE process_id = ?",
            (result, tx.ts(tx.now()), destination_key, process_id),
        )

    db.write(_do, phase="process_status")


def deregister_process(db: SiemDb, process_id: str) -> None:
    db.write(
        lambda tx: tx.execute(
            "DELETE FROM siem_process_status WHERE process_id = ?", (process_id,)
        ),
        phase="process_status",
    )


def live_processes(db: SiemDb, limit: int = 200) -> List[Dict[str, Any]]:
    def _q(tx: SiemTx) -> List[Dict[str, Any]]:
        return tx.query(
            "SELECT process_id, node_id, destination_key, probe_result, probed_at, "
            "expires_at FROM siem_process_status WHERE expires_at > ? "
            "ORDER BY process_id LIMIT ?",
            (tx.ts(tx.now()), limit),
        )

    return db.read(_q)


def upsert_destination(db: SiemDb, key: str, cfg: SiemDeliveryConfig) -> None:
    def _do(tx: SiemTx) -> None:
        tx.execute(
            "INSERT INTO siem_destinations (destination_key, region, project_id, "
            "location, instance_id, first_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (destination_key) DO NOTHING",
            (
                key,
                cfg.region or None,
                cfg.project_id,
                cfg.location,
                cfg.instance_id,
                tx.ts(tx.now()),
            ),
        )

    db.write(_do, phase="process_status")


# --- version fence and atomic arming ---------------------------------------------


def _fence(
    tx: SiemTx, version: int, enabled: bool, destination_key: Optional[str]
) -> None:
    if not enabled or destination_key is None:
        tx.execute(
            "UPDATE siem_delivery_state SET seen_config_version = ?, "
            "armed_destination_key = NULL WHERE id = 1 AND seen_config_version < ?",
            (version, version),
        )
        return
    tx.execute(
        "UPDATE siem_delivery_state SET seen_config_version = ?, "
        "armed_destination_key = CASE WHEN armed_destination_key = ? "
        "THEN armed_destination_key ELSE NULL END "
        "WHERE id = 1 AND seen_config_version < ?",
        (version, destination_key, version),
    )


def _arm(
    tx: SiemTx,
    version: int,
    destination_key: str,
    mapping_version: int,
    probe_fresh_seconds: float,
) -> int:
    """ONE conditional UPDATE: the whole readiness predicate is evaluated in
    this statement's snapshot (SQLite: inside BEGIN EXCLUSIVE)."""
    now = tx.now()
    now_p = tx.ts(now)
    fresh_p = tx.ts(now - timedelta(seconds=probe_fresh_seconds))
    sql = (
        "UPDATE siem_delivery_state SET armed_destination_key = ?, armed_at = ?, "
        "armed_config_version = ? "
        "WHERE id = 1 AND armed_destination_key IS NULL "
        "AND seen_config_version <= ? "
        "AND canary_destination_key = ? AND canary_result = 'accepted' "
        "AND canary_mapping_version = ? "
        "AND canary_visible_confirmed_at IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM siem_process_status p WHERE p.expires_at > ?) "
        "AND NOT EXISTS (SELECT 1 FROM siem_process_status p WHERE p.expires_at > ? "
        "AND NOT (p.probe_result = 'ok' AND p.destination_key IS NOT NULL "
        "AND p.destination_key = ? AND p.probed_at IS NOT NULL AND p.probed_at >= ?))"
    )
    params: List[Any] = [
        destination_key,
        now_p,
        version,
        version,
        destination_key,
        mapping_version,
        now_p,
        now_p,
        destination_key,
        fresh_p,
    ]
    if tx.dialect.name == "postgres":
        # Node quorum: every active heartbeat node has a live process row.
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM cluster_nodes n WHERE n.status = 'online' "
            "AND n.last_heartbeat >= ? AND NOT EXISTS (SELECT 1 FROM "
            "siem_process_status p WHERE p.node_id = n.node_id AND p.expires_at > ?))"
        )
        params += [now - timedelta(seconds=NODE_ACTIVE_THRESHOLD_SECONDS), now_p]
    return tx.execute(sql, params)


def fence_and_arm(
    db: SiemDb,
    *,
    version: int,
    enabled: bool,
    destination_key: Optional[str],
    mapping_version: int,
    probe_fresh_seconds: float,
) -> Dict[str, Any]:
    """Apply the committed config version (monotonic), try to arm, and
    return the resulting state row."""

    def _do(tx: SiemTx) -> Dict[str, Any]:
        _fence(tx, version, enabled, destination_key)
        if enabled and destination_key is not None:
            _arm(tx, version, destination_key, mapping_version, probe_fresh_seconds)
        return state_in(tx)

    return db.write(_do, phase="arming")


def capture_active(
    state: Dict[str, Any],
    *,
    version: int,
    enabled: bool,
    destination_key: Optional[str],
) -> bool:
    return bool(
        enabled
        and destination_key is not None
        and state.get("armed_destination_key") == destination_key
        and int(state.get("seen_config_version") or 0) <= version
    )


# --- canary --------------------------------------------------------------------


def record_canary(
    db: SiemDb,
    *,
    run_id: str,
    destination_key: str,
    mapping_version: int,
    expected: Sequence[Dict[str, str]],
    result: str,
    signature: Optional[str],
    actor: str,
) -> None:
    def _do(tx: SiemTx) -> None:
        tx.execute(
            "UPDATE siem_delivery_state SET canary_run_id = ?, "
            "canary_destination_key = ?, canary_mapping_version = ?, "
            "canary_expected = ?, canary_sent_at = ?, canary_result = ?, "
            "canary_result_signature = ?, canary_actor = ?, "
            "canary_confirmed_ids = NULL, canary_missing_action_types = NULL, "
            "canary_visible_confirmed_by = NULL, canary_visible_confirmed_at = NULL "
            "WHERE id = 1",
            (
                run_id,
                destination_key,
                mapping_version,
                json.dumps(list(expected)),
                tx.ts(tx.now()),
                result,
                signature,
                actor,
            ),
        )

    db.write(_do, phase="canary")


@dataclass
class CanaryConfirmation:
    stale: bool = False
    confirmed: bool = False
    expected_count: int = 0
    confirmed_count: int = 0
    missing_action_types: List[str] = field(default_factory=list)


def confirm_canary(
    db: SiemDb,
    *,
    run_id: str,
    destination_key: Optional[str],
    mapping_version: int,
    visible_ids: Sequence[str],
    actor: str,
) -> CanaryConfirmation:
    def _do(tx: SiemTx) -> CanaryConfirmation:
        from code_indexer.server.storage.json_column import parse_json_column

        st = state_in(tx, lock=True)
        if (
            st.get("canary_run_id") != run_id
            or st.get("canary_destination_key") != destination_key
            or st.get("canary_mapping_version") != mapping_version
            or st.get("canary_result") != "accepted"
        ):
            return CanaryConfirmation(stale=True)
        expected = (
            parse_json_column(st.get("canary_expected"), list, "canary_expected") or []
        )
        visible = set(visible_ids)
        confirmed = [e for e in expected if e.get("product_log_id") in visible]
        missing = sorted(
            {str(e["action_type"]) for e in expected if e not in confirmed}
        )
        done = not missing and bool(expected)
        tx.execute(
            "UPDATE siem_delivery_state SET canary_confirmed_ids = ?, "
            "canary_missing_action_types = ?, canary_visible_confirmed_by = ?, "
            "canary_visible_confirmed_at = ? WHERE id = 1",
            (
                json.dumps([e["product_log_id"] for e in confirmed]),
                json.dumps(missing),
                actor if done else None,
                tx.ts(tx.now()) if done else None,
            ),
        )
        return CanaryConfirmation(
            confirmed=done,
            expected_count=len(expected),
            confirmed_count=len(confirmed),
            missing_action_types=missing,
        )

    return db.write(_do, phase="canary")


# --- halt ------------------------------------------------------------------------


def halt(
    tx: SiemTx,
    *,
    cls: str,
    signature: str,
    batch_id: Optional[str],
    mapping_version: int,
    probe_interval_seconds: float,
) -> None:
    now = tx.now()
    tx.execute(
        "UPDATE siem_delivery_state SET halted_class = ?, halted_signature = ?, "
        "halted_since = ?, halted_batch_id = ?, halted_mapping_version = ?, "
        "next_probe_at = ? WHERE id = 1",
        (
            cls,
            signature,
            tx.ts(now),
            batch_id,
            mapping_version,
            tx.ts(now + timedelta(seconds=probe_interval_seconds)),
        ),
    )


def clear_halt(tx: SiemTx, *, reset_quarantine_window: bool) -> int:
    sql = (
        "UPDATE siem_delivery_state SET halted_class = NULL, halted_signature = NULL, "
        "halted_since = NULL, halted_batch_id = NULL, halted_mapping_version = NULL, "
        "next_probe_at = NULL"
    )
    params: List[Any] = []
    if reset_quarantine_window:
        sql += ", quarantine_window_count = 0, quarantine_window_start = ?"
        params.append(tx.ts(tx.now()))
    return tx.execute(sql + " WHERE id = 1", params)
