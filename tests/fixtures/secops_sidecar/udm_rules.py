"""The sidecar's OWN Chronicle request and UDM rules.

Written from Google's public documentation, independently of CIDX: this
module NEVER imports CIDX's SIEM mapping, so a misunderstanding shared by
both sides is not validated tautologically.

Sources (retrieved for this story; marked "unverified" where a rule could not
be confirmed against the live pages -- the real-tenant canary is the final
arbiter):
  - events:import REST reference (envelope, all-or-nothing, empty success body)
    https://cloud.google.com/chronicle/docs/reference/rest/v1/projects.locations.instances.events/import
  - Ingestion methods: limits (4 MB request, 10,000-event guidance),
    camelCase passthrough, principal must carry a machine or user detail and
    must not carry email/file/registry fields
    https://cloud.google.com/chronicle/docs/ingestion/ingestion-methods
  - UDM field list and event-type enum
    https://cloud.google.com/chronicle/docs/reference/udm-field-list
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MAX_EVENTS_PER_REQUEST = 10_000
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
MAX_NESTING_DEPTH = 32
MAX_VIOLATIONS_REPORTED = 100
REQUEST_LEVEL_FIELD = "inline_source"
EVENTS_FIELD = "inline_source.events"

# Schema vocabulary: a scalar, a list of scalars, a free-form Struct (any keys,
# any depth -- Google's "additional"), an object (dict of field -> schema), or
# a repeated object ("repeated", dict).
SCALAR = "scalar"
SCALAR_LIST = "scalar_list"
STRUCT = "struct"

# A subset of the UDM metadata.eventType enum (udm-field-list page).  Values
# outside it are rejected.  UNVERIFIED: per-type required fields (e.g. whether
# USER_LOGIN demands target.user) are NOT enforced here; the real-tenant
# canary is the final arbiter.
SIDECAR_UDM_EVENT_TYPES = frozenset({
    "GENERIC_EVENT", "STATUS_UPDATE", "SETTING_MODIFICATION",
    "USER_LOGIN", "USER_LOGOUT", "USER_CREATION", "USER_DELETION",
    "USER_CHANGE_PASSWORD", "USER_CHANGE_PERMISSIONS", "USER_UNCATEGORIZED",
    "USER_RESOURCE_ACCESS", "USER_RESOURCE_CREATION", "USER_RESOURCE_DELETION",
    "USER_RESOURCE_UPDATE_CONTENT", "USER_RESOURCE_UPDATE_PERMISSIONS",
    "GROUP_CREATION", "GROUP_DELETION", "GROUP_MODIFICATION",
    "GROUP_UNCATEGORIZED", "NETWORK_CONNECTION", "NETWORK_HTTP",
    "RESOURCE_CREATION", "RESOURCE_DELETION", "RESOURCE_PERMISSIONS_CHANGE",
    "RESOURCE_READ", "RESOURCE_WRITTEN", "SYSTEM_AUDIT_LOG_UNCATEGORIZED",
})  # fmt: skip

_USER = {
    "userid": SCALAR, "userDisplayName": SCALAR, "productObjectId": SCALAR,
    "windowsSid": SCALAR, "employeeId": SCALAR, "firstName": SCALAR,
    "lastName": SCALAR, "title": SCALAR, "accountType": SCALAR,
    "groupIdentifiers": SCALAR_LIST, "emailAddresses": SCALAR_LIST,
    "attribute": STRUCT,
}  # fmt: skip
_GROUP = {"groupDisplayName": SCALAR, "productObjectId": SCALAR, "attribute": STRUCT}
_RESOURCE = {
    "name": SCALAR, "type": SCALAR, "resourceType": SCALAR, "resourceSubtype": SCALAR,
    "productObjectId": SCALAR, "id": SCALAR, "attribute": STRUCT,
}  # fmt: skip
_NOUN = {
    "hostname": SCALAR, "assetId": SCALAR, "ip": SCALAR_LIST, "mac": SCALAR_LIST,
    "port": SCALAR, "application": SCALAR, "platform": SCALAR,
    "namespace": SCALAR, "administrativeDomain": SCALAR, "url": SCALAR,
    "user": _USER, "group": _GROUP, "resource": _RESOURCE,
    "file": STRUCT, "registry": STRUCT,
}  # fmt: skip
_METADATA = {
    "eventTimestamp": SCALAR, "eventType": SCALAR, "vendorName": SCALAR,
    "productName": SCALAR, "productVersion": SCALAR, "productEventType": SCALAR,
    "productLogId": SCALAR, "description": SCALAR, "collectedTimestamp": SCALAR,
    "logType": SCALAR, "urlBackToProduct": SCALAR,
}  # fmt: skip
_SECURITY_RESULT = {
    "action": SCALAR_LIST, "actionDetails": SCALAR, "summary": SCALAR,
    "description": SCALAR, "severity": SCALAR, "severityDetails": SCALAR,
    "category": SCALAR_LIST, "categoryDetails": SCALAR_LIST, "ruleName": SCALAR,
    "ruleId": SCALAR, "detectionFields": ("repeated", {"key": SCALAR, "value": SCALAR}),
}  # fmt: skip
_EXTENSIONS = {
    "auth": {"type": SCALAR, "mechanism": SCALAR_LIST, "authDetails": SCALAR}
}
_NETWORK = {
    "applicationProtocol": SCALAR, "ipProtocol": SCALAR, "direction": SCALAR,
    "sessionId": SCALAR,
    "http": {"method": SCALAR, "userAgent": SCALAR, "responseCode": SCALAR},
}  # fmt: skip
UDM_SCHEMA = {
    "metadata": _METADATA, "principal": _NOUN, "target": _NOUN, "src": _NOUN,
    "observer": _NOUN, "intermediary": ("repeated", _NOUN), "about": ("repeated", _NOUN),
    "securityResult": ("repeated", _SECURITY_RESULT), "network": _NETWORK,
    "extensions": _EXTENSIONS, "additional": STRUCT,
}  # fmt: skip
# Ingestion-methods field table: the principal must not carry these.
PRINCIPAL_FORBIDDEN_PATHS = ("user.emailAddresses", "file", "registry")
PRINCIPAL_DETAIL_PATHS = ("user.userid", "hostname", "ip", "application")
PRINCIPAL_DETAIL_REQUIRED = "Required: a user or machine detail."
UDM_ROOT_DEPTH = 1


@dataclass
class Envelope:
    """A parsed request: events when the envelope is valid, else violations."""

    events: Optional[List[Dict[str, Any]]]
    event_count: Optional[int]
    violations: List[Dict[str, str]] = field(default_factory=list)
    message: str = "Request contains an invalid argument."


Problem = Tuple[str, str]  # (udm path, fixed description)
_SCALAR_TYPES = (str, int, float, bool)


def _struct_too_deep(value: Any, depth: int) -> bool:
    if depth > MAX_NESTING_DEPTH:
        return True
    if isinstance(value, dict):
        return any(_struct_too_deep(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_struct_too_deep(v, depth + 1) for v in value)
    return False


def _walk(value: Any, schema: Any, path: str, depth: int, out: List[Problem]) -> None:
    """Check *value* against *schema*; append problems (bounded by input size)."""
    if depth > MAX_NESTING_DEPTH:
        out.append((path, "Nesting is too deep."))
    elif schema == SCALAR:
        if not isinstance(value, _SCALAR_TYPES):
            out.append((path, "Expected a scalar value."))
    elif schema == SCALAR_LIST:
        if not isinstance(value, list) or not all(
            isinstance(v, _SCALAR_TYPES) for v in value
        ):
            out.append((path, "Expected a list of scalar values."))
    elif schema == STRUCT:
        if _struct_too_deep(value, depth):
            out.append((path, "Nesting is too deep."))
    elif isinstance(schema, tuple):  # ("repeated", object schema)
        if not isinstance(value, list):
            out.append((path, "Expected a repeated field."))
            return
        for index, item in enumerate(value):
            _walk(item, schema[1], f"{path}[{index}]", depth + 1, out)
    elif not isinstance(value, dict):
        out.append((path, "Expected an object."))
    else:
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key not in schema:
                out.append(
                    (child_path, "Unknown field name (UDM field names are camelCase).")
                )
            else:
                _walk(child, schema[key], child_path, depth + 1, out)


def _violation(field_path: str, description: str) -> Dict[str, str]:
    return {"field": field_path, "description": description}


def _element_violation(element: Any) -> Optional[Dict[str, str]]:
    if not isinstance(element, dict):
        return _violation(EVENTS_FIELD, "Each element must be an object.")
    unknown = sorted(k for k in element if k != "udm")
    if unknown:
        return _violation(EVENTS_FIELD, f'Unknown name "{unknown[0]}" in an element.')
    if not isinstance(element.get("udm"), dict):
        return _violation(EVENTS_FIELD, 'Each element needs a "udm" object.')
    return None


def parse_envelope(body: bytes, parent: str) -> Envelope:
    """Request-level contract.  Violations here never carry an event index."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Envelope(None, None, [], "Invalid JSON payload received.")
    if not isinstance(doc, dict):
        return Envelope(None, None, [_violation(REQUEST_LEVEL_FIELD, "required")])
    unknown = sorted(k for k in doc if k not in ("inlineSource", "parent"))
    if unknown or "inlineSource" not in doc:
        name = unknown[0] if unknown else "inlineSource"
        return Envelope(
            None,
            None,
            [_violation(REQUEST_LEVEL_FIELD, f'Unknown or missing name "{name}".')],
            f'Invalid JSON payload received. Unknown name "{name}".',
        )
    if "parent" in doc and doc["parent"] != parent:
        return Envelope(None, None, [_violation("parent", "Does not match the path.")])
    source = doc["inlineSource"]
    events = source.get("events") if isinstance(source, dict) else None
    if not isinstance(source, dict) or set(source) != {"events"}:
        return Envelope(None, None, [_violation(REQUEST_LEVEL_FIELD, 'Only "events".')])
    if not isinstance(events, list) or not events:
        return Envelope(None, None, [_violation(EVENTS_FIELD, "Must be non-empty.")])
    if len(events) > MAX_EVENTS_PER_REQUEST:
        return Envelope(None, len(events), [_violation(EVENTS_FIELD, "Too many.")])
    for element in events:
        problem = _element_violation(element)
        if problem is not None:
            return Envelope(None, len(events), [problem])
    return Envelope(events, len(events))


