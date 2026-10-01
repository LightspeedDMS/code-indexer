"""SIEM delivery admin actions and the stats document.

Every action is a sync function (the REST routes are sync ``def``, so they
run on the threadpool, never on the event loop) and is audited in this
shared service method with an EXPLICIT SIEM destination (the configured one,
or None when none is configured).  Responses carry enums, counts, ids and
destination keys only -- never a token, header, key material or vendor text.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from code_indexer.server.services.audit_events import SystemComponent
from code_indexer.server.services.audit_outcome import record_outcome
from code_indexer.server.services.siem_delivery import state_store, stats
from code_indexer.server.services.siem_delivery.canary import synthetic_events
from code_indexer.server.services.siem_delivery.capture import SiemTarget, capture_state
from code_indexer.server.services.siem_delivery.db import SiemTx
from code_indexer.server.services.siem_delivery.projection import project
from code_indexer.server.services.siem_delivery.sender import (
    CredentialError,
    send_batch,
)
from code_indexer.server.services.siem_delivery.udm import (
    UdmRow,
    build_udm,
    canonical_json,
    envelope,
    validate_udm,
)

logger = logging.getLogger(__name__)

RETARGET_ROWS_PER_TX = 500
_MAX_ROUNDS = 100_000


class SiemAdminError(Exception):
    """An admin action that cannot proceed (HTTP status + safe message)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _dest_target(scheduler: Any) -> Optional[SiemTarget]:
    """The configured destination (committed config, read now), or None
    when none is configured: the self-report's explicit destination."""
    ctx = scheduler.committed_context()
    return SiemTarget(ctx.destination.key, None) if ctx is not None else None


def _audit(
    scheduler: Any, actor: Any, action_type: str, details: Mapping[str, Any]
) -> None:
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details=dict(details),
        siem_destination=_dest_target(scheduler),
    )


def record_requeue_event(scheduler: Any, count: int, *, trigger: str) -> None:
    _audit(
        scheduler,
        SystemComponent.SIEM_DELIVERY,
        "siem_quarantine_requeued",
        {
            "count": count,
            "mapping_version": scheduler.mapping_version,
            "trigger": trigger,
        },
    )


def _require_ctx(scheduler: Any) -> Any:
    from code_indexer.server.services.siem_delivery.destination import (
        SiemConfigInvalid,
    )

    try:
        ctx = scheduler.committed_context()
    except SiemConfigInvalid as exc:
        raise SiemAdminError(
            409, f"SIEM configuration invalid here: {exc.field}"
        ) from None
    if ctx is None:
        raise SiemAdminError(409, "no SIEM destination is configured")
    return ctx


# --- canary ------------------------------------------------------------------


def run_canary(scheduler: Any, actor: str) -> Dict[str, Any]:
    ctx = _require_ctx(scheduler)
    run_id = str(uuid.uuid4())
    members: List[bytes] = []
    expected: List[Dict[str, str]] = []
    invalid: List[str] = []
    for event in synthetic_events(run_id):
        payload, error = project(event, mapping=ctx.mapping)
        if payload is None:
            invalid.append(event.action_type)
            continue
        udm = build_udm(
            UdmRow(
                event.event_uuid,
                event.action_type,
                json.loads(payload),
                ctx.source_instance_label,
            ),
            mapping=ctx.mapping,
            canary=True,
            count_unmapped=False,
        )
        if validate_udm(udm) is not None:
            invalid.append(event.action_type)
            continue
        members.append(canonical_json(udm))
        expected.append(
            {
                "product_log_id": event.event_uuid,
                "action_type": event.action_type,
                "event_type": str(udm["metadata"]["eventType"]),
            }
        )
    if invalid:
        raise SiemAdminError(422, f"canary failed local validation: {sorted(invalid)}")
    try:
        token = ctx.credentials.token(ctx.destination)
    except CredentialError as exc:
        raise SiemAdminError(
            503, f"cannot mint a SecOps token: {exc.result.value}"
        ) from None
    cls = send_batch(
        ctx.http_factory,
        ctx.destination,
        token,
        envelope(members),
        event_count=len(members),
        timeout=ctx.timings.request_timeout_seconds,
    )
    result = "accepted" if cls.cls == "accepted" else "rejected"
    state_store.record_canary(
        ctx.db,
        run_id=run_id,
        destination_key=ctx.destination.key,
        mapping_version=ctx.mapping_version,
        expected=expected,
        result=result,
        signature=None if result == "accepted" else cls.signature,
        actor=actor,
    )
    _audit(
        scheduler,
        actor,
        "siem_canary_sent",
        {
            "canary_run_id": run_id,
            "event_count": len(members),
            "result": result,
            "mapping_version": ctx.mapping_version,
        },
    )
    return {
        "canary_run_id": run_id,
        "result": result,
        "result_signature": None if result == "accepted" else cls.signature,
        "expected_product_log_ids": [e["product_log_id"] for e in expected],
        "event_count": len(members),
        "mapping_version": ctx.mapping_version,
    }


