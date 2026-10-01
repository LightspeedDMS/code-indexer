"""Audit rows for configuration changes: key diff, value allowlist, recording.

A configuration row names the keys that changed as dotted SCHEMA field
paths (``golden_repos_config.refresh_interval_seconds``), derived by walking
the config dataclasses -- never from request input, so a key name is always
a field name.  Values are recorded only for keys in
:data:`NON_SECRET_SCALAR_CONFIG_KEYS`, an explicit ALLOWLIST of int, bool and
enum settings that are safe and useful to an investigator.  Every other key
(secrets, URLs, hosts, e-mail addresses, free text, lists) is recorded by
name only; a key missing from the allowlist is value-free by default.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from code_indexer.server.services.audit_outcome import conforming_details
from code_indexer.server.services.audit_outcome import record_outcome

CONFIG_CHANGED = "config_changed"
PROVIDER_KEY_ACTIONS: FrozenSet[str] = frozenset(
    {"provider_api_key_set", "provider_api_key_cleared"}
)
CONFIG_ACTIONS: FrozenSet[str] = frozenset({CONFIG_CHANGED}) | PROVIDER_KEY_ACTIONS
CHANGE_KINDS: FrozenSet[str] = frozenset({"update", "reset_to_defaults"})

_MAX_KEYS = 200

NON_SECRET_SCALAR_CONFIG_KEYS: FrozenSet[str] = frozenset(
    {
        # server
        "workers",
        "log_level",
        "jwt_expiration_minutes",
        # TOTP step-up elevation
        "elevation_enforcement_enabled",
        "elevation_idle_timeout_seconds",
        "elevation_max_age_seconds",
        # golden repositories
        "golden_repos_config.refresh_interval_seconds",
        "golden_repos_config.externally_managed",
        "golden_repos_config.analysis_model",
        # data retention
        "data_retention_config.operational_logs_retention_hours",
        "data_retention_config.audit_logs_retention_hours",
        "data_retention_config.sync_jobs_retention_hours",
        "data_retention_config.dep_map_history_retention_hours",
        "data_retention_config.background_jobs_retention_hours",
        "data_retention_config.cleanup_interval_hours",
        # web security toggles
        "web_security_config.web_session_timeout_seconds",
        "web_security_config.admin_session_timeout_seconds",
        "web_security_config.restrict_non_sso_to_web_ui",
        "web_security_config.self_registration_enabled",
        # password policy
        "password_security.min_length",
        "password_security.max_length",
        "password_security.required_char_classes",
        "password_security.min_entropy_bits",
        "password_security.check_common_passwords",
        "password_security.check_personal_info",
        "password_security.check_keyboard_patterns",
        "password_security.check_sequential_chars",
        # SSO toggles (never issuer, client id or secret)
        "oidc_provider_config.enabled",
        "oidc_provider_config.use_pkce",
        "oidc_provider_config.require_email_verification",
        "oidc_provider_config.enable_jit_provisioning",
        "oidc_provider_config.default_role",
        # self-monitoring
        "self_monitoring_config.enabled",
        "self_monitoring_config.cadence_minutes",
        "self_monitoring_config.model",
        # cidx-meta backup (never the remote URL)
        "cidx_meta_backup_config.enabled",
        # langfuse pull (never hosts or project keys)
        "langfuse_config.pull_enabled",
        "langfuse_config.pull_sync_interval_seconds",
        "langfuse_config.pull_trace_age_days",
        "langfuse_config.pull_max_concurrent_observations",
        # indexing parallelism and gates
        "indexing_config.voyage_ai_parallel_requests",
        "indexing_config.cohere_parallel_requests",
        "indexing_config.temporal_parallel_requests",
        "indexing_config.temporal_all_branches_enabled",
        # LLM credential mode (never the provider URL or key)
        "claude_integration_config.claude_auth_mode",
        # SIEM delivery (project, location, instance, key path and harness
        # endpoint are recorded by NAME only)
        "siem_delivery_config.enabled",
        "siem_delivery_config.api_version",
        "siem_delivery_config.max_batch_events",
        "siem_delivery_config.region",
    }
)


def _field_paths(prefix: str, before: Any, after: Any, out: List[str]) -> None:
    if (
        dataclasses.is_dataclass(before)
        and dataclasses.is_dataclass(after)
        and type(before) is type(after)
    ):
        for field in dataclasses.fields(before):
            _field_paths(
                f"{prefix}{field.name}.",
                getattr(before, field.name),
                getattr(after, field.name),
                out,
            )
        return
    if before != after:
        out.append(prefix[:-1])


def diff_config_keys(before: Any, after: Any) -> List[str]:
    """Dotted schema field paths whose values differ, sorted."""
    changed: List[str] = []
    _field_paths("", before, after, changed)
    return sorted(changed)


def _value_of(config: Any, dotted: str) -> Any:
    value = config
    for part in dotted.split("."):
        value = getattr(value, part, None)
    return value


def _allowlisted_values(before: Any, after: Any, keys: List[str]) -> Dict[str, Any]:
    return {
        key: [_value_of(before, key), _value_of(after, key)]
        for key in keys
        if key in NON_SECRET_SCALAR_CONFIG_KEYS
    }


def _safe_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only pairs the allowlisted value type accepts (scalar tokens)."""
    return {
        key: pair
        for key, pair in values.items()
        if conforming_details(CONFIG_CHANGED, values={key: pair})
    }


