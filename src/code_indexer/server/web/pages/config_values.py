"""
Configuration page data: current values, section validation, restart-required fields.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from typing import Any, List, Optional, Union
from ...utils.host_validation import normalize_server_host

from ._shared import (
    logger,
)

# Story #1157: Result cache for background discovery jobs.
# Maps job_id -> result_holder dict. Consumed read-once via .pop().

# Story #885 Phase 5b (A7b): minimum gap between outer and shell lifecycle timeouts.
# outer_timeout_seconds must be >= shell_timeout_seconds + LIFECYCLE_OUTER_TIMEOUT_GRACE_SECONDS
# so the Python-level watchdog always outlives the shell kill budget.
LIFECYCLE_OUTER_TIMEOUT_GRACE_SECONDS = 30

# Story #198: Settings that require server restart
# These settings are read during server startup (singleton init, thread pool creation)
# and changing them has no effect until the server is restarted. A feature's
# keys are not listed here: its ConfigKey.restart_required carries the note
# on the Feature Settings block.
RESTART_REQUIRED_FIELDS = [
    "host",  # Server binding address (read at uvicorn startup)
    "port",  # Server binding port (read at uvicorn startup)
    "telemetry_enabled",  # Telemetry integration (read at server startup)
    "langfuse_enabled",  # Langfuse integration (read at server startup)
    "max_concurrent_claude_cli",  # ClaudeCliManager thread pool size (singleton init)
    "multi_search_max_workers",  # Multi-search thread pool size (singleton init)
    "scip_multi_max_workers",  # SCIP multi-repo thread pool size (singleton init)
    "max_concurrent_background_jobs",  # BackgroundJobManager thread pool size (singleton init)
    "subprocess_max_workers",  # Subprocess executor pool size (singleton init)
    "temporal_lane_concurrency",  # Story #1400: temporal-lane worker pool size (singleton init)
    "workers",  # Uvicorn worker count — read at uvicorn startup; applied by auto-updater on next deploy
    "log_level",  # Log level — read at uvicorn startup; Story #1195 AC1
    # Bug #1399 item 6: MODERATE-classified fields captured once at server
    # startup (singleton/manager construction, module globals, or
    # middleware wiring) and never re-consulted afterward. A Web UI change
    # has no effect until the server is restarted; these entries only add
    # the operator-visible hint -- they do NOT implement hot-reload.
    # password_security.* — captured once at UserManager.__init__ /
    # PasswordStrengthValidator construction.
    "min_length",
    "max_length",
    "required_char_classes",
    "min_entropy_bits",
    # server.jwt_expiration_minutes — captured once at service_init.py JWT
    # manager construction.
    "jwt_expiration_minutes",
    # health.* — written once to module globals at health_service.py
    # construction; never re-invoked.
    "memory_warning_threshold_percent",
    "memory_critical_threshold_percent",
    "disk_warning_threshold_percent",
    "disk_critical_threshold_percent",
    "cpu_sustained_threshold_percent",
    # error_handling.* — baked into middleware at startup (app_wiring.py).
    "max_retry_attempts",
    "base_retry_delay_seconds",
    "max_retry_delay_seconds",
    # telemetry.* sub-fields — captured once into TracerProvider/
    # MeterProvider/exporters at startup. telemetry_enabled (the kill
    # switch) already has its own hint above; these are the sub-fields.
    "collector_endpoint",
    "collector_protocol",
    "service_name",
    "export_traces",
    "export_metrics",
    "machine_metrics_enabled",
    "machine_metrics_interval_seconds",
    "deployment_environment",
    # telemetry.trace_sample_rate -- Story #1676 AC4: captured once into
    # the ParentBased(TraceIdRatioBased(...)) sampler at TracerProvider
    # construction, same lifecycle as its telemetry.* siblings above.
    "trace_sample_rate",
    # telemetry.export_logs -- Story #1676 AC3: captured once into the
    # LoggerProvider + context-aware log bridge handler registration at
    # TelemetryManager construction, same lifecycle as its telemetry.*
    # siblings above.
    "export_logs",
    # langfuse.* TRACING-CREDENTIAL fields only (frozen into an eagerly
    # created client at startup). Pull-sync fields (pull_enabled, pull_host,
    # pull_trace_age_days, pull_max_concurrent_observations, pull_projects)
    # are correctly re-read live every sync cycle and are NOT listed here.
    "public_key",
    "secret_key",
    "auto_trace_enabled",
    # codex_integration.* PROVISIONING fields only (provisioned once at
    # boot). `enabled`/`codex_weight` are correctly read live per dispatched
    # dep-map job and are NOT listed here.
    "credential_mode",
    "api_key",
    "lcp_url",
    "lcp_vendor",
    # server.coalesce_k_min / coalesce_k_max — construction-scoped seed for
    # the concurrency governor.
    "coalesce_k_min",
    "coalesce_k_max",
    # golden_repos.analysis_model — SCHEDULER path only (captured once at
    # startup). The interactive Research Assistant path reads it live and
    # is unaffected; the flat list has no per-consumer split.
    "analysis_model",
    # multi_search timeout/limit fields — module singletons built once.
    # multi_search_max_workers / scip_multi_max_workers already have hints
    # above; these are the sibling timeout/limit fields that do not.
    "multi_search_timeout_seconds",
    "scip_multi_timeout_seconds",
    # claude_cli.scheduled_catchup_* — captured once in
    # ScheduledCatchupService.__init__; loop uses the frozen value.
    "scheduled_catchup_enabled",
    "scheduled_catchup_interval_minutes",
    # cache payload_* fields — snapshotted once at lifespan.py startup.
    "payload_preview_size_chars",
    "payload_max_fetch_size_chars",
    "payload_cache_ttl_seconds",
    "payload_cleanup_interval_seconds",
    # issue-66121 trusted_gateway.* — the gateway authenticator is built once at startup
    # (gateway_auth_wiring.build_gateway_authenticator); no live reload. Only the field
    # names UNIQUE to this section are listed (the hint matches by bare field name, so
    # shared names like "enabled"/"email_claim" would wrongly flag other sections too;
    # the section description carries the restart caveat for those).
    "issuer",
    "jwks_uri",
    "audience",
    "leeway_seconds",
]


def _siem_trusted_ca_rows(section: Any) -> Any:
    from ...services.siem_delivery.config_view import trusted_ca_rows

    return trusted_ca_rows(section)


# Story #683 AC5: immutable tuple used by POST /config/{section} handler.
# "job_queue" removed — it was a fabricated section (SyncJobConfig defaults), not persisted.
_VALID_CONFIG_SECTIONS = (
    "server",
    "cache",
    "timeouts",
    "password_security",
    "totp_elevation",  # Bug #943: TOTP step-up elevation runtime config
    "oidc",
    "trusted_gateway",  # issue-66121: trusted the API gateway RS256 bearer issuer
    "telemetry",
    "langfuse",
    "search_limits",
    "golden_repos",
    "mcp_session",
    "health",
    "scip",
    # Story #3 - Phase 2: P2 settings (AC12-AC26)
    "git_timeouts",
    "error_handling",
    "api_limits",
    "web_security",
    # Story #683 AC3: "auth" section removed (AuthConfig deleted).
    # Story #25 - Multi-search limits configuration
    "multi_search",
    # Story #26 - Background jobs configuration
    "background_jobs",
    # Issue #1753: "omni_search" removed -- zero template consumers (dead
    # code confirmed by Issue #1750's investigation and re-verified while
    # fixing #1753). Omni-search's real, live-consumed settings live under
    # the "multi_search" section above (Story #29 merged them there).
    # Story #32 - Unified content limits configuration
    "content_limits",
    # Story #190 - Claude CLI configuration
    "claude_cli",
    # Story #223 - Indexing configuration
    "indexing",
    # Story #400 - Data retention configuration
    "data_retention",
    # Story #967 - Activated repository reaper configuration
    "activated_reaper",
    # Story #1397 - HNSW orphan-repair sweep operating-hours window config
    "hnsw_orphan_sweep",
    # SIEM delivery (Google SecOps)
    "siem_delivery",
    # Issue #1530 - Indexing-subprocess activity watchdog configuration
    "indexing_watchdog",
    # Story #1458 (Epic #1454) - Fleet migration scheduler configuration
    "fleet_migration",
    # Issue #1548 - Legacy temporal shard relocation configuration
    "temporal_legacy_migration",
    # Issue #1546 Phase 2 - DB-backed alias lock rollout gate
    "alias_lock",
    # Content seam step 5a - aliases and write-mode markers in the registry
    "control_plane_metadata",
    # Issue #1398 - Query & search timeouts configuration
    "search_timeouts",
    # Story #652 - Reranking configuration
    "rerank",
    # Story #885 - Lifecycle analysis timeouts
    "lifecycle_analysis",
    # Story #844 - Codex CLI integration configuration
    "codex_integration",
    "cidx_meta_backup",
    # Story #997 - Pace-maker pacing-only enforcement
    "pace_maker",
    # Story #1107 S3 - Query embedding cache LRU cap + Web UI config section
    "query_embedding_cache",
    # Story #1159 - Search event log data retention config
    "search_event_log",
    # Issue #1160 - Export retention config
    "export",
    # Story #1418 Phase 3 - Embedding & reranker call tracking config
    "embedding_stats",
    # Story #1404 - Global temporal indexing floor date configuration
    "temporal_indexing",
)


def _get_qec_total_entries() -> int:
    """Return the live total row count from the QueryEmbeddingCache, or 0.

    Used by _get_current_config to surface the total-cached-entries readout in
    the Web UI config page (Story #1107 S3 finding B3).  Fail-open: returns 0
    when the cache is not wired (CLI / pre-lifespan) or on any backend error.
    """
    try:
        from ...services.governed_call import get_query_embedding_cache

        cache = get_query_embedding_cache()
        if cache is None:
            return 0
        return int(cache.total_entries())
    except Exception:
        logger.warning(
            "query_embedding_cache: failed to read total_entries for Web UI (fail-open)",
            exc_info=True,
        )
        return 0


def _get_current_config() -> dict:
    """Get current configuration from ConfigService (persisted to ~/.cidx-server/config.json)."""
    from ...services.config_service import get_config_service
    from ...utils.config_manager import (
        OIDCProviderConfig,
        TrustedGatewayConfig,
        TelemetryConfig,
        LangfuseConfig,
        SearchLimitsConfig,
        GoldenReposConfig,
        # Story #3 - Phase 2: P0/P1 settings (AC2-AC11)
        McpSessionConfig,
        HealthConfig,
        ScipConfig,
        # Story #3 - Phase 2: P2 settings (AC12-AC26)
        GitTimeoutsConfig,
        ErrorHandlingConfig,
        ApiLimitsConfig,
        WebSecurityConfig,
        # Story #683 AC3: AuthConfig import removed (deleted).
        # Story #32 - Unified content limits configuration
        ContentLimitsConfig,
        # Story #223 - Indexing configuration
        IndexingConfig,
        # Story #400 - Data retention configuration
        DataRetentionConfig,
        # Story #967 - Activated repository reaper configuration
        ActivatedReaperConfig,
        # Story #1397 - HNSW orphan-repair sweep operating-hours window config
        HNSWOrphanRepairSweepConfig,
        SiemDeliveryConfig,
        IndexingWatchdogConfig,
        # Story #1458 (Epic #1454) - Fleet migration scheduler configuration
        FleetMigrationConfig,
        # Issue #1548 - Legacy temporal shard relocation configuration
        TemporalLegacyMigrationConfig,
        # Issue #1546 Phase 2 - DB-backed alias lock rollout gate
        AliasLockConfig,
        # Content seam step 5a - aliases and write-mode markers in the registry
        ControlPlaneMetadataConfig,
        # Issue #1398 - Query & search timeouts configuration
        SearchTimeoutsConfig,
        # Story #1418 Phase 3 - Embedding & reranker call tracking config
        EmbeddingStatsConfig,
        # Story #1404 - Global temporal indexing floor date configuration
        TemporalIndexingConfig,
        # Story #652 - Reranking configuration
        RerankConfig,
        # Story #885 - Lifecycle analysis configuration
        LifecycleAnalysisConfig,
        # Story #844 - Codex CLI integration configuration
        CodexIntegrationConfig,
        # Story #1107 S3 - Query embedding cache configuration
        QueryEmbeddingCacheConfig,
    )
    from dataclasses import asdict

    config_service = get_config_service()
    settings = config_service.get_all_settings()

    # Ensure OIDC config has all required fields with defaults
    oidc_config = settings.get("oidc")
    if not oidc_config:
        # Provide defaults if OIDC config is missing
        oidc_config = asdict(OIDCProviderConfig())

    # issue-66121: trusted the API gateway bearer config (defaults if missing)
    trusted_gateway_config = settings.get("trusted_gateway")
    if not trusted_gateway_config:
        trusted_gateway_config = asdict(TrustedGatewayConfig())

    # Ensure telemetry config has all required fields with defaults
    telemetry_config = settings.get("telemetry")
    if not telemetry_config:
        # Provide defaults if telemetry config is missing
        telemetry_config = asdict(TelemetryConfig())

    # Ensure langfuse config has all required fields with defaults
    langfuse_config = settings.get("langfuse")
    if not langfuse_config:
        # Provide defaults if langfuse config is missing
        langfuse_config = asdict(LangfuseConfig())

    # Get search_limits, file_content_limits, and golden_repos config (Story #3)
    # Provide defaults if config sections are missing (backward compatibility)
    search_limits_config = settings.get("search_limits")
    if not search_limits_config:
        search_limits_config = asdict(SearchLimitsConfig())

    golden_repos_config = settings.get("golden_repos")
    if not golden_repos_config:
        golden_repos_config = asdict(GoldenReposConfig())
    else:
        # Merge defaults for keys added after initial config creation
        for key, value in asdict(GoldenReposConfig()).items():
            golden_repos_config.setdefault(key, value)

    # Story #3 - Phase 2: P0/P1 settings (AC2-AC11)
    # Get mcp_session, health, scip config with defaults for backward compatibility
    mcp_session_config = settings.get("mcp_session")
    if not mcp_session_config:
        mcp_session_config = asdict(McpSessionConfig())

    health_config = settings.get("health")
    if not health_config:
        health_config = asdict(HealthConfig())

    scip_config = settings.get("scip")
    if not scip_config:
        scip_config = asdict(ScipConfig())

    # Story #3 - Phase 2: P2 settings (AC12-AC26)
    # Get git_timeouts, error_handling, api_limits, web_security config with defaults
    git_timeouts_config = settings.get("git_timeouts")
    if not git_timeouts_config:
        git_timeouts_config = asdict(GitTimeoutsConfig())

    error_handling_config = settings.get("error_handling")
    if not error_handling_config:
        error_handling_config = asdict(ErrorHandlingConfig())

    api_limits_config = settings.get("api_limits")
    if not api_limits_config:
        api_limits_config = asdict(ApiLimitsConfig())

    web_security_config = settings.get("web_security")
    if not web_security_config:
        web_security_config = asdict(WebSecurityConfig())

    # Story #683 AC3: auth_config section removed (AuthConfig deleted).

    # Story #20: Provider API Keys (Anthropic/VoyageAI)
    # Bug #153: Provide defaults when claude_cli is None or empty dict
    claude_cli_raw = settings.get("claude_cli")
    if not claude_cli_raw:
        # If None or empty dict, use full defaults
        claude_cli_config = {
            "max_concurrent_claude_cli": 3,
            "description_refresh_interval_hours": 24,
            "description_refresh_enabled": False,
            "research_assistant_timeout_seconds": 300,
        }
    else:
        # If dict exists, merge with defaults (preserve existing values)
        claude_cli_config = {
            "max_concurrent_claude_cli": claude_cli_raw.get(
                "max_concurrent_claude_cli", 3
            ),
            "description_refresh_interval_hours": claude_cli_raw.get(
                "description_refresh_interval_hours", 24
            ),
            "description_refresh_enabled": claude_cli_raw.get(
                "description_refresh_enabled", False
            ),
            "research_assistant_timeout_seconds": claude_cli_raw.get(
                "research_assistant_timeout_seconds", 300
            ),
        }
        # Preserve additional keys like API keys
        for key, value in claude_cli_raw.items():
            if key not in claude_cli_config:
                claude_cli_config[key] = value

    provider_api_keys_config = {
        "anthropic_configured": bool(claude_cli_config.get("anthropic_api_key")),
        "voyageai_configured": bool(claude_cli_config.get("voyageai_api_key")),
        "cohere_configured": bool(claude_cli_config.get("cohere_api_key")),
    }

    # Convert to template-friendly format
    return {
        "server": settings["server"],
        "cache": settings["cache"],
        "timeouts": settings["timeouts"],
        "password_security": settings["password_security"],
        # Bug #943: TOTP step-up elevation runtime config
        "totp_elevation": settings.get(
            "totp_elevation",
            {
                "elevation_enforcement_enabled": True,
                "elevation_idle_timeout_seconds": 300,
                "elevation_max_age_seconds": 1800,
            },
        ),
        # Story #997: Pace-maker mode enforcement
        "pace_maker": settings.get("pace_maker", {"pace_maker_mode": "disabled"}),
        "oidc": oidc_config,
        "trusted_gateway": trusted_gateway_config,
        "telemetry": telemetry_config,
        "langfuse": langfuse_config,
        "search_limits": search_limits_config,
        "golden_repos": golden_repos_config,
        # Story #3 - Phase 2: P0/P1 settings
        "mcp_session": mcp_session_config,
        "health": health_config,
        "scip": scip_config,
        # Story #3 - Phase 2: P2 settings (AC12-AC26)
        "git_timeouts": git_timeouts_config,
        "error_handling": error_handling_config,
        "api_limits": api_limits_config,
        "web_security": web_security_config,
        # Story #683 AC3: "auth" key removed (AuthConfig deleted).
        # Story #20: Provider API Keys
        "provider_api_keys": provider_api_keys_config,
        # Claude CLI integration settings (for Claude Integration config section)
        "claude_cli": claude_cli_config,
        # Story #25: Multi-search limits configuration
        "multi_search": settings.get("multi_search", {}),
        # Story #26: Background jobs configuration
        "background_jobs": settings.get("background_jobs", {}),
        # Issue #1753: "omni_search" key removed -- zero template
        # consumers (dead code confirmed by Issue #1750's investigation).
        # Story #32: Unified content limits configuration
        "content_limits": settings.get("content_limits", asdict(ContentLimitsConfig())),
        # Story #223: Indexing configuration
        "indexing": settings.get("indexing", asdict(IndexingConfig())),
        # Story #400: Data retention configuration
        "data_retention": settings.get("data_retention", asdict(DataRetentionConfig())),
        # Story #967: Activated repository reaper configuration
        "activated_reaper": settings.get(
            "activated_reaper", asdict(ActivatedReaperConfig())
        ),
        # Story #1397: HNSW orphan-repair sweep operating-hours window config
        "hnsw_orphan_sweep": settings.get(
            "hnsw_orphan_sweep", asdict(HNSWOrphanRepairSweepConfig())
        ),
        "siem_delivery": settings.get("siem_delivery", asdict(SiemDeliveryConfig())),
        # SIEM delivery additional trusted CA: public identity rows (no I/O).
        "siem_delivery_trusted_ca": _siem_trusted_ca_rows(
            settings.get("siem_delivery", asdict(SiemDeliveryConfig()))
        ),
        # Issue #1530: Indexing-subprocess activity watchdog configuration
        "indexing_watchdog": settings.get(
            "indexing_watchdog", asdict(IndexingWatchdogConfig())
        ),
        # Story #1458 (Epic #1454): Fleet migration scheduler configuration
        "fleet_migration": settings.get(
            "fleet_migration", asdict(FleetMigrationConfig())
        ),
        # Issue #1548: Legacy temporal shard relocation configuration
        "temporal_legacy_migration": settings.get(
            "temporal_legacy_migration", asdict(TemporalLegacyMigrationConfig())
        ),
        # Issue #1546 Phase 2: DB-backed alias lock rollout gate
        "alias_lock": settings.get("alias_lock", asdict(AliasLockConfig())),
        # Content seam step 5a: aliases and write-mode markers in the registry
        "control_plane_metadata": settings.get(
            "control_plane_metadata", asdict(ControlPlaneMetadataConfig())
        ),
        # Issue #1398: Query & search timeouts configuration
        "search_timeouts": settings.get(
            "search_timeouts", asdict(SearchTimeoutsConfig())
        ),
        # Story #1418 Phase 3: Embedding & reranker call tracking config
        "embedding_stats": settings.get(
            "embedding_stats", asdict(EmbeddingStatsConfig())
        ),
        # Story #1404: Global temporal indexing floor date configuration
        "temporal_indexing": settings.get(
            "temporal_indexing", asdict(TemporalIndexingConfig())
        ),
        # Story #652: Reranking configuration
        "rerank": settings.get("rerank", asdict(RerankConfig())),
        # Story #1107 S3: Query embedding cache configuration
        # total_cached_entries is injected as a live read-only readout (B3).
        "query_embedding_cache": {
            **settings.get(
                "query_embedding_cache", asdict(QueryEmbeddingCacheConfig())
            ),
            "total_cached_entries": _get_qec_total_entries(),
        },
        # Story #885: Lifecycle analysis configuration
        "lifecycle_analysis": settings.get(
            "lifecycle_analysis", asdict(LifecycleAnalysisConfig())
        ),
        # Story #844: Codex CLI integration configuration
        "codex_integration": settings.get(
            "codex_integration", asdict(CodexIntegrationConfig())
        ),
        "cidx_meta_backup": settings.get(
            "cidx_meta_backup", {"enabled": False, "remote_url": ""}
        ),
        # Bug #1179: search_event_log section was missing, causing Jinja
        # UndefinedError (HTTP 500) on every admin Config page render.
        # Both fields are rendered inside the same <details id="section-search_event_log">
        # block (template lines 2440/2445/2464/2471), so they are merged here
        # into one dict under the "search_event_log" key.
        # search_event_log_retention_days comes from settings["search_event_log"]
        # (Issue #1159); export_retention_days comes from settings["export"]
        # (Issue #1160).
        "search_event_log": {
            **settings.get(
                "search_event_log",
                {"search_event_log_retention_days": 90},
            ),
            **{
                "export_retention_days": settings.get("export", {}).get(
                    "export_retention_days", 30
                )
            },
        },
    }


def _validate_config_section(
    section: str, data: dict, *, persisted_host: Optional[str] = None
) -> Optional[str]:
    """Validate configuration for a section, return error message if invalid.

    persisted_host: the currently persisted server host. A submitted host
    equal to it (after stripping) is not a host change and is not
    re-validated, so a host persisted before host validation existed never
    blocks a server-section save that leaves it untouched.
    """
    if section == "server":
        # Validate host - cannot be empty
        host = data.get("host")
        if host is not None:
            host_str = str(host).strip()
            if not host_str:
                return "Host cannot be empty"
            try:
                normalize_server_host(host_str, current_host=persisted_host)
            except ValueError:
                return "Host must be a valid IPv4/IPv6 address or hostname"

        port = data.get("port")
        if port is not None:
            try:
                port_int = int(port)
                if port_int < 1 or port_int > 65535:
                    return "Port must be between 1 and 65535"
            except (ValueError, TypeError):
                return "Port must be a valid number"

        workers = data.get("workers")
        if workers is not None:
            try:
                workers_int = int(workers)
                if workers_int < 1 or workers_int > 64:
                    return "Workers must be between 1 and 64"
            except (ValueError, TypeError):
                return "Workers must be a valid number"

        # Validate log_level - must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL
        log_level = data.get("log_level")
        if log_level is not None:
            valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if log_level.upper() not in valid_log_levels:
                return f"Log level must be one of: {', '.join(valid_log_levels)}"

        jwt_expiration = data.get("jwt_expiration_minutes")
        if jwt_expiration is not None:
            try:
                jwt_int = int(jwt_expiration)
                if jwt_int < 1:
                    return "JWT expiration must be a positive number"
            except (ValueError, TypeError):
                return "JWT expiration must be a valid number"

    elif section == "cache":
        # Validate cache TTL values.
        # Bug #1396: blank means "no override, use default" -- must not
        # reject the whole form (same idiom as the size-cap fields below).
        for field in ["index_cache_ttl_minutes", "fts_cache_ttl_minutes"]:
            value = data.get(field)
            if value is not None and str(value).strip() != "":
                try:
                    val_float = float(value)
                    if val_float <= 0:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # Validate cleanup intervals.
        # Bug #1396: blank means "no override, use default" -- must not
        # reject the whole form (same idiom as the size-cap fields below).
        for field in ["index_cache_cleanup_interval", "fts_cache_cleanup_interval"]:
            value = data.get(field)
            if value is not None and str(value).strip() != "":
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # Validate cache size-cap fields (Bug #880)
        # Empty string is valid (means "use 4096 MiB default").
        # Non-empty must be a positive integer >= 1.
        for field in ["index_cache_max_size_mb", "fts_cache_max_size_mb"]:
            value = data.get(field)
            if value is not None and str(value).strip() != "":
                try:
                    val_int = int(value)
                    if val_int < 1:
                        return f"{field} must be empty or a positive integer (MB)"
                except (ValueError, TypeError):
                    return f"{field} must be empty or a positive integer (MB)"

        # Validate payload cache settings (Story #679).
        # Bug #1396: blank means "no override, use default" -- must not
        # reject the whole form (same idiom as the size-cap fields above).
        for field in [
            "payload_preview_size_chars",
            "payload_max_fetch_size_chars",
            "payload_cache_ttl_seconds",
            "payload_cleanup_interval_seconds",
        ]:
            value = data.get(field)
            if value is not None and str(value).strip() != "":
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        # Validate memory governor swap-in threshold (Bug #1225).
        # Bug #1396: blank means "no override, use default" -- must not
        # reject the whole form (same idiom as the size-cap fields above).
        swap_threshold = data.get("memory_governor_swap_pswpin_red_threshold")
        if swap_threshold is not None and str(swap_threshold).strip() != "":
            try:
                val_int = int(swap_threshold)
                if val_int < 0:
                    return "Memory Governor Swap Pswpin Red Threshold must be a non-negative integer"
            except (ValueError, TypeError):
                return (
                    "Memory Governor Swap Pswpin Red Threshold must be a valid integer"
                )

    elif section == "timeouts":
        # Validate timeout values (must be positive integers)
        for field in [
            "git_clone_timeout",
            "git_pull_timeout",
            "git_refresh_timeout",
        ]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "password_security":
        # Validate password length settings
        min_length = data.get("min_length")
        max_length = data.get("max_length")

        if min_length is not None:
            try:
                min_int = int(min_length)
                if min_int < 1:
                    return "Minimum password length must be at least 1"
            except (ValueError, TypeError):
                return "Minimum password length must be a valid number"

        if max_length is not None:
            try:
                max_int = int(max_length)
                if max_int < 1:
                    return "Maximum password length must be at least 1"
            except (ValueError, TypeError):
                return "Maximum password length must be a valid number"

        # Validate required char classes (1-4)
        char_classes = data.get("required_char_classes")
        if char_classes is not None:
            try:
                cc_int = int(char_classes)
                if cc_int < 1 or cc_int > 4:
                    return "Required character classes must be between 1 and 4"
            except (ValueError, TypeError):
                return "Required character classes must be a valid number"

    # Old sections kept for backwards compatibility during transition
    elif section == "indexing":
        batch_size = data.get("batch_size")
        if batch_size is not None:
            try:
                batch_int = int(batch_size)
                if batch_int < 1:
                    return "Batch size must be a positive number"
            except (ValueError, TypeError):
                return "Batch size must be a valid number"

        # Story #1158 - AC1: Embedding API parallelism (required when present, range 1-32)
        _PARALLEL_MIN = 1
        _PARALLEL_MAX = 32
        for field_name in ("voyage_ai_parallel_requests", "cohere_parallel_requests"):
            if field_name not in data:
                continue
            raw = data[field_name]
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                label = field_name.replace("_", " ").title()
                return f"{label} is required"
            try:
                val_int = int(raw)
                if val_int < _PARALLEL_MIN or val_int > _PARALLEL_MAX:
                    label = field_name.replace("_", " ").title()
                    return (
                        f"{label} must be between {_PARALLEL_MIN} and {_PARALLEL_MAX}"
                    )
            except (ValueError, TypeError):
                label = field_name.replace("_", " ").title()
                return f"{label} must be a valid number"

        # Story #1158 - AC2: Temporal git-diff parallelism (optional, range 1-32 when provided)
        if "temporal_parallel_requests" in data:
            temporal_raw = data["temporal_parallel_requests"]
            if temporal_raw is not None and not (
                isinstance(temporal_raw, str) and temporal_raw.strip() == ""
            ):
                try:
                    temporal_int = int(temporal_raw)
                    if temporal_int < _PARALLEL_MIN or temporal_int > _PARALLEL_MAX:
                        return f"Temporal Parallel Requests must be between {_PARALLEL_MIN} and {_PARALLEL_MAX}"
                except (ValueError, TypeError):
                    return "Temporal Parallel Requests must be a valid number"

        # Story #1290: per-commit temporal embedder registry.
        _submitted_embedders: Optional[List[str]] = None
        if "temporal_embedders" in data:
            raw_embedders = data["temporal_embedders"]
            _submitted_embedders = [
                v.strip() for v in str(raw_embedders).split(",") if v.strip()
            ]
            if not _submitted_embedders:
                return "Temporal Embedders must not be empty"

        if "temporal_active_embedder" in data:
            active_raw = str(data["temporal_active_embedder"]).strip()
            if not active_raw:
                return "Temporal Active Embedder must not be empty"
            if (
                _submitted_embedders is not None
                and active_raw not in _submitted_embedders
            ):
                return (
                    "Temporal Active Embedder must be one of the Temporal "
                    "Embedders submitted"
                )

        if "temporal_aggregation_chunk_chars" in data:
            chunk_raw = data["temporal_aggregation_chunk_chars"]
            try:
                chunk_int = int(chunk_raw)
                if chunk_int <= 0:
                    return "Temporal Aggregation Chunk Size must be a positive number"
            except (ValueError, TypeError):
                return "Temporal Aggregation Chunk Size must be a valid number"

    elif section == "query":
        for field in ["default_limit", "max_limit", "timeout"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be a positive number"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

        min_score = data.get("min_score")
        if min_score is not None:
            try:
                score_float = float(min_score)
                if score_float < 0 or score_float > 1:
                    return "Min score must be between 0 and 1"
            except (ValueError, TypeError):
                return "Min score must be a valid number"

    elif section == "security":
        for field in ["session_timeout", "token_expiration"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 60:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be at least 60 seconds"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "telemetry":
        # Validate collector_protocol
        collector_protocol = data.get("collector_protocol")
        if collector_protocol is not None:
            if collector_protocol.lower() not in ["grpc", "http"]:
                return "Collector protocol must be 'grpc' or 'http'"

        # Validate machine_metrics_interval_seconds
        interval = data.get("machine_metrics_interval_seconds")
        if interval is not None:
            try:
                interval_int = int(interval)
                if interval_int < 1:
                    return "Machine metrics interval must be at least 1 second"
            except (ValueError, TypeError):
                return "Machine metrics interval must be a valid number"

        # Story #1676 AC4: validate trace_sample_rate is in [0.0, 1.0].
        # Rejected, never clamped.
        _TRACE_SAMPLE_RATE_MIN = 0.0
        _TRACE_SAMPLE_RATE_MAX = 1.0
        trace_sample_rate = data.get("trace_sample_rate")
        if trace_sample_rate is not None:
            try:
                trace_sample_rate_float = float(trace_sample_rate)
                if not (
                    _TRACE_SAMPLE_RATE_MIN
                    <= trace_sample_rate_float
                    <= _TRACE_SAMPLE_RATE_MAX
                ):
                    return "Trace sample rate must be between 0.0 and 1.0"
            except (ValueError, TypeError):
                return "Trace sample rate must be a valid number"

    elif section == "langfuse":
        # Validate Langfuse host URL format
        host = data.get("host")
        if host is not None:
            host_str = str(host).strip()
            if host_str and not (
                host_str.startswith("http://") or host_str.startswith("https://")
            ):
                return "Langfuse host must start with http:// or https://"

    elif section == "search_limits":
        # Validate max_result_size_mb (1-100 MB)
        max_size = data.get("max_result_size_mb")
        if max_size is not None:
            try:
                max_size_int = int(max_size)
                if max_size_int < 1 or max_size_int > 100:
                    return "Max Result Size must be between 1 and 100 MB"
            except (ValueError, TypeError):
                return "Max Result Size must be a valid number"

        # Validate timeout_seconds (5-300 seconds)
        timeout = data.get("timeout_seconds")
        if timeout is not None:
            try:
                timeout_int = int(timeout)
                if timeout_int < 5 or timeout_int > 300:
                    return "Timeout must be between 5 and 300 seconds"
            except (ValueError, TypeError):
                return "Timeout must be a valid number"

    elif section == "golden_repos":
        # Validate refresh_interval_seconds (minimum 60 seconds)
        refresh_interval = data.get("refresh_interval_seconds")
        if refresh_interval is not None:
            try:
                interval_int = int(refresh_interval)
                if interval_int < 60:
                    return "Refresh Interval must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Refresh Interval must be a valid number"

        # Validate analysis_model (must be opus or sonnet)
        analysis_model = data.get("analysis_model")
        if analysis_model is not None:
            if analysis_model not in ("opus", "sonnet"):
                return "Analysis Model must be 'opus' or 'sonnet'"

    elif section == "mcp_session":
        # Story #3 Phase 2 AC2-AC3: MCP Session configuration
        session_ttl = data.get("session_ttl_seconds")
        if session_ttl is not None:
            try:
                ttl_int = int(session_ttl)
                if ttl_int < 60:
                    return "Session TTL must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Session TTL must be a valid number"

        cleanup_interval = data.get("cleanup_interval_seconds")
        if cleanup_interval is not None:
            try:
                interval_int = int(cleanup_interval)
                if interval_int < 60:
                    return "Cleanup Interval must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Cleanup Interval must be a valid number"

    elif section == "health":
        # Story #3 Phase 2 AC4-AC8: Health monitoring configuration
        for field in [
            "memory_warning_threshold_percent",
            "memory_critical_threshold_percent",
            "disk_warning_threshold_percent",
            "disk_critical_threshold_percent",
            "cpu_sustained_threshold_percent",
        ]:
            value = data.get(field)
            if value is not None:
                try:
                    val_float = float(value)
                    if val_float < 0 or val_float > 100:
                        field_name = field.replace("_", " ").title()
                        return f"{field_name} must be between 0 and 100"
                except (ValueError, TypeError):
                    field_name = field.replace("_", " ").title()
                    return f"{field_name} must be a valid number"

    elif section == "scip":
        # Story #3 Phase 2 AC9-AC11: the core's temporal staleness threshold
        # (stored as scip_config). The cidx-scip feature's query limits are
        # validated by the generic feature config door.
        stale_threshold = data.get("temporal_stale_threshold_days")
        if stale_threshold is not None:
            try:
                days_int = int(stale_threshold)
                if days_int < 1:
                    return "Temporal Stale Threshold must be at least 1 day"
            except (ValueError, TypeError):
                return "Temporal Stale Threshold must be a valid number"

    elif section == "git_timeouts":
        # Story #3 Phase 2 AC12-AC14: Git timeouts configuration
        # Story #3 Phase 2 AC27-AC28: API provider timeouts
        from ...services.constants import (
            MIN_GIT_LOCAL_TIMEOUT_SECONDS,
            MIN_GIT_REMOTE_TIMEOUT_SECONDS,
            MIN_GITHUB_API_TIMEOUT_SECONDS,
            MAX_GITHUB_API_TIMEOUT_SECONDS,
            MIN_GITLAB_API_TIMEOUT_SECONDS,
            MAX_GITLAB_API_TIMEOUT_SECONDS,
        )

        git_local = data.get("git_local_timeout")
        if git_local is not None:
            try:
                timeout_int = int(git_local)
                if timeout_int < MIN_GIT_LOCAL_TIMEOUT_SECONDS:
                    return f"Git Local Timeout must be at least {MIN_GIT_LOCAL_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Git Local Timeout must be a valid number"

        git_remote = data.get("git_remote_timeout")
        if git_remote is not None:
            try:
                timeout_int = int(git_remote)
                if timeout_int < MIN_GIT_REMOTE_TIMEOUT_SECONDS:
                    return f"Git Remote Timeout must be at least {MIN_GIT_REMOTE_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Git Remote Timeout must be a valid number"

        # P3 settings (AC27-AC28)
        github_api = data.get("github_api_timeout")
        if github_api is not None:
            try:
                timeout_int = int(github_api)
                if (
                    timeout_int < MIN_GITHUB_API_TIMEOUT_SECONDS
                    or timeout_int > MAX_GITHUB_API_TIMEOUT_SECONDS
                ):
                    return f"GitHub API Timeout must be between {MIN_GITHUB_API_TIMEOUT_SECONDS} and {MAX_GITHUB_API_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "GitHub API Timeout must be a valid number"

        gitlab_api = data.get("gitlab_api_timeout")
        if gitlab_api is not None:
            try:
                timeout_int = int(gitlab_api)
                if (
                    timeout_int < MIN_GITLAB_API_TIMEOUT_SECONDS
                    or timeout_int > MAX_GITLAB_API_TIMEOUT_SECONDS
                ):
                    return f"GitLab API Timeout must be between {MIN_GITLAB_API_TIMEOUT_SECONDS} and {MAX_GITLAB_API_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "GitLab API Timeout must be a valid number"

    elif section == "error_handling":
        # Story #3 Phase 2 AC16-AC18: Error handling configuration
        from ...services.constants import (
            MIN_RETRY_ATTEMPTS,
            MAX_RETRY_ATTEMPTS,
            MIN_BASE_RETRY_DELAY_SECONDS,
            MAX_BASE_RETRY_DELAY_SECONDS,
            MIN_MAX_RETRY_DELAY_SECONDS,
            MAX_MAX_RETRY_DELAY_SECONDS,
        )

        max_retry = data.get("max_retry_attempts")
        if max_retry is not None:
            try:
                retry_int = int(max_retry)
                if retry_int < MIN_RETRY_ATTEMPTS or retry_int > MAX_RETRY_ATTEMPTS:
                    return f"Max Retry Attempts must be between {MIN_RETRY_ATTEMPTS} and {MAX_RETRY_ATTEMPTS}"
            except (ValueError, TypeError):
                return "Max Retry Attempts must be a valid number"

        base_delay = data.get("base_retry_delay_seconds")
        if base_delay is not None:
            try:
                delay_float = float(base_delay)
                if (
                    delay_float < MIN_BASE_RETRY_DELAY_SECONDS
                    or delay_float > MAX_BASE_RETRY_DELAY_SECONDS
                ):
                    return f"Base Retry Delay must be between {MIN_BASE_RETRY_DELAY_SECONDS} and {MAX_BASE_RETRY_DELAY_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Base Retry Delay must be a valid number"

        max_delay = data.get("max_retry_delay_seconds")
        if max_delay is not None:
            try:
                delay_float = float(max_delay)
                if (
                    delay_float < MIN_MAX_RETRY_DELAY_SECONDS
                    or delay_float > MAX_MAX_RETRY_DELAY_SECONDS
                ):
                    return f"Max Retry Delay must be between {MIN_MAX_RETRY_DELAY_SECONDS} and {MAX_MAX_RETRY_DELAY_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Max Retry Delay must be a valid number"

    elif section == "api_limits":
        # Story #3 Phase 2 AC19-AC24: API limits configuration
        # Story #3 Phase 2 AC35, AC38-AC39: Audit/log limits
        from ...services.constants import (
            MIN_DEFAULT_FILE_READ_LINES,
            MAX_DEFAULT_FILE_READ_LINES,
            MIN_MAX_FILE_READ_LINES,
            MAX_MAX_FILE_READ_LINES,
            MIN_DEFAULT_DIFF_LINES,
            MAX_DEFAULT_DIFF_LINES,
            MIN_MAX_DIFF_LINES,
            MAX_MAX_DIFF_LINES,
            MIN_DEFAULT_LOG_COMMITS,
            MAX_DEFAULT_LOG_COMMITS,
            MIN_MAX_LOG_COMMITS,
            MAX_MAX_LOG_COMMITS,
            MIN_AUDIT_LOG_DEFAULT_LIMIT,
            MAX_AUDIT_LOG_DEFAULT_LIMIT,
            MIN_LOG_PAGE_SIZE_DEFAULT,
            MAX_LOG_PAGE_SIZE_DEFAULT,
            MIN_LOG_PAGE_SIZE_MAX,
            MAX_LOG_PAGE_SIZE_MAX,
        )

        default_file_lines = data.get("default_file_read_lines")
        if default_file_lines is not None:
            try:
                lines_int = int(default_file_lines)
                if (
                    lines_int < MIN_DEFAULT_FILE_READ_LINES
                    or lines_int > MAX_DEFAULT_FILE_READ_LINES
                ):
                    return f"Default File Read Lines must be between {MIN_DEFAULT_FILE_READ_LINES} and {MAX_DEFAULT_FILE_READ_LINES}"
            except (ValueError, TypeError):
                return "Default File Read Lines must be a valid number"

        max_file_lines = data.get("max_file_read_lines")
        if max_file_lines is not None:
            try:
                lines_int = int(max_file_lines)
                if (
                    lines_int < MIN_MAX_FILE_READ_LINES
                    or lines_int > MAX_MAX_FILE_READ_LINES
                ):
                    return f"Max File Read Lines must be between {MIN_MAX_FILE_READ_LINES} and {MAX_MAX_FILE_READ_LINES}"
            except (ValueError, TypeError):
                return "Max File Read Lines must be a valid number"

        default_diff_lines = data.get("default_diff_lines")
        if default_diff_lines is not None:
            try:
                lines_int = int(default_diff_lines)
                if (
                    lines_int < MIN_DEFAULT_DIFF_LINES
                    or lines_int > MAX_DEFAULT_DIFF_LINES
                ):
                    return f"Default Diff Lines must be between {MIN_DEFAULT_DIFF_LINES} and {MAX_DEFAULT_DIFF_LINES}"
            except (ValueError, TypeError):
                return "Default Diff Lines must be a valid number"

        max_diff_lines = data.get("max_diff_lines")
        if max_diff_lines is not None:
            try:
                lines_int = int(max_diff_lines)
                if lines_int < MIN_MAX_DIFF_LINES or lines_int > MAX_MAX_DIFF_LINES:
                    return f"Max Diff Lines must be between {MIN_MAX_DIFF_LINES} and {MAX_MAX_DIFF_LINES}"
            except (ValueError, TypeError):
                return "Max Diff Lines must be a valid number"

        default_log = data.get("default_log_commits")
        if default_log is not None:
            try:
                commits_int = int(default_log)
                if (
                    commits_int < MIN_DEFAULT_LOG_COMMITS
                    or commits_int > MAX_DEFAULT_LOG_COMMITS
                ):
                    return f"Default Log Commits must be between {MIN_DEFAULT_LOG_COMMITS} and {MAX_DEFAULT_LOG_COMMITS}"
            except (ValueError, TypeError):
                return "Default Log Commits must be a valid number"

        max_log = data.get("max_log_commits")
        if max_log is not None:
            try:
                commits_int = int(max_log)
                if (
                    commits_int < MIN_MAX_LOG_COMMITS
                    or commits_int > MAX_MAX_LOG_COMMITS
                ):
                    return f"Max Log Commits must be between {MIN_MAX_LOG_COMMITS} and {MAX_MAX_LOG_COMMITS}"
            except (ValueError, TypeError):
                return "Max Log Commits must be a valid number"

        # P3 settings (AC35, AC38-AC39)
        audit_limit = data.get("audit_log_default_limit")
        if audit_limit is not None:
            try:
                limit_int = int(audit_limit)
                if (
                    limit_int < MIN_AUDIT_LOG_DEFAULT_LIMIT
                    or limit_int > MAX_AUDIT_LOG_DEFAULT_LIMIT
                ):
                    return f"Audit Log Default Limit must be between {MIN_AUDIT_LOG_DEFAULT_LIMIT} and {MAX_AUDIT_LOG_DEFAULT_LIMIT}"
            except (ValueError, TypeError):
                return "Audit Log Default Limit must be a valid number"

        log_page_default = data.get("log_page_size_default")
        if log_page_default is not None:
            try:
                size_int = int(log_page_default)
                if (
                    size_int < MIN_LOG_PAGE_SIZE_DEFAULT
                    or size_int > MAX_LOG_PAGE_SIZE_DEFAULT
                ):
                    return f"Log Page Size Default must be between {MIN_LOG_PAGE_SIZE_DEFAULT} and {MAX_LOG_PAGE_SIZE_DEFAULT}"
            except (ValueError, TypeError):
                return "Log Page Size Default must be a valid number"

        log_page_max = data.get("log_page_size_max")
        if log_page_max is not None:
            try:
                size_int = int(log_page_max)
                if size_int < MIN_LOG_PAGE_SIZE_MAX or size_int > MAX_LOG_PAGE_SIZE_MAX:
                    return f"Log Page Size Max must be between {MIN_LOG_PAGE_SIZE_MAX} and {MAX_LOG_PAGE_SIZE_MAX}"
            except (ValueError, TypeError):
                return "Log Page Size Max must be a valid number"

    elif section == "web_security":
        # Story #3 Phase 2 AC25-AC26: Web security configuration
        # Story #683 AC2: csrf_max_age_seconds validation removed (field deleted).
        from ...services.constants import (
            MIN_WEB_SESSION_TIMEOUT_SECONDS,
            MAX_WEB_SESSION_TIMEOUT_SECONDS,
        )

        session_timeout = data.get("web_session_timeout_seconds")
        if session_timeout is not None:
            try:
                timeout_int = int(session_timeout)
                if (
                    timeout_int < MIN_WEB_SESSION_TIMEOUT_SECONDS
                    or timeout_int > MAX_WEB_SESSION_TIMEOUT_SECONDS
                ):
                    return f"Web Session Timeout must be between {MIN_WEB_SESSION_TIMEOUT_SECONDS} and {MAX_WEB_SESSION_TIMEOUT_SECONDS} seconds"
            except (ValueError, TypeError):
                return "Web Session Timeout must be a valid number"

        # Hotfix: self-registration gate is a Yes/No <select>; reject anything
        # else rather than letting _parse_bool silently coerce it to False.
        self_registration = data.get("self_registration_enabled")
        if self_registration is not None and self_registration not in (
            "true",
            "false",
        ):
            return "Self-Registration Enabled must be 'true' or 'false'"

    # Story #683 AC3: "auth" validation block removed (AuthConfig deleted).

    elif section == "multi_search":
        # Story #25: Multi-search limits configuration
        # Validate worker counts (1-50 range)
        for field in ["multi_search_max_workers", "scip_multi_max_workers"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 1 or val_int > 50:
                        return f"{field} must be between 1 and 50"
                except (ValueError, TypeError):
                    return f"{field} must be a valid number"

        # Validate timeout values (5-600 range)
        for field in ["multi_search_timeout_seconds", "scip_multi_timeout_seconds"]:
            value = data.get(field)
            if value is not None:
                try:
                    val_int = int(value)
                    if val_int < 5 or val_int > 600:
                        return f"{field} must be between 5 and 600 seconds"
                except (ValueError, TypeError):
                    return f"{field} must be a valid number"

    elif section == "background_jobs":
        # Story #26: Background jobs configuration
        max_concurrent = data.get("max_concurrent_background_jobs")
        if max_concurrent is not None:
            try:
                val_int = int(max_concurrent)
                if val_int < 1 or val_int > 100:
                    return "Max Concurrent Background Jobs must be between 1 and 100"
            except (ValueError, TypeError):
                return "Max Concurrent Background Jobs must be a valid number"

        # Story #27: Subprocess max workers configuration
        subprocess_workers = data.get("subprocess_max_workers")
        if subprocess_workers is not None:
            try:
                val_int = int(subprocess_workers)
                if val_int < 1 or val_int > 50:
                    return "Subprocess Max Workers must be between 1 and 50"
            except (ValueError, TypeError):
                return "Subprocess Max Workers must be a valid number"

    elif section == "data_retention":
        # Story #400: Data retention configuration validation
        retention_fields = [
            ("operational_logs_retention_hours", "Operational Logs Retention"),
            ("audit_logs_retention_hours", "Audit Logs Retention"),
            ("sync_jobs_retention_hours", "Sync Jobs Retention"),
            ("dep_map_history_retention_hours", "Dependency Map History Retention"),
            ("background_jobs_retention_hours", "Background Jobs Retention"),
        ]
        for field_name, label in retention_fields:
            field_value = data.get(field_name)
            if field_value is not None:
                try:
                    val_int = int(field_value)
                    if val_int < 1 or val_int > 8760:
                        return f"{label} hours must be between 1 and 8760"
                except (ValueError, TypeError):
                    return f"{label} hours must be a valid number"

        cleanup_interval = data.get("cleanup_interval_hours")
        if cleanup_interval is not None:
            try:
                val_int = int(cleanup_interval)
                if val_int < 1 or val_int > 24:
                    return "Cleanup Interval hours must be between 1 and 24"
            except (ValueError, TypeError):
                return "Cleanup Interval hours must be a valid number"

    elif section == "activated_reaper":
        # Story #967: Activated repository reaper configuration validation
        ttl_days = data.get("ttl_days")
        if ttl_days is not None:
            try:
                val_int = int(ttl_days)
                if val_int < 1 or val_int > 3650:
                    return "TTL Days must be between 1 and 3650"
            except (ValueError, TypeError):
                return "TTL Days must be a valid number"

        cadence_hours = data.get("cadence_hours")
        if cadence_hours is not None:
            try:
                val_int = int(cadence_hours)
                if val_int < 1 or val_int > 168:
                    return "Cadence Hours must be between 1 and 168"
            except (ValueError, TypeError):
                return "Cadence Hours must be a valid number"

    elif section == "hnsw_orphan_sweep":
        # Story #1397: HNSW orphan-repair sweep operating-hours window
        # configuration validation.
        for hour_field, label in (
            ("operating_hours_start_utc", "Operating Hours Start"),
            ("operating_hours_end_utc", "Operating Hours End"),
        ):
            hour_value = data.get(hour_field)
            if hour_value is not None:
                try:
                    val_int = int(hour_value)
                    if val_int < 0 or val_int > 23:
                        return f"{label} must be between 0 and 23"
                except (ValueError, TypeError):
                    return f"{label} must be a valid number"

        tick_interval_minutes = data.get("tick_interval_minutes")
        if tick_interval_minutes is not None:
            try:
                val_int = int(tick_interval_minutes)
                if val_int < 1:
                    return "Tick Interval Minutes must be at least 1"
            except (ValueError, TypeError):
                return "Tick Interval Minutes must be a valid number"

        batch_size = data.get("batch_size")
        if batch_size is not None:
            try:
                val_int = int(batch_size)
                if val_int < 1:
                    return "Batch Size must be at least 1"
            except (ValueError, TypeError):
                return "Batch Size must be a valid number"

    elif section == "indexing_watchdog":
        # Issue #1530: indexing-subprocess activity watchdog configuration
        # validation. Bounds are IMPORTED from config_manager.py (never
        # duplicated as literals) so both layers can never drift apart.
        from ...utils.config_manager import (
            INDEXING_WATCHDOG_MAX_STALE_ACTIVITY_TIMEOUT_SECONDS,
            INDEXING_WATCHDOG_MIN_STALE_ACTIVITY_TIMEOUT_SECONDS,
        )

        stale_timeout = data.get("stale_activity_timeout_seconds")
        if stale_timeout is not None:
            try:
                val_float = float(stale_timeout)
                if not (
                    INDEXING_WATCHDOG_MIN_STALE_ACTIVITY_TIMEOUT_SECONDS
                    <= val_float
                    <= INDEXING_WATCHDOG_MAX_STALE_ACTIVITY_TIMEOUT_SECONDS
                ):
                    return (
                        f"Stale Activity Timeout must be between "
                        f"{INDEXING_WATCHDOG_MIN_STALE_ACTIVITY_TIMEOUT_SECONDS} and "
                        f"{INDEXING_WATCHDOG_MAX_STALE_ACTIVITY_TIMEOUT_SECONDS} seconds"
                    )
            except (ValueError, TypeError):
                return "Stale Activity Timeout must be a valid number"

    elif section == "fleet_migration":
        # Story #1458 (Epic #1454), round-6 item #10: fleet migration
        # scheduler configuration validation -- mirrors the
        # hnsw_orphan_sweep tick_interval_minutes check exactly.
        tick_interval_minutes = data.get("tick_interval_minutes")
        if tick_interval_minutes is not None:
            try:
                val_int = int(tick_interval_minutes)
                if val_int < 1:
                    return "Tick Interval Minutes must be at least 1"
            except (ValueError, TypeError):
                return "Tick Interval Minutes must be a valid number"

    elif section == "temporal_legacy_migration":
        # Issue #1548: both fields are booleans (Yes/No <select>) -- no
        # numeric range validation needed, mirroring how fleet_migration's
        # own boolean fields (enabled, canary_gate_enabled) require none.
        pass

    elif section == "search_timeouts":
        # Issue #1398: Query & search timeouts configuration validation.
        # Ranges mirror config_manager.validate_config's SearchTimeoutsConfig checks.
        for field_name, label, min_val, max_val in (
            (
                "search_code_handler_timeout_seconds",
                "Semantic/Temporal Search Tool Timeout",
                30,
                600,
            ),
            (
                "default_handler_timeout_seconds",
                "Default MCP Tool Timeout",
                10,
                300,
            ),
            (
                "write_mode_handler_timeout_seconds",
                "Write-Mode Refresh Timeout",
                600,
                3600,
            ),
            (
                "embedding_provider_timeout_seconds",
                "Embedding Provider HTTP Timeout",
                5,
                120,
            ),
            ("reranker_timeout_seconds", "Reranker HTTP Timeout", 5, 120),
            (
                "rest_query_handler_timeout_seconds",
                "REST Query Handler Timeout",
                30,
                600,
            ),
        ):
            field_value = data.get(field_name)
            if field_value is not None:
                try:
                    val_int = int(field_value)
                    if val_int < min_val or val_int > max_val:
                        return (
                            f"{label} must be between {min_val} and {max_val} seconds"
                        )
                except (ValueError, TypeError):
                    return f"{label} must be a valid number"

        # Story #1400 CRITICAL 5/6: the ONE float field. Basic sanity
        # (non-negative) only -- the cross-field grace-budget relationship
        # against search_code_handler_timeout_seconds is validated later by
        # config_manager.validate_config against the whole candidate.
        temporal_wait = data.get("temporal_inline_wait_seconds")
        if temporal_wait is not None:
            try:
                val_float = float(temporal_wait)
                if val_float < 0.0:
                    return "Temporal Query Inline Sync-Wait must be >= 0.0 seconds"
            except (ValueError, TypeError):
                return "Temporal Query Inline Sync-Wait must be a valid number"

    elif section == "embedding_stats":
        # Story #1418 Phase 3: Embedding & reranker call tracking
        # configuration validation. Ranges mirror
        # config_manager.validate_config's EmbeddingStatsConfig checks.
        flush_interval = data.get("flush_interval_seconds")
        if flush_interval is not None:
            try:
                val_float = float(flush_interval)
                if val_float <= 0:
                    return "Flush Interval must be greater than 0 seconds"
            except (ValueError, TypeError):
                return "Flush Interval must be a valid number"

        retention_days = data.get("retention_days")
        if retention_days is not None:
            try:
                val_int = int(retention_days)
                if val_int <= 0:
                    return "Retention Days must be greater than 0"
            except (ValueError, TypeError):
                return "Retention Days must be a valid number"

    elif section == "temporal_indexing":
        # Story #1404: Global temporal indexing floor date validation.
        # Delegates to TemporalIndexingConfig.validate() (strptime +
        # strftime round-trip shape, mirrors
        # temporal_search_service.parse_date_range) rather than
        # re-implementing date-format checks inline.
        index_floor_date = data.get("index_floor_date")
        if index_floor_date is not None:
            from ...utils.config_manager import TemporalIndexingConfig

            try:
                TemporalIndexingConfig(
                    index_floor_date=str(index_floor_date)
                ).validate()
            except ValueError as exc:
                return str(exc)

    # Issue #1753: the "omni_search" elif branch was removed here -- zero
    # template consumers (dead code confirmed by Issue #1750's
    # investigation), and "omni_search" is no longer in
    # _VALID_CONFIG_SECTIONS so this branch was already unreachable.

    elif section == "content_limits":
        # Story #32: Unified content limits configuration
        chars_per_token = data.get("chars_per_token")
        if chars_per_token is not None:
            try:
                val_int = int(chars_per_token)
                if val_int < 1 or val_int > 10:
                    return "Chars Per Token must be between 1 and 10"
            except (ValueError, TypeError):
                return "Chars Per Token must be a valid number"

        file_content_max_tokens = data.get("file_content_max_tokens")
        if file_content_max_tokens is not None:
            try:
                val_int = int(file_content_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "File Content Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "File Content Max Tokens must be a valid number"

        git_diff_max_tokens = data.get("git_diff_max_tokens")
        if git_diff_max_tokens is not None:
            try:
                val_int = int(git_diff_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Git Diff Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Git Diff Max Tokens must be a valid number"

        git_log_max_tokens = data.get("git_log_max_tokens")
        if git_log_max_tokens is not None:
            try:
                val_int = int(git_log_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Git Log Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Git Log Max Tokens must be a valid number"

        search_result_max_tokens = data.get("search_result_max_tokens")
        if search_result_max_tokens is not None:
            try:
                val_int = int(search_result_max_tokens)
                if val_int < 1000 or val_int > 200000:
                    return "Search Result Max Tokens must be between 1000 and 200000"
            except (ValueError, TypeError):
                return "Search Result Max Tokens must be a valid number"

        cache_ttl_seconds = data.get("cache_ttl_seconds")
        if cache_ttl_seconds is not None:
            try:
                val_int = int(cache_ttl_seconds)
                if val_int < 60:
                    return "Cache TTL must be at least 60 seconds"
            except (ValueError, TypeError):
                return "Cache TTL must be a valid number"

    elif section == "rerank":
        # Story #652: Reranking configuration validation
        for field_name, label in [
            ("overfetch_multiplier", "Overfetch Multiplier"),
        ]:
            field_value = data.get(field_name)
            if field_value is not None:
                try:
                    val_int = int(field_value)
                    if val_int < 1:
                        return f"{label} must be a positive integer"
                except (ValueError, TypeError):
                    return f"{label} must be a valid number"

    elif section == "lifecycle_analysis":
        # Story #885 Phase 5b (A7b): Lifecycle analysis timeout validation.
        # Rule: outer_timeout_seconds >= shell_timeout_seconds + LIFECYCLE_OUTER_TIMEOUT_GRACE_SECONDS

        def _parse_positive_timeout(
            raw: Optional[str], label: str
        ) -> Union[int, str, None]:
            """Return parsed int, error string, or None if raw is None."""
            if raw is None:
                return None
            try:
                val = int(raw)
            except (ValueError, TypeError):
                return f"{label} must be a valid number"
            if val < 1:
                return f"{label} must be a positive number"
            return val

        shell_result = _parse_positive_timeout(
            data.get("shell_timeout_seconds"), "Shell timeout"
        )
        if isinstance(shell_result, str):
            return shell_result

        outer_result = _parse_positive_timeout(
            data.get("outer_timeout_seconds"), "Outer timeout"
        )
        if isinstance(outer_result, str):
            return outer_result

        # Cross-field check: only applies when both values are present
        if shell_result is not None and outer_result is not None:
            min_outer = shell_result + LIFECYCLE_OUTER_TIMEOUT_GRACE_SECONDS
            if outer_result < min_outer:
                return (
                    f"Outer timeout ({outer_result}s) must be at least "
                    f"shell timeout + {LIFECYCLE_OUTER_TIMEOUT_GRACE_SECONDS}s "
                    f"({min_outer}s)"
                )

    elif section == "query_embedding_cache":
        # Story #1107 S3: Query embedding cache runtime config validation.
        _QEC_VALID_MODES = {"off", "shadow", "on"}

        for mode_field in (
            "query_embedding_cache_voyage_mode",
            "query_embedding_cache_cohere_mode",
        ):
            mode_val = data.get(mode_field)
            if mode_val is not None:
                if str(mode_val) not in _QEC_VALID_MODES:
                    return (
                        f"{mode_field} must be one of: "
                        f"{', '.join(sorted(_QEC_VALID_MODES))}"
                    )

        for rate_field in (
            "query_embedding_cache_voyage_audit_sample_rate",
            "query_embedding_cache_cohere_audit_sample_rate",
        ):
            rate_val = data.get(rate_field)
            if rate_val is not None:
                try:
                    rate_float = float(rate_val)
                except (ValueError, TypeError):
                    return f"{rate_field} must be a valid float"
                if rate_float < 0.0 or rate_float > 1.0:
                    return f"{rate_field} must be between 0.0 and 1.0"

        max_entries_val = data.get("query_embedding_cache_max_entries")
        if max_entries_val is not None:
            try:
                max_entries_int = int(max_entries_val)
            except (ValueError, TypeError):
                return "query_embedding_cache_max_entries must be a valid integer"
            if max_entries_int < 100:
                return (
                    "query_embedding_cache_max_entries must be at least 100 "
                    "(the minimum safe cap)"
                )

        for anchor_field in (
            "query_embedding_cache_voyage_anchor_tokens",
            "query_embedding_cache_cohere_anchor_tokens",
        ):
            anchor_val = data.get(anchor_field)
            # Empty string or None means "inherit global" — accepted as a valid reset.
            if anchor_val is None or anchor_val == "":
                continue
            try:
                anchor_int = int(anchor_val)
            except (ValueError, TypeError):
                return f"{anchor_field} must be a valid integer"
            if anchor_int < 0:
                return f"{anchor_field} must be >= 0"

    elif section == "search_event_log":
        # Story #1159: Search event log data retention configuration.
        retention_days_val = data.get("search_event_log_retention_days")
        if retention_days_val is not None:
            try:
                retention_days_int = int(retention_days_val)
            except (ValueError, TypeError):
                return "search_event_log_retention_days must be a valid integer"
            if retention_days_int < 1 or retention_days_int > 3650:
                return "search_event_log_retention_days must be between 1 and 3650"

    elif section == "export":
        # Issue #1160: Export file retention configuration.
        export_days_val = data.get("export_retention_days")
        if export_days_val is not None:
            try:
                export_days_int = int(export_days_val)
            except (ValueError, TypeError):
                return "export_retention_days must be a valid integer"
            if export_days_int < 1 or export_days_int > 3650:
                return "export_retention_days must be between 1 and 3650"

    # Content seam step 5a: where aliases and write-mode markers are read from
    elif section == "control_plane_metadata":
        from code_indexer.server.utils.config_manager import (
            METADATA_OUTBOX_SINKS,
            METADATA_READ_SOURCES,
        )

        read_source = data.get("metadata_read_source")
        if read_source is not None and str(read_source) not in METADATA_READ_SOURCES:
            return f"metadata_read_source must be one of {list(METADATA_READ_SOURCES)}"
        sink = data.get("metadata_outbox_sink")
        if sink is not None and str(sink) not in METADATA_OUTBOX_SINKS:
            return f"metadata_outbox_sink must be one of {list(METADATA_OUTBOX_SINKS)}"
        drain_timeout = data.get("metadata_outbox_drain_timeout_s")
        if drain_timeout is not None:
            try:
                if int(drain_timeout) < 1:
                    return "metadata_outbox_drain_timeout_s must be at least 1"
            except (ValueError, TypeError):
                return "metadata_outbox_drain_timeout_s must be a valid integer"

    return None
