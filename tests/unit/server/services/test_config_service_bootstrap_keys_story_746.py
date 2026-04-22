"""Story #746 BOOTSTRAP_KEYS regression test — ensures fault_injection flags
survive ConfigService._strip_config_file_to_bootstrap()."""

from code_indexer.server.services.config_service import BOOTSTRAP_KEYS


def test_fault_injection_keys_in_bootstrap() -> None:
    """Story #746: both fault injection bootstrap keys must be in BOOTSTRAP_KEYS.

    Without this, ConfigService._strip_config_file_to_bootstrap() erases them
    from config.json at startup, and the harness never activates on a real
    server (E2E validation caught this bug).
    """
    assert "fault_injection_enabled" in BOOTSTRAP_KEYS
    assert "fault_injection_nonprod_ack" in BOOTSTRAP_KEYS


def test_all_server_config_fields_are_classified() -> None:
    """Regression: every ServerConfig field must be classified as bootstrap
    (in BOOTSTRAP_KEYS) or runtime (in _KNOWN_RUNTIME_KEYS), and the two sets
    must be disjoint with no stale entries in either set.

    Four-point partition audit:
    1. No unclassified fields — every real field appears in one of the two sets.
    2. No stale entries in _KNOWN_RUNTIME_KEYS — every name there is a real field.
    3. No stale entries in BOOTSTRAP_KEYS — every name there is a real field.
    4. No overlap — the two sets are disjoint.

    Prevents future silent omissions like the Story #746 wiring bug where a
    new field went unnoticed because no partition audit existed.
    """
    import dataclasses
    from code_indexer.server.utils.config_manager import ServerConfig

    # All ServerConfig fields that are runtime-DB-resident (not bootstrap-only).
    # Must be disjoint from BOOTSTRAP_KEYS and cover all non-bootstrap fields.
    _KNOWN_RUNTIME_KEYS: frozenset = frozenset(
        {
            "jwt_expiration_minutes",
            "service_display_name",
            "password_security",
            "resource_config",
            "cache_config",
            "oidc_provider_config",
            "telemetry_config",
            "search_limits_config",
            "file_content_limits_config",
            "golden_repos_config",
            "mcp_session_config",
            "health_config",
            "scip_config",
            "git_timeouts_config",
            "error_handling_config",
            "api_limits_config",
            "web_security_config",
            "indexing_config",
            "claude_integration_config",
            "repository_config",
            "multi_search_limits_config",
            "background_jobs_config",
            "content_limits_config",
            "self_monitoring_config",
            "langfuse_config",
            "mcp_self_registration",
            "wiki_config",
            "data_retention_config",
            "password_expiry_config",
            "rerank_config",
            "voyage_ai_sinbin",
            "cohere_sinbin",
            "query_orchestration",
            "clone_backend",
            "cow_daemon",
            "memory_retrieval_config",
        }
    )

    all_fields = {f.name for f in dataclasses.fields(ServerConfig)}
    classified = BOOTSTRAP_KEYS | _KNOWN_RUNTIME_KEYS

    # 1. No unclassified fields: every real field must appear in one of the two sets.
    unclassified = all_fields - classified
    assert not unclassified, (
        f"ServerConfig fields not classified as bootstrap or runtime: {unclassified}. "
        "Add new fields to BOOTSTRAP_KEYS (bootstrap-only) OR "
        "_KNOWN_RUNTIME_KEYS (DB-migrated)."
    )

    # 2. No stale entries in _KNOWN_RUNTIME_KEYS: every name must be a real field.
    stale_runtime = _KNOWN_RUNTIME_KEYS - all_fields
    assert not stale_runtime, (
        f"Names in _KNOWN_RUNTIME_KEYS that are not real ServerConfig fields: {stale_runtime}. "
        "Remove stale entries from _KNOWN_RUNTIME_KEYS."
    )

    # 3. No stale entries in BOOTSTRAP_KEYS: every name must be a real field.
    stale_bootstrap = BOOTSTRAP_KEYS - all_fields
    assert not stale_bootstrap, (
        f"Names in BOOTSTRAP_KEYS that are not real ServerConfig fields: {stale_bootstrap}. "
        "Remove stale entries from BOOTSTRAP_KEYS."
    )

    # 4. No overlap: the two sets must be disjoint (a field cannot be both bootstrap and runtime).
    overlap = BOOTSTRAP_KEYS & _KNOWN_RUNTIME_KEYS
    assert not overlap, (
        f"Fields appear in both BOOTSTRAP_KEYS and _KNOWN_RUNTIME_KEYS: {overlap}. "
        "Each field must belong to exactly one set."
    )
