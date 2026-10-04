"""Arming through the Web UI only (the SIEM section's operator panel).

A NEW destination is configured so the gate is fresh: the same sidecar
through ``localhost`` (the destination key hashes the endpoint text; the
sidecar serves exactly one import parent, so a changed instance id could
never be armed against it).  Before the switch, delivery is held by a
duplicate-response halt while one login is captured for the PREVIOUS
destination, so that row is stranded there once the switch is saved; the
scenario finally retargets it through the Web route and sees it delivered.
"""

from __future__ import annotations

import os
import time
from typing import Iterator

import pytest

from code_indexer.server.services.siem_delivery.canary import synthetic_events
from tests.e2e.siem_delivery.conftest import AttachedSidecar, SiemE2EConfig
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    ARMING_TIMEOUT,
    POLL_SECONDS,
    SiemDelivery,
    capture_state,
    poll,
    requests_carrying,
    search_sidecar,
    wait_delivered,
)
from tests.e2e.siem_delivery.web_ops import (
    DestinationRestore,
    WebSession,
    canary_ids,
    canary_run_id,
    configured_key,
    is_armed,
    localhost_endpoint,
    strand_one_login,
    stranded_keys,
    switch_destination,
    text_of,
)

UNARMED_OBSERVE_SECONDS = float(os.environ.get("E2E_SIEM_UNARMED_OBSERVE", "30"))


@pytest.fixture()
def web(siem_config: SiemE2EConfig) -> Iterator[WebSession]:
    with WebSession(
        siem_config.server_url, siem_config.admin_user, siem_config.admin_pass
    ) as session:
        yield session


def test_arm_through_the_web_ui_only(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    web: WebSession,
    destination_restore: DestinationRestore,  # inherited state, recorded first
) -> None:
    delivery.arm()
    previous = configured_key(delivery)
    destination_restore.created.append(previous)
    stranded = strand_one_login(delivery, door, sidecar)
    new_key = switch_destination(delivery, localhost_endpoint(sidecar.coords))
    destination_restore.created.append(new_key)
    delivery.acknowledge_halted_batch()  # release: the held batch only
    assert not is_armed(web.arming())
    ran = web.act("canary")
    assert ran.status_code == 200, ran.text[:300]
    assert "Canary run" in text_of(ran.text)
    page = web.arming()
    ids, run_id = canary_ids(page), canary_run_id(page)
    assert len(ids) == len(synthetic_events("count")) == len(set(ids))
    assert len(requests_carrying(sidecar, ids)) == 1, "one request carries them"
    assert all(len(search_sidecar(sidecar, i)) == 1 for i in ids)

    half = len(ids) // 2
    partial = web.act(
        "canary/confirm-visible",
        {
            "canary_run_id": run_id,
            "visible_id": ids[:half],
            "visible_ids_text": "\n".join(ids[half:-1]),
        },
    )
    assert partial.status_code == 200, partial.text[:300]
    assert f"Confirmed {len(ids) - 1} of {len(ids)}" in text_of(partial.text)
    deadline = time.monotonic() + UNARMED_OBSERVE_SECONDS
    while time.monotonic() < deadline:
        assert capture_state(delivery.stats()) != "armed", "armed on a partial"
        time.sleep(POLL_SECONDS)

    full = web.act(
        "canary/confirm-visible",
        {
            "canary_run_id": run_id,
            "visible_id": ids[:half],
            "visible_ids_text": ", ".join(ids[half:]),
        },
    )
    assert f"Confirmed {len(ids)} of {len(ids)}" in text_of(full.text)
    poll(
        lambda: True if is_armed(web.arming()) else None,
        ARMING_TIMEOUT,
        "the ARMED row",
    )
    user = unique_name("siem-web-armed")
    door.create_user(user)
    assert len(wait_delivered(sidecar, door.rest_login_success(user).event_uuid))

    assert previous in stranded_keys(web.recovery())
    assert "Retarget" in web.dialog(previous)
    moved = web.act(f"destinations/{previous}/retarget")
    assert moved.status_code == 200, moved.text[:300]
    assert "Retargeted" in text_of(moved.text)
    assert len(wait_delivered(sidecar, stranded)) == 1
    assert previous not in stranded_keys(web.recovery())


def test_stale_run_is_refused(
    delivery: SiemDelivery,
    web: WebSession,
    destination_restore: DestinationRestore,  # re-arms only if it was armed
) -> None:
    delivery.arm()
    destination_restore.created.append(configured_key(delivery))
    assert web.act("canary").status_code == 200
    first = canary_run_id(web.arming())
    second, _ = delivery.run_canary()  # another admin, through REST
    stale = web.act(
        "canary/confirm-visible",
        {"canary_run_id": first, "visible_ids_text": ""},
    )
    assert stale.status_code == 409
    assert "canary run is stale" in text_of(stale.text)
    assert canary_run_id(web.arming()) == second
