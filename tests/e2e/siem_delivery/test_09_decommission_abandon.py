"""Decommissioning SIEM delivery through the front door, the last scenario
of the phase (it leaves delivery disabled and unconfigured).

Delivery is armed, a row is quarantined, then delivery is disabled and its
configuration cleared.  The clearing save captures its own row for the
removed destination, so health is DEGRADED ("events pending for
unconfigured destination").  Abandoning the removed destination's key --
with NOTHING configured -- must resolve its pending AND quarantined rows and
clear every SIEM health reason.  Finally the stored key is removed.
"""

from __future__ import annotations

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DEGRADED_TIMEOUT,
    DELIVERY_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    halt_class,
    poll,
    sidecar_fault,
)

CLEARED = {
    "enabled": "false",
    "region": "",
    "harness_endpoint": "",
    "project_id": "",
    "location": "",
    "instance_id": "",
    "source_instance_label": "",
}


def _quarantine_one_row(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    """Hold delivery with a duplicate-response halt, capture one login, make
    the receiver reject it, release: that row alone is quarantined."""
    user = unique_name("siem-decom")
    door.create_user(user)
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 409}
    )
    door.rest_login_failure(user)
    delivery.wait_stats(
        lambda s: halt_class(s) == "duplicate_response",
        DELIVERY_TIMEOUT,
        "the holding duplicate-response halt",
    )
    before = fleet(delivery.stats(), "quarantined")
    poison = door.rest_login_success(user).event_uuid
    sidecar_fault(
        sidecar,
        "/_control/faults",
        {"mode": "reject_if_contains", "product_log_id": poison},
    )
    delivery.acknowledge_halted_batch()
    delivery.wait_stats(
        lambda s: fleet(s, "quarantined") > before,
        DELIVERY_TIMEOUT,
        "the row to be quarantined",
    )
    assert poison in delivery.quarantined_event_uuids()


def test_abandon_after_disable_and_clear_resolves_rows_and_health(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    old_key = delivery.stats()["capture"]["configured_destination_key"]
    assert old_key
    _quarantine_one_row(delivery, door, sidecar)

    delivery.save_config(CLEARED)  # disable AND clear
    delivery.wait_stats(
        lambda s: s["capture"]["configured_destination_key"] is None,
        DELIVERY_TIMEOUT,
        "the cleared configuration to be applied",
    )
    poll(
        lambda: [
            r
            for r in delivery.siem_health_reasons()
            if f"unconfigured destination {old_key}" in r
        ]
        or None,
        DEGRADED_TIMEOUT,
        "health to report the removed destination's pending rows",
    )

    resp = delivery.abandon(old_key)  # nothing is configured now
    assert resp.status_code == 200, resp.text
    assert resp.json()["abandoned"] >= 2  # the quarantined row + the clear's row
    delivery.wait_stats(
        lambda s: fleet(s, "quarantined") == 0 and fleet(s, "pending") == 0,
        DELIVERY_TIMEOUT,
        "no undelivered or quarantined rows left",
    )
    poll(
        lambda: True if delivery.siem_health_reasons() == [] else None,
        DEGRADED_TIMEOUT,
        "every SIEM health reason to clear",
    )

    # Retarget needs a configured target: a clear refusal, not a mystery.
    refused = delivery.http.post(
        f"/api/admin/siem-delivery/destinations/{old_key}/retarget",
        headers={"Authorization": "Bearer " + delivery.admin.get_token()},
        json={},
    )
    assert refused.status_code == 409
    assert "configure the new destination first" in refused.text

    removed = delivery.remove_credential()
    assert removed.status_code == 200
    assert "credential removed" in removed.text
    assert delivery.stats()["credential"] is None
    again = delivery.remove_credential()
    assert again.status_code == 404
