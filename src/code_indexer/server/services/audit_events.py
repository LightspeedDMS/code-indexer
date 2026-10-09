"""Audit event model, action catalog and ``details`` allowlist.

Every row in ``audit_logs`` is built here, exactly once, as an immutable
:class:`AuditEvent`:

- :func:`build_event` for an action performed by an authenticated (or, for a
  failed login, attempted) human actor;
- :func:`build_system_event` for an action performed by a closed set of
  server components.  It is the ONLY builder that sets
  ``actor_is_system``, so no request-supplied name can become a system actor;
- :func:`build_legacy_event` for the pre-existing writers whose payloads
  predate the allowlist.

Each event gets its ``event_uuid`` here, at construction, and never at insert
time, so a retried insert reuses it.  Ambient attribution (front door, peer
address, auth method, correlation id, node id) is also resolved here.

``details`` is ALLOWLISTED per action type (:data:`AUDIT_ACTION_CATALOG`):
an unknown field or a wrongly typed value is rejected.  No field type accepts
a URL, free text, a raw config value or an exception string.  Validation
errors name the action type and the FIELD NAME only, never the value.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from code_indexer.server.middleware.audit_request_context import (
    SOURCE_SYSTEM,
    McpPrincipal,
    current_audit_request_context,
    current_mcp_principal,
)

OUTCOMES: FrozenSet[str] = frozenset({"success", "failure", "denied", "attempted"})
AUTH_METHODS: FrozenSet[str] = frozenset(
    {"jwt", "oauth_token", "mcp_credential", "web_session", "none", "system"}
)
AUTH_METHOD_SYSTEM = "system"
SYSTEM_ACTOR_PREFIX = "system:"
# Actor recorded for a refused login whose typed name matches no existing
# account (it may be a password typed into the wrong field).
UNKNOWN_ACCOUNT_ACTOR = "(unknown)"
# Target id accepted for an action that applies to every group / every
# config category at once.
ALL_TARGETS_MARKER = "*"

_MAX_ACTOR_LENGTH = 255
_MAX_REPORTED_FIELD_NAME = 64


class Delivery(str, Enum):
    """How an event reaches the store (chosen per action type, never by caller)."""

    DURABLE = "durable"  # synchronous write, own transaction, on the caller's thread
    QUEUED = "queued"  # the async writer thread (high-volume auth activity)


class SystemComponent(str, Enum):
    """Closed set of server components that may act as an audit actor."""

    SELF_REGISTRATION = "self-registration"
    SSO_PROVISIONING = "sso-provisioning"
    MCP_SELF_REGISTRATION = "mcp-self-registration"
    GOLDEN_REPO_RECONCILER = "golden-repo-reconciler"
    # The loopback-only maintenance switch, driven by the local auto-updater.
    LOCALHOST_MAINTENANCE = "localhost-maintenance"
    # The SIEM delivery worker (automatic requeue after a mapping change).
    SIEM_DELIVERY = "siem-delivery"


class AuditEventInvalid(ValueError):
    """An event violates a construction precondition (a programming defect).

    The message and attributes carry the action type and the field NAME
    only -- never the offending value.
    """

    def __init__(self, reason: str, *, action_type: str, field: str) -> None:
        super().__init__(f"{reason} (action_type={action_type}, field={field})")
        self.reason = reason
        self.action_type = action_type
        self.field = field


class AuditDetailsSchemaViolation(AuditEventInvalid):
    """A ``details`` entry is outside its action type's allowlist."""


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldType:
    """Type of one allowlisted ``details`` field (or of a target id)."""

    kind: str
    values: FrozenSet[str] = frozenset()
    item: Optional["FieldType"] = None
    max_items: int = 0


def _enum(*values: str) -> FieldType:
    return FieldType(kind="enum", values=frozenset(values))


def _list(item: FieldType, max_items: int) -> FieldType:
    return FieldType(kind="list", item=item, max_items=max_items)


BOOL = FieldType(kind="bool")
INT = FieldType(kind="int")
USERNAME = FieldType(kind="username")
HOSTNAME = FieldType(kind="hostname")
REPO_ALIAS = FieldType(kind="repo_alias")
GIT_REF = FieldType(kind="git_ref")
OPAQUE_ID = FieldType(kind="opaque_id")
CONFIG_KEY = FieldType(kind="config_key")
CONFIG_VALUE_MAP = FieldType(kind="config_value_map", max_items=200)
OPAQUE_ID_OR_ALL = FieldType(kind="opaque_id_or_all")
CONFIG_KEY_OR_ALL = FieldType(kind="config_key_or_all")

