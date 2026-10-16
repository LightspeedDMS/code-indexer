"""Typed projection of an audit event into the self-contained SIEM payload.

Only typed values leave the process: the fixed-type event attributes, the
target id (typed by its target type), and the details fields the READ
allowlist (``details_read_schema``) names, each checked against its field
type.  No raw details blob, free text, message, URL or exception text is
ever copied, so no credential-pattern scanning is needed.

A type violation returns the FIELD NAME (never the value) as the
projection error; the row is still captured and the claim path decides.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AUDIT_TARGET_ID_TYPE,
    AUTH_METHODS,
    OUTCOMES,
    AuditEvent,
    FieldType,
    conforms,
)
from code_indexer.server.services.audit_log_query import details_read_schema

SCHEMA_VERSION = 1
_MAX_SOURCE_LENGTH = 64
_MAX_ID_LENGTH = 256


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_rfc3339_utc(value: str) -> Optional[str]:
    """``value`` as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC), or None."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _short_token(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all(c.isalnum() or c in "-_.:+=" for c in value)
    )


def _check_base(name: str, value: Any) -> bool:
    """Type rules of the fixed-type attributes (FALLBACK_PROJECTION)."""
    if name in ("actor", "impersonated_user"):
        return conforms(AUDIT_TARGET_ID_TYPE["user"], value)
    if name == "outcome":
        return value in OUTCOMES
    if name == "source":
        return _short_token(value, _MAX_SOURCE_LENGTH)
    if name == "ip_address":
        try:
            ipaddress.ip_address(value)
        except (TypeError, ValueError):
            return False
        return True
    if name in ("correlation_id", "event_uuid", "node_id"):
        return _short_token(value, _MAX_ID_LENGTH)
    if name == "auth_method":
        return value in AUTH_METHODS
    if name == "actor_is_system":
        return isinstance(value, bool)
    raise AssertionError(f"unknown base projection field: {name}")


# The correlation id may originate from a client request header: a value that
# does not fit is untrusted input, so it is omitted (never copied, never a
# projection error a client could use to push rows toward quarantine).
_OMIT_IF_NONCONFORMING = frozenset({"correlation_id"})

FALLBACK_PROJECTION = (
    "actor",
    "outcome",
    "source",
    "ip_address",
    "correlation_id",
    "node_id",
    "auth_method",
    "actor_is_system",
    "event_uuid",
    # Set only during MCP impersonation: the subject ("actor" is then the
    # authenticated administrator).
    "impersonated_user",
)


def _details(event: AuditEvent) -> Dict[str, Any]:
    if not event.details_json:
        return {}
    try:
        decoded = json.loads(event.details_json)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _typed_details(
    event: AuditEvent,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    schema: Mapping[str, FieldType] = details_read_schema(event.action_type)
    kept: Dict[str, Any] = {}
    for name, value in _details(event).items():
        ftype = schema.get(name) if isinstance(name, str) else None
        if ftype is None or value is None:
            continue  # outside the read allowlist: never projected
        if not conforms(ftype, value):
            return None, f"details.{name}"
        kept[name] = value
    return kept, None


def _target(event: AuditEvent) -> Tuple[Optional[str], Optional[str]]:
    spec = AUDIT_ACTION_CATALOG.get(event.action_type)
    ftype = AUDIT_TARGET_ID_TYPE.get(event.target_type)
    if spec is None or ftype is None or not conforms(ftype, event.target_id):
        return None, "target_id"
    return event.target_id, None


def project(
    event: AuditEvent, mapping: Optional[Mapping[str, str]] = None
) -> Tuple[Optional[str], Optional[str]]:
    """``(payload_json, None)`` or ``(None, field_name)``; pure, never raises."""
    if mapping is None:
        from code_indexer.server.services.siem_delivery.udm import UDM_MAPPING

        mapping = UDM_MAPPING
    out: Dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for name in FALLBACK_PROJECTION:
        value = getattr(event, name)
        if value is None:
            continue
        if not _check_base(name, value):
            if name in _OMIT_IF_NONCONFORMING:
                continue
            return None, name
        out[name] = value
    occurred = to_rfc3339_utc(event.occurred_at)
    if occurred is None:
        return None, "occurred_at"
    out["occurred_at"] = occurred
    if event.action_type in mapping:
        target_id, error = _target(event)
        if error is not None:
            return None, error
        out["target_type"] = event.target_type
        out["target_id"] = target_id
        details, error = _typed_details(event)
        if error is not None:
            return None, error
        if details:
            out["details"] = details
    return canonical_dumps(out), None
