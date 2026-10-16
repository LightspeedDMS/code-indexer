"""SecOps outage: a backlog grows, health is DEGRADED never ERROR, then it drains.

RED until SIEM delivery exists.  The outage is the sidecar's connection-refused switch,
always ended (in ``finally``) so later scenarios start clean.

The refused port also serves the token endpoint, so the scheduler's credential
self-check may fail inside the outage and log one probe WARNING.  After each
outage the credential probe must recover, and only probe rows logged inside
that outage are excused from the Phase 7 audit (``outage_probe_window``).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import httpx

from tests.e2e.log_audit_gate import (
    get_log_watermark,
    poll_until_stable_count,
    query_logs_via_mcp,
)
from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, PilotEvent, unique_name
from tests.e2e.siem_delivery.outage_probe_window import (
    EXCUSED_LOG_IDS,
    OutageWindow,
    in_window_probe_failures,
)
from tests.e2e.siem_delivery.siem_api import (
    DEGRADED_TIMEOUT,
    DELIVERY_TIMEOUT,
    PROBE_TIMEOUT,
    SiemDelivery,
    fleet,
    poll,
    search_sidecar,
    sidecar_fault,
    wait_delivered,
)

BACKLOG_LOGINS = 3
PROBE_FAILURE_REASON = "cannot mint SecOps tokens"
# Same settle bounds the Phase 7 audit fixture uses before reading the logs.
LOG_SETTLE_MAX_ATTEMPTS = 10
LOG_SETTLE_SLEEP_SECONDS = 0.3


def _health(http: httpx.Client, admin: AdminTokenProvider) -> Dict[str, Any]:
    resp = http.get(
        "/api/system/health", headers={"Authorization": "Bearer " + admin.get_token()}
    )
    assert resp.status_code == 200, f"health query failed: {resp.status_code}"
    return dict(resp.json())


def _probe_recovered(http: httpx.Client, admin: AdminTokenProvider) -> Optional[bool]:
    reasons = _health(http, admin)["failure_reasons"]
    return None if any(PROBE_FAILURE_REASON in r for r in reasons) else True


def _excuse_in_window_probe_failures(
    http: httpx.Client, admin: AdminTokenProvider, window: OutageWindow
) -> None:
    poll(
        lambda: _probe_recovered(http, admin),
        PROBE_TIMEOUT,
        "the SecOps credential probe to recover after the outage",
    )
    poll_until_stable_count(
        count_fn=lambda: len(query_logs_via_mcp(http, admin.get_token())),
        max_attempts=LOG_SETTLE_MAX_ATTEMPTS,
        sleep_seconds=LOG_SETTLE_SLEEP_SECONDS,
    )
    entries = query_logs_via_mcp(http, admin.get_token())
    through_log_id = max((int(e.get("id") or 0) for e in entries), default=0)
    EXCUSED_LOG_IDS.update(in_window_probe_failures(entries, window, through_log_id))


@contextmanager
def _sidecar_outage(
    delivery: SiemDelivery,
    sidecar: AttachedSidecar,
    http: httpx.Client,
    admin: AdminTokenProvider,
) -> Iterator[None]:
    """Arm capture, refuse connections at the sidecar until the block ends.

    The queue drains BEFORE the window opens (a one-shot fault would otherwise
    hit an earlier scenario's rows); the window spans only the refuse itself.
    """
    delivery.arm()
    delivery.wait_stats(
        lambda s: fleet(s, "pending") == 0,
        DELIVERY_TIMEOUT,
        "the SIEM queue to drain before the outage",
    )
    after_log_id = get_log_watermark(http, admin.get_token())
    started = datetime.now(timezone.utc)
    sidecar_fault(sidecar, "/_control/outage", {"mode": "refuse"})
    try:
        yield
    finally:
        sidecar_fault(sidecar, "/_control/outage", {"mode": "end"})
    window = OutageWindow(after_log_id, started, datetime.now(timezone.utc))
    _excuse_in_window_probe_failures(http, admin, window)


def _backlog_events(door: FrontDoor) -> List[PilotEvent]:
    user = unique_name("siem-outage")
    events = [door.create_user(user)]
    events.extend(door.rest_login_success(user) for _ in range(BACKLOG_LOGINS))
    return events


def test_outage_backlog_then_drains_after_recovery(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    siem_http: httpx.Client,
    siem_admin: AdminTokenProvider,
) -> None:
    with _sidecar_outage(delivery, sidecar, siem_http, siem_admin):
        pending_before = fleet(delivery.stats(), "pending")
        events = _backlog_events(door)
        delivery.wait_stats(
            lambda s: fleet(s, "pending") >= pending_before + len(events),
            DELIVERY_TIMEOUT,
            "this scenario's rows to be pending",
        )
        assert all(search_sidecar(sidecar, e.event_uuid) == [] for e in events)
    for event in events:
        assert len(wait_delivered(sidecar, event.event_uuid)) == 1


def _degraded_siem_reason(
    http: httpx.Client, admin: AdminTokenProvider
) -> Optional[Dict[str, Any]]:
    """Asserts never-ERROR on EVERY call; returns the health once SIEM is DEGRADED."""
    assert http.get("/healthz").status_code == 200, (
        "an outage must never drain the node"
    )
    health = _health(http, admin)
    assert health["status"] != "unhealthy", f"SIEM outage escalated to ERROR: {health}"
    reasons = [
        r
        for r in health["failure_reasons"]
        if "siem" in r.lower() or "backlog" in r.lower()
    ]
    if health["status"] == "degraded" and reasons:
        return health
    return None


def test_health_is_degraded_never_error_during_outage(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    siem_http: httpx.Client,
    siem_admin: AdminTokenProvider,
) -> None:
    with _sidecar_outage(delivery, sidecar, siem_http, siem_admin):
        assert len(_backlog_events(door)) == BACKLOG_LOGINS + 1
        poll(
            lambda: _degraded_siem_reason(siem_http, siem_admin),
            DEGRADED_TIMEOUT,
            "a DEGRADED SIEM health reason during the outage",
        )