# The names account creation accepts (validate_username_path_safe): not
# blank, not "." or "..", no "/", "\\", C0 control character or DEL.
_USERNAME_RE = re.compile(r"^(?!\s*$)(?!\.{1,2}$)[^/\\\x00-\x1f\x7f]{1,255}$")
# No ":", "@" or "/" so URL userinfo can never ride in a hostname field.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
# An alias, never a URL.
_REPO_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._+-][A-Za-z0-9._/+-]{0,254}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+=-]{0,255}$")
_CONFIG_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")
# Scalar config values recorded in ``config_changed`` (enum-like tokens only).
_SCALAR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "username": _USERNAME_RE,
    "hostname": _HOSTNAME_RE,
    "repo_alias": _REPO_ALIAS_RE,
    "git_ref": _GIT_REF_RE,
    "opaque_id": _OPAQUE_ID_RE,
    "config_key": _CONFIG_KEY_RE,
}


def _is_scalar_config_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and bool(_SCALAR_TOKEN_RE.match(value))


def _conforms_config_value_map(ftype: FieldType, value: Any) -> bool:
    if not isinstance(value, Mapping) or len(value) > ftype.max_items:
        return False
    for key, pair in value.items():
        if not isinstance(key, str) or not _CONFIG_KEY_RE.match(key):
            return False
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return False
        if not all(_is_scalar_config_value(v) for v in pair):
            return False
    return True


def conforms(ftype: FieldType, value: Any) -> bool:
    """Return True when *value* is a valid instance of *ftype*."""
    kind = ftype.kind
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "enum":
        return isinstance(value, str) and value in ftype.values
    if kind == "list":
        if not isinstance(value, (list, tuple)) or len(value) > ftype.max_items:
            return False
        item = ftype.item
        return item is not None and all(conforms(item, v) for v in value)
    if kind == "config_value_map":
        return _conforms_config_value_map(ftype, value)
    if kind in ("opaque_id_or_all", "config_key_or_all"):
        if value == ALL_TARGETS_MARKER:
            return True
        base = "opaque_id" if kind == "opaque_id_or_all" else "config_key"
        return isinstance(value, str) and bool(_PATTERNS[base].match(value))
    pattern = _PATTERNS.get(kind)
    if pattern is None:
        raise AssertionError(f"unknown audit field kind: {kind}")
    return isinstance(value, str) and bool(pattern.match(value))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """Catalog entry for one ``action_type``.

    ``details_schema`` is None for LEGACY-ONLY types: rows written by the
    pre-existing writers whose payloads predate the allowlist.  Only
    :func:`build_legacy_event` may carry such a type.
    """

    target_type: str
    delivery: Delivery
    details_schema: Optional[Mapping[str, FieldType]]
    job_based: bool = False


AUDIT_TARGET_ID_TYPE: Dict[str, FieldType] = {
    "user": USERNAME,
    "group": OPAQUE_ID_OR_ALL,
    "repo": REPO_ALIAS,
    "api_key": OPAQUE_ID,
    "mcp_credential": OPAQUE_ID,
    "ssh_key": OPAQUE_ID,
    "git_credential": OPAQUE_ID,
    "config": CONFIG_KEY_OR_ALL,
    "provider_index": OPAQUE_ID,
    "ci_token": OPAQUE_ID,
    "tool": OPAQUE_ID,
    "auth": USERNAME,
    # Server-wide operational state: this node, or every node of the cluster.
    "server": _enum("node", "cluster"),
}

_D = Delivery.DURABLE
_Q = Delivery.QUEUED

