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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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


def find_scheduler(app_state: Any) -> Tuple[Optional[Any], Optional[str]]:
    """``(scheduler, None)``, or ``(None, startup_error)`` when SIEM delivery
    is not running in this process.  Never raises: each front door maps an
    absent scheduler to its own 503."""
    scheduler = getattr(app_state, "siem_delivery_scheduler", None)
    if scheduler is not None:
        return scheduler, None
    return None, getattr(app_state, "siem_delivery_startup_error", None)


ABANDON_CONFIRM_WORD = "ABANDON"


def require_abandon_confirmation(value: Any) -> None:
    """The Web abandon is confirmed only by the exact word (case-sensitive,
    never trimmed or normalised); anything else is refused BEFORE
    :func:`abandon_destination` runs."""
    if not (isinstance(value, str) and value == ABANDON_CONFIRM_WORD):
        raise SiemAdminError(400, f"type {ABANDON_CONFIRM_WORD} to confirm")


def _dest_target(scheduler: Any) -> Optional[SiemTarget]:
    """The configured destination (committed config, read now), or None
    when none is configured: the self-report's explicit destination."""
    ctx = scheduler.committed_context()
    return SiemTarget(ctx.destination.key, None) if ctx is not None else None


_RESOLVE = object()  # sentinel: read the destination now


def _audit(
    scheduler: Any,
    actor: Any,
    action_type: str,
    details: Mapping[str, Any],
    target: Any = _RESOLVE,
) -> None:
    """Record one self-report row (``record_outcome`` never raises).  A
    caller that already resolved the destination passes it as *target*, so
    nothing can fail between its own write and this row."""
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details=dict(details),
        siem_destination=_dest_target(scheduler) if target is _RESOLVE else target,
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
    # Bug #2018: read BEFORE minting, so a key replaced from here on makes
    # the record refuse this canary (it would prove the old key); the run
    # ordinal, issued before the send, orders this run against any other.
    credential_id = scheduler.credential_store.credential_id()
    run_seq = state_store.issue_canary_run(ctx.db)
    started_at = ctx.db.read(lambda tx: tx.ts(tx.now()))
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
    refused = state_store.record_canary(
        ctx.db,
        run_id=run_id,
        run_seq=run_seq,
        destination_key=ctx.destination.key,
        mapping_version=ctx.mapping_version,
        expected=expected,
        result=result,
        signature=None if result == "accepted" else cls.signature,
        actor=actor,
        config_epoch=ctx.config_epoch,
        credential_id=credential_id,
        started_at=started_at,
        committed_epoch=scheduler.committed_epoch,
    )
    if refused is not None:  # nothing was recorded
        messages = {
            state_store.CANARY_CREDENTIAL_CHANGED: (
                "the service-account credential changed during the canary; run it again"
            ),
            state_store.CANARY_STALE_LIFETIME: (
                "canary run is stale: the configuration lifetime changed during "
                "the canary; run it again"
            ),
            state_store.CANARY_SUPERSEDED: (
                "canary run is stale: a newer canary run was already recorded"
            ),
        }
        raise SiemAdminError(409, messages[refused])
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
        config_epoch=ctx.config_epoch,
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


RoundCheck = Callable[[], None]


def _snapshot_max_id(scheduler: Any, key: str) -> int:
    """The key's highest queue id when the action starts: rows captured
    after this (e.g. after the key is configured again) are never moved."""
    row = scheduler.db.read(
        lambda tx: tx.one(
            "SELECT MAX(id) AS m FROM siem_delivery_queue WHERE destination_key = ?",
            (key,),
        )
    )
    return int(row["m"]) if row and row["m"] is not None else 0


