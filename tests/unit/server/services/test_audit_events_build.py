"""Event construction for the unified audit capture path.

Covers build_event / build_system_event / build_legacy_event: one uuid per
event assigned at construction, precondition enforcement, the trusted
system-actor flag, and ambient request attribution (source, ip, auth
method, correlation id, node id).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest

from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContext,
    bind_audit_request_context,
    reset_audit_request_context,
)
from code_indexer.server.services import audit_events
from code_indexer.server.services.audit_events import (
    AuditEvent,
    AuditEventInvalid,
    SystemComponent,
    build_event,
    build_legacy_event,
    build_system_event,
)


def _user_deleted(**overrides: Any) -> AuditEvent:
    kwargs: Dict[str, Any] = dict(
        actor="alice",
        action_type="user_deleted",
        target_type="user",
        target_id="bob",
        outcome="success",
        details={"deleted_role": "normal_user"},
    )
    kwargs.update(overrides)
    return build_event(**kwargs)


@pytest.mark.parametrize("name", ["first last", "o'neil", "-lead", "émile"])
def test_username_target_accepts_every_account_username(name: str) -> None:
    # Account creation accepts these names, so their rows must be writable.
    assert _user_deleted(target_id=name).target_id == name


@pytest.mark.parametrize("name", ["a/b", "a\\b", ".", "..", "   ", "a\nb", ""])
def test_username_target_rejects_path_hazards_and_blanks(name: str) -> None:
    with pytest.raises(AuditEventInvalid) as info:
        _user_deleted(target_id=name)
    assert info.value.field == "target_id"


@pytest.fixture(autouse=True)
def _no_process_node_id():
    audit_events.set_process_node_id(None)
    yield
    audit_events.set_process_node_id(None)


class TestBuildEvent:
    def test_each_event_gets_its_own_uuid4(self) -> None:
        first = _user_deleted()
        second = _user_deleted()
        assert first.event_uuid != second.event_uuid
        assert uuid.UUID(first.event_uuid).version == 4

    def test_fields_and_serialised_details(self) -> None:
        event = _user_deleted()
        assert event.actor == "alice"
        assert event.actor_is_system is False
        assert event.outcome == "success"
        assert event.details_json == '{"deleted_role": "normal_user"}'
        assert event.occurred_at.endswith("+00:00")

    def test_no_request_context_means_system_source_and_fresh_correlation(
        self,
    ) -> None:
        event = _user_deleted()
        assert event.source == "system"
        assert event.ip_address is None
        assert event.auth_method is None
        assert event.correlation_id.startswith("evt-")

    def test_request_context_supplies_source_ip_and_auth_method(self) -> None:
        token = bind_audit_request_context(
            AuditRequestContext(
                source="web", client_ip="192.0.2.10", auth_method="web_session"
            )
        )
        try:
            event = _user_deleted()
        finally:
            reset_audit_request_context(token)
        assert event.source == "web"
        assert event.ip_address == "192.0.2.10"
        assert event.auth_method == "web_session"

    def test_explicit_auth_method_overrides_context(self) -> None:
        event = _user_deleted(auth_method="none")
        assert event.auth_method == "none"

    def test_node_id_comes_from_process_wiring(self) -> None:
        audit_events.set_process_node_id("node-a")
        assert _user_deleted().node_id == "node-a"

    @pytest.mark.parametrize(
        "overrides, field",
        [
            ({"actor": ""}, "actor"),
            ({"actor": "   "}, "actor"),
            ({"outcome": "maybe"}, "outcome"),
            ({"action_type": "no_such_action"}, "action_type"),
            ({"target_type": "group"}, "target_type"),
            ({"target_id": "bad/id"}, "target_id"),
            ({"auth_method": "system"}, "auth_method"),
            ({"auth_method": "carrier_pigeon"}, "auth_method"),
        ],
    )
    def test_precondition_violations_raise(self, overrides, field) -> None:
        with pytest.raises(AuditEventInvalid) as info:
            _user_deleted(**overrides)
        assert info.value.field == field

    def test_legacy_only_action_type_is_rejected(self) -> None:
        with pytest.raises(AuditEventInvalid) as info:
            build_event(
                actor="alice",
                action_type="group_create",
                target_type="group",
                target_id="7",
                outcome="success",
            )
        assert info.value.field == "action_type"


class TestBuildSystemEvent:
    def test_sets_trusted_system_attribution(self) -> None:
        event = build_system_event(
            component=SystemComponent.GOLDEN_REPO_RECONCILER,
            action_type="golden_repo_removed",
            target_type="repo",
            target_id="example-repo",
            outcome="success",
            details={"job_id": "job-1"},
        )
        assert event.actor == "system:golden-repo-reconciler"
        assert event.actor_is_system is True
        assert event.source == "system"
        assert event.auth_method == "system"
        assert event.ip_address is None

    def test_human_named_like_a_system_actor_is_not_flagged(self) -> None:
        event = _user_deleted(actor="system:x")
        assert event.actor == "system:x"
        assert event.actor_is_system is False

    def test_component_must_be_a_system_component(self) -> None:
        with pytest.raises(AuditEventInvalid) as info:
            build_system_event(
                component="self-registration",  # type: ignore[arg-type]
                action_type="user_created",
                target_type="user",
                target_id="bob",
                outcome="success",
            )
        assert info.value.field == "component"


class TestBuildLegacyEvent:
    def test_legacy_event_keeps_payload_and_gets_uuid(self) -> None:
        event = build_legacy_event(
            actor="admin",
            action_type="test_action",
            target_type="user",
            target_id="any free text",
            details_json='{"k": 1}',
            occurred_at="2026-01-01T00:00:00",
        )
        assert event.occurred_at == "2026-01-01T00:00:00"
        assert event.details_json == '{"k": 1}'
        assert event.outcome is None
        assert event.actor_is_system is False
        assert uuid.UUID(event.event_uuid).version == 4
        assert event.correlation_id.startswith("evt-")

    def test_legacy_event_defaults_occurred_at_to_now_utc(self) -> None:
        event = build_legacy_event(
            actor="admin",
            action_type="group_create",
            target_type="group",
            target_id="7",
            details_json=None,
        )
        assert event.occurred_at.endswith("+00:00")