_ROLE = _enum("admin", "power_user", "normal_user")
_LOGIN_METHOD = _enum("password", "api_key", "sso", "mcp_credential")
_MFA_STATE = _enum(
    "not_enrolled",
    "totp",
    "recovery_code",
    "not_applicable",
    "not_checked_password_expired",
    "not_checked_sso_oauth",
)
_LOGIN_FLOW = _enum("rest_token", "web_session", "oauth_code", "mcp_jwt")
_FAIL_STAGE = _enum("credentials", "mfa_code", "challenge", "issuance")
_FAIL_REASON = _enum(
    "bad_credentials",
    "mfa_code_invalid",
    "challenge_invalid_or_expired",
    "account_locked",
    "rate_limited",
    "password_expired",
    "server_error",
)
_FORGE_PLATFORM = _enum("github", "gitlab")
_EMBEDDING_PROVIDER = _enum("voyage-ai", "cohere")
_API_KEY_PROVIDER = _enum("anthropic", "voyageai", "cohere")
_INDEX_TYPE = _enum("semantic", "fts", "temporal", "scip")
_BULK_LIST_MAX = 100
_CONFIG_KEYS_MAX = 200

_ELEVATION = {"scope": _enum("full", "totp_repair"), "used_recovery_code": BOOL}
_CREDENTIAL = {"credential_id": OPAQUE_ID, "for_self": BOOL}
_GIT_CREDENTIAL = {"platform": _FORGE_PLATFORM, "forge_host": HOSTNAME}
_PROVIDER_INDEX = {"provider": _EMBEDDING_PROVIDER, "job_id": OPAQUE_ID}
_TOOL_ACCESS = {"tool_name": OPAQUE_ID, "all_groups": BOOL}
_MAINTENANCE = {"origin": _enum("loopback")}


def _spec(
    target_type: str,
    details: Optional[Mapping[str, FieldType]],
    delivery: Delivery = _D,
    job_based: bool = False,
) -> ActionSpec:
    return ActionSpec(target_type, delivery, details, job_based)


