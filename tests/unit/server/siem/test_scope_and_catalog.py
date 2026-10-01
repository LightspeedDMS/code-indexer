"""SIEM delivery scope: pilot types, self-report types and the catalog."""

from __future__ import annotations

import dataclasses

import pytest

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AUDIT_TARGET_ID_TYPE,
    Delivery,
    SystemComponent,
    build_event,
    build_system_event,
    conforms,
)
from code_indexer.server.services.siem_delivery.scope import (
    PILOT_ACTION_TYPES,
    SIEM_CONFIG_KEY_PREFIX,
    SIEM_SELF_REPORT_TYPES,
    is_self_report,
    is_siem_config_change,
)


def test_every_pilot_self_report_and_config_type_is_durable() -> None:
    for action_type in PILOT_ACTION_TYPES | SIEM_SELF_REPORT_TYPES | {"config_changed"}:
        assert action_type in AUDIT_ACTION_CATALOG, action_type
        assert AUDIT_ACTION_CATALOG[action_type].delivery is Delivery.DURABLE


def test_pilot_scope_is_exactly_the_recorded_pilot() -> None:
    assert len(PILOT_ACTION_TYPES) == 23
    assert "access_denied" not in PILOT_ACTION_TYPES
    assert "api_key_deleted" not in PILOT_ACTION_TYPES
    assert "token_refresh_success" not in PILOT_ACTION_TYPES


def test_self_report_types_target_the_siem_config_section() -> None:
    for action_type in SIEM_SELF_REPORT_TYPES:
        spec = AUDIT_ACTION_CATALOG[action_type]
        assert spec.target_type == "config"
        assert spec.details_schema is not None
        assert conforms(AUDIT_TARGET_ID_TYPE["config"], "siem_delivery")


def test_system_component_for_siem_delivery() -> None:
    assert SystemComponent.SIEM_DELIVERY.value == "siem-delivery"
    event = build_system_event(
        component=SystemComponent.SIEM_DELIVERY,
        action_type="siem_quarantine_requeued",
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details={"count": 3, "mapping_version": 2, "trigger": "admin"},
    )
    assert event.actor == "system:siem-delivery"


def _config_row(target_id: str, **details: object):  # type: ignore[no-untyped-def]
    return build_event(
        actor="alice",
        action_type="config_changed",
        target_type="config",
        target_id=target_id,
        outcome="success",
        details=details or None,
    )


def test_siem_section_row_is_a_siem_config_change() -> None:
    assert is_siem_config_change(_config_row("siem_delivery"))
    assert is_self_report(_config_row("siem_delivery"))


def test_multi_section_row_with_siem_key_is_a_siem_config_change() -> None:
    row = _config_row(
        "*",
        change_kind="update",
        changed_keys=[SIEM_CONFIG_KEY_PREFIX + "enabled", "workers"],
    )
    assert is_siem_config_change(row)
    failed = _config_row(
        "*", change_kind="update", attempted_keys=[SIEM_CONFIG_KEY_PREFIX + "region"]
    )
    assert is_siem_config_change(failed)


def test_other_config_rows_are_not_siem_changes() -> None:
    assert not is_siem_config_change(_config_row("server"))
    row = _config_row("*", change_kind="reset_to_defaults", changed_keys=["workers"])
    assert not is_siem_config_change(row)
    assert not is_self_report(row)


def test_key_prefix_matches_the_config_dataclass_field() -> None:
    from code_indexer.server.utils.config_manager import ServerConfig

    names = {f.name for f in dataclasses.fields(ServerConfig)}
    assert SIEM_CONFIG_KEY_PREFIX.rstrip(".") in names


@pytest.mark.parametrize(
    "action_type,details",
    [
        (
            "siem_canary_sent",
            {
                "canary_run_id": "run-1",
                "event_count": 3,
                "result": "accepted",
                "mapping_version": 1,
            },
        ),
        (
            "siem_canary_visibility_confirmed",
            {
                "canary_run_id": "run-1",
                "expected_count": 3,
                "confirmed_count": 2,
                "missing_action_types": ["user_role_changed"],
            },
        ),
        (
            "siem_delivery_resumed",
            {"halted_class": "credential", "signature_reset": True},
        ),
        ("siem_batch_acknowledged", {"batch_id": "b-1", "event_count": 1}),
        ("siem_batch_rebatched", {"batch_id": "b-1", "event_count": 1}),
        (
            "siem_destination_retargeted",
            {"from_destination_key": "gsecops:abc", "count": 5},
        ),
        (
            "siem_destination_abandoned",
            {"destination_key": "gsecops:abc", "count": 5},
        ),
    ],
)
def test_self_report_details_are_typed(action_type: str, details: dict) -> None:
    event = build_event(
        actor="alice",
        action_type=action_type,
        target_type="config",
        target_id="siem_delivery",
        outcome="success",
        details=details,
    )
    assert event.details_json is not None