def confirm_visible(
    scheduler: Any, actor: str, run_id: str, visible_ids: Sequence[str]
) -> Dict[str, Any]:
    ctx = _require_ctx(scheduler)
    outcome = state_store.confirm_canary(
        ctx.db,
        run_id=run_id,
        destination_key=ctx.destination.key,
        mapping_version=ctx.mapping_version,
        visible_ids=list(visible_ids),
        actor=actor,
    )
    if outcome.stale:
        raise SiemAdminError(
            409, "canary run is stale, for another destination, or not accepted"
        )
    _audit(
        scheduler,
        actor,
        "siem_canary_visibility_confirmed",
        {
            "canary_run_id": run_id,
            "expected_count": outcome.expected_count,
            "confirmed_count": outcome.confirmed_count,
            "missing_action_types": [
                t for t in outcome.missing_action_types if t != "siem_canary_unmapped"
            ],
        },
    )
    return {
        "confirmed": outcome.confirmed,
        "expected_count": outcome.expected_count,
        "confirmed_count": outcome.confirmed_count,
        "missing_action_types": outcome.missing_action_types,
    }


# --- halts ---------------------------------------------------------------------


def resume(scheduler: Any, actor: str) -> Dict[str, Any]:
    ctx = _require_ctx(scheduler)

    def _do(tx: SiemTx) -> Optional[str]:
        st = state_store.state_in(tx, lock=True)
        halted = st.get("halted_class")
        if halted:
            state_store.clear_halt(tx, reset_quarantine_window=True)
        return str(halted) if halted else None

    halted = ctx.db.write(_do, phase="admin")
    if halted is None:
        return {"resumed": False}
    _audit(
        scheduler,
        actor,
        "siem_delivery_resumed",
        {"halted_class": halted, "signature_reset": True},
    )
    return {"resumed": True, "halted_class": halted}


def _duplicate_halted_batch(tx: SiemTx, batch_id: str) -> Dict[str, Any]:
    st = state_store.state_in(tx, lock=True)
    if (
        st.get("halted_class") != "duplicate_response"
        or st.get("halted_batch_id") != batch_id
    ):
        raise SiemAdminError(409, "batch is not the one halted by a duplicate response")
    batch = tx.one(
        "SELECT batch_id, event_count FROM siem_delivery_batches WHERE batch_id = ? "
        "AND state = 'pending_send'",
        (batch_id,),
    )
    if batch is None:
        raise SiemAdminError(409, "batch is not open")
    return batch


def acknowledge_batch(scheduler: Any, actor: str, batch_id: str) -> Dict[str, Any]:
    """The operator confirmed the batch's events are present in SecOps."""
    ctx = _require_ctx(scheduler)

    def _do(tx: SiemTx) -> int:
        batch = _duplicate_halted_batch(tx, batch_id)
        now = tx.ts(tx.now())
        tx.execute(
            "UPDATE siem_delivery_batches SET state = 'delivered', body = NULL, "
            "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE batch_id = ?",
            (batch_id,),
        )
        tx.execute(
            "UPDATE siem_delivery_queue SET status = 'delivered', delivered_at = ?, "
            "delivered_via = 'admin_acknowledged' WHERE batch_id = ? AND status = 'batched'",
            (now, batch_id),
        )
        state_store.clear_halt(tx, reset_quarantine_window=False)
        return int(batch["event_count"])

    count = ctx.db.write(_do, phase="admin")
    _audit(
        scheduler,
        actor,
        "siem_batch_acknowledged",
        {"batch_id": batch_id, "event_count": count},
    )
    return {"acknowledged": True, "batch_id": batch_id, "event_count": count}


def rebatch_batch(scheduler: Any, actor: str, batch_id: str) -> Dict[str, Any]:
    ctx = _require_ctx(scheduler)

    def _do(tx: SiemTx) -> int:
        batch = _duplicate_halted_batch(tx, batch_id)
        tx.execute(
            "UPDATE siem_delivery_queue SET status = 'pending', batch_id = NULL, "
            "batch_ordinal = NULL WHERE batch_id = ? AND status = 'batched'",
            (batch_id,),
        )
        tx.execute(
            "UPDATE siem_delivery_batches SET state = 'dissolved', body = NULL, "
            "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE batch_id = ?",
            (batch_id,),
        )
        state_store.clear_halt(tx, reset_quarantine_window=False)
        return int(batch["event_count"])

    count = ctx.db.write(_do, phase="admin")
    _audit(
        scheduler,
        actor,
        "siem_batch_rebatched",
        {"batch_id": batch_id, "event_count": count},
    )
    return {"rebatched": True, "batch_id": batch_id, "event_count": count}


