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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

NODE_ACTIVE_THRESHOLD_SECONDS = 30  # NodeHeartbeatService default rule
MISSING_NODES_SHOWN = 50  # node ids listed by the readiness checklist


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
    tx: SiemTx,
    version: int,
    enabled: bool,
    destination_key: Optional[str],
    config_epoch: str,
) -> None:
    """A newer committed version keeps the arming only for the SAME
    destination in the SAME configuration lifetime (Bug #2018)."""
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
        "AND canary_config_epoch = ? THEN armed_destination_key ELSE NULL END "
        "WHERE id = 1 AND seen_config_version < ?",
        (version, destination_key, config_epoch, version),
    )


# The readiness predicates, defined ONCE and shared by the arming statement
# and the operator checklist (``readiness_view``).  Over ``siem_process_status
# p``: LIVE_PROCESS takes (now); NOT_READY_FOR_DESTINATION takes
# (destination_key, probe-fresh cutoff).  Over ``cluster_nodes n`` (PostgreSQL
# only): NODE_WITHOUT_LIVE_PROCESS takes (heartbeat cutoff, now).
LIVE_PROCESS = "p.expires_at > ?"
NOT_READY_FOR_DESTINATION = (
    "NOT (p.probe_result = 'ok' AND p.destination_key IS NOT NULL "
    "AND p.destination_key = ? AND p.probed_at IS NOT NULL AND p.probed_at >= ?)"
)
NODE_WITHOUT_LIVE_PROCESS = (
    "n.status = 'online' AND n.last_heartbeat >= ? AND NOT EXISTS (SELECT 1 FROM "
    "siem_process_status p WHERE p.node_id = n.node_id AND p.expires_at > ?)"
)
# The canary half of the arming predicate, over the state row's own columns:
# both take (destination_key, mapping_version, config_epoch).  A canary
# belongs to one destination, mapping AND configuration lifetime (Bug #2018);
# canary_is_for() is the same check over a state row read in Python.
CANARY_FOR = (
    "canary_destination_key = ? AND canary_mapping_version = ? "
    "AND canary_config_epoch = ?"
)
CANARY_CONFIRMED_FOR = (
    f"{CANARY_FOR} AND canary_result = 'accepted' "
    "AND canary_visible_confirmed_at IS NOT NULL"
)


def canary_is_for(
    state: Mapping[str, Any],
    *,
    destination_key: Optional[str],
    mapping_version: int,
    config_epoch: str,
) -> bool:
    """:data:`CANARY_FOR` over a state row: the recorded canary belongs to
    this destination, mapping and configuration lifetime.  The one check the
    status line (``admin.capture_status``) and the arming checklist
    (``ops_documents.arming_document``) share."""
    return (
        destination_key is not None
        and state.get("canary_destination_key") == destination_key
        and state.get("canary_mapping_version") == mapping_version
        and state.get("canary_config_epoch") == config_epoch
    )


def _arm(
    tx: SiemTx,
    version: int,
    destination_key: str,
    mapping_version: int,
    config_epoch: str,
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
        f"AND {CANARY_CONFIRMED_FOR} "
        f"AND EXISTS (SELECT 1 FROM siem_process_status p WHERE {LIVE_PROCESS}) "
        f"AND NOT EXISTS (SELECT 1 FROM siem_process_status p WHERE {LIVE_PROCESS} "
        f"AND {NOT_READY_FOR_DESTINATION})"
    )
    params: List[Any] = [
        destination_key,
        now_p,
        version,
        version,
        destination_key,
        mapping_version,
        config_epoch,
        now_p,
        now_p,
        destination_key,
        fresh_p,
    ]
    if tx.dialect.name == "postgres":
        # Node quorum: every active heartbeat node has a live process row.
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM cluster_nodes n "
            f"WHERE {NODE_WITHOUT_LIVE_PROCESS})"
        )
        params += [now - timedelta(seconds=NODE_ACTIVE_THRESHOLD_SECONDS), now_p]
    return tx.execute(sql, params)


def readiness_view(
    db: SiemDb,
    destination_key: str,
    probe_fresh_seconds: float,
    detail_limit: int = 200,
) -> Dict[str, Any]:
    """Why the fleet is (not) ready for *destination_key*.

    The aggregates are ONE statement over the FULL live set with the same
    predicates as :func:`_arm`; only the displayed process rows are capped
    (failing first), flagged by ``details_truncated``."""
    if not destination_key:
        raise ValueError("readiness needs a configured destination")
    if detail_limit < 1 or probe_fresh_seconds <= 0:
        raise ValueError("detail_limit and probe_fresh_seconds must be positive")

    def _q(tx: SiemTx) -> Dict[str, Any]:
        now = tx.now()
        cut = _Cutoffs(
            tx.ts(now),
            tx.ts(now - timedelta(seconds=probe_fresh_seconds)),
            tx.ts(now - timedelta(seconds=NODE_ACTIVE_THRESHOLD_SECONDS)),
        )
        rows = _readiness_rows(tx, destination_key, cut, detail_limit + 1)
        return {
            **_readiness_aggregates(tx, destination_key, cut),
            "processes": [_process_detail(r) for r in rows[:detail_limit]],
            "details_truncated": len(rows) > detail_limit,
            "missing_nodes": _readiness_missing_nodes(tx, cut),
        }

    return db.read(_q)


