"""Byte-identical resend after a REAL crash (RED until SIEM delivery exists).

The sidecar stores the batch, then closes the connection without a reply
(``accept_then_drop``), so the sender cannot know the outcome.  The test then
SIGKILLs the server it owns and restarts it on the same data dir.  The batch
can only be resent byte-identically if its exact body was persisted -- a body
held in memory dies with the process.  The resend counter and the delivered
outcome must be recorded in the server's durable state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx

from tests.e2e.siem_delivery.conftest import (
    AttachedSidecar,
    SiemE2EConfig,
    admin_provider_for,
)
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.restartable_server import RestartableServer
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    LEASE_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    poll,
    request_body,
    requests_carrying,
    sidecar_fault,
    wait_delivered,
)

RESENT = "resent_after_unknown_outcome"


def _clients(
    server: RestartableServer, sidecar: AttachedSidecar
) -> Tuple[httpx.Client, SiemDelivery, FrontDoor]:
    http = httpx.Client(base_url=server.url, timeout=60)
    admin = admin_provider_for(server.url, server.admin_user, server.admin_pass)
    config = SiemE2EConfig(server.url, server.admin_user, server.admin_pass)
    return http, SiemDelivery(http, admin, config, sidecar), FrontDoor(http, admin)


def test_resend_after_crash_is_byte_identical_and_recorded_durably(
    restartable_server: RestartableServer, sidecar: AttachedSidecar
) -> None:
    http, delivery, door = _clients(restartable_server, sidecar)
    try:
        delivery.arm()
        sidecar_fault(sidecar, "/_control/config", {"duplicate_mode": "ok_noop"})
        user = unique_name("siem-crash")
        door.create_user(user)  # its own deliverable event drains first
        arm_fault_after_drain(
            delivery, sidecar, "/_control/faults", {"mode": "accept_then_drop"}
        )
        before = delivery.stats()
        uuid = door.rest_login_success(user).event_uuid
        dropped = poll(
            lambda: requests_carrying(sidecar, [uuid]) or None,
            DELIVERY_TIMEOUT,
            "the login batch to be accepted and its response dropped",
        )[0]
        assert uuid.encode() in request_body(sidecar, dropped["seq"])
        assert dropped["fault_applied"] == "accept_then_drop"
        assert dropped["http_status_returned"] is None
    finally:
        http.close()
    restartable_server.kill()  # crash before the sender can record anything
    restartable_server.start()
    http, delivery, _ = _clients(restartable_server, sidecar)
    try:

        def _resent() -> Optional[List[Dict[str, Any]]]:
            carrying = requests_carrying(sidecar, [uuid])
            return carrying if len(carrying) >= 2 else None

        resend = poll(_resent, LEASE_TIMEOUT, "the resend after restart")[1]
        original = request_body(sidecar, dropped["seq"])
        assert request_body(sidecar, resend["seq"]) == original, "resend bytes differ"
        assert resend["duplicate"] is True and resend["http_status_returned"] == 200
        assert len(wait_delivered(sidecar, uuid)) == 1
        delivery.wait_stats(
            lambda s: fleet(s, RESENT) > fleet(before, RESENT)
            and fleet(s, "delivered_total") > fleet(before, "delivered_total"),
            DELIVERY_TIMEOUT,
            "the resend and the delivered outcome to be recorded",
        )
    finally:
        http.close()
