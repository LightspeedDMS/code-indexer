"""A duplicate response halts delivery and is NEVER counted as delivered (RED for now).

The sidecar's 409 ALREADY_EXISTS exercises delivery's DUPLICATE_POLICY = HALT
branch; it proves nothing about Google's real (undocumented) duplicate
response.  Two cases, in this order:
  1. a first-attempt 409: halts, is never resent, and the admin acknowledge
     front door then clears the halt (so the next case starts unhalted);
  2. the specified sequence -- the batch is accepted, the response is lost,
     the resend is answered 409 -- halts with the row undelivered and no
     retry.  The ``delivery`` fixture's teardown resolves its halt through the
     admin front door (also when an assertion fails).
Every assertion is scoped to this scenario's own uuids, requests and deltas.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    halt_class,
    poll,
    request_body,
    requests_carrying,
    search_sidecar,
    sidecar_fault,
)

# Two delivery loop cycles (CYCLE_SECONDS = 30) plus margin: long enough to
# see any automatic resend, which must not happen.
NO_RESEND_OBSERVE_SECONDS = float(os.environ.get("E2E_SIEM_NO_RESEND_OBSERVE", "70"))
OBSERVE_POLL_SECONDS = 5.0
DUPLICATE = "duplicate_response"


def _assert_halted_without_retry(
    delivery: SiemDelivery, sidecar: AttachedSidecar, uuid: str, delivered_before: int
) -> None:
    """Over the observation window: still halted, nothing delivered, no resend."""
    requests_at_halt = len(requests_carrying(sidecar, [uuid]))
    deadline = time.monotonic() + NO_RESEND_OBSERVE_SECONDS
    while time.monotonic() < deadline:
        stats = delivery.stats()
        assert halt_class(stats) == DUPLICATE, "the halt cleared on its own"
        assert fleet(stats, "delivered_total") == delivered_before
        time.sleep(OBSERVE_POLL_SECONDS)
    assert len(requests_carrying(sidecar, [uuid])) == requests_at_halt, (
        "a duplicate-halted batch was retried"
    )


def test_first_attempt_duplicate_halts_until_admin_acknowledges(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-dup")
    door.create_user(user)
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 409}
    )
    delivered_before = fleet(delivery.stats(), "delivered_total")
    uuid = door.rest_login_success(user).event_uuid
    delivery.wait_stats(
        lambda s: halt_class(s) == DUPLICATE, DELIVERY_TIMEOUT, "a halt"
    )
    assert [r["http_status_returned"] for r in requests_carrying(sidecar, [uuid])] == [
        409
    ]
    _assert_halted_without_retry(delivery, sidecar, uuid, delivered_before)
    assert search_sidecar(sidecar, uuid) == []
    delivery.acknowledge_halted_batch()
    delivery.wait_stats(
        lambda s: halt_class(s) is None, DELIVERY_TIMEOUT, "halt cleared"
    )


def test_resend_after_lost_response_answered_409_halts_and_stays_undelivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-dup2")
    door.create_user(user)
    sidecar_fault(sidecar, "/_control/config", {"duplicate_mode": "already_exists"})
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "accept_then_drop"}
    )
    delivered_before = fleet(delivery.stats(), "delivered_total")
    uuid = door.rest_login_success(user).event_uuid

    def _resend_seen() -> Optional[List[Dict[str, Any]]]:
        carrying = requests_carrying(sidecar, [uuid])
        return carrying if len(carrying) >= 2 else None

    first, resend = poll(_resend_seen, DELIVERY_TIMEOUT, "the resend")[:2]
    assert first["fault_applied"] == "accept_then_drop"
    assert resend["http_status_returned"] == 409 and resend["duplicate"] is True
    assert request_body(sidecar, resend["seq"]) == request_body(sidecar, first["seq"])
    delivery.wait_stats(
        lambda s: halt_class(s) == DUPLICATE, DELIVERY_TIMEOUT, "a halt"
    )
    _assert_halted_without_retry(delivery, sidecar, uuid, delivered_before)
