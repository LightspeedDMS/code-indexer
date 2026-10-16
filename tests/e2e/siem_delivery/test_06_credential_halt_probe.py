"""A credential failure halts delivery; the probe then self-heals (RED until delivery exists).

The sidecar answers the next import with 401 UNAUTHENTICATED, once.  Delivery
must halt systemically (class ``credential``) WITHOUT quarantining anything,
keep the node serviceable, and resume on its own when the probe time arrives
(PROBE_INTERVAL is 15 min in production code, hence PROBE_TIMEOUT) by
resending the SAME persisted bytes of the halted batch.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    PROBE_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    halt_class,
    poll,
    request_body,
    requests_carrying,
)


def test_credential_failure_halts_then_probe_self_heals(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    siem_http: httpx.Client,
    siem_admin: AdminTokenProvider,
) -> None:
    delivery.arm()
    user = unique_name("siem-cred")
    door.create_user(user)
    quarantined_before = fleet(delivery.stats(), "quarantined")
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 401}
    )
    uuid = door.rest_login_success(user).event_uuid
    delivery.wait_stats(
        lambda s: halt_class(s) == "credential", DELIVERY_TIMEOUT, "a credential halt"
    )
    rejected = requests_carrying(sidecar, [uuid])
    assert [r["http_status_returned"] for r in rejected] == [401]
    assert siem_http.get("/healthz").status_code == 200
    health = siem_http.get(
        "/api/system/health",
        headers={"Authorization": "Bearer " + siem_admin.get_token()},
    ).json()
    assert health["status"] == "degraded"
    assert any("halted" in r.lower() for r in health["failure_reasons"])

    def _probe_resend() -> Optional[List[Dict[str, Any]]]:
        carrying = requests_carrying(sidecar, [uuid])
        return carrying if len(carrying) >= 2 else None

    resend = poll(_probe_resend, PROBE_TIMEOUT, "the halt probe's resend")[1]
    assert resend["http_status_returned"] == 200
    assert request_body(sidecar, resend["seq"]) == request_body(
        sidecar, rejected[0]["seq"]
    ), "the probe did not resend the persisted batch byte-identically"
    stats = delivery.wait_stats(
        lambda s: halt_class(s) is None, DELIVERY_TIMEOUT, "the halt to clear"
    )
    assert fleet(stats, "quarantined") == quarantined_before, (
        "a credential halt quarantined rows"
    )