@dataclass(frozen=True)
class _Cutoffs:
    """The instants one readiness read compares against, derived once from
    the database clock.  ``Any``: ``SiemTx.ts`` yields an ISO string on
    SQLite and a datetime on PostgreSQL."""

    now: Any
    fresh: Any
    heartbeat: Any


def _readiness_aggregates(
    tx: SiemTx, destination_key: str, cut: _Cutoffs
) -> Dict[str, Any]:
    sql = (
        "SELECT (SELECT COUNT(*) FROM siem_process_status p "
        f"WHERE {LIVE_PROCESS}) AS total_live, "
        "(SELECT COUNT(*) FROM siem_process_status p "
        f"WHERE {LIVE_PROCESS} AND {NOT_READY_FOR_DESTINATION}) AS failing"
    )
    params: List[Any] = [cut.now, cut.now, destination_key, cut.fresh]
    if tx.dialect.name == "postgres":
        sql += (
            ", (SELECT COUNT(*) FROM cluster_nodes n "
            f"WHERE {NODE_WITHOUT_LIVE_PROCESS}) AS nodes_without_process"
        )
        params += [cut.heartbeat, cut.now]
    agg = tx.one(sql, params)
    if agg is None:
        raise RuntimeError("readiness aggregate returned no row")
    total, failing = int(agg["total_live"]), int(agg["failing"])
    nodes_without = int(agg.get("nodes_without_process") or 0)
    return {
        "total_live": total,
        "failing": failing,
        "nodes_without_process": nodes_without,
        "all_ready": total > 0 and failing == 0 and nodes_without == 0,
    }


def _readiness_rows(
    tx: SiemTx, destination_key: str, cut: "_Cutoffs", limit: int
) -> List[Dict[str, Any]]:
    """Live process rows, failing first, then by process id (capped)."""
    return tx.query(
        "SELECT process_id, node_id, destination_key, probe_result, probed_at, "
        f"CASE WHEN {NOT_READY_FOR_DESTINATION} THEN 1 ELSE 0 END AS failing "
        f"FROM siem_process_status p WHERE {LIVE_PROCESS} "
        "ORDER BY failing DESC, process_id LIMIT ?",
        (destination_key, cut.fresh, cut.now, limit),
    )


def _readiness_missing_nodes(tx: SiemTx, cut: "_Cutoffs") -> List[str]:
    """PostgreSQL: online nodes without a live process (none on SQLite)."""
    if tx.dialect.name != "postgres":
        return []
    rows = tx.query(
        "SELECT n.node_id FROM cluster_nodes n "
        f"WHERE {NODE_WITHOUT_LIVE_PROCESS} ORDER BY n.node_id LIMIT ?",
        (cut.heartbeat, cut.now, MISSING_NODES_SHOWN),
    )
    return [str(r["node_id"]) for r in rows]


def _process_detail(row: Dict[str, Any]) -> Dict[str, Any]:
    from code_indexer.server.services.siem_delivery.db import Dialect

    probed = Dialect.parse_ts(row["probed_at"])
    return {
        "process_id": row["process_id"],
        "node_id": row["node_id"],
        "destination_key": row["destination_key"],
        "probe_result": row["probe_result"],
        "probed_at": probed.isoformat() if probed else None,
        "ready": int(row["failing"]) == 0,
    }


def fence_and_arm(
    db: SiemDb,
    *,
    version: int,
    enabled: bool,
    destination_key: Optional[str],
    mapping_version: int,
    config_epoch: str,
    probe_fresh_seconds: float,
) -> Dict[str, Any]:
    """Apply the committed config version (monotonic), try to arm, and
    return the resulting state row.  *config_epoch* is the committed
    section's ``arming_epoch``."""

    def _do(tx: SiemTx) -> Dict[str, Any]:
        _fence(tx, version, enabled, destination_key, config_epoch)
        if enabled and destination_key is not None:
            _arm(
                tx,
                version,
                destination_key,
                mapping_version,
                config_epoch,
                probe_fresh_seconds,
            )
        return state_in(tx)

    return db.write(_do, phase="arming")


