"""Synthetic canary events: one per shipped UDM mapping entry plus one
unmapped type (exercising the fallback).  Neutral sample data only; each
event goes through the SAME projection, mapping, validation and envelope
as a real row.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AuditEvent,
    FieldType,
)
from code_indexer.server.services.audit_log_query import details_read_schema
from code_indexer.server.services.siem_delivery.udm import UDM_MAPPING

CANARY_ACTOR = "system:siem-canary"
CANARY_UNMAPPED_TYPE = "siem_canary_unmapped"
_TARGET_SAMPLES = {
    "user": "example-user",
    "auth": "example-user",
    "group": "example-group",
    "repo": "example-repo-global",
    "config": "siem_delivery",
    "server": "node",
}
_KIND_SAMPLES = {
    "bool": True,
    "int": 1,
    "username": "example-user",
    "hostname": "host.example.com",
    "repo_alias": "example-repo-global",
    "git_ref": "main",
    "opaque_id": "example-id",
    "config_key": "example_key",
    "opaque_id_or_all": "example-id",
    "config_key_or_all": "siem_delivery",
}


def _sample(ftype: FieldType) -> Any:
    if ftype.kind == "enum":
        return sorted(ftype.values)[0]
    if ftype.kind == "list":
        assert ftype.item is not None
        return [_sample(ftype.item)]
    if ftype.kind == "config_value_map":
        return {"workers": [1, 2]}
    return _KIND_SAMPLES[ftype.kind]


def _details(action_type: str) -> Optional[str]:
    schema: Mapping[str, FieldType] = details_read_schema(action_type)
    if not schema:
        return None
    return json.dumps({name: _sample(ftype) for name, ftype in schema.items()})


def _event(action_type: str, target_type: str, run_id: str, now: str) -> AuditEvent:
    return AuditEvent(
        event_uuid=str(uuid.uuid4()),
        occurred_at=now,
        actor=CANARY_ACTOR,
        actor_is_system=True,
        action_type=action_type,
        target_type=target_type,
        target_id=_TARGET_SAMPLES.get(target_type, "example-id"),
        outcome="success",
        source="system",
        ip_address="192.0.2.10",
        correlation_id=f"canary-{run_id}",
        node_id=None,
        auth_method="system",
        details_json=_details(action_type),
    )


def synthetic_events(run_id: str) -> List[AuditEvent]:
    """One event per UDM_MAPPING entry (sorted), then the unmapped one."""
    now = datetime.now(timezone.utc).isoformat()
    events = [
        _event(name, AUDIT_ACTION_CATALOG[name].target_type, run_id, now)
        for name in sorted(UDM_MAPPING)
    ]
    events.append(_event(CANARY_UNMAPPED_TYPE, "config", run_id, now))
    return events
