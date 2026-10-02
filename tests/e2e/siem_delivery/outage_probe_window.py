"""The ONE log row test_03's scripted outage may excuse from the Phase 7 audit.

test_03 refuses connections at the mock receiver, whose port also serves the
token endpoint.  If the scheduler's periodic credential self-check
(``scheduler._maybe_probe_credentials``) lands inside that outage, it logs one
WARNING: "this process cannot mint SecOps tokens (token_endpoint_unreachable)".

That is expected there and nowhere else, so it is never allowlisted by text.
test_03 records the exact ids of matching rows logged inside its own outage
window (log ids AND wall-clock time) into ``EXCUSED_LOG_IDS``; the Phase 7
audit then drops only those ids.  The same text at any other time -- a genuine
token-endpoint failure in another scenario -- still fails the audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Collection, Dict, Iterable, List, Set

from tests.e2e.log_audit_gate import AuditGateResult

PROBE_SOURCE = "code_indexer.server.services.siem_delivery.scheduler"
PROBE_MESSAGE = (
    "SIEM delivery: this process cannot mint SecOps tokens (token_endpoint_unreachable)"
)
PROBE_LEVEL = "WARNING"

# Row ids test_03 proved were logged inside its own outage (one pytest process).
EXCUSED_LOG_IDS: Set[int] = set()


@dataclass(frozen=True)
class OutageWindow:
    """Log watermark and wall-clock bounds of one scripted outage."""

    after_log_id: int  # audit watermark taken just before the refuse
    started: datetime  # just before the refuse (timezone-aware)
    ended: datetime  # once the outage-end call returned (timezone-aware)


def in_window_probe_failures(
    entries: Iterable[Dict[str, Any]], window: OutageWindow, through_log_id: int
) -> List[int]:
    """Ids of exact probe-failure rows logged inside *window*.

    A row qualifies only with the exact level, logger source and full message,
    and an id in ``(window.after_log_id, through_log_id]``.  Such a row whose
    timestamp falls outside the outage fails loudly rather than being excused.
    """
    ids: List[int] = []
    for entry in entries:
        log_id = int(entry.get("id") or 0)
        if not window.after_log_id < log_id <= through_log_id:
            continue
        signature = (entry.get("level"), entry.get("source"), entry.get("message"))
        if signature != (PROBE_LEVEL, PROBE_SOURCE, PROBE_MESSAGE):
            continue
        logged_at = datetime.fromisoformat(str(entry.get("timestamp")))
        assert window.started <= logged_at <= window.ended, (
            f"probe failure (log id {log_id}) logged at {logged_at.isoformat()}, "
            f"outside the outage {window.started.isoformat()} .. "
            f"{window.ended.isoformat()}"
        )
        ids.append(log_id)
    return ids


def without_excused(
    result: AuditGateResult, excused: Collection[int]
) -> AuditGateResult:
    """*result* minus the violations whose id is in *excused*."""
    remaining = [v for v in result.violations if v.get("id") not in excused]
    return AuditGateResult(
        passed=not remaining, violations=remaining, phase_name=result.phase_name
    )