AUDIT_ACTION_CATALOG: Dict[str, ActionSpec] = {
    # --- Logins (one outcome row per attempt) ---
    "authentication_success": _spec(
        "auth", {"method": _LOGIN_METHOD, "mfa": _MFA_STATE, "flow": _LOGIN_FLOW}
    ),
    "authentication_failure": _spec(
        "auth", {"method": _LOGIN_METHOD, "stage": _FAIL_STAGE, "reason": _FAIL_REASON}
    ),
    # --- MFA and elevation ---
    "mfa_activated": _spec("user", {"recovery_codes_issued": BOOL}),
    "mfa_recovery_codes_regenerated": _spec("user", {"count": INT}),
    "mfa_disabled": _spec("user", {"method": _enum("totp", "recovery_code")}),
    "mfa_secret_regenerated_cross_user": _spec("user", {}),
    "elevation_granted": _spec("user", _ELEVATION),
    "elevation_failed": _spec("user", _ELEVATION),
    # --- Users ---
    "user_created": _spec(
        "user",
        {"role": _ROLE, "provisioning": _enum("admin", "self_registration", "sso")},
    ),
    "user_deleted": _spec("user", {"deleted_role": _ROLE}),
    "user_role_changed": _spec("user", {"old_role": _ROLE, "new_role": _ROLE}),
    "user_password_reset_by_admin": _spec("user", {}),
    "user_email_changed": _spec("user", {}),
    # --- Groups and permissions ---
    "group_tool_access_granted": _spec("group", _TOOL_ACCESS),
    "group_tool_access_revoked": _spec("group", _TOOL_ACCESS),
    # --- Credentials ---
    "api_key_created": _spec("api_key", {"key_id": OPAQUE_ID}),
    "api_key_deleted": _spec("api_key", {"key_id": OPAQUE_ID}),
    "mcp_credential_created": _spec("mcp_credential", _CREDENTIAL),
    "mcp_credential_revoked": _spec("mcp_credential", _CREDENTIAL),
    "ssh_key_created": _spec(
        "ssh_key", {"key_type": _enum("ed25519", "rsa", "ecdsa", "dsa")}
    ),
    "ssh_key_deleted": _spec("ssh_key", {}),
    "ssh_key_host_assigned": _spec(
        "ssh_key", {"key_name": OPAQUE_ID, "host": HOSTNAME}
    ),
    "git_credential_configured": _spec("git_credential", _GIT_CREDENTIAL),
    "git_credential_deleted": _spec("git_credential", _GIT_CREDENTIAL),
    # --- Golden repositories (job-based: a row means "submitted") ---
    "golden_repo_added": _spec(
        "repo",
        {"job_id": OPAQUE_ID, "repo_host": HOSTNAME, "branch": GIT_REF},
        job_based=True,
    ),
    "golden_repo_removed": _spec("repo", {"job_id": OPAQUE_ID}, job_based=True),
    "golden_repo_refreshed": _spec(
        "repo", {"job_id": OPAQUE_ID, "force_reset": BOOL}, job_based=True
    ),
    "golden_repo_index_added": _spec(
        "repo",
        {"index_types": _list(_INDEX_TYPE, 4), "job_id": OPAQUE_ID},
        job_based=True,
    ),
    "golden_repo_branch_changed": _spec(
        "repo", {"new_branch": GIT_REF, "job_id": OPAQUE_ID}, job_based=True
    ),
    "provider_index_added": _spec("provider_index", _PROVIDER_INDEX, job_based=True),
    "provider_index_recreated": _spec(
        "provider_index", _PROVIDER_INDEX, job_based=True
    ),
    "provider_index_removed": _spec("provider_index", _PROVIDER_INDEX, job_based=True),
    "provider_index_bulk_added": _spec(
        "provider_index",
        {
            "provider": _EMBEDDING_PROVIDER,
            "aliases": _list(REPO_ALIAS, _BULK_LIST_MAX),
            "aliases_truncated": INT,
            "job_ids": _list(OPAQUE_ID, _BULK_LIST_MAX),
        },
        job_based=True,
    ),
    # --- Configuration and server secrets (key names, never secret values) ---
    "config_changed": _spec(
        "config",
        {
            "change_kind": _enum("update", "reset_to_defaults"),
            "changed_keys": _list(CONFIG_KEY, _CONFIG_KEYS_MAX),
            "attempted_keys": _list(CONFIG_KEY, _CONFIG_KEYS_MAX),
            "values": CONFIG_VALUE_MAP,
        },
    ),
    "git_settings_changed": _spec(
        "config", {"changed_keys": _list(CONFIG_KEY, _CONFIG_KEYS_MAX)}
    ),
    "provider_api_key_set": _spec("config", {"provider": _API_KEY_PROVIDER}),
    "provider_api_key_cleared": _spec("config", {"provider": _API_KEY_PROVIDER}),
    "ci_token_set": _spec("ci_token", {"platform": _FORGE_PLATFORM}),
    "ci_token_deleted": _spec("ci_token", {"platform": _FORGE_PLATFORM}),
    # --- Server operations ---
    "maintenance_mode_entered": _spec("server", _MAINTENANCE),
    "maintenance_mode_exited": _spec("server", _MAINTENANCE),
    # Written before the restart is triggered (a row means "requested"); a
    # trigger that then raises adds a failure row after the request row.
    "server_restart_requested": _spec("server", {}),
    # --- Activated repositories an admin manages for another user ---
    "user_repo_activated_by_admin": _spec(
        "user",
        {
            "user_alias": REPO_ALIAS,
            "golden_repo_alias": REPO_ALIAS,
            "job_id": OPAQUE_ID,
        },
        job_based=True,
    ),
    "user_repo_deactivated_by_admin": _spec(
        "user", {"user_alias": REPO_ALIAS, "job_id": OPAQUE_ID}, job_based=True
    ),
    # --- Legacy-only types (pre-existing writers; payload predates allowlist) ---
    "group_create": _spec("group", None),
    "group_update": _spec("group", None),
    "group_delete": _spec("group", None),
    "user_group_change": _spec("user", None),
    "user_group_assign": _spec("user", None),
    "user_assign": _spec("user", None),
    "repo_access_grant": _spec("repo", None),
    "repo_access_revoke": _spec("repo", None),
    "tool_access_grant": _spec("tool", None),
    "tool_access_revoke": _spec("tool", None),
    "tool_access_bulk_disable": _spec("tool", None),
    "impersonation_set": _spec("auth", None),
    "impersonation_cleared": _spec("auth", None),
    "impersonation_denied": _spec("auth", None),
    "password_change_success": _spec("auth", None),
    "password_change_failure": _spec("auth", None),
    "password_change_rate_limit": _spec("auth", None),
    "password_change_concurrent_conflict": _spec("auth", None),
    "security_incident": _spec("auth", None),
    # High-volume authentication activity: QUEUED on the async writer.
    "token_refresh_success": _spec("auth", None, _Q),
    "token_refresh_failure": _spec("auth", None, _Q),
    "oauth_client_registration": _spec("auth", None, _Q),
    "oauth_authorization": _spec("auth", None, _Q),
    "oauth_token_exchange": _spec("auth", None, _Q),
    "oauth_token_revocation": _spec("auth", None, _Q),
    "registration_attempt": _spec("auth", None, _Q),
    "password_reset_attempt": _spec("auth", None, _Q),
    "pr_creation_success": _spec("auth", None, _Q),
    "pr_creation_failure": _spec("auth", None, _Q),
    "pr_creation_disabled": _spec("auth", None, _Q),
    "git_cleanup": _spec("auth", None, _Q),
}