# --- quarantine and destinations ---------------------------------------------------


def requeue_quarantined(
    scheduler: Any, actor: str, event_uuids: Sequence[str]
) -> Dict[str, Any]:
    if len(event_uuids) > RETARGET_ROWS_PER_TX:
        raise SiemAdminError(
            400, f"at most {RETARGET_ROWS_PER_TX} event uuids per call"
        )

    def _do(tx: SiemTx) -> int:
        state_store.state_in(tx, lock=True)  # lock order: state row first
        now = tx.ts(tx.now())
        moved = 0
        for event_uuid in event_uuids:
            moved += tx.execute(
                "UPDATE siem_delivery_queue SET status = 'pending', quarantine_reason = NULL, "
                "quarantine_signature = NULL, next_attempt_at = ? "
                "WHERE event_uuid = ? AND status = 'quarantined'",
                (now, event_uuid),
            )
        return moved

    count = scheduler.db.write(_do, phase="admin")
    record_outcome(
        actor=actor,
        action_type="siem_quarantine_requeued",
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details={
            "count": count,
            "mapping_version": scheduler.mapping_version,
            "trigger": "admin",
        },
        siem_destination=_dest_target(scheduler),
    )
    return {"requeued": count}


def _move_rows(scheduler: Any, key: str, sql: str, params_head: Sequence[Any]) -> int:
    total = 0
    for _ in range(_MAX_ROUNDS):

        def _round(tx: SiemTx) -> int:
            state_store.state_in(tx, lock=True)  # lock order: state row first
            rows = tx.query(
                "SELECT id FROM siem_delivery_queue WHERE destination_key = ? AND status IN "
                "('pending', 'batched', 'quarantined') ORDER BY id LIMIT ?",
                (key, RETARGET_ROWS_PER_TX),
            )
            for row in rows:
                tx.execute(sql, list(params_head) + [tx.ts(tx.now()), row["id"]])
            return len(rows)

        moved = int(scheduler.db.write(_round, phase="admin"))
        total += moved
        if moved < RETARGET_ROWS_PER_TX:
            return total
    return total


def _close_batches(scheduler: Any, key: str) -> None:
    def _do(tx: SiemTx) -> None:
        state_store.state_in(tx, lock=True)  # lock order: state row first
        tx.execute(
            "UPDATE siem_delivery_batches SET state = 'retargeted', body = NULL, "
            "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE destination_key = ? AND state = 'pending_send'",
            (key,),
        )

    scheduler.db.write(_do, phase="admin")


def _close_and_move(
    scheduler: Any, key: str, sql: str, params_head: Sequence[Any]
) -> int:
    """Close the key's open batches, move its rows, then close and move once
    more: a stale tick may have formed a batch while the rows were moving."""
    total = 0
    for _ in range(2):
        _close_batches(scheduler, key)
        total += _move_rows(scheduler, key, sql, params_head)
    return total


def _refuse_configured(scheduler: Any, key: str) -> Any:
    ctx = _require_ctx(scheduler)
    if key == ctx.destination.key:
        raise SiemAdminError(409, "rows already target the configured destination")
    return ctx


def retarget_destination(scheduler: Any, actor: str, key: str) -> Dict[str, Any]:
    ctx = _refuse_configured(scheduler, key)
    count = _close_and_move(
        scheduler,
        key,
        "UPDATE siem_delivery_queue SET destination_key = ?, status = 'pending', "
        "batch_id = NULL, batch_ordinal = NULL, quarantine_reason = NULL, "
        "quarantine_signature = NULL, next_attempt_at = ? WHERE id = ?",
        [ctx.destination.key],
    )
    _audit(
        scheduler,
        actor,
        "siem_destination_retargeted",
        {"from_destination_key": key, "count": count},
    )
    return {"retargeted": count, "to_destination_key": ctx.destination.key}


def abandon_destination(scheduler: Any, actor: str, key: str) -> Dict[str, Any]:
    _refuse_configured(scheduler, key)
    count = _close_and_move(
        scheduler,
        key,
        "UPDATE siem_delivery_queue SET status = 'abandoned', batch_id = NULL, "
        "batch_ordinal = NULL, next_attempt_at = ? WHERE id = ?",
        [],
    )
    _audit(
        scheduler,
        actor,
        "siem_destination_abandoned",
        {"destination_key": key, "count": count},
    )
    return {"abandoned": count}


