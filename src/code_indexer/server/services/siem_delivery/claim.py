"""Claim: resend a due batch first, else build a new immutable batch.

No UDM work happens under a lock: candidates are read and re-projected,
built, validated and packed OUTSIDE any transaction; the short write
transaction then re-verifies row state, applies the fleet-wide quarantine
cap, and commits membership (immutable from then on) plus the exact request
body, or rolls back if any row changed meanwhile.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from code_indexer.server.services.audit_events import AUDIT_ROW_COLUMNS, AuditEvent
from code_indexer.server.services.siem_delivery import state_store, telemetry
from code_indexer.server.services.siem_delivery.classifier import local_signature
from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx
from code_indexer.server.services.siem_delivery.destination import Destination
from code_indexer.server.services.siem_delivery.projection import project
from code_indexer.server.services.siem_delivery.sender import CredentialProvider
from code_indexer.server.services.siem_delivery.timings import SiemTimings
from code_indexer.server.services.siem_delivery.udm import (
    BYTE_BUDGET,
    HARD_CEILING,
    MAPPING_VERSION,
    UDM_MAPPING,
    UdmRow,
    build_udm,
    canonical_json,
    envelope,
    pack,
    validate_udm,
)

MAX_BATCHES_PER_TICK = 50
MAX_OPEN_BATCHES = 3
QUARANTINE_CAP = 5
PROBE_SAMPLE_ROWS = 50
_QUEUE_COLUMNS = (
    "id, event_uuid, action_type, event_payload, projection_error, mapping_version"
)


@dataclass
class EngineContext:
    """Everything one tick needs (built per tick by the scheduler)."""

    db: SiemDb
    timings: SiemTimings
    http_factory: Any
    credentials: CredentialProvider
    process_id: str
    destination: Destination
    max_batch_events: int
    source_instance_label: str
    mapping: Mapping[str, str] = field(default_factory=lambda: UDM_MAPPING)
    mapping_version: int = MAPPING_VERSION
    jitter: Any = None  # callable() -> float in [0, 1); None = no jitter


@dataclass
class Claim:
    batch_id: str
    fence: int
    body: bytes
    event_count: int
    send_attempts: int
    lease_expires_at: Any
    bisect_depth: int
    mapping_version: int


@dataclass
class Candidates:
    """Phases 2-3 outcome (pure, nothing persisted)."""

    ok: List[Tuple[Dict[str, Any], bytes]] = field(default_factory=list)
    failures: List[Tuple[Dict[str, Any], str, str]] = field(default_factory=list)
    reprojected: List[Tuple[Dict[str, Any], Optional[str], Optional[str]]] = field(
        default_factory=list
    )
    unrecoverable: List[Dict[str, Any]] = field(default_factory=list)


class _Conflict(Exception):
    """A row changed since it was read: roll the claim back."""


def event_from_audit_row(row: Mapping[str, Any]) -> AuditEvent:
    from code_indexer.server.services.siem_delivery.db import Dialect

    occurred = Dialect.parse_ts(row["timestamp"])
    details = row.get("details")
    if details is not None and not isinstance(details, str):
        details = json.dumps(details)
    return AuditEvent(
        event_uuid=str(row["event_uuid"]),
        occurred_at=occurred.isoformat() if occurred else str(row["timestamp"]),
        actor=str(row["admin_id"]),
        actor_is_system=bool(row.get("actor_is_system")),
        action_type=str(row["action_type"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        outcome=row.get("outcome"),
        source=row.get("source"),
        ip_address=row.get("ip_address"),
        correlation_id=str(row.get("correlation_id") or ""),
        node_id=row.get("node_id"),
        auth_method=row.get("auth_method"),
        details_json=details,
    )


def _audit_sources(ctx: EngineContext, uuids: Sequence[str]) -> Dict[str, AuditEvent]:
    """Read-only, indexed (event_uuid), outside any write transaction."""
    if not uuids:
        return {}
    marks = ", ".join("?" for _ in uuids)
    columns = ", ".join(AUDIT_ROW_COLUMNS)
    rows = ctx.db.read(
        lambda tx: tx.query(
            f"SELECT {columns} FROM audit_logs WHERE event_uuid IN ({marks})",
            list(uuids),
        )
    )
    return {str(r["event_uuid"]): event_from_audit_row(r) for r in rows}


def _payload(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    from code_indexer.server.storage.json_column import parse_json_column

    return parse_json_column(row.get("event_payload"), dict, "event_payload")


def build_candidates(ctx: EngineContext, rows: Sequence[Dict[str, Any]]) -> Candidates:
    """Phases 2b-3: reproject stale rows, build, validate (no transaction)."""
    out = Candidates()
    stale = [
        r
        for r in rows
        if r.get("projection_error") and int(r["mapping_version"]) < ctx.mapping_version
    ]
    sources = _audit_sources(ctx, [str(r["event_uuid"]) for r in stale])
    stale_ids = {r["id"] for r in stale}
    for row in rows:
        payload: Optional[Dict[str, Any]]
        if row["id"] in stale_ids:
            source = sources.get(str(row["event_uuid"]))
            if source is None:
                out.unrecoverable.append(row)
                continue
            payload_json, error = project(source, mapping=ctx.mapping)
            out.reprojected.append((row, payload_json, error))
            telemetry.add_counter(
                "reprojected", 1, {"result": "still_failing" if error else "ok"}
            )
            if error:
                out.failures.append(
                    (
                        row,
                        local_signature("projection_type_violation", error),
                        "projection_type_violation",
                    )
                )
                continue
            payload = json.loads(payload_json) if payload_json else None
        elif row.get("projection_error"):
            out.failures.append(
                (
                    row,
                    local_signature(
                        "projection_type_violation", str(row["projection_error"])
                    ),
                    "projection_type_violation",
                )
            )
            continue
        else:
            payload = _payload(row)
        if payload is None:
            out.failures.append(
                (row, local_signature("payload_unreadable", ""), "payload_unreadable")
            )
            continue
        udm = build_udm(
            UdmRow(
                str(row["event_uuid"]),
                str(row["action_type"]),
                payload,
                ctx.source_instance_label,
            ),
            mapping=ctx.mapping,
        )
        problem = validate_udm(udm)
        if problem is not None:
            reason = "oversize" if problem[0] == "oversize" else problem[0]
            out.failures.append((row, local_signature(reason, problem[1]), reason))
            continue
        member = canonical_json(udm)
        if len(member) > HARD_CEILING:
            out.failures.append((row, local_signature("oversize", ""), "oversize"))
            continue
        out.ok.append((row, member))
    return out


def _window(
    tx: SiemTx, state: Mapping[str, Any], window_seconds: float
) -> Tuple[bool, int]:
    """(expired, quarantines counted in the CURRENT rolling window)."""
    start = tx.dialect.parse_ts(state.get("quarantine_window_start"))
    expired = start is None or start <= tx.now() - timedelta(seconds=window_seconds)
    return expired, 0 if expired else int(state.get("quarantine_window_count") or 0)


def window_admits(
    tx: SiemTx, state: Mapping[str, Any], failures: int, window_seconds: float
) -> bool:
    """Would the guard admit *failures* quarantines under the current window?"""
    _expired, count = _window(tx, state, window_seconds)
    return count + failures <= QUARANTINE_CAP


def quarantine_guard(
    tx: SiemTx,
    state: Mapping[str, Any],
    signatures: Sequence[str],
    window_seconds: float,
) -> Tuple[int, Optional[str]]:
    """Fleet-wide cap: at most QUARANTINE_CAP quarantines per rolling window,
    whatever the cause, batch size or signature.

    Returns ``(admitted, halt_signature)``: the first *admitted* failures may
    be quarantined (and are counted now); when more failed than the window
    can absorb, the next one halts BEFORE quarantining and the signature
    (the most frequent one) is returned for the halt."""
    if not signatures:
        return 0, None
    now = tx.now()
    expired, count = _window(tx, state, window_seconds)
    admitted = max(0, min(len(signatures), QUARANTINE_CAP - count))
    if admitted:
        tx.execute(
            "UPDATE siem_delivery_state SET quarantine_window_start = ?, "
            "quarantine_window_count = ? WHERE id = 1",
            (
                tx.ts(now) if expired else state.get("quarantine_window_start"),
                count + admitted,
            ),
        )
    if admitted < len(signatures):
        return admitted, Counter(signatures).most_common(1)[0][0]
    return admitted, None


def _lease(
    tx: SiemTx, ctx: EngineContext, state: Mapping[str, Any], batch_id: str
) -> Tuple[int, Any]:
    fence = int(state["fence_counter"]) + 1
    expires = tx.now() + timedelta(seconds=ctx.timings.lease_seconds)
    tx.execute(
        "UPDATE siem_delivery_state SET fence_counter = ? WHERE id = 1", (fence,)
    )
    tx.execute(
        "UPDATE siem_delivery_batches SET lease_owner = ?, lease_token = ?, "
        "lease_expires_at = ? WHERE batch_id = ?",
        (ctx.process_id, fence, tx.ts(expires), batch_id),
    )
    return fence, expires


def _claim_from_row(row: Mapping[str, Any], fence: int, expires: Any) -> Claim:
    return Claim(
        batch_id=str(row["batch_id"]),
        fence=fence,
        body=bytes(row["body"]),
        event_count=int(row["event_count"]),
        send_attempts=int(row["send_attempts"]),
        lease_expires_at=expires,
        bisect_depth=int(row["bisect_depth"]),
        mapping_version=int(row["mapping_version"]),
    )


_BATCH_COLUMNS = (
    "batch_id, body, event_count, send_attempts, bisect_depth, mapping_version"
)


def _phase1(ctx: EngineContext, bypass_halt: bool) -> Tuple[Optional[Claim], bool]:
    """(claim, may_form_new)."""

    def _do(tx: SiemTx) -> Tuple[Optional[Claim], bool]:
        st = state_store.state_in(tx, lock=True)
        if st.get("halted_class") and not bypass_halt:
            return None, False
        now = tx.ts(tx.now())
        row = tx.one(
            f"SELECT {_BATCH_COLUMNS} FROM siem_delivery_batches WHERE destination_key = ? "
            "AND state = 'pending_send' AND next_attempt_at <= ? "
            "AND (lease_expires_at IS NULL OR lease_expires_at < ?) "
            "ORDER BY created_at, batch_id LIMIT 1",
            (ctx.destination.key, now, now),
        )
        if row is not None:
            fence, expires = _lease(tx, ctx, st, str(row["batch_id"]))
            return _claim_from_row(row, fence, expires), False
        open_rows = tx.query(
            "SELECT batch_id FROM siem_delivery_batches WHERE destination_key = ? "
            "AND state = 'pending_send' LIMIT ?",
            (ctx.destination.key, MAX_OPEN_BATCHES),
        )
        return None, len(open_rows) < MAX_OPEN_BATCHES

    return ctx.db.write(_do, phase="claim")


# Walks idx_siem_queue_status_dest_id in id order (no sort step);
# next_attempt_at is checked from the index entry.
CANDIDATE_SQL = (
    f"SELECT {_QUEUE_COLUMNS} FROM siem_delivery_queue WHERE status = 'pending' "
    "AND destination_key = ? AND next_attempt_at <= ? AND batch_id IS NULL "
    "ORDER BY status, destination_key, id LIMIT ?"
)


def read_candidate_rows(ctx: EngineContext, limit: int) -> List[Dict[str, Any]]:
    def _q(tx: SiemTx) -> List[Dict[str, Any]]:
        return tx.query(CANDIDATE_SQL, (ctx.destination.key, tx.ts(tx.now()), limit))

    return ctx.db.read(_q)


_UNCHANGED = "status = 'pending' AND batch_id IS NULL AND destination_key = ?"


def _persist_reprojection(tx: SiemTx, ctx: EngineContext, cand: Candidates) -> None:
    for row, payload, error in cand.reprojected:
        n = tx.execute(
            "UPDATE siem_delivery_queue SET event_payload = ?, projection_error = ?, "
            f"mapping_version = ? WHERE id = ? AND {_UNCHANGED} AND mapping_version = ?",
            (
                payload,
                error,
                ctx.mapping_version,
                row["id"],
                ctx.destination.key,
                row["mapping_version"],
            ),
        )
        if n != 1:
            raise _Conflict()
    for row in cand.unrecoverable:
        n = tx.execute(
            "UPDATE siem_delivery_queue SET status = 'unrecoverable', "
            "quarantine_reason = 'projection_source_expired', mapping_version = ? "
            f"WHERE id = ? AND {_UNCHANGED} AND mapping_version = ?",
            (
                ctx.mapping_version,
                row["id"],
                ctx.destination.key,
                row["mapping_version"],
            ),
        )
        if n != 1:
            raise _Conflict()
    if cand.unrecoverable:
        tx.execute(
            "UPDATE siem_delivery_state SET unrecoverable_total = unrecoverable_total + ? "
            "WHERE id = 1",
            (len(cand.unrecoverable),),
        )


def _insert_batch(
    tx: SiemTx,
    ctx: EngineContext,
    batch_id: str,
    member_ids: Sequence[int],
    body: bytes,
    *,
    bisect_depth: int = 0,
    mapping_version: Optional[int] = None,
) -> None:
    now = tx.now()
    tx.execute(
        "INSERT INTO siem_delivery_batches (batch_id, destination_key, body, "
        "body_sha256, event_count, mapping_version, state, created_at, "
        "next_attempt_at, bisect_depth) VALUES (?, ?, ?, ?, ?, ?, 'pending_send', ?, ?, ?)",
        (
            batch_id,
            ctx.destination.key,
            body,
            hashlib.sha256(body).hexdigest(),
            len(member_ids),
            ctx.mapping_version if mapping_version is None else mapping_version,
            tx.ts(now),
            tx.ts(now),
            bisect_depth,
        ),
    )


def _phase4(
    ctx: EngineContext,
    cand: Candidates,
    members: List[Tuple[Dict[str, Any], bytes]],
    bypass_halt: bool,
) -> Optional[Claim]:
    failures = cand.failures

    def _do(tx: SiemTx) -> Optional[Claim]:
        st = state_store.state_in(tx, lock=True)
        if st.get("halted_class") and not bypass_halt:
            return None
        admitted, halt_sig = quarantine_guard(
            tx, st, [f[1] for f in failures], ctx.timings.quarantine_window_seconds
        )
        _persist_reprojection(tx, ctx, cand)
        for row, signature, reason in failures[:admitted]:
            n = tx.execute(
                "UPDATE siem_delivery_queue SET status = 'quarantined', quarantine_reason = ?, "
                f"quarantine_signature = ?, mapping_version = ? WHERE id = ? AND {_UNCHANGED}",
                (
                    reason,
                    signature,
                    ctx.mapping_version,
                    row["id"],
                    ctx.destination.key,
                ),
            )
            if n != 1:
                raise _Conflict()
        if halt_sig is not None:
            # The cap is reached: nothing more from this claim is batched or
            # quarantined; every other row stays pending.
            state_store.halt(
                tx,
                cls="local_validation_burst",
                signature=halt_sig,
                batch_id=None,
                mapping_version=ctx.mapping_version,
                probe_interval_seconds=ctx.timings.probe_interval_seconds,
            )
            return None
        batch_id = str(uuid.uuid4())
        for ordinal, (row, _member) in enumerate(members):
            n = tx.execute(
                "UPDATE siem_delivery_queue SET status = 'batched', batch_id = ?, "
                f"batch_ordinal = ?, mapping_version = ? WHERE id = ? AND {_UNCHANGED}",
                (
                    batch_id,
                    ordinal,
                    ctx.mapping_version,
                    row["id"],
                    ctx.destination.key,
                ),
            )
            if n != 1:
                raise _Conflict()
        if not members:
            return None
        body = envelope([m for _row, m in members])
        _insert_batch(tx, ctx, batch_id, [r["id"] for r, _m in members], body)
        fence, expires = _lease(tx, ctx, st, batch_id)
        return Claim(
            batch_id, fence, body, len(members), 0, expires, 0, ctx.mapping_version
        )

    try:
        return ctx.db.write(_do, phase="claim")
    except _Conflict:
        return None


def claim_batch(
    ctx: EngineContext, *, bypass_halt: bool = False, limit: Optional[int] = None
) -> Optional[Claim]:
    claim, may_form_new = _phase1(ctx, bypass_halt)
    if claim is not None or not may_form_new:
        return claim
    rows = read_candidate_rows(ctx, limit or ctx.max_batch_events)
    if not rows:
        return None
    cand = build_candidates(ctx, rows)
    count = pack([m for _r, m in cand.ok], BYTE_BUDGET)
    members = cand.ok[:count]
    if (
        not members
        and not cand.failures
        and not cand.reprojected
        and not cand.unrecoverable
    ):
        return None
    return _phase4(ctx, cand, members, bypass_halt)


def claim_specific(ctx: EngineContext, batch_id: str) -> Optional[Claim]:
    """Lease one named pending_send batch (the halted batch, for its probe)."""

    def _do(tx: SiemTx) -> Optional[Claim]:
        st = state_store.state_in(tx, lock=True)
        now = tx.ts(tx.now())
        row = tx.one(
            f"SELECT {_BATCH_COLUMNS} FROM siem_delivery_batches WHERE batch_id = ? "
            "AND state = 'pending_send' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (batch_id, now),
        )
        if row is None:
            return None
        fence, expires = _lease(tx, ctx, st, batch_id)
        return _claim_from_row(row, fence, expires)

    return ctx.db.write(_do, phase="claim")
