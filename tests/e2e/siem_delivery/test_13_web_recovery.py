"""Halts and recovery through the Web UI routes (the SIEM section's panel).

The fault setups are those of test_05 (quarantine), test_07 (duplicate
response) and test_09 (a destination left behind), driven to their
resolution through the Web front door instead of REST: acknowledge,
re-batch, requeue, resume, and abandon confirmed by typing ``ABANDON``.
Every assertion is scoped to this scenario's own uuids, batches and keys.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Iterator, Optional

import pytest

from tests.e2e.siem_delivery.conftest import AttachedSidecar, SiemE2EConfig
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    SiemDelivery,
    arm_fault_after_drain,
    fleet,
    halt_class,
    requests_carrying,
    search_sidecar,
    sidecar_fault,
    wait_delivered,
)
from tests.e2e.siem_delivery.web_ops import (
    WebSession,
    configured_key,
    halted_batch_id,
    localhost_endpoint,
    original_state,
    quarantined_uuids,
    restore_destination,
    strand_one_login,
    stranded_keys,
    switch_destination,
    text_of,
)

NO_RESEND_OBSERVE_SECONDS = float(os.environ.get("E2E_SIEM_NO_RESEND_OBSERVE", "20"))


@pytest.fixture()
def web(siem_config: SiemE2EConfig) -> Iterator[WebSession]:
    with WebSession(
        siem_config.server_url, siem_config.admin_user, siem_config.admin_pass
    ) as session:
        yield session


def _hold(delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar) -> str:
    """One login answered 409: a duplicate-response halt holds its batch."""
    user = unique_name("siem-web-hold")
    door.create_user(user)
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 409}
    )
    held = door.rest_login_success(user).event_uuid
    delivery.wait_stats(
        lambda s: halt_class(s) == "duplicate_response", DELIVERY_TIMEOUT, "a halt"
    )
    return held


def _web_audit_row(door: FrontDoor, action_type: str, after_id: int) -> Dict[str, Any]:
    rows = [
        r
        for r in door._audit_rows(action_type)
        if r["id"] > after_id and r.get("source") == "web"
    ]
    assert rows, f"no Web {action_type} audit row after id {after_id}"
    return rows[0]


def _unhalted(delivery: SiemDelivery) -> None:
    delivery.wait_stats(lambda s: halt_class(s) is None, DELIVERY_TIMEOUT, "unhalted")


def test_acknowledge_through_the_web(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar, web: WebSession
) -> None:
    delivery.arm()
    held = _hold(delivery, door, sidecar)
    batch = halted_batch_id(web.recovery())
    assert batch and batch == delivery.stats()["halt"]["batch_id"]
    before = door.max_audit_id()
    ack = web.act(f"batches/{batch}/acknowledge")
    assert ack.status_code == 200, ack.text[:300]
    assert f"Acknowledged batch {batch}" in text_of(ack.text)
    _unhalted(delivery)
    time.sleep(NO_RESEND_OBSERVE_SECONDS)  # bounded observation: no resend
    assert len(requests_carrying(sidecar, [held])) == 1, "an acknowledged batch resent"
    assert search_sidecar(sidecar, held) == []
    _web_audit_row(door, "siem_batch_acknowledged", before)


def test_rebatch_through_the_web(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar, web: WebSession
) -> None:
    delivery.arm()
    held = _hold(delivery, door, sidecar)
    batch = halted_batch_id(web.recovery())
    rebatched = web.act(f"batches/{batch}/rebatch")
    assert rebatched.status_code == 200, rebatched.text[:300]
    assert "Re-batched" in text_of(rebatched.text)
    _unhalted(delivery)
    assert len(wait_delivered(sidecar, held)) == 1  # re-sent in a new batch


def test_requeue_and_resume_through_the_web(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar, web: WebSession
) -> None:
    delivery.arm()
    poison_user = unique_name("siem-web-q")
    door.create_user(poison_user)
    _hold(delivery, door, sidecar)
    quarantined_before = fleet(delivery.stats(), "quarantined")
    poison = door.rest_login_success(poison_user).event_uuid
    sidecar_fault(
        sidecar,
        "/_control/faults",
        {"mode": "reject_if_contains", "product_log_id": poison},
    )
    batch = halted_batch_id(web.recovery())
    assert web.act(f"batches/{batch}/acknowledge").status_code == 200
    delivery.wait_stats(
        lambda s: fleet(s, "quarantined") > quarantined_before,
        DELIVERY_TIMEOUT,
        "the row to be quarantined",
    )
    assert poison in quarantined_uuids(web.recovery())
    sidecar.control.reset(keep_tokens=True)  # the receiver is fixed
    requeued = web.act("quarantine/requeue", {"event_uuid": [poison]})
    assert "Requeued 1 events" in text_of(requeued.text), requeued.text[:300]
    assert len(wait_delivered(sidecar, poison)) == 1

    idle = web.act("resume")
    assert idle.status_code == 200 and "resumed: false" in text_of(idle.text)
    held = _hold(delivery, door, sidecar)
    resumed = web.act("resume")
    assert "resumed: true" in text_of(resumed.text), resumed.text[:300]
    _unhalted(delivery)
    assert len(wait_delivered(sidecar, held)) == 1


def test_abandon_a_stranded_destination_through_the_web(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar, web: WebSession
) -> None:
    delivery.arm()
    original = original_state(delivery)  # recorded BEFORE any mutation
    previous = configured_key(delivery)
    new_key: Optional[str] = None
    try:
        strand_one_login(delivery, door, sidecar)
        new_key = switch_destination(delivery, localhost_endpoint(sidecar.coords))
        delivery.acknowledge_halted_batch()
        dialog = text_of(web.dialog(previous))
        assert "IRREVERSIBLE" in dialog
        # the stranded login, plus the switch's own Web login (captured for
        # the destination configured at that moment)
        found = re.search(r"pending (\d+), batched 0, quarantined 0", dialog)
        assert found and int(found.group(1)) >= 1, dialog
        pending = int(found.group(1))
        assert "currently queued; may grow until the action runs" in dialog.lower()
        path = f"destinations/{previous}/abandon"
        refused = web.act(path, {"confirm_word": "abandon"})
        assert refused.status_code == 400
        assert "type ABANDON to confirm" in text_of(refused.text)
        assert previous in stranded_keys(web.recovery())
        before = door.max_audit_id()
        done = web.act(path, {"confirm_word": "ABANDON"})
        assert done.status_code == 200, done.text[:300]
        assert f"Abandoned {pending} events" in text_of(done.text)
        assert previous not in stranded_keys(web.recovery())
        _web_audit_row(door, "siem_destination_abandoned", before)
        own = web.act(f"destinations/{new_key}/abandon", {"confirm_word": "ABANDON"})
        assert own.status_code == 409
        assert "rows already target the configured destination" in text_of(own.text)
    finally:
        restore_destination(delivery, original, new_key)
