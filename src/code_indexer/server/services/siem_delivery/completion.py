"""Send a leased batch and record its outcome under the fencing token.

Every completion UPDATE is conditional on ``(batch_id, lease_token)``: a
sender whose lease was taken over (a slower stale tick) updates nothing.
Resends are always the persisted bytes, so a stale sender's request and the
new owner's are byte-identical.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, List, Optional, Sequence, Tuple

from code_indexer.server.services.siem_delivery import state_store, telemetry
from code_indexer.server.services.siem_delivery.claim import (
    Claim,
    EngineContext,
    _insert_batch,
    quarantine_guard,
)
from code_indexer.server.services.siem_delivery.classifier import (
    ACCEPTED,
    CREDENTIAL,
    ROW_REJECTION,
    THROTTLED,
    TRANSIENT,
    Classification,
)
from code_indexer.server.services.siem_delivery.db import SiemTx
from code_indexer.server.services.siem_delivery.sender import (
    CredentialError,
    send_batch,
)
from code_indexer.server.services.siem_delivery.udm import (
    canonical_json,
    envelope,
    member_udms,
)

logger = logging.getLogger(__name__)

_JITTER_FRACTION = 0.10
_MAX_THROTTLE_SECONDS = 3600.0


@dataclass
class SendResult:
    classification: Optional[Classification]
    completed: bool  # False: lease lost, stale completion, or no token
    systemic: bool


def backoff_seconds(ctx: EngineContext, attempts: int) -> float:
    base = ctx.timings.backoff_base_seconds * (2 ** max(0, attempts - 1))
    delay = min(base, ctx.timings.backoff_cap_seconds)
    if ctx.jitter is not None:
        delay *= 1 + _JITTER_FRACTION * (2 * float(ctx.jitter()) - 1)
    return float(delay)


def _start(ctx: EngineContext, claim: Claim) -> bool:
    def _do(tx: SiemTx) -> bool:
        now = tx.now()
        remaining = (
            ctx.timings.request_timeout_seconds
            + ctx.timings.token_timeout_seconds
            + ctx.timings.lease_renew_margin_seconds
        )
        lease_until = now + timedelta(seconds=max(ctx.timings.lease_seconds, remaining))
        n = tx.execute(
            "UPDATE siem_delivery_batches SET send_attempts = send_attempts + 1, "
            "last_sent_at = ?, first_sent_at = COALESCE(first_sent_at, ?), "
            "prior_outcome_unknown = outcome_unknown, outcome_unknown = 1, "
            "lease_expires_at = ? WHERE batch_id = ? AND lease_token = ? "
            "AND state = 'pending_send'",
            (tx.ts(now), tx.ts(now), tx.ts(lease_until), claim.batch_id, claim.fence),
        )
        return n == 1

    return bool(ctx.db.write(_do, phase="complete"))


def _release(ctx: EngineContext, claim: Claim) -> None:
    ctx.db.write(
        lambda tx: tx.execute(
            "UPDATE siem_delivery_batches SET lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL WHERE batch_id = ? AND lease_token = ?",
            (claim.batch_id, claim.fence),
        ),
        phase="complete",
    )


def send_and_complete(ctx: EngineContext, claim: Claim) -> SendResult:
    try:
        token = ctx.credentials.token(ctx.destination)
    except CredentialError as exc:
        logger.info("SIEM delivery: no token in this process (%s)", exc.result.value)
        _release(ctx, claim)
        return SendResult(None, False, True)
    if not _start(ctx, claim):
        return SendResult(None, False, False)
    cls = send_batch(
        ctx.http_factory,
        ctx.destination,
        token,
        claim.body,
        event_count=claim.event_count,
        timeout=ctx.timings.request_timeout_seconds,
    )
    if cls.cls == CREDENTIAL:
        ctx.credentials.invalidate(ctx.destination)
    if cls.unexpected_success_body:
        telemetry.add_counter("unexpected_success_body", 1)
    completed = complete(ctx, claim, cls)
    if not completed:
        logger.warning(
            "SIEM delivery: stale completion for batch %s (lease taken over)",
            claim.batch_id,
        )
    return SendResult(cls, completed, cls.systemic)


def _fenced(tx: SiemTx, claim: Claim, sql: str, params: Sequence[Any]) -> int:
    return tx.execute(
        sql + " WHERE batch_id = ? AND lease_token = ?",
        list(params) + [claim.batch_id, claim.fence],
    )


class _StaleCompletion(Exception):
    """The lease was taken over: roll the whole completion back."""


def _own(tx: SiemTx, claim: Claim, sql: str, params: Sequence[Any]) -> None:
    """Serialise with claimers (state row lock FIRST, as every claim path
    takes it), then take the batch with the fenced UPDATE; any other write
    happens only after it changed exactly one row."""
    state_store.state_in(tx, lock=True)
    if _fenced(tx, claim, sql, params) != 1:
        raise _StaleCompletion()


def _members(tx: SiemTx, batch_id: str) -> List[int]:
    rows = tx.query(
        "SELECT id FROM siem_delivery_queue WHERE batch_id = ? ORDER BY batch_ordinal",
        (batch_id,),
    )
    return [int(r["id"]) for r in rows]


def _complete_accepted(
    tx: SiemTx, ctx: EngineContext, claim: Claim, cls: Classification
) -> bool:
    _own(
        tx,
        claim,
        "UPDATE siem_delivery_batches SET state = 'delivered', body = NULL, last_class = ?, "
        "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, outcome_unknown = 0",
        (cls.cls,),
    )
    row = tx.one(
        "SELECT prior_outcome_unknown FROM siem_delivery_batches WHERE batch_id = ?",
        (claim.batch_id,),
    )
    assert row is not None, "owned batch vanished inside its own transaction"
    now = tx.ts(tx.now())
    tx.execute(
        "UPDATE siem_delivery_queue SET status = 'delivered', delivered_at = ?, "
        "delivered_via = 'accepted' WHERE batch_id = ? AND status = 'batched'",
        (now, claim.batch_id),
    )
    resent = 1 if int(row["prior_outcome_unknown"] or 0) else 0
    tx.execute(
        "UPDATE siem_delivery_state SET delivered_total = delivered_total + ?, "
        "resent_after_unknown_outcome = resent_after_unknown_outcome + ? WHERE id = 1",
        (claim.event_count, resent),
    )
    telemetry.add_counter("delivered", claim.event_count)
    return True


def _complete_retry(
    tx: SiemTx, ctx: EngineContext, claim: Claim, cls: Classification
) -> bool:
    delay = backoff_seconds(ctx, claim.send_attempts + 1)
    if cls.cls == THROTTLED:
        delay = min(max(cls.retry_after_seconds or 0.0, delay), _MAX_THROTTLE_SECONDS)
    next_at = tx.now() + timedelta(seconds=delay)
    _own(
        tx,
        claim,
        "UPDATE siem_delivery_batches SET next_attempt_at = ?, last_class = ?, "
        "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
        "outcome_unknown = ?",
        (tx.ts(next_at), cls.cls, 1 if cls.outcome_unknown else 0),
    )
    return True


def _split_children(
    member_ids: List[int], cls: Classification
) -> Tuple[List[int], List[List[int]]]:
    """(rejected positions, children position-groups)."""
    positions = list(range(len(member_ids)))
    if cls.indices is not None:
        rejected = list(cls.indices)
        survivors = [p for p in positions if p not in set(rejected)]
        return rejected, ([survivors] if survivors else [])
    if len(positions) == 1:
        return positions, []
    half = (len(positions) + 1) // 2
    return [], [positions[:half], positions[half:]]


def _complete_rejection(
    tx: SiemTx, ctx: EngineContext, claim: Claim, cls: Classification
) -> bool:
    _own(
        tx,
        claim,
        "UPDATE siem_delivery_batches SET last_class = ?, lease_owner = NULL, "
        "lease_token = NULL, lease_expires_at = NULL",
        (cls.cls,),
    )
    st = state_store.state_in(tx)  # already locked by _own
    member_ids = _members(tx, claim.batch_id)
    udms = member_udms(claim.body)
    rejected, children = _split_children(member_ids, cls)
    admitted, halt_sig = quarantine_guard(
        tx, st, [cls.signature] * len(rejected), ctx.timings.quarantine_window_seconds
    )
    if halt_sig is not None:
        _quarantine(tx, claim, member_ids, rejected[:admitted], cls.signature)
        # a 400 means nothing was ingested: every other member is unbatched
        _dissolve(tx, claim.batch_id, "dissolved", cls.cls)
        state_store.halt(
            tx,
            cls="row_rejection_burst",
            signature=halt_sig,
            batch_id=None,
            mapping_version=ctx.mapping_version,
            probe_interval_seconds=ctx.timings.probe_interval_seconds,
        )
        return True
    _quarantine(tx, claim, member_ids, rejected, cls.signature)
    for group in children:
        child_id = str(uuid.uuid4())
        body = envelope([canonical_json(udms[p]) for p in group])
        _insert_batch(
            tx,
            ctx,
            child_id,
            [member_ids[p] for p in group],
            body,
            bisect_depth=claim.bisect_depth + (0 if cls.indices is not None else 1),
            mapping_version=claim.mapping_version,
        )
        for ordinal, position in enumerate(group):
            tx.execute(
                "UPDATE siem_delivery_queue SET batch_id = ?, batch_ordinal = ? "
                "WHERE id = ? AND batch_id = ?",
                (child_id, ordinal, member_ids[position], claim.batch_id),
            )
    tx.execute(
        "UPDATE siem_delivery_batches SET state = 'split', body = NULL WHERE batch_id = ?",
        (claim.batch_id,),
    )
    if rejected:
        logger.warning(
            "SIEM delivery quarantined %d event(s): reason=row_rejection signature=%s",
            len(rejected),
            cls.signature,
        )
    return True


def _quarantine(
    tx: SiemTx,
    claim: Claim,
    member_ids: List[int],
    positions: Sequence[int],
    signature: str,
) -> None:
    for position in positions:
        tx.execute(
            "UPDATE siem_delivery_queue SET status = 'quarantined', batch_id = NULL, "
            "batch_ordinal = NULL, quarantine_reason = 'row_rejection', "
            "quarantine_signature = ? WHERE id = ? AND batch_id = ?",
            (signature, member_ids[position], claim.batch_id),
        )


def _dissolve(tx: SiemTx, batch_id: str, state: str, last_class: Optional[str]) -> None:
    tx.execute(
        "UPDATE siem_delivery_queue SET status = 'pending', batch_id = NULL, "
        "batch_ordinal = NULL WHERE batch_id = ? AND status = 'batched'",
        (batch_id,),
    )
    tx.execute(
        "UPDATE siem_delivery_batches SET state = ?, body = NULL, last_class = "
        "COALESCE(?, last_class), lease_owner = NULL, lease_token = NULL, "
        "lease_expires_at = NULL WHERE batch_id = ?",
        (state, last_class, batch_id),
    )


def dissolve_batch(ctx: EngineContext, batch_id: str, state: str = "dissolved") -> None:
    def _do(tx: SiemTx) -> None:
        state_store.state_in(tx, lock=True)  # lock order: state row first
        _dissolve(tx, batch_id, state, None)

    ctx.db.write(_do, phase="complete")


def _complete_systemic(
    tx: SiemTx, ctx: EngineContext, claim: Claim, cls: Classification
) -> bool:
    _own(
        tx,
        claim,
        "UPDATE siem_delivery_batches SET last_class = ?, lease_owner = NULL, "
        "lease_token = NULL, lease_expires_at = NULL, outcome_unknown = 0",
        (cls.cls,),
    )
    state_store.halt(
        tx,
        cls=cls.cls,
        signature=cls.signature,
        batch_id=claim.batch_id,
        mapping_version=ctx.mapping_version,
        probe_interval_seconds=ctx.timings.probe_interval_seconds,
    )
    logger.warning(
        "SIEM delivery halted: class=%s signature=%s batch_id=%s",
        cls.cls,
        cls.signature,
        claim.batch_id,
    )
    return True


def complete(ctx: EngineContext, claim: Claim, cls: Classification) -> bool:
    if cls.cls == ACCEPTED:
        handler = _complete_accepted
    elif cls.cls in (TRANSIENT, THROTTLED):
        handler = _complete_retry
    elif cls.cls == ROW_REJECTION:
        handler = _complete_rejection
    else:
        handler = _complete_systemic
    try:
        return bool(
            ctx.db.write(lambda tx: handler(tx, ctx, claim, cls), phase="complete")
        )
    except _StaleCompletion:
        return False
