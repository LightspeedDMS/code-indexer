"""Pilot events reach the sidecar as valid UDM (RED until SIEM delivery exists).

Each scenario arms capture, performs real actions through REST, MCP or the
Web UI, and finds every resulting audit event_uuid (= metadata.productLogId)
through the sidecar's search.  The sidecar's independent UDM rules have
already validated anything it stored.
"""

from __future__ import annotations

import json
from typing import Dict, FrozenSet, List

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, PilotEvent, unique_name
from tests.e2e.siem_delivery.siem_api import SiemDelivery, wait_delivered

# The design's PROPOSED UDM mapping; MFA types map to one of two candidates.
EXPECTED_EVENT_TYPES: Dict[str, FrozenSet[str]] = {
    "authentication_success": frozenset({"USER_LOGIN"}),
    "authentication_failure": frozenset({"USER_LOGIN"}),
    "mfa_activated": frozenset({"USER_CHANGE_PERMISSIONS", "USER_UNCATEGORIZED"}),
    "user_role_changed": frozenset({"USER_CHANGE_PERMISSIONS"}),
    "user_group_change": frozenset({"GROUP_MODIFICATION"}),
    "user_created": frozenset({"USER_CREATION"}),
}


def _snake_keys(value: object, path: str = "") -> List[str]:
    """Keys containing '_' outside the free-form additional Struct."""
    found: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "additional":
                continue
            if "_" in key:
                found.append(f"{path}.{key}")
            found.extend(_snake_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for item in value:
            found.extend(_snake_keys(item, path))
    return found


def _assert_delivered_as_udm(
    sidecar: AttachedSidecar, events: List[PilotEvent]
) -> None:
    for event in events:
        results = wait_delivered(sidecar, event.event_uuid)
        assert len(results) == 1, f"{event} delivered {len(results)} times"
        udm = results[0]["udm"]
        metadata = udm["metadata"]
        assert metadata["productEventType"] == event.action_type
        assert metadata["eventType"] in EXPECTED_EVENT_TYPES[event.action_type]
        assert (metadata["vendorName"], metadata["productName"]) == (
            "CIDX",
            "cidx-server",
        )
        assert _snake_keys(udm) == [], f"non-camelCase UDM keys: {_snake_keys(udm)}"
        body = sidecar.control.get(
            f"/_control/requests/{results[0]['seq']}/body"
        ).content
        assert list(json.loads(body)) == ["inlineSource"]


def test_rest_login_success_and_failure_are_delivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-rest")
    door.create_user(user)
    events = [door.rest_login_success(user), door.rest_login_failure(user)]
    _assert_delivered_as_udm(sidecar, events)


def test_mcp_login_success_and_failure_are_delivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-mcp")
    door.create_user(user)
    api_key = door.create_api_key(user)
    events = [door.mcp_login_success(user, api_key), door.mcp_login_failure(user)]
    _assert_delivered_as_udm(sidecar, events)


def test_mfa_change_is_delivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-mfa")
    door.create_user(user)
    _assert_delivered_as_udm(sidecar, [door.web_activate_mfa(user)])


def test_group_and_permission_changes_are_delivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    user = unique_name("siem-perm")
    door.create_user(user)
    events = [
        door.change_role(user, "power_user"),
        door.mcp_move_group(user, door.group_id("powerusers")),
    ]
    _assert_delivered_as_udm(sidecar, events)


def test_admin_action_is_delivered(
    delivery: SiemDelivery, door: FrontDoor, sidecar: AttachedSidecar
) -> None:
    delivery.arm()
    _assert_delivered_as_udm(sidecar, [door.create_user(unique_name("siem-admin"))])
