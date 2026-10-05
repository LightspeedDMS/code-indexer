"""SIEM delivery of audit records written during MCP impersonation.

Audit records written during MCP impersonation name the authenticated
administrator as the actor and the impersonated user as the subject.  The
SIEM payload and its UDM mapping carry both: the administrator as the
principal and the impersonated user as ``additional.impersonated_user`` (the
UDM target stays the action's own target).  The column is never dropped on
the way out, including when a row is reprojected from ``audit_logs``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Optional

from code_indexer.server.middleware.audit_request_context import (
    bind_audit_request_context,
    build_request_context,
    note_mcp_principal,
    reset_audit_request_context,
)
from code_indexer.server.services.audit_events import AuditEvent, build_event
from code_indexer.server.services.siem_delivery.projection import project
from code_indexer.server.services.siem_delivery.udm import (
    UdmRow,
    build_udm,
    validate_udm,
)
from tests.fixtures.secops_sidecar.udm_rules import validate_events

_ADMIN = "example-admin"
_SUBJECT = "example-subject"


def _role_change(impersonating: Optional[str]) -> AuditEvent:
    """A mapped action built as the MCP dispatcher would attribute it."""
    token = bind_audit_request_context(build_request_context("/mcp", "192.0.2.10"))
    try:
        note_mcp_principal(_ADMIN, impersonating)
        return build_event(
            actor=impersonating or _ADMIN,
            action_type="user_role_changed",
            target_type="user",
            target_id="example-target",
            outcome="success",
            details={"old_role": "normal_user", "new_role": "power_user"},
        )
    finally:
        reset_audit_request_context(token)


def _payload(event: AuditEvent) -> dict:
    payload, error = project(event)
    assert error is None, error
    assert payload is not None
    decoded: dict = json.loads(payload)
    return decoded


def test_event_names_admin_as_actor_and_impersonated_user_as_subject() -> None:
    event = _role_change(_SUBJECT)

    assert (event.actor, event.impersonated_user) == (_ADMIN, _SUBJECT)


def test_projection_carries_the_impersonated_user() -> None:
    payload = _payload(_role_change(_SUBJECT))

    assert payload["actor"] == _ADMIN
    assert payload["impersonated_user"] == _SUBJECT


def test_projection_omits_the_field_outside_impersonation() -> None:
    payload = _payload(_role_change(None))

    assert payload["actor"] == _ADMIN
    assert "impersonated_user" not in payload


def test_projection_rejects_a_nonconforming_subject_by_field_name() -> None:
    event = dataclasses.replace(_role_change(_SUBJECT), impersonated_user="a/b")

    assert project(event) == (None, "impersonated_user")


def test_udm_principal_is_the_admin_and_additional_names_the_subject() -> None:
    event = _role_change(_SUBJECT)
    udm = build_udm(
        UdmRow(
            event_uuid=event.event_uuid,
            action_type=event.action_type,
            payload=_payload(event),
            source_instance_label="example-label",
        )
    )

    assert udm["principal"]["user"] == {"userid": _ADMIN}
    assert udm["target"] == {"user": {"userid": "example-target"}}
    assert udm["additional"]["impersonated_user"] == _SUBJECT
    assert validate_udm(udm) is None
    assert validate_events([{"udm": udm}]) == []


def test_reprojection_reads_the_column_back_from_the_audit_row() -> None:
    from code_indexer.server.services.audit_events import (
        AUDIT_ROW_COLUMNS,
        event_row_values,
    )
    from code_indexer.server.services.siem_delivery.claim import (
        event_from_audit_row,
    )

    event = _role_change(_SUBJECT)
    row = dict(zip(AUDIT_ROW_COLUMNS, event_row_values(event)))

    assert event_from_audit_row(row).impersonated_user == _SUBJECT
