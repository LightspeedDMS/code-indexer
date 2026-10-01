"""Harness preconditions (GREEN today): the Phase 7 setup itself is sound.

These prove that every fixture, driver and endpoint the delivery scenarios
use works NOW, so a RED scenario can only fail on the missing SIEM delivery
capability -- never on a typo, a missing fixture, or a broken front-door
driver.
"""

from __future__ import annotations

import httpx

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.fixtures.secops_sidecar.client import mint_token


def test_sidecar_is_up_empty_and_issues_tokens_for_its_key_file(
    sidecar: AttachedSidecar,
) -> None:
    health = sidecar.control.get("/_control/health").json()
    assert health == {"ingest_listening": True, "received_count": 0}
    key = sidecar.read_key_file()
    assert key["token_uri"] == sidecar.coords.harness_endpoint + "/token"
    assert sidecar.coords.harness_endpoint.startswith("http://127.0.0.1:")
    assert mint_token(sidecar.coords, key)


def test_server_runs_with_the_non_production_fault_injection_gate(
    siem_http: httpx.Client, siem_admin: AdminTokenProvider
) -> None:
    resp = siem_http.get(
        "/admin/fault-injection/status",
        headers={"Authorization": "Bearer " + siem_admin.get_token()},
    )
    assert resp.status_code == 200, "the harness gate must be active in Phase 7"
    assert resp.json()["enabled"] is True


def test_every_pilot_driver_yields_an_audit_event_uuid(
    siem_http: httpx.Client, siem_admin: AdminTokenProvider
) -> None:
    door = FrontDoor(siem_http, siem_admin)
    user, mfa_user = unique_name("siem-pre"), unique_name("siem-pre-mfa")
    events = [door.create_user(user), door.create_user(mfa_user)]
    events.append(door.rest_login_success(user))
    events.append(door.rest_login_failure(user))
    events.append(door.mcp_login_success(user, door.create_api_key(user)))
    events.append(door.mcp_login_failure(user))
    events.append(door.change_role(user, "power_user"))
    events.append(door.mcp_move_group(user, door.group_id("powerusers")))
    events.append(door.web_activate_mfa(mfa_user))
    assert [(e.action_type, e.door) for e in events] == [
        ("user_created", "rest"),
        ("user_created", "rest"),
        ("authentication_success", "rest"),
        ("authentication_failure", "rest"),
        ("authentication_success", "mcp"),
        ("authentication_failure", "mcp"),
        ("user_role_changed", "rest"),
        ("user_group_change", "mcp"),
        ("mfa_activated", "web"),
    ]
    assert len({e.event_uuid for e in events}) == len(events)