# --- SIEM delivery self-reports (target: the "siem_delivery" config section) ---
_SIEM_HALT_CLASS = _enum(
    "local_validation_burst",
    "row_rejection_burst",
    "request_rejection",
    "credential",
    "duplicate_response",
    "unclassified",
)
_SIEM_BATCH = {"batch_id": OPAQUE_ID, "event_count": INT}
_SIEM_MISSING_TYPES_MAX = 200
AUDIT_ACTION_CATALOG.update(
    {
        "siem_canary_sent": _spec(
            "config",
            {
                "canary_run_id": OPAQUE_ID,
                "event_count": INT,
                "result": _enum("accepted", "rejected"),
                "mapping_version": INT,
            },
        ),
        "siem_canary_visibility_confirmed": _spec(
            "config",
            {
                "canary_run_id": OPAQUE_ID,
                "expected_count": INT,
                "confirmed_count": INT,
                "missing_action_types": _list(
                    _enum(*AUDIT_ACTION_CATALOG), _SIEM_MISSING_TYPES_MAX
                ),
            },
        ),
        "siem_quarantine_requeued": _spec(
            "config",
            {
                "count": INT,
                "mapping_version": INT,
                "trigger": _enum("mapping_version_change", "admin"),
            },
        ),
        "siem_delivery_resumed": _spec(
            "config", {"halted_class": _SIEM_HALT_CLASS, "signature_reset": BOOL}
        ),
        "siem_batch_acknowledged": _spec("config", _SIEM_BATCH),
        "siem_batch_rebatched": _spec("config", _SIEM_BATCH),
        "siem_destination_retargeted": _spec(
            "config", {"from_destination_key": OPAQUE_ID, "count": INT}
        ),
        "siem_destination_abandoned": _spec(
            "config", {"destination_key": OPAQUE_ID, "count": INT}
        ),
        # The service-account key: its non-secret identity only, never the key.
        "siem_credential_changed": _spec(
            "config",
            {
                "change": _enum("set", "replaced", "removed"),
                "client_email": USERNAME,
                "private_key_id": OPAQUE_ID,
            },
        ),
    }
)

JOB_BASED_ACTION_TYPES: FrozenSet[str] = frozenset(
    name for name, spec in AUDIT_ACTION_CATALOG.items() if spec.job_based
)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """One audit row, fully attributed, built once and never mutated."""

    event_uuid: str
    occurred_at: str
    actor: str  # stored in admin_id
    actor_is_system: bool
    action_type: str
    target_type: str
    target_id: str
    outcome: Optional[str]  # None only for legacy rows
    source: Optional[str]
    ip_address: Optional[str]
    correlation_id: str
    node_id: Optional[str]
    auth_method: Optional[str]
    details_json: Optional[str]
    # The user an administrator was impersonating over MCP when the action
    # was performed (the subject); ``actor`` is then the administrator.
    impersonated_user: Optional[str] = None


# Column order of an ``audit_logs`` INSERT, shared by both backends.
AUDIT_ROW_COLUMNS = (
    "timestamp",
    "admin_id",
    "action_type",
    "target_type",
    "target_id",
    "details",
    "outcome",
    "source",
    "ip_address",
    "correlation_id",
    "node_id",
    "auth_method",
    "actor_is_system",
    "event_uuid",
    "impersonated_user",
)