def _move_rows(
    scheduler: Any,
    key: str,
    sql: str,
    params_head: Sequence[Any],
    check: RoundCheck,
    max_id: int,
) -> int:
    total = 0
    for _ in range(_MAX_ROUNDS):

        def _round(tx: SiemTx) -> int:
            state_store.state_in(tx, lock=True)  # lock order: state row first
            check()  # the COMMITTED destination (a fresh read), every round
            rows = tx.query(
                "SELECT id FROM siem_delivery_queue WHERE destination_key = ? AND status IN "
                "('pending', 'batched', 'quarantined') AND id <= ? ORDER BY id LIMIT ?",
                (key, max_id, RETARGET_ROWS_PER_TX),
            )
            for row in rows:
                tx.execute(sql, list(params_head) + [tx.ts(tx.now()), row["id"]])
            return len(rows)

        moved = int(scheduler.db.write(_round, phase="admin"))
        total += moved
        if moved < RETARGET_ROWS_PER_TX:
            return total
    return total


def _close_batches(scheduler: Any, key: str, check: RoundCheck, max_id: int) -> None:
    def _do(tx: SiemTx) -> None:
        state_store.state_in(tx, lock=True)  # lock order: state row first
        check()
        # Claims lease under this same state-row lock, so this check is
        # race-free: a batch leased now is in flight and must not be closed
        # under its sender (its completion would be lost).  A crashed
        # sender's lease blocks only until it expires (lease_seconds).
        in_flight = tx.one(
            "SELECT batch_id FROM siem_delivery_batches WHERE destination_key = ? "
            "AND state = 'pending_send' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at >= ? LIMIT 1",
            (key, tx.ts(tx.now())),
        )
        if in_flight is not None:
            raise SiemAdminError(
                409, "a send to this destination is in flight; retry shortly"
            )
        # rows newer than the snapshot inside a batch being closed go back to
        # pending (they are not this action's to move, and never stranded)
        tx.execute(
            "UPDATE siem_delivery_queue SET status = 'pending', batch_id = NULL, "
            "batch_ordinal = NULL WHERE status = 'batched' AND id > ? AND batch_id IN "
            "(SELECT batch_id FROM siem_delivery_batches WHERE destination_key = ? "
            "AND state = 'pending_send')",
            (max_id, key),
        )
        tx.execute(
            "UPDATE siem_delivery_batches SET state = 'retargeted', body = NULL, "
            "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE destination_key = ? AND state = 'pending_send'",
            (key,),
        )

    scheduler.db.write(_do, phase="admin")


def _close_and_move(
    scheduler: Any,
    key: str,
    sql: str,
    params_head: Sequence[Any],
    check: RoundCheck,
    max_id: int,
) -> int:
    """Close the key's open batches, move its rows, then close and move once
    more: a stale tick may have formed a batch while the rows were moving.

    Safe by construction against a concurrent configuration change: only
    rows with ``id <= max_id`` (snapshotted when the action started) are ever
    touched, and *check* re-reads the COMMITTED destination (never this
    process's cached view) in every transaction, stopping with 409."""
    total = 0
    for _ in range(2):
        _close_batches(scheduler, key, check, max_id)
        total += _move_rows(scheduler, key, sql, params_head, check, max_id)
    return total


_CONFIGURED_KEY_REFUSAL = "rows already target the configured destination"


def _configured_key(scheduler: Any) -> Optional[str]:
    """The committed destination key, or None when nothing is configured
    (an invalid stored configuration is refused: its key is unknown here)."""
    from code_indexer.server.services.siem_delivery.destination import (
        SiemConfigInvalid,
    )

    try:
        ctx = scheduler.committed_context()
    except SiemConfigInvalid as exc:
        raise SiemAdminError(
            409, f"SIEM configuration invalid here: {exc.field}"
        ) from None
    return str(ctx.destination.key) if ctx is not None else None


def retarget_destination(scheduler: Any, actor: str, key: str) -> Dict[str, Any]:
    max_id = _snapshot_max_id(scheduler, key)  # before any check
    if _configured_key(scheduler) is None:
        raise SiemAdminError(
            409,
            "no SIEM destination is configured to retarget to; configure the "
            "new destination first, or abandon the rows instead",
        )
    ctx = _require_ctx(scheduler)
    target = str(ctx.destination.key)
    if key == target:
        raise SiemAdminError(409, _CONFIGURED_KEY_REFUSAL)

    def _still_the_target() -> None:
        if _configured_key(scheduler) != target:
            raise SiemAdminError(
                409, "the configured destination changed during the retarget"
            )

    count = _close_and_move(
        scheduler,
        key,
        "UPDATE siem_delivery_queue SET destination_key = ?, status = 'pending', "
        "batch_id = NULL, batch_ordinal = NULL, quarantine_reason = NULL, "
        "quarantine_signature = NULL, next_attempt_at = ? WHERE id = ?",
        [target],
        _still_the_target,
        max_id,
    )
    _audit(
        scheduler,
        actor,
        "siem_destination_retargeted",
        {"from_destination_key": key, "count": count},
    )
    return {"retargeted": count, "to_destination_key": ctx.destination.key}