def config_details(
    action_type: str,
    *,
    change_kind: str,
    before: Any,
    after: Any,
    outcome: str,
    provider: Optional[str],
) -> Dict[str, Any]:
    """The allowlisted ``details`` of one configuration row."""
    if action_type in PROVIDER_KEY_ACTIONS:
        return conforming_details(action_type, provider=provider)
    keys = diff_config_keys(before, after)[:_MAX_KEYS] if after is not None else []
    if outcome != "success":
        return conforming_details(
            CONFIG_CHANGED, change_kind=change_kind, attempted_keys=keys
        )
    return conforming_details(
        CONFIG_CHANGED,
        change_kind=change_kind,
        changed_keys=keys,
        values=_safe_values(_allowlisted_values(before, after, keys)),
    )


def record_config_outcome(
    *,
    actor: str,
    action_type: str,
    target_id: str,
    change_kind: str,
    before: Any,
    after: Any,
    outcome: str,
    provider: Optional[str] = None,
) -> None:
    """Record one configuration row (never raises).

    A SIEM delivery configuration change is captured for SIEM delivery from
    the COMMITTED before/after configuration (an explicit destination and
    boundary kind), whatever the scheduler's snapshot says.
    """
    details = config_details(
        action_type,
        change_kind=change_kind,
        before=before,
        after=after,
        outcome=outcome,
        provider=provider,
    )
    kwargs: Dict[str, Any] = {}
    if action_type == CONFIG_CHANGED:
        from code_indexer.server.services.siem_delivery.boundary import (
            siem_boundary_target,
        )

        is_siem, target = siem_boundary_target(
            before,
            after,
            target_id=target_id,
            change_kind=change_kind,
            details=details,
            outcome=outcome,
        )
        if is_siem:
            kwargs["siem_destination"] = target
    record_outcome(
        actor=actor,
        action_type=action_type,
        target_type="config",
        target_id=target_id,
        outcome=outcome,
        details=details,
        **kwargs,
    )


def _git_service_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[str]:
    return sorted(
        f"git_service.{name}"
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )


def apply_git_settings_change(
    config_manager: Any, *, default_committer_email: Optional[str], actor: str
) -> Any:
    """Set the server's default committer e-mail; record ``git_settings_changed``.

    Records the changed key names only: committer identities are personal
    data and never appear as values.  Returns the saved config.
    """
    before: Dict[str, Any] = {}
    after: Dict[str, Any] = {}
    try:
        config = config_manager.load()
        before = config.git_service.model_dump()
        config.git_service.default_committer_email = default_committer_email
        after = config.git_service.model_dump()
        config_manager.save(config)
    except Exception:
        _record_git(actor, "failure", _git_service_keys(before, after or before))
        raise
    _record_git(actor, "success", _git_service_keys(before, after))
    return config


def _record_git(actor: str, outcome: str, keys: List[str]) -> None:
    record_outcome(
        actor=actor,
        action_type="git_settings_changed",
        target_type="config",
        target_id="git_service",
        outcome=outcome,
        details=conforming_details("git_settings_changed", changed_keys=keys),
    )