def event_row_values(event: AuditEvent) -> tuple:
    """Return *event* as a row tuple in :data:`AUDIT_ROW_COLUMNS` order."""
    return (
        event.occurred_at,
        event.actor,
        event.action_type,
        event.target_type,
        event.target_id,
        event.details_json,
        event.outcome,
        event.source,
        event.ip_address,
        event.correlation_id,
        event.node_id,
        event.auth_method,
        1 if event.actor_is_system else 0,
        event.event_uuid,
        event.impersonated_user,
    )


# This process's cluster node identity (None in solo mode).  Process wiring
# set once by the audit binding at startup -- not cross-request state.
_process_node_id: Optional[str] = None


def set_process_node_id(node_id: Optional[str]) -> None:
    """Record this process's node id for every event built from now on."""
    global _process_node_id
    _process_node_id = node_id or None


def process_node_id() -> Optional[str]:
    """Return the node id stamped on events built in this process."""
    return _process_node_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ambient_correlation_id() -> str:
    """The request's correlation id, or a fresh pairing id outside a request."""
    from code_indexer.server.middleware.correlation import get_correlation_id

    correlation_id = get_correlation_id()
    return correlation_id if correlation_id else f"evt-{uuid.uuid4()}"


def _impersonation_attribution(
    actor: str, principal: Optional[McpPrincipal]
) -> Tuple[str, Optional[str]]:
    """``(actor, impersonated_user)`` for an event built in an MCP tool call
    whose bound principal is *principal*.

    During MCP impersonation (the dispatcher bound the call's principal), the
    authenticated administrator is the actor and the impersonated user is the
    subject.  A handler acting under impersonation names the impersonated
    user as its caller; that name is replaced by the administrator's.
    Outside impersonation the actor is unchanged and the subject is None.
    """
    if principal is None:
        return actor, None
    if actor == principal.impersonated_user:
        actor = principal.authenticated_actor
    return actor, principal.impersonated_user


def _reportable_field_name(key: Any) -> str:
    text = str(key)
    return text if _FIELD_NAME_RE.match(text) else "<non-identifier-field>"


def _require_spec(action_type: str) -> ActionSpec:
    spec = AUDIT_ACTION_CATALOG.get(action_type)
    if spec is None:
        raise AuditEventInvalid(
            "action type not in catalog",
            action_type=str(action_type)[:_MAX_REPORTED_FIELD_NAME],
            field="action_type",
        )
    if spec.details_schema is None:
        raise AuditEventInvalid(
            "legacy-only action type", action_type=action_type, field="action_type"
        )
    return spec


def validate_details(
    action_type: str, details: Optional[Mapping[str, Any]]
) -> Optional[str]:
    """Check *details* against the allowlist; return its JSON (or None)."""
    spec = _require_spec(action_type)
    schema = spec.details_schema or {}
    if details is None:
        return None
    if not isinstance(details, Mapping):
        raise AuditDetailsSchemaViolation(
            "details must be a mapping", action_type=action_type, field="details"
        )
    for key, value in details.items():
        ftype = schema.get(key) if isinstance(key, str) else None
        if ftype is None:
            raise AuditDetailsSchemaViolation(
                "field not allowlisted",
                action_type=action_type,
                field=_reportable_field_name(key),
            )
        if not conforms(ftype, value):
            raise AuditDetailsSchemaViolation(
                "field value has the wrong type", action_type=action_type, field=key
            )
    return json.dumps(dict(details)) if details else None


def _require_outcome(outcome: Any, action_type: str) -> None:
    if outcome not in OUTCOMES:
        raise AuditEventInvalid(
            "outcome not in enum", action_type=action_type, field="outcome"
        )


def _require_target(
    spec: ActionSpec, target_type: str, target_id: Any, action_type: str
) -> None:
    if target_type != spec.target_type:
        raise AuditEventInvalid(
            "target type does not match catalog",
            action_type=action_type,
            field="target_type",
        )
    if not conforms(AUDIT_TARGET_ID_TYPE[target_type], target_id):
        raise AuditEventInvalid(
            "target id has the wrong type", action_type=action_type, field="target_id"
        )


