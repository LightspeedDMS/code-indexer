"""SIEM delivery health: DEGRADED reasons only (never an error that would
drain nodes fleet-wide on a SecOps outage), and the operator alert signals
(one rate-limited ERROR per reason, INFO on recovery; no paging).

Everything here reads in-memory scheduler state only: O(1), no I/O.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from code_indexer.server.services.siem_delivery.sender import ProbeResult
from code_indexer.server.services.siem_delivery.stats import persisted_snapshot

logger = logging.getLogger(__name__)

GIB = 1024**3
DEGRADED_BACKLOG_BYTES = 1 * GIB
DEGRADED_HOURS_TO_FULL = 24.0
ALERT_BACKLOG_BYTES = 5 * GIB
ALERT_HOURS_TO_FULL = 12.0


def _pending(state: Mapping[str, Any]) -> Dict[str, Any]:
    return persisted_snapshot(dict(state))


def siem_health_reasons(
    inputs: Optional[Mapping[str, Any]], startup_error: Optional[str]
) -> List[str]:
    """DEGRADED reasons, in priority order (a halt first)."""
    if startup_error is not None:
        return [f"SIEM delivery not running in this process: {startup_error}"]
    if inputs is None:
        return []
    view = inputs["view"]
    state: Mapping[str, Any] = inputs["state"]
    snap = _pending(state)
    timings = inputs["timings"]
    enabled = bool(view is not None and view.section.enabled)
    pending = int(snap.get("pending") or 0)
    quiet = not enabled and pending == 0 and not state.get("halted_class")
    if quiet and view is not None:
        return []  # delivery disabled and nothing pending: no SIEM signal
    if view is None and inputs.get("config_error") is None:
        return []  # this process has not run its first cycle yet
    reasons: List[str] = []
    if state.get("halted_class"):
        reasons.append(f"SIEM delivery halted: {state['halted_class']}")
    last = inputs.get("last_loop_cycle_monotonic")
    if last is not None and time.monotonic() - last > (
        _STALL_FACTOR * max(timings.cycle_idle_seconds, timings.cycle_busy_seconds) + 60
    ):
        reasons.append("SIEM delivery loop stalled in this process")
    if view is None:
        reasons.append(
            "SIEM delivery config never loaded in this process; capture INACTIVE here"
        )
    elif inputs.get("config_error"):
        reasons.append(
            "SIEM delivery config unreadable in this process; using last-known-good "
            "(the 90 s stale-capture bound is suspended here)"
        )
    age = float(snap.get("oldest_pending_age_seconds") or 0.0)
    if pending and age > timings.backlog_degraded_age_seconds:
        reasons.append(
            f"SIEM delivery backlog: oldest pending event is {int(age)} s old"
        )
    quarantined = int(snap.get("quarantined") or 0)
    if quarantined:
        reasons.append(f"SIEM delivery: {quarantined} events quarantined")
    unrecoverable = int(state.get("unrecoverable_total") or 0)
    if unrecoverable:
        reasons.append(
            f"SIEM delivery: {unrecoverable} events unrecoverable (projection failed "
            "and the audit row has aged out)"
        )
    if (
        state.get("armed_destination_key")
        and int(state.get("canary_mapping_version") or 0) != inputs["mapping_version"]
    ):
        reasons.append(
            f"SIEM delivery canary not run for mapping version {inputs['mapping_version']}"
        )
    backlog_bytes = int(snap.get("backlog_bytes_estimate") or 0)
    hours = snap.get("projected_hours_to_disk_full")
    if backlog_bytes > DEGRADED_BACKLOG_BYTES or (
        hours is not None and float(hours) < DEGRADED_HOURS_TO_FULL
    ):
        reasons.append("SIEM delivery backlog large or disk headroom low")
    for proc in inputs.get("processes") or []:
        if proc.get("probe_result") not in (
            ProbeResult.OK.value,
            ProbeResult.PENDING.value,
        ):
            reasons.append(
                f"SIEM delivery: process {proc['process_id']} cannot mint SecOps "
                f"tokens: {proc['probe_result']}"
            )
    for entry in snap.get("unconfigured_destinations") or []:
        reasons.append(
            f"SIEM delivery: {entry['pending']} events pending for unconfigured "
            f"destination {entry['destination_key']}"
        )
    failures = sum((inputs.get("capture_failures") or {}).values())
    if failures:
        reasons.append(f"SIEM capture failures since boot: {failures}")
    late = int(state.get("capture_after_boundary_late_total") or 0)
    if late:
        reasons.append(
            f"SIEM delivery: {late} pilot events captured more than 90 s after a "
            "SIEM disable/change"
        )
    return reasons


_STALL_FACTOR = 3


class AlertSignals:
    """Operator alerts: ERROR when a signal first holds, at most once per repeat window
    per reason while it persists; INFO once on recovery.  Per-process
    telemetry (evaluated by the loop, never on a request path)."""

    def __init__(
        self, timings: Any, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._timings = timings
        self._clock = clock
        self._lock = threading.Lock()
        self._active: Dict[str, float] = {}

    def _signals(self, inputs: Mapping[str, Any]) -> Dict[str, str]:
        state = inputs["state"]
        snap = _pending(state)
        out: Dict[str, str] = {}
        age = float(snap.get("oldest_pending_age_seconds") or 0.0)
        if (
            int(snap.get("pending") or 0)
            and age > self._timings.alert_oldest_pending_seconds
        ):
            out["backlog_age"] = f"oldest pending event is {int(age)} s old"
        since = state.get("halted_since")
        if since is not None and state.get("halted_class"):
            from code_indexer.server.services.siem_delivery.db import Dialect

            parsed = Dialect.parse_ts(since)
            if parsed is not None:
                from datetime import datetime, timezone

                halted_for = (datetime.now(timezone.utc) - parsed).total_seconds()
                if halted_for > self._timings.alert_halted_seconds:
                    out["halted"] = (
                        f"halted ({state['halted_class']}) for {int(halted_for)} s"
                    )
        if int(snap.get("backlog_bytes_estimate") or 0) > ALERT_BACKLOG_BYTES:
            out["backlog_size"] = "backlog estimate over 5 GB"
        hours = snap.get("projected_hours_to_disk_full")
        if hours is not None and float(hours) < ALERT_HOURS_TO_FULL:
            out["disk"] = "projected disk full in under 12 h"
        if sum((inputs.get("capture_failures") or {}).values()):
            out["capture_failures"] = "capture failures since boot"
        return out

    def evaluate(self, inputs: Mapping[str, Any]) -> None:
        current = self._signals(inputs)
        now = self._clock()
        with self._lock:
            for reason, text in current.items():
                last = self._active.get(reason)
                if last is None or now - last >= self._timings.alert_repeat_seconds:
                    self._active[reason] = now
                    logger.error("SIEM delivery alert: %s: %s", reason, text)
            for reason in [r for r in self._active if r not in current]:
                del self._active[reason]
                logger.info("SIEM delivery alert recovered: %s", reason)
