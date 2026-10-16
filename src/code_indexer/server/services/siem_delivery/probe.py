"""The tick body, systemic-halt probes, and the automatic requeue of rows
quarantined by an older mapping version.

While halted, the per-class probe is the ONLY sending path; every halt class
but ``duplicate_response`` (which needs an admin decision) can clear itself.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, Mapping, Optional, Tuple

from code_indexer.server.services.siem_delivery import state_store
from code_indexer.server.services.siem_delivery.claim import (
    MAX_BATCHES_PER_TICK,
    PROBE_SAMPLE_ROWS,
    EngineContext,
    build_candidates,
    claim_batch,
    claim_specific,
    read_candidate_rows,
    window_admits,
)
from code_indexer.server.services.siem_delivery.classifier import (
    ACCEPTED,
    DUPLICATE_RESPONSE,
    NOT_INGESTED_CLASSES,
    THROTTLED,
)
from code_indexer.server.services.siem_delivery.completion import (
    dissolve_batch,
    send_and_complete,
)
from code_indexer.server.services.siem_delivery.db import SiemTx

logger = logging.getLogger(__name__)

REQUEUE_ROWS_PER_TX = 500
_MAX_REQUEUE_ROUNDS = 100_000


def _state_and_now(ctx: EngineContext) -> Tuple[Dict[str, Any], Any]:
    return ctx.db.read(lambda tx: (state_store.state_in(tx), tx.now()))


def probe_due(ctx: EngineContext, state: Mapping[str, Any], now: Any) -> bool:
    cls = state.get("halted_class")
    if not cls or cls == DUPLICATE_RESPONSE:
        return False
    if int(state.get("halted_mapping_version") or 0) < ctx.mapping_version:
        return True  # a release with a fix probes at once
    next_at = ctx.db.dialect.parse_ts(state.get("next_probe_at"))
    return next_at is None or now >= next_at


def run_tick(ctx: EngineContext) -> Dict[str, Any]:
    deadline = time.monotonic() + ctx.timings.tick_time_budget_seconds
    state, now = _state_and_now(ctx)
    if state.get("halted_class"):
        if probe_due(ctx, state, now):
            return {"probe": run_probe(ctx, state)}
        return {"halted": state["halted_class"]}
    sent = 0
    for _ in range(MAX_BATCHES_PER_TICK):
        if time.monotonic() > deadline:
            break
        claim = claim_batch(ctx)
        if claim is None:
            break
        result = send_and_complete(ctx, claim)
        sent += 1
        throttled = (
            result.classification is not None and result.classification.cls == THROTTLED
        )
        if result.systemic or throttled:
            break  # a 429 ends this tick for the destination
    return {"batches_sent": sent}


def _clear(
    ctx: EngineContext,
    halted_since: Any,
    reset_window: bool,
    admit_failures: Optional[int] = None,
) -> bool:
    """Clear the halt.  With *admit_failures*, only when the quarantine guard
    would admit that many failures under the CURRENT window (evaluated under
    the state lock); the window itself is then kept."""

    def _do(tx: SiemTx) -> int:
        st = state_store.state_in(tx, lock=True)
        if st.get("halted_since") != halted_since:
            return 0
        if admit_failures is not None and not window_admits(
            tx, st, admit_failures, ctx.timings.quarantine_window_seconds
        ):
            return 0
        return state_store.clear_halt(tx, reset_quarantine_window=reset_window)

    cleared = bool(ctx.db.write(_do, phase="probe"))
    if cleared:
        logger.info("SIEM delivery halt cleared by probe")
    return cleared


def _defer(ctx: EngineContext, halted_since: Any) -> None:
    def _do(tx: SiemTx) -> None:
        st = state_store.state_in(tx, lock=True)
        if st.get("halted_since") != halted_since:
            return
        tx.execute(
            "UPDATE siem_delivery_state SET next_probe_at = ?, halted_mapping_version = ? "
            "WHERE id = 1",
            (
                tx.ts(tx.now() + timedelta(seconds=ctx.timings.probe_interval_seconds)),
                ctx.mapping_version,
            ),
        )

    ctx.db.write(_do, phase="probe")


def _probe_local(ctx: EngineContext, state: Mapping[str, Any]) -> str:
    rows = read_candidate_rows(ctx, PROBE_SAMPLE_ROWS)
    cand = build_candidates(ctx, rows)  # nothing sent, nothing persisted
    signatures = [f[1] for f in cand.failures]
    if state.get("halted_signature") not in signatures and _clear(
        ctx,
        state.get("halted_since"),
        reset_window=False,
        admit_failures=len(signatures),
    ):
        return "cleared"
    _defer(ctx, state.get("halted_since"))
    return "still_failing"


def _probe_send(ctx: EngineContext, state: Mapping[str, Any], claim: Any) -> str:
    if claim is None:
        # nothing left to deliver: the halt has nothing to protect
        _clear(ctx, state.get("halted_since"), reset_window=True)
        return "cleared_empty"
    result = send_and_complete(ctx, claim)
    cls = result.classification.cls if result.classification else None
    if cls == ACCEPTED:
        _clear(ctx, state.get("halted_since"), reset_window=True)
        return "cleared"
    current, _now = _state_and_now(ctx)
    if current.get("halted_since") == state.get("halted_since"):
        _defer(ctx, state.get("halted_since"))
    return f"still_halted:{cls}"


def run_probe(ctx: EngineContext, state: Mapping[str, Any]) -> str:
    cls = state.get("halted_class")
    if cls == "local_validation_burst":
        return _probe_local(ctx, state)
    if cls == "row_rejection_burst":
        claim = claim_batch(ctx, bypass_halt=True, limit=PROBE_SAMPLE_ROWS)
        return _probe_send(ctx, state, claim)
    batch_id = state.get("halted_batch_id")
    batch = None
    if batch_id:
        batch = ctx.db.read(
            lambda tx: tx.one(
                "SELECT batch_id, last_class, mapping_version FROM siem_delivery_batches "
                "WHERE batch_id = ? AND state = 'pending_send'",
                (batch_id,),
            )
        )
    if (
        batch is not None
        and batch.get("last_class") in NOT_INGESTED_CLASSES
        and int(batch["mapping_version"]) < ctx.mapping_version
    ):
        dissolve_batch(ctx, str(batch_id))  # older code built it: rebuild
        batch = None
    claim = (
        claim_specific(ctx, str(batch_id))
        if batch is not None
        else claim_batch(ctx, bypass_halt=True, limit=PROBE_SAMPLE_ROWS)
    )
    return _probe_send(ctx, state, claim)


def requeue_after_mapping_change(ctx: EngineContext) -> int:
    """Once per MAPPING_VERSION, quarantined rows from older code return
    to pending (paced).  Returns the number requeued (0 when already done)."""
    state = state_store.read_state(ctx.db)
    if int(state.get("requeued_mapping_version") or 0) >= ctx.mapping_version:
        return 0
    total = 0
    for _ in range(_MAX_REQUEUE_ROUNDS):

        def _round(tx: SiemTx) -> int:
            state_store.state_in(tx, lock=True)  # lock order: state row first
            rows = tx.query(
                "SELECT id FROM siem_delivery_queue WHERE status = 'quarantined' "
                "AND mapping_version < ? ORDER BY id LIMIT ?",
                (ctx.mapping_version, REQUEUE_ROWS_PER_TX),
            )
            now = tx.ts(tx.now())
            for row in rows:
                tx.execute(
                    "UPDATE siem_delivery_queue SET status = 'pending', batch_id = NULL, "
                    "quarantine_reason = NULL, quarantine_signature = NULL, "
                    "next_attempt_at = ? WHERE id = ? AND status = 'quarantined'",
                    (now, row["id"]),
                )
            return len(rows)

        moved = int(ctx.db.write(_round, phase="requeue"))
        total += moved
        if moved < REQUEUE_ROWS_PER_TX:
            break
    ctx.db.write(
        lambda tx: tx.execute(
            "UPDATE siem_delivery_state SET requeued_mapping_version = ? WHERE id = 1 "
            "AND requeued_mapping_version < ?",
            (ctx.mapping_version, ctx.mapping_version),
        ),
        phase="requeue",
    )
    return total