def abandon_destination(scheduler: Any, actor: str, key: str) -> Dict[str, Any]:
    """Abandon every undelivered row (pending, batched AND quarantined) of a
    destination that is not the configured one -- including when nothing is
    configured at all (decommissioning)."""

    def _not_configured() -> None:
        if key == _configured_key(scheduler):
            raise SiemAdminError(409, _CONFIGURED_KEY_REFUSAL)

    max_id = _snapshot_max_id(scheduler, key)  # before any check
    _not_configured()
    count = _close_and_move(
        scheduler,
        key,
        "UPDATE siem_delivery_queue SET status = 'abandoned', batch_id = NULL, "
        "batch_ordinal = NULL, next_attempt_at = ? WHERE id = ?",
        [],
        _not_configured,
        max_id,
    )
    _audit(
        scheduler,
        actor,
        "siem_destination_abandoned",
        {"destination_key": key, "count": count},
    )
    return {"abandoned": count}


# --- service-account credential (write-only) ------------------------------------------


def _audit_credential(
    scheduler: Any,
    actor: str,
    change: str,
    identity: Mapping[str, Any],
    target: Optional[SiemTarget],
) -> None:
    """Identity only: never the key, never the JSON."""
    _audit(
        scheduler,
        actor,
        "siem_credential_changed",
        {
            "change": change,
            "client_email": identity["client_email"],
            "private_key_id": identity["private_key_id"],
        },
        target=target,
    )


def _resolved_target(scheduler: Any) -> Optional[SiemTarget]:
    """The self-report destination, resolved BEFORE a credential write (409
    on a configuration this process cannot use), so the write and its audit
    row cannot be separated by a later configuration read."""
    key = _configured_key(scheduler)
    return SiemTarget(key, None) if key is not None else None


def set_credential(scheduler: Any, actor: str, key_json: str) -> Dict[str, Any]:
    """Validate and store (set or replace) the service-account key; the
    response carries the non-secret identity only."""
    from code_indexer.server.services.siem_delivery.credential import (
        SiemCredentialInvalid,
        validate_service_account_json,
    )

    try:
        info = validate_service_account_json(
            key_json, harness_active=scheduler.harness_active
        )
    except SiemCredentialInvalid as exc:
        raise SiemAdminError(400, str(exc)) from None
    target = _resolved_target(scheduler)
    change, identity = scheduler.credential_store.set(info, actor=actor)
    scheduler.credentials.invalidate_all()
    scheduler.note_credential_identity(identity)
    _audit_credential(scheduler, actor, change, identity, target)
    scheduler.apply_committed_change()  # a replacement disarmed: capture stops now
    return {"change": change, "credential": identity}


def remove_credential(scheduler: Any, actor: str) -> Dict[str, Any]:
    target = _resolved_target(scheduler)
    removed = scheduler.credential_store.remove(actor=actor)
    if removed is None:
        raise SiemAdminError(404, "no SIEM service-account credential is stored")
    scheduler.credentials.invalidate_all()
    scheduler.note_credential_identity(None)
    _audit_credential(scheduler, actor, "removed", removed, target)
    scheduler.apply_committed_change()  # the removal disarmed: capture stops now
    return {"change": "removed", "credential": removed}


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
    if not state.get("canary_result") or not state_store.canary_is_for(
        state,
        destination_key=dest.key,
        mapping_version=scheduler.mapping_version,
        config_epoch=view.section.arming_epoch,
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
        # non-secret identity of the stored key (None: no credential)
        "credential": scheduler.credential_store.identity(),
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