def _lookup(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _check_metadata(udm: Dict[str, Any], out: List[Problem]) -> None:
    timestamp = _lookup(udm, "metadata.eventTimestamp")
    if not isinstance(timestamp, str) or not RFC3339_UTC.match(timestamp):
        out.append(("metadata.eventTimestamp", "Required RFC 3339 UTC timestamp."))
    event_type = _lookup(udm, "metadata.eventType")
    if event_type not in SIDECAR_UDM_EVENT_TYPES:
        out.append(("metadata.eventType", "Required; must be a UDM event type."))


def _check_principal(udm: Dict[str, Any], out: List[Problem]) -> None:
    principal = udm.get("principal")
    if not isinstance(principal, dict):
        out.append(("principal", PRINCIPAL_DETAIL_REQUIRED))
        return
    for forbidden in PRINCIPAL_FORBIDDEN_PATHS:
        if _lookup(principal, forbidden) is not None:
            out.append((f"principal.{forbidden}", "Not allowed on the principal."))
    if not any(
        _lookup(principal, p) not in (None, "", []) for p in PRINCIPAL_DETAIL_PATHS
    ):
        out.append(("principal", PRINCIPAL_DETAIL_REQUIRED))


def validate_events(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Field violations for every invalid event (an empty list means valid)."""
    violations: List[Dict[str, str]] = []
    for index, element in enumerate(events):
        udm = element["udm"]
        problems: List[Problem] = []
        _walk(udm, UDM_SCHEMA, "", UDM_ROOT_DEPTH, problems)
        _check_metadata(udm, problems)
        _check_principal(udm, problems)
        for path, description in problems:
            field_path = f"inline_source.events[{index}].udm.{path}"
            violations.append(_violation(field_path, description))
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                return violations
    return violations
