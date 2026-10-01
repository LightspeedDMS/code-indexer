"""Arming gate (RED until SIEM delivery exists): canary accepted AND visible, or no capture.

Runs first in the phase, on a fresh server that has never armed capture.
"""

from __future__ import annotations

import os
import time

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    ARMING_TIMEOUT,
    SiemDelivery,
    capture_state,
    search_sidecar,
    wait_delivered,
)

# Longer than ARMING_SETTLE (180 s): an unarmed gate must STAY unarmed.
UNARMED_OBSERVE_SECONDS = float(os.environ.get("E2E_SIEM_UNARMED_OBSERVE", "200"))
OBSERVE_POLL_SECONDS = 5.0


def test_hidden_canary_event_keeps_capture_unarmed(
    delivery: SiemDelivery, sidecar: AttachedSidecar
) -> None:
    hide = sidecar.control.post(
        "/_control/visibility", {"hide_event_type": "USER_CHANGE_PERMISSIONS"}
    )
    assert hide.status_code == 200
    delivery.configure_harness_destination()
    run_id, expected = delivery.run_canary()
    visible = [i for i in expected if search_sidecar(sidecar, i)]
    assert 0 < len(visible) < len(expected), (
        "the hide rule must hide some canary events"
    )
    delivery.confirm_visible(run_id, visible)
    deadline = time.monotonic() + UNARMED_OBSERVE_SECONDS
    while time.monotonic() < deadline:
        stats = delivery.stats()
        assert capture_state(stats) != "armed", (
            "capture armed with an invisible canary event"
        )
        time.sleep(OBSERVE_POLL_SECONDS)
    status = delivery.stats()["capture"]["status"]
    assert f"{len(visible)} of {len(expected)} visible" in status


def test_full_canary_confirmation_arms_capture(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.configure_harness_destination()
    run_id, expected = delivery.run_canary()
    requests = sidecar.control.get("/_control/requests").json()["requests"]
    assert len(requests) == 1 and requests[0]["event_count"] == len(expected)
    for product_log_id in expected:
        found = search_sidecar(sidecar, product_log_id)
        assert len(found) == 1
        assert found[0]["udm"]["additional"]["cidx_canary"] is True
    delivery.confirm_visible(run_id, expected)
    delivery.wait_stats(
        lambda s: capture_state(s) == "armed", ARMING_TIMEOUT, "capture armed"
    )
    user = unique_name("siem-arm")
    door.create_user(user)
    login = door.rest_login_success(user)
    assert len(wait_delivered(sidecar, login.event_uuid)) == 1