def build_event(
    *,
    actor: str,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
    auth_method: Optional[str] = None,
) -> AuditEvent:
    """Build an event performed by a human actor (see module docstring)."""
    spec = _require_spec(action_type)
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or len(actor) > _MAX_ACTOR_LENGTH
    ):
        raise AuditEventInvalid(
            "actor must be a non-empty string", action_type=action_type, field="actor"
        )
    _require_outcome(outcome, action_type)
    _require_target(spec, target_type, target_id, action_type)
    if auth_method is not None and (
        auth_method not in AUTH_METHODS or auth_method == AUTH_METHOD_SYSTEM
    ):
        raise AuditEventInvalid(
            "auth method not allowed", action_type=action_type, field="auth_method"
        )
    details_json = validate_details(action_type, details)
    ctx = current_audit_request_context()
    if auth_method is None and ctx is not None:
        auth_method = ctx.auth_method
    actor, impersonated_user = _impersonation_attribution(
        actor, current_mcp_principal()
    )
    return AuditEvent(
        event_uuid=str(uuid.uuid4()),
        occurred_at=_now_iso(),
        actor=actor,
        actor_is_system=False,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        source=ctx.source if ctx is not None else SOURCE_SYSTEM,
        ip_address=ctx.client_ip if ctx is not None else None,
        correlation_id=_ambient_correlation_id(),
        node_id=_process_node_id,
        auth_method=auth_method,
        details_json=details_json,
        impersonated_user=impersonated_user,
    )


def build_system_event(
    *,
    component: SystemComponent,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
) -> AuditEvent:
    """Build an event performed by a server component (trusted system actor)."""
    if not isinstance(component, SystemComponent):
        raise AuditEventInvalid(
            "component must be a SystemComponent",
            action_type=str(action_type)[:_MAX_REPORTED_FIELD_NAME],
            field="component",
        )
    spec = _require_spec(action_type)
    _require_outcome(outcome, action_type)
    _require_target(spec, target_type, target_id, action_type)
    details_json = validate_details(action_type, details)
    return AuditEvent(
        event_uuid=str(uuid.uuid4()),
        occurred_at=_now_iso(),
        actor=f"{SYSTEM_ACTOR_PREFIX}{component.value}",
        actor_is_system=True,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        source=SOURCE_SYSTEM,
        ip_address=None,
        correlation_id=_ambient_correlation_id(),
        node_id=_process_node_id,
        auth_method=AUTH_METHOD_SYSTEM,
        details_json=details_json,
    )


_IMPLIED_OUTCOME_SUFFIXES = (
    ("_success", "success"),
    ("_failure", "failure"),
    ("_denied", "denied"),
)


def implied_legacy_outcome(action_type: str) -> Optional[str]:
    """The outcome a legacy action type's name implies, or None."""
    for suffix, outcome in _IMPLIED_OUTCOME_SUFFIXES:
        if action_type.endswith(suffix):
            return outcome
    return None


def build_legacy_event(
    *,
    actor: str,
    action_type: str,
    target_type: str,
    target_id: str,
    details_json: Optional[str],
    occurred_at: Optional[str] = None,
    outcome: Optional[str] = None,
) -> AuditEvent:
    """Build a row for a pre-existing writer.

    The payload is stored restricted to the read-side legacy allowlist
    (``audit_log_query.restrict_legacy_details``): a field a reader may not
    see is never stored.  The event gets its uuid and ambient attribution.
    Without an explicit *outcome*, the outcome the action type's name
    implies is recorded (``*_success`` / ``*_failure`` / ``*_denied``),
    otherwise NULL.
    """
    # Imported here: audit_log_query imports this module.
    from code_indexer.server.services.audit_log_query import (
        restrict_legacy_details,
    )

    if outcome is None:
        outcome = implied_legacy_outcome(action_type)
    else:
        _require_outcome(outcome, str(action_type)[:_MAX_REPORTED_FIELD_NAME])
    details_json = restrict_legacy_details(action_type, details_json)
    ctx = current_audit_request_context()
    actor, impersonated_user = _impersonation_attribution(
        actor, current_mcp_principal()
    )
    return AuditEvent(
        event_uuid=str(uuid.uuid4()),
        occurred_at=occurred_at if occurred_at is not None else _now_iso(),
        actor=actor,
        actor_is_system=False,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        source=ctx.source if ctx is not None else SOURCE_SYSTEM,
        ip_address=ctx.client_ip if ctx is not None else None,
        correlation_id=_ambient_correlation_id(),
        node_id=_process_node_id,
        auth_method=ctx.auth_method if ctx is not None else None,
        details_json=details_json,
        impersonated_user=impersonated_user,
    )
