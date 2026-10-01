"""Typed projection, UDM mapping (permissive fallback) and local validation."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Dict

import pytest

from code_indexer.server.services.audit_events import (
    AuditEvent,
    build_event,
    build_legacy_event,
)
from code_indexer.server.services.siem_delivery import udm as udm_mod
from code_indexer.server.services.siem_delivery.projection import project
from code_indexer.server.services.siem_delivery.scope import (
    PILOT_ACTION_TYPES,
    SIEM_SELF_REPORT_TYPES,
)
from code_indexer.server.services.siem_delivery.udm import (
    FALLBACK_EVENT_TYPE,
    HARD_CEILING,
    UDM_MAPPING,
    UdmRow,
    build_udm,
    canonical_json,
    envelope,
    validate_udm,
)
from tests.fixtures.secops_sidecar.udm_rules import validate_events

MARKER = "S3CR3T-MARKER-7f1e"


def _row(event: AuditEvent, mapping: Dict[str, str] = UDM_MAPPING) -> UdmRow:
    payload, error = project(event, mapping=mapping)
    assert error is None, error
    assert payload is not None
    return UdmRow(
        event_uuid=event.event_uuid,
        action_type=event.action_type,
        payload=json.loads(payload),
        source_instance_label="example-label",
    )


def _login(outcome: str, actor: str = "alice") -> AuditEvent:
    if outcome == "success":
        return build_event(
            actor=actor,
            action_type="authentication_success",
            target_type="auth",
            target_id=actor,
            outcome="success",
            details={"method": "password", "mfa": "not_enrolled", "flow": "rest_token"},
        )
    return build_event(
        actor=actor,
        action_type="authentication_failure",
        target_type="auth",
        target_id=actor,
        outcome="failure",
        details={
            "method": "password",
            "stage": "credentials",
            "reason": "bad_credentials",
        },
    )


def test_projection_keeps_only_typed_fields() -> None:
    event = build_event(
        actor="alice",
        action_type="user_role_changed",
        target_type="user",
        target_id="bob",
        outcome="success",
        details={"old_role": "normal_user", "new_role": "admin"},
    )
    payload, error = project(event)
    assert error is None and payload is not None
    data = json.loads(payload)
    assert data["schema_version"] == 1
    assert data["target_id"] == "bob"
    assert data["details"] == {"old_role": "normal_user", "new_role": "admin"}
    assert data["occurred_at"].endswith("Z")


def test_projection_type_violation_returns_the_field_name_never_the_value() -> None:
    event = dataclasses.replace(
        _login("success"), details_json=json.dumps({"method": MARKER})
    )
    payload, error = project(event)
    assert payload is None
    assert error == "details.method"


def test_legacy_free_text_is_never_projected() -> None:
    event = build_legacy_event(
        actor="alice",
        action_type="user_group_change",
        target_type="user",
        target_id="bob",
        details_json=json.dumps({"user_id": "bob", "message": MARKER}),
    )
    raw = dataclasses.replace(
        event, details_json=json.dumps({"user_id": "bob", "message": MARKER})
    )
    payload, error = project(raw)
    assert error is None and payload is not None
    assert MARKER not in payload
    assert json.loads(payload)["details"] == {"user_id": "bob"}


def test_unmapped_type_uses_the_fallback_projection() -> None:
    mapping = {k: v for k, v in UDM_MAPPING.items() if k != "user_role_changed"}
    event = build_event(
        actor="alice",
        action_type="user_role_changed",
        target_type="user",
        target_id="bob",
        outcome="success",
        details={"old_role": "normal_user", "new_role": "admin"},
    )
    payload, _ = project(event, mapping=mapping)
    assert payload is not None
    data = json.loads(payload)
    assert "details" not in data and "target_id" not in data
    assert data["actor"] == "alice"


def test_login_success_and_failure_map_to_user_login() -> None:
    ok = build_udm(_row(_login("success")))
    bad = build_udm(_row(_login("failure")))
    for udm in (ok, bad):
        assert udm["metadata"]["eventType"] == "USER_LOGIN"
        assert udm["metadata"]["vendorName"] == "CIDX"
        assert udm["metadata"]["productName"] == "cidx-server"
        assert udm["target"]["user"]["userid"] == "alice"
        assert validate_udm(udm) is None
    assert ok["securityResult"][0]["action"] == ["ALLOW"]
    assert bad["securityResult"][0]["action"] == ["BLOCK"]
    assert ok["metadata"]["productLogId"]
    assert ok["additional"]["cidx_instance"] == "example-label"


def test_unknown_actor_principal_never_empty() -> None:
    udm = build_udm(_row(_login("failure", actor="(unknown)")))
    assert "user" not in udm["principal"]
    assert udm["principal"]["application"] == "cidx-server"
    assert udm["additional"]["principal_unknown"] is True
    assert "target" not in udm or "user" not in udm.get("target", {})
    assert validate_udm(udm) is None


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda u: u["metadata"].update(eventType="NOT_A_TYPE"), "event_type"),
        (lambda u: u.update(principal={}), "principal_missing_detail"),
        (
            lambda u: u["principal"].update(
                user={"userid": "a", "emailAddresses": ["x"]}
            ),
            "unknown_field",
        ),
        (lambda u: u["metadata"].update(eventTimestamp="yesterday"), "timestamp"),
        (lambda u: u.update(snake_case={}), "unknown_field"),
    ],
)
def test_validate_udm_rejects(mutate: Any, reason: str) -> None:
    udm = build_udm(_row(_login("success")))
    mutate(udm)
    result = validate_udm(udm)
    assert result is not None and result[0] == reason


def test_oversize_udm_is_rejected() -> None:
    udm = build_udm(_row(_login("success")))
    udm["additional"]["pad"] = "x" * (HARD_CEILING + 1)
    result = validate_udm(udm)
    assert result is not None and result[0] == "oversize"


def test_envelope_is_canonical_json() -> None:
    members = [build_udm(_row(_login("success"))), build_udm(_row(_login("failure")))]
    body = envelope([canonical_json(m) for m in members])
    expected = json.dumps(
        {"inlineSource": {"events": [{"udm": m} for m in members]}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert body == expected


def test_every_in_scope_type_without_a_mapping_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fallback for every unmapped in-scope type, plus one removed pilot
    type so the path is always exercised."""
    mapping = {k: v for k, v in UDM_MAPPING.items() if k != "mfa_activated"}
    unmapped = sorted(
        t for t in PILOT_ACTION_TYPES | SIEM_SELF_REPORT_TYPES if t not in mapping
    )
    assert "mfa_activated" in unmapped
    event = build_event(
        actor="alice",
        action_type="mfa_activated",
        target_type="user",
        target_id="alice",
        outcome="success",
        details={"recovery_codes_issued": True},
    )
    udm_mod.reset_unmapped_reporter_for_tests()
    before = udm_mod.unmapped_count("mfa_activated")
    with caplog.at_level(logging.WARNING):
        first = build_udm(_row(event, mapping), mapping=mapping)
        build_udm(_row(event, mapping), mapping=mapping)
    assert first["metadata"]["eventType"] == FALLBACK_EVENT_TYPE
    assert first["metadata"]["productEventType"] == "mfa_activated"
    assert validate_udm(first) is None
    assert udm_mod.unmapped_count("mfa_activated") == before + 2
    warnings = [r for r in caplog.records if "mfa_activated" in r.getMessage()]
    assert len(warnings) == 1


def test_sidecar_independent_rules_accept_every_mapped_type() -> None:
    from code_indexer.server.services.siem_delivery.canary import synthetic_events

    events = synthetic_events(run_id="run-1")
    udms = [build_udm(_row(e), canary=True, count_unmapped=False) for e in events]
    mapped = {u["metadata"]["productEventType"] for u in udms}
    assert set(UDM_MAPPING) <= mapped
    assert any(u["metadata"]["eventType"] == FALLBACK_EVENT_TYPE for u in udms)
    assert all(u["additional"]["cidx_canary"] is True for u in udms)
    assert validate_events([{"udm": u} for u in udms]) == []
    assert all(validate_udm(u) is None for u in udms)
