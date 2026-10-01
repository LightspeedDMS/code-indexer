"""UDM mapping (permissive fallback), local validation, canonical envelope.

``MAPPING_VERSION`` covers EVERYTHING that shapes a request body: the
mapping table, :func:`build_udm`, the envelope, canonical serialisation and
the packing constants.  Any change to any of them must bump it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAPPING_VERSION = 2
FALLBACK_EVENT_TYPE = "GENERIC_EVENT"
BYTE_BUDGET = 2_000_000
HARD_CEILING = 4_000_000
VENDOR_NAME = "CIDX"
PRODUCT_NAME = "cidx-server"
UNKNOWN_ACTOR = "(unknown)"
_PLACEHOLDER_TARGETS = frozenset({UNKNOWN_ACTOR, "unresolved"})

_USER_LOGIN = "USER_LOGIN"
_UNCATEGORIZED = "USER_UNCATEGORIZED"
UDM_MAPPING: Dict[str, str] = {
    "authentication_success": _USER_LOGIN,
    "authentication_failure": _USER_LOGIN,
    "elevation_granted": _USER_LOGIN,
    "elevation_failed": _USER_LOGIN,
    "mfa_activated": _UNCATEGORIZED,
    "mfa_disabled": _UNCATEGORIZED,
    "mfa_recovery_codes_regenerated": _UNCATEGORIZED,
    "mfa_secret_regenerated_cross_user": _UNCATEGORIZED,
    "user_role_changed": "USER_CHANGE_PERMISSIONS",
    "user_group_assign": "GROUP_MODIFICATION",
    "user_group_change": "GROUP_MODIFICATION",
    "repo_access_grant": "USER_RESOURCE_UPDATE_PERMISSIONS",
    "repo_access_revoke": "USER_RESOURCE_UPDATE_PERMISSIONS",
    "user_created": "USER_CREATION",
    "user_deleted": "USER_DELETION",
    "user_password_reset_by_admin": "USER_CHANGE_PASSWORD",
    "mcp_credential_created": _UNCATEGORIZED,
    "mcp_credential_revoked": _UNCATEGORIZED,
    "api_key_created": _UNCATEGORIZED,
    "ssh_key_host_assigned": _UNCATEGORIZED,
    "impersonation_set": _UNCATEGORIZED,
    "impersonation_cleared": _UNCATEGORIZED,
    "impersonation_denied": _UNCATEGORIZED,
    "config_changed": "SETTING_MODIFICATION",
    "siem_canary_sent": "STATUS_UPDATE",
    "siem_canary_visibility_confirmed": "STATUS_UPDATE",
    "siem_quarantine_requeued": "STATUS_UPDATE",
    "siem_delivery_resumed": "STATUS_UPDATE",
    "siem_batch_acknowledged": "STATUS_UPDATE",
    "siem_batch_rebatched": "STATUS_UPDATE",
    "siem_destination_retargeted": "STATUS_UPDATE",
    "siem_destination_abandoned": "STATUS_UPDATE",
    "siem_credential_changed": "SETTING_MODIFICATION",
}
VALIDATED_EVENT_TYPES: FrozenSet[str] = frozenset(UDM_MAPPING.values()) | {
    FALLBACK_EVENT_TYPE
}

_AUTH_TYPE_FOR_METHOD = {
    "password": "AUTHTYPE_UNSPECIFIED",
    "sso": "SSO",
    "api_key": "MACHINE",
    "mcp_credential": "MACHINE",
}
_ADDITIONAL_SCALARS = (
    "outcome",
    "source",
    "correlation_id",
    "auth_method",
    "actor_is_system",
    "node_id",
)


@dataclass(frozen=True)
class UdmRow:
    """What :func:`build_udm` needs: identity plus the stored projection."""

    event_uuid: str
    action_type: str
    payload: Mapping[str, Any]
    source_instance_label: str


class _UnmappedReporter:
    """Per-process counter plus one WARNING per type (process telemetry).

    Bounded: keys are action types, a closed catalog set.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}

    def report(self, action_type: str) -> None:
        with self._lock:
            first = action_type not in self._counts
            self._counts[action_type] = self._counts.get(action_type, 0) + 1
        if first:
            logger.warning(
                "SIEM delivery: action type %s has no UDM mapping; delivered as %s",
                action_type,
                FALLBACK_EVENT_TYPE,
            )
        from code_indexer.server.services.siem_delivery import telemetry

        telemetry.add_counter("unmapped_action_type", 1, {"action_type": action_type})

    def count(self, action_type: str) -> int:
        with self._lock:
            return self._counts.get(action_type, 0)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_unmapped = _UnmappedReporter()


