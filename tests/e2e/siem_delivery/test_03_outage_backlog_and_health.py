"""SecOps outage: a backlog grows, health is DEGRADED never ERROR, then it drains.

RED until SIEM delivery exists.  The outage is the sidecar's connection-refused switch,
always ended (in ``finally``) so later scenarios start clean.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import httpx

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, PilotEvent, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DEGRADED_TIMEOUT,
    DELIVERY_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    poll,
    search_sidecar,
    sidecar_fault,
    wait_delivered,
)

BACKLOG_LOGINS = 3


@contextmanager
def _sidecar_outage(delivery: SiemDelivery, sidecar: AttachedSidecar) -> Iterator[None]:
    """Arm capture, then refuse connections at the sidecar until the block ends."""
    delivery.arm()
    arm_fault_after_drain(delivery, sidecar, "/_control/outage", {"mode": "refuse"})
    try:
        yield
    finally:
        sidecar_fault(sidecar, "/_control/outage", {"mode": "end"})


def _backlog_events(door: FrontDoor) -> List[PilotEvent]:
    user = unique_name("siem-outage")
    events = [door.create_user(user)]
    events.extend(door.rest_login_success(user) for _ in range(BACKLOG_LOGINS))
    return events


def test_outage_backlog_then_drains_after_recovery(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    with _sidecar_outage(delivery, sidecar):
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
    health = http.get(
        "/api/system/health", headers={"Authorization": "Bearer " + admin.get_token()}
    ).json()
    assert health["status"] != "unhealthy", f"SIEM outage escalated to ERROR: {health}"
    reasons = [
        r
        for r in health["failure_reasons"]
        if "siem" in r.lower() or "backlog" in r.lower()
    ]
    if health["status"] == "degraded" and reasons:
        return dict(health)
    return None


def test_health_is_degraded_never_error_during_outage(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    siem_http: httpx.Client,
    siem_admin: AdminTokenProvider,
) -> None:
    with _sidecar_outage(delivery, sidecar):
        assert len(_backlog_events(door)) == BACKLOG_LOGINS + 1
        poll(
            lambda: _degraded_siem_reason(siem_http, siem_admin),
            DEGRADED_TIMEOUT,
            "a DEGRADED SIEM health reason during the outage",
        )