# --- stats document ----------------------------------------------------------------


def capture_status(scheduler: Any, state: Mapping[str, Any]) -> Dict[str, Any]:
    """Capture state (as this process sees it) plus a human status line."""
    view = scheduler.view
    snap = capture_state()
    dest = view.destination if view is not None else None
    canary_missing = state.get("canary_missing_action_types")
    if snap.loaded and snap.active:
        return {"state": "armed", "status": "armed"}
    if view is None:
        return {
            "state": "inactive",
            "status": "configuration never loaded in this process",
        }
    if not view.section.enabled or dest is None:
        return {"state": "inactive", "status": "delivery disabled or no destination"}
    if (
        state.get("canary_destination_key") != dest.key
        or state.get("canary_mapping_version") != scheduler.mapping_version
        or not state.get("canary_result")
    ):
        return {"state": "awaiting canary", "status": "awaiting canary"}
    if state.get("canary_result") != "accepted":
        return {"state": "canary rejected", "status": "canary rejected"}
    if not state.get("canary_visible_confirmed_at"):
        expected = stats_json_list(state.get("canary_expected"))
        confirmed = stats_json_list(state.get("canary_confirmed_ids"))
        missing = stats_json_list(canary_missing)
        return {
            "state": "awaiting visibility confirmation",
            "status": f"canary accepted, {len(confirmed)} of {len(expected)} visible"
            + (f"; not visible: {', '.join(missing)}" if missing else ""),
        }
    return {
        "state": "awaiting process readiness",
        "status": "awaiting process readiness",
    }


def stats_json_list(raw: Any) -> List[str]:
    from code_indexer.server.storage.json_column import parse_json_column

    if raw is None:
        return []
    value = parse_json_column(raw, list, "siem_json_list") or []
    return [v if isinstance(v, str) else str(v.get("product_log_id")) for v in value]


def _ts(state: Mapping[str, Any], name: str) -> Optional[str]:
    from code_indexer.server.services.siem_delivery.db import Dialect

    parsed = Dialect.parse_ts(state.get(name))
    return parsed.isoformat() if isinstance(parsed, datetime) else None


def stats_document(scheduler: Any) -> Dict[str, Any]:
    view = scheduler.view
    dest_key = view.destination.key if view is not None and view.destination else None
    counts = scheduler.db.read(lambda tx: stats.live_counts(tx, dest_key))
    state = state_store.read_state(scheduler.db)
    snap = stats.persisted_snapshot(state)
    fleet = {
        **counts,
        "delivered_total": int(state.get("delivered_total") or 0),
        "resent_after_unknown_outcome": int(
            state.get("resent_after_unknown_outcome") or 0
        ),
        "unrecoverable_total": int(state.get("unrecoverable_total") or 0),
        "capture_after_boundary_total": int(
            state.get("capture_after_boundary_total") or 0
        ),
        "capture_after_boundary_late_total": int(
            state.get("capture_after_boundary_late_total") or 0
        ),
        "backlog_rows_estimate": snap.get("backlog_rows_estimate"),
        "backlog_bytes_estimate": snap.get("backlog_bytes_estimate"),
        "growth_per_hour": snap.get("growth_per_hour"),
        "projected_hours_to_disk_full": snap.get("projected_hours_to_disk_full"),
        "snapshot_refreshed_at": snap.get("refreshed_at"),
    }
    status = capture_status(scheduler, state)
    return {
        "fleet": fleet,
        "capture": {
            **status,
            "configured_destination_key": dest_key,
            "armed_destination_key": state.get("armed_destination_key"),
            "canary": {
                "run_id": state.get("canary_run_id"),
                "result": state.get("canary_result"),
                "mapping_version": state.get("canary_mapping_version"),
                "visible_confirmed_at": _ts(state, "canary_visible_confirmed_at"),
                "missing_action_types": stats_json_list(
                    state.get("canary_missing_action_types")
                ),
            },
            "processes": [
                {
                    "process_id": p["process_id"],
                    "node_id": p["node_id"],
                    "probe_result": p["probe_result"],
                }
                for p in state_store.live_processes(scheduler.db)
            ],
        },
        "halt": {
            "class": state.get("halted_class"),
            "signature": state.get("halted_signature"),
            "since": _ts(state, "halted_since"),
            "next_probe_at": _ts(state, "next_probe_at"),
            "batch_id": state.get("halted_batch_id"),
        },
        "local_process": scheduler.get_liveness(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
