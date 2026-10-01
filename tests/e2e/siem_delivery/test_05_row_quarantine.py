"""One poison row is quarantined without blocking the others.

The three logins must be pending TOGETHER when delivery runs, so the first
request carrying them is one batch.  A sidecar outage cannot guarantee that
(the delivery loop claims a batch as soon as the first login is captured).
The design guarantees it another way: NO batch is formed while delivery is
halted, and a ``duplicate_response`` halt is never probed automatically.  So
a sacrificial row is answered 409 (a duplicate-response halt), the three
logins are captured while the halt holds, the poison rule is armed, and the
admin acknowledge front door releases delivery.  The sidecar then rejects
(unindexed, no event index) every request containing the middle row:
delivery must isolate exactly that row by bisection under the quarantine
cap, quarantine it, deliver the other two, and NOT halt.  Every assertion is
scoped to this scenario's own uuids and requests.
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
    # Hold delivery: the next batch (a sacrificial login) is answered 409.
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 409}
    )
    door.rest_login_failure(user)
    delivery.wait_stats(
        lambda s: halt_class(s) == "duplicate_response",
        DELIVERY_TIMEOUT,
        "the holding duplicate-response halt",
    )
    quarantined_before = fleet(delivery.stats(), "quarantined")
    uuids = [door.rest_login_success(user).event_uuid for _ in range(ROWS)]
    poison = uuids[1]
    sidecar_fault(
        sidecar,
        "/_control/faults",
        {"mode": "reject_if_contains", "product_log_id": poison},
    )
    assert requests_carrying(sidecar, uuids) == [], "a batch formed while halted"
    delivery.acknowledge_halted_batch()  # release delivery (admin front door)
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
