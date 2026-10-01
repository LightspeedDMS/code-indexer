"""One poison row is quarantined without blocking the others (RED until delivery exists).

The three logins are captured back-to-back while the sidecar refuses
connections, so they are pending together when it recovers.  The sidecar then
rejects (unindexed, no event index) every request that contains the middle
row.  Delivery must isolate exactly that row by bisection under the
signature guard, quarantine it, deliver the other two, and NOT halt.  Every
assertion is scoped to this scenario's own uuids and requests.
"""

from __future__ import annotations

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    halt_class,
    request_body,
    requests_carrying,
    search_sidecar,
    sidecar_fault,
    wait_delivered,
)

ROWS = 3


def test_single_rejected_row_is_quarantined_without_blocking_others(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-poison")
    door.create_user(user)
    # Earlier rows (incl. user_created) drain first, so nothing else is
    # pending when the outage starts and only this scenario's rows batch up.
    arm_fault_after_drain(delivery, sidecar, "/_control/outage", {"mode": "refuse"})
    quarantined_before = fleet(delivery.stats(), "quarantined")
    try:
        uuids = [door.rest_login_success(user).event_uuid for _ in range(ROWS)]
        poison = uuids[1]
        sidecar_fault(
            sidecar,
            "/_control/faults",
            {"mode": "reject_if_contains", "product_log_id": poison},
        )
    finally:
        sidecar_fault(sidecar, "/_control/outage", {"mode": "end"})
    for good in (uuids[0], uuids[2]):
        assert len(wait_delivered(sidecar, good)) == 1
    delivery.wait_stats(
        lambda s: fleet(s, "quarantined") > quarantined_before,
        DELIVERY_TIMEOUT,
        "the poison row to be quarantined",
    )
    carrying = requests_carrying(sidecar, uuids)
    first_body = request_body(sidecar, carrying[0]["seq"])
    assert all(u.encode() in first_body for u in uuids), "rows were not one batch"
    assert carrying[0]["http_status_returned"] == 400
    # Bisection of an n-event batch needs at most 2n - 1 requests.
    assert len(carrying) <= 2 * carrying[0]["event_count"] - 1
    with_poison = requests_carrying(sidecar, [poison])
    assert with_poison and all(r["http_status_returned"] == 400 for r in with_poison)
    stats = delivery.stats()
    assert fleet(stats, "quarantined") == quarantined_before + 1, "exactly one row"
    quarantined = delivery.quarantined_event_uuids()
    assert poison in quarantined, "the poison row itself must be the quarantined one"
    assert uuids[0] not in quarantined and uuids[2] not in quarantined
    assert halt_class(stats) is None, "one bad row must not halt delivery"
    assert search_sidecar(sidecar, poison) == []