def capture_active(
    state: Dict[str, Any],
    *,
    version: int,
    enabled: bool,
    destination_key: Optional[str],
    config_epoch: str,
) -> bool:
    """Capture is armed for the committed configuration *version*.

    Fail closed (Bug #2018): a committed change that ended the
    configuration lifetime (a new *config_epoch*) reads as NOT active even
    before the fence clears ``armed_destination_key``."""
    return bool(
        enabled
        and destination_key is not None
        and state.get("armed_destination_key") == destination_key
        and state.get("canary_config_epoch") == config_epoch
        and int(state.get("seen_config_version") or 0) <= version
    )


# --- canary --------------------------------------------------------------------


# record_canary refusal reasons (nothing is changed when refused)
CANARY_CREDENTIAL_CHANGED = "credential_changed"
CANARY_STALE_LIFETIME = "stale_lifetime"
CANARY_SUPERSEDED = "superseded"


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
    config_epoch: str,
    credential_id: Optional[str],
    started_at: Any,  # a SiemTx.ts value: ISO text (SQLite) or datetime (PG)
    committed_epoch: Callable[[], str],
) -> Optional[str]:
    """Record a canary run for the configuration lifetime *config_epoch*,
    sent with the credential *credential_id* (read BEFORE the send), whose
    send started at the database time *started_at* (stored as the run's
    ``canary_sent_at``).

    None when recorded.  Otherwise the refusal reason, with NOTHING changed
    (checked under the state-row lock): the stored credential changed
    meanwhile (CANARY_CREDENTIAL_CHANGED: the canary proves the old key),
    the committed configuration lifetime is no longer *config_epoch*
    (CANARY_STALE_LIFETIME), or a run that started later was already
    recorded (CANARY_SUPERSEDED).  A canary of another lifetime also ends an
    arming made in the old one."""

    def _do(tx: SiemTx) -> Optional[str]:
        state_in(tx, lock=True)  # serialise with credential changes
        stored = tx.one(
            "SELECT credential_id FROM siem_delivery_credential WHERE id = 1"
        )
        if (stored["credential_id"] if stored else None) != credential_id:
            return CANARY_CREDENTIAL_CHANGED
        if committed_epoch() != config_epoch:
            return CANARY_STALE_LIFETIME
        if tx.one(
            "SELECT 1 AS newer FROM siem_delivery_state "
            "WHERE id = 1 AND canary_sent_at > ?",
            (started_at,),
        ):
            return CANARY_SUPERSEDED
        tx.execute(
            "UPDATE siem_delivery_state SET canary_run_id = ?, "
            "canary_destination_key = ?, canary_mapping_version = ?, "
            "canary_expected = ?, canary_sent_at = ?, canary_result = ?, "
            "canary_result_signature = ?, canary_actor = ?, "
            "canary_confirmed_ids = NULL, canary_missing_action_types = NULL, "
            "canary_visible_confirmed_by = NULL, canary_visible_confirmed_at = NULL, "
            "armed_destination_key = CASE WHEN canary_config_epoch = ? "
            "THEN armed_destination_key ELSE NULL END, canary_config_epoch = ? "
            "WHERE id = 1",
            (
                run_id,
                destination_key,
                mapping_version,
                json.dumps(list(expected)),
                started_at,
                result,
                signature,
                actor,
                config_epoch,
                config_epoch,
            ),
        )
        return None

    refused: Optional[str] = db.write(_do, phase="canary")
    return refused


def invalidate_canary(tx: SiemTx) -> None:
    """Forget the canary run and its confirmation, and disarm (Bug #2018):
    the service-account credential was replaced or removed.  The caller
    holds the state-row lock.

    ``canary_config_epoch`` is left as is: it is NOT NULL and, with
    ``canary_result`` cleared, no row can satisfy CANARY_CONFIRMED_FOR until
    a new canary is recorded (which rewrites it)."""
    tx.execute(
        "UPDATE siem_delivery_state SET armed_destination_key = NULL, "
        "canary_run_id = NULL, canary_destination_key = NULL, "
        "canary_mapping_version = NULL, canary_expected = NULL, "
        "canary_sent_at = NULL, canary_result = NULL, "
        "canary_result_signature = NULL, canary_actor = NULL, "
        "canary_confirmed_ids = NULL, canary_missing_action_types = NULL, "
        "canary_visible_confirmed_by = NULL, canary_visible_confirmed_at = NULL "
        "WHERE id = 1"
    )


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
    config_epoch: str,
) -> CanaryConfirmation:
    def _do(tx: SiemTx) -> CanaryConfirmation:
        from code_indexer.server.storage.json_column import parse_json_column

        st = state_in(tx, lock=True)
        if (
            st.get("canary_run_id") != run_id
            or st.get("canary_destination_key") != destination_key
            or st.get("canary_mapping_version") != mapping_version
            or st.get("canary_config_epoch") != config_epoch
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