def unmapped_count(action_type: str) -> int:
    return _unmapped.count(action_type)


def reset_unmapped_reporter_for_tests() -> None:
    _unmapped.reset()


def _principal(
    payload: Mapping[str, Any], additional: Dict[str, Any]
) -> Dict[str, Any]:
    principal: Dict[str, Any] = {}
    actor = payload.get("actor")
    if payload.get("actor_is_system"):
        principal["application"] = PRODUCT_NAME
    elif isinstance(actor, str) and actor != UNKNOWN_ACTOR:
        principal["user"] = {"userid": actor}
    ip = payload.get("ip_address")
    if isinstance(ip, str):
        principal["ip"] = [ip]
    if not principal:
        principal["application"] = PRODUCT_NAME
        additional["principal_unknown"] = True
    return principal


def _target(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    target_id = payload.get("target_id")
    target_type = payload.get("target_type")
    if not isinstance(target_id, str) or target_id in _PLACEHOLDER_TARGETS:
        return None
    if target_type in ("user", "auth"):
        return {"user": {"userid": target_id}}
    if target_type == "group":
        return {"group": {"productObjectId": target_id}}
    return {"resource": {"name": target_id, "type": str(target_type)}}


def build_udm(
    row: UdmRow,
    *,
    mapping: Mapping[str, str] = UDM_MAPPING,
    canary: bool = False,
    count_unmapped: bool = True,
) -> Dict[str, Any]:
    """Pure (no I/O apart from the unmapped-type counter)."""
    payload = row.payload
    event_type = mapping.get(row.action_type)
    if event_type is None:
        event_type = FALLBACK_EVENT_TYPE
        if count_unmapped:
            _unmapped.report(row.action_type)
    additional: Dict[str, Any] = {
        "schema_version": payload.get("schema_version"),
        "cidx_instance": row.source_instance_label,
    }
    for name in _ADDITIONAL_SCALARS:
        if payload.get(name) is not None:
            additional[name] = payload[name]
    if payload.get("details"):
        additional["cidx_details"] = dict(payload["details"])
    if canary:
        additional["cidx_canary"] = True
    udm: Dict[str, Any] = {
        "metadata": {
            "eventTimestamp": payload.get("occurred_at"),
            "eventType": event_type,
            "vendorName": VENDOR_NAME,
            "productName": PRODUCT_NAME,
            "productEventType": row.action_type,
            "productLogId": row.event_uuid,
        },
        "principal": _principal(payload, additional),
    }
    target = _target(payload)
    if target is not None:
        udm["target"] = target
    outcome = payload.get("outcome")
    if outcome in ("success", "failure", "denied"):
        action = "ALLOW" if outcome == "success" else "BLOCK"
        udm["securityResult"] = [{"action": [action]}]
    method = (payload.get("details") or {}).get("method")
    if event_type == _USER_LOGIN and method in _AUTH_TYPE_FOR_METHOD:
        udm["extensions"] = {"auth": {"type": _AUTH_TYPE_FOR_METHOD[method]}}
    udm["additional"] = additional
    return udm


# --- local validation -------------------------------------------------------

_SCALAR, _LIST, _STRUCT = "scalar", "list", "struct"
_NOUN = {
    "user": {"userid": _SCALAR},
    "ip": _LIST,
    "application": _SCALAR,
    "group": {"productObjectId": _SCALAR},
    "resource": {"name": _SCALAR, "type": _SCALAR},
}
_ALLOWED: Dict[str, Any] = {
    "metadata": {
        k: _SCALAR
        for k in (
            "eventTimestamp",
            "eventType",
            "vendorName",
            "productName",
            "productEventType",
            "productLogId",
        )
    },
    "principal": _NOUN,
    "target": _NOUN,
    "securityResult": ("repeated", {"action": _LIST}),
    "extensions": {"auth": {"type": _SCALAR}},
    "additional": _STRUCT,
}
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")

Problem = Tuple[str, str]  # (reason enum, udm path)


def _walk(value: Any, schema: Any, path: str) -> Optional[Problem]:
    if schema == _STRUCT:
        return None
    if schema == _SCALAR:
        ok = isinstance(value, (str, int, float, bool))
        return None if ok else ("wrong_shape", path)
    if schema == _LIST:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        return None if ok else ("wrong_shape", path)
    if isinstance(schema, tuple):
        if not isinstance(value, list):
            return ("wrong_shape", path)
        for item in value:
            problem = _walk(item, schema[1], f"{path}[*]")
            if problem:
                return problem
        return None
    if not isinstance(value, dict):
        return ("wrong_shape", path)
    for key, child in value.items():
        child_path = f"{path}.{key}" if path else key
        if key not in schema:
            return ("unknown_field", child_path)
        problem = _walk(child, schema[key], child_path)
        if problem:
            return problem
    return None


def validate_udm(udm: Mapping[str, Any]) -> Optional[Problem]:
    """None when valid, else ``(reason, path)`` (field names only)."""
    problem = _walk(dict(udm), _ALLOWED, "")
    if problem:
        return problem
    metadata = udm.get("metadata") or {}
    for key in _ALLOWED["metadata"]:
        if not metadata.get(key):
            return ("missing_field", f"metadata.{key}")
    if metadata.get("eventType") not in VALIDATED_EVENT_TYPES:
        return ("event_type", "metadata.eventType")
    if not _RFC3339.match(str(metadata.get("eventTimestamp"))):
        return ("timestamp", "metadata.eventTimestamp")
    principal = udm.get("principal") or {}
    has_detail = (
        (principal.get("user") or {}).get("userid")
        or principal.get("ip")
        or principal.get("application")
    )
    if not has_detail:
        return ("principal_missing_detail", "principal")
    if len(canonical_json(udm)) > HARD_CEILING:
        return ("oversize", "")
    return None


# --- canonical serialisation, envelope, packing ------------------------------


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


_ENVELOPE_HEAD = b'{"inlineSource":{"events":['
_ENVELOPE_TAIL = b"]}}"
_ENVELOPE_OVERHEAD = len(_ENVELOPE_HEAD) + len(_ENVELOPE_TAIL)


def envelope(members: Sequence[bytes]) -> bytes:
    """``{"inlineSource":{"events":[{"udm":...},...]}}`` from canonical members
    (byte-identical to canonical serialisation of the whole document)."""
    return (
        _ENVELOPE_HEAD
        + b",".join(b'{"udm":' + member + b"}" for member in members)
        + _ENVELOPE_TAIL
    )


def member_udms(body: bytes) -> List[Dict[str, Any]]:
    """The UDM members of a persisted request body, in order."""
    doc = json.loads(body.decode("utf-8"))
    return [element["udm"] for element in doc["inlineSource"]["events"]]


def pack(members: Sequence[bytes], budget: int = BYTE_BUDGET) -> int:
    """How many leading members fit one request of at most *budget* bytes
    (at least one, when any member is given)."""
    size = _ENVELOPE_OVERHEAD
    count = 0
    for member in members:
        added = len(member) + len(b'{"udm":}') + (1 if count else 0)
        if count and size + added > budget:
            break
        size += added
        count += 1
    return count
