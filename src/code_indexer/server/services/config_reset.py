"""The "Reset to Defaults" candidate: runtime settings only (Bug #2094).

A reset returns every RUNTIME setting to its default and keeps:

* every bootstrap key (``config.json``: storage mode, PostgreSQL DSN,
  cluster identity, clone backend, CoW daemon, pool sizes, ...), so a
  cluster node never comes back as a standalone SQLite server;
* the launch settings (:data:`LAUNCH_KEYS`), so a restart never rebinds the
  server to loopback or changes its port or worker count;
* every stored credential (:data:`STORED_CREDENTIAL_FIELDS`), so provider
  API keys and client secrets are never silently cleared;
* every security control (:data:`PRESERVED_SECURITY_FIELDS`): elevation
  (TOTP step-up) enforcement, session lifetimes, API-access restrictions,
  password policy, the SSO provider, SIEM delivery, the research
  assistant's network allowlist and the credential-use modes, so a reset
  can never weaken them;
* the deployment identity (:data:`PRESERVED_IDENTITY_FIELDS`): display and
  service names, deployment environment, and the external endpoints this
  deployment reports to or pulls from.

Settings next to a kept credential (enable flags, URLs, limits) are still
reset unless they are security controls themselves.  The candidate is built
from the committed live configuration inside the normal change path
(``ConfigService.apply_audited_change``), so the reset commits with the same
compare-and-set as every other change (Bug #2017).
"""

from __future__ import annotations

from typing import AbstractSet, Optional, Tuple

from code_indexer.server.utils.config_manager import ServerConfig

# Launch settings: runtime keys applied to the systemd launch config at the
# next restart (Story #1197).
LAUNCH_KEYS = frozenset({"host", "port", "workers", "log_level"})

# Field marker: keep the whole section.
WHOLE_SECTION = "*"

# (runtime section attribute, field) of every stored credential.  Bootstrap
# sections (ontap, cow_daemon) are kept whole with the bootstrap keys.
STORED_CREDENTIAL_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("claude_integration_config", "anthropic_api_key"),
    ("claude_integration_config", "voyageai_api_key"),
    ("claude_integration_config", "cohere_api_key"),
    ("claude_integration_config", "llm_creds_provider_api_key"),
    ("codex_integration_config", "api_key"),
    ("oidc_provider_config", "client_id"),
    ("oidc_provider_config", "client_secret"),
    ("langfuse_config", "public_key"),
    ("langfuse_config", "secret_key"),
    ("langfuse_config", "pull_projects"),
    ("mcp_self_registration", "client_id"),
    ("mcp_self_registration", "client_secret"),
)

# (runtime section attribute or None for a top-level key, field or
# WHOLE_SECTION) of every security control a reset keeps.
PRESERVED_SECURITY_FIELDS: Tuple[Tuple[Optional[str], str], ...] = (
    (None, "elevation_enforcement_enabled"),
    (None, "elevation_idle_timeout_seconds"),
    (None, "elevation_max_age_seconds"),
    (None, "jwt_expiration_minutes"),
    ("web_security_config", "web_session_timeout_seconds"),
    ("web_security_config", "admin_session_timeout_seconds"),
    ("web_security_config", "restrict_non_sso_to_web_ui"),
    ("web_security_config", "self_registration_enabled"),
    ("mcp_session_config", "session_ttl_seconds"),
    ("password_security", WHOLE_SECTION),
    ("password_expiry_config", WHOLE_SECTION),
    ("oidc_provider_config", WHOLE_SECTION),
    ("siem_delivery_config", WHOLE_SECTION),
    ("claude_integration_config", "ra_curl_allowed_cidrs"),
    # How the stored credentials are used (api key vs subscription).
    ("claude_integration_config", "claude_auth_mode"),
    ("codex_integration_config", "credential_mode"),
)

# Settings that look security-related but are deliberately reset (each is
# housekeeping or a gate whose default is the closed position).  Kept
# explicit so every auth/security field is a recorded decision.
RESETTABLE_SECURITY_FIELDS: Tuple[Tuple[Optional[str], str], ...] = (
    ("mcp_session_config", "cleanup_interval_seconds"),  # housekeeping cadence
    ("temporal_legacy_migration_config", "cleanup_authorized"),  # default OFF
    (None, "research_session_retention_days"),  # data retention
)

# Deployment identity: WHICH deployment this is (names, environment, the
# external endpoints it reports to or pulls from), never a tunable.
# The OIDC issuer and the SIEM destination are kept with their whole
# sections (PRESERVED_SECURITY_FIELDS).
PRESERVED_IDENTITY_FIELDS: Tuple[Tuple[Optional[str], str], ...] = (
    (None, "service_display_name"),
    ("telemetry_config", "service_name"),
    ("telemetry_config", "deployment_environment"),
    ("telemetry_config", "collector_endpoint"),
    ("claude_integration_config", "llm_creds_provider_url"),
    ("claude_integration_config", "llm_creds_provider_consumer_id"),
    ("codex_integration_config", "lcp_url"),
    ("codex_integration_config", "lcp_vendor"),
    ("langfuse_config", "host"),
    ("langfuse_config", "pull_host"),
    ("cidx_meta_backup_config", "remote_url"),
)

# Behaviour tunables whose names look identity-like; deliberately reset.
RESETTABLE_IDENTITY_FIELDS: Tuple[Tuple[Optional[str], str], ...] = (
    ("multi_search_limits_config", "omni_default_aggregation_mode"),
    ("query_embedding_cache_config", "query_embedding_cache_voyage_mode"),
    ("query_embedding_cache_config", "query_embedding_cache_cohere_mode"),
    (None, "pace_maker_mode"),
)


def _carry(
    live: ServerConfig, target: ServerConfig, section: Optional[str], field_name: str
) -> None:
    """Move *live*'s value of (section, field) onto *target*."""
    if section is None:
        setattr(target, field_name, getattr(live, field_name))
        return
    live_section = getattr(live, section)
    if live_section is None:
        return
    if field_name == WHOLE_SECTION:
        setattr(target, section, live_section)
        return
    target_section = getattr(target, section)
    if target_section is None:
        raise RuntimeError(
            f"default ServerConfig has no {section}; cannot keep {field_name}"
        )
    setattr(target_section, field_name, getattr(live_section, field_name))


def build_reset_candidate(
    live: ServerConfig, defaults: ServerConfig, bootstrap_keys: AbstractSet[str]
) -> ServerConfig:
    """*defaults* with *live*'s bootstrap keys, launch keys, credentials,
    security controls and deployment identity.

    *live* must be a private copy (the change path's candidate): its values
    are moved into *defaults*, not copied.
    """
    for key in sorted(set(bootstrap_keys) | LAUNCH_KEYS):
        setattr(defaults, key, getattr(live, key))
    for section, field_name in STORED_CREDENTIAL_FIELDS:
        _carry(live, defaults, section, field_name)
    for maybe_section, field_name in PRESERVED_SECURITY_FIELDS:
        _carry(live, defaults, maybe_section, field_name)
    for maybe_section, field_name in PRESERVED_IDENTITY_FIELDS:
        _carry(live, defaults, maybe_section, field_name)
    return defaults
