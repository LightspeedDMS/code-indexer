"""Which audit events SIEM delivery captures.

The pilot scope is a code constant; there is no scope setting.  Self-report
rows (the SIEM feature's own configuration changes and admin actions) are
captured only with an explicit destination supplied by their emitter.
"""

from __future__ import annotations

import json
from typing import Any, FrozenSet, Iterable

from code_indexer.server.services.audit_events import ALL_TARGETS_MARKER, AuditEvent

PILOT_ACTION_TYPES: FrozenSet[str] = frozenset(
    {
        # logins (every door)
        "authentication_success",
        "authentication_failure",
        # MFA changes
        "mfa_activated",
        "mfa_disabled",
        "mfa_recovery_codes_regenerated",
        "mfa_secret_regenerated_cross_user",
        # group and permission changes
        "user_role_changed",
        "user_group_assign",
        "user_group_change",
        "repo_access_grant",
        "repo_access_revoke",
        # admin actions
        "user_created",
        "user_deleted",
        "user_password_reset_by_admin",
        "mcp_credential_created",
        "mcp_credential_revoked",
        "api_key_created",
        "ssh_key_host_assigned",
        "elevation_granted",
        "elevation_failed",
        "impersonation_set",
        "impersonation_cleared",
        "impersonation_denied",
    }
)

SIEM_SELF_REPORT_TYPES: FrozenSet[str] = frozenset(
    {
        "siem_canary_sent",
        "siem_canary_visibility_confirmed",
        "siem_quarantine_requeued",
        "siem_delivery_resumed",
        "siem_batch_acknowledged",
        "siem_batch_rebatched",
        "siem_destination_retargeted",
        "siem_destination_abandoned",
        "siem_credential_changed",
    }
)

CONFIG_CHANGED = "config_changed"
SIEM_CONFIG_SECTION = "siem_delivery"
# Dotted schema path prefix of every key of the SIEM configuration section
# (the ServerConfig field name).
SIEM_CONFIG_KEY_PREFIX = "siem_delivery_config."


def _details(event: AuditEvent) -> Any:
    if not event.details_json:
        return {}
    try:
        decoded = json.loads(event.details_json)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        return [k for k in value if isinstance(k, str)]
    return []


def is_siem_config_change(event: AuditEvent) -> bool:
    """A ``config_changed`` row for the SIEM section (or a multi-section
    update/reset that touched a SIEM key).  ``config_changed`` carries the
    section as ``target_id``, never as a details field."""
    if event.action_type != CONFIG_CHANGED:
        return False
    if event.target_id == SIEM_CONFIG_SECTION:
        return True
    if event.target_id != ALL_TARGETS_MARKER:
        return False
    details = _details(event)
    keys = list(_keys(details.get("changed_keys"))) + list(
        _keys(details.get("attempted_keys"))
    )
    return any(key.startswith(SIEM_CONFIG_KEY_PREFIX) for key in keys)


def is_self_report(event: AuditEvent) -> bool:
    return event.action_type in SIEM_SELF_REPORT_TYPES or is_siem_config_change(event)
