"""
Configuration Service for CIDX Server Admin UI.

Provides a high-level interface for reading and updating server configuration.
All settings persist to ~/.cidx-server/config.json via ServerConfigManager.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import json
import logging
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    RerankConfig,
    LifecycleAnalysisConfig,
    CidxMetaBackupConfig,
)
from ..config.delegation_config import ClaudeDelegationManager, ClaudeDelegationConfig

logger = logging.getLogger(__name__)

# Story #578: Keys that must stay in local config.json (chicken-and-egg: needed
# before PG pool exists).  Everything else is "runtime" and lives in PG in
# cluster mode.
BOOTSTRAP_KEYS = frozenset(
    {
        "server_dir",
        "host",
        "port",
        "workers",
        "log_level",
        "storage_mode",
        "postgres_dsn",
        "ontap",
        "cluster",
        "enable_malloc_arena_max",  # Bug #897
        "enable_malloc_trim",  # Bug #897
        "enable_graph_channel_repair",  # Story #908 / Epic #907
        "graph_repair_self_loop",  # Story #920
        "graph_repair_malformed_yaml",  # Story #920
        "graph_repair_garbage_domain",  # Story #920
        "graph_repair_bidirectional_mismatch",  # Story #920
        "fault_injection_enabled",  # Story #746
        "fault_injection_nonprod_ack",  # Story #746
    }
)
CONFIG_KEY_RUNTIME = "runtime"
UPDATER_WEB_UI = "web-ui"
UPDATER_SEED = "config-seed"

# Bug #875: minimum bounds for new claude_cli settings
_MIN_FACT_CHECK_TIMEOUT_SECONDS = 60
_MIN_SCHEDULED_CATCHUP_INTERVAL_MINUTES = 1


def _parse_bool(value: Any) -> bool:
    """Return True when value is the string 'true', 'True', or the boolean True."""
    return value in ["true", True, "True"]


class ConfigService:
    """
    Service for managing server configuration.

    Provides methods for loading, updating, and saving server configuration
    with validation. All changes persist to ~/.cidx-server/config.json.
    """

    def __init__(self, server_dir_path: Optional[str] = None):
        """
        Initialize the configuration service.

        Args:
            server_dir_path: Optional path to server directory.
                           Defaults to ~/.cidx-server
        """
        self.config_manager = ServerConfigManager(server_dir_path)
        self._config: Optional[ServerConfig] = None
        self._delegation_manager = ClaudeDelegationManager(server_dir_path)
        # Story #578: Unified DB for runtime config (SQLite or PG)
        self._pool: Any = None  # PG pool (set via set_connection_pool for cluster)
        self._sqlite_db_path: Optional[str] = None  # SQLite path (solo mode)
        self._db_config_version: int = 0
        self._reload_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_change_callbacks: List[Any] = []

    def register_on_change_callback(self, callback: Any) -> None:
        """Register a callback fired when config reloads from DB.

        Callback receives the new ServerConfig as its sole argument.
        """
        self._on_change_callbacks.append(callback)

    def initialize_runtime_db(self, db_path: str) -> None:
        """Initialize SQLite-backed runtime config (solo mode).

        Called on startup. If server_config table is empty and config.json
        has runtime keys, auto-migrates from file to DB.
        """
        self._sqlite_db_path = db_path
        runtime = self._load_runtime_from_sqlite()
        if runtime is not None:
            # DB has runtime config -- merge with bootstrap
            self._merge_runtime_config(runtime)
            self._strip_config_file_to_bootstrap()
            # A7d (Story #885 AC-V4-17): persist lifecycle_analysis_config defaults
            # to SQLite on first boot after upgrade (key absent from legacy row).
            if "lifecycle_analysis_config" not in runtime:
                runtime["lifecycle_analysis_config"] = asdict(LifecycleAnalysisConfig())
                self._save_runtime_to_sqlite(runtime)
        else:
            # First boot or pre-migration: seed DB from config.json
            config = self.get_config()
            runtime_dict = self._extract_runtime_dict(config)
            if runtime_dict:
                self._save_runtime_to_sqlite(runtime_dict)
                self._strip_config_file_to_bootstrap()
                logger.info(
                    "ConfigService: migrated %d runtime keys to SQLite",
                    len(runtime_dict),
                )
        logger.info("ConfigService: using SQLite for runtime config")

    def set_connection_pool(self, pool: Any) -> None:
        """Enable PostgreSQL-backed config storage for cluster mode.

        When set, runtime config is read from/written to the server_config
        PG table. Bootstrap config continues to come from local config.json.
        """
        self._pool = pool
        self._load_runtime_from_pg()
        logger.info("ConfigService: using PostgreSQL for runtime config (cluster mode)")

    def start_config_reload(self, interval_seconds: int = 30) -> None:
        """Start background thread that polls PG for config version changes."""
        if self._pool is None:
            return
        if self._reload_thread is not None and self._reload_thread.is_alive():
            return
        self._stop_event.clear()

        def _poll_loop() -> None:
            while not self._stop_event.wait(timeout=interval_seconds):
                try:
                    if self.check_config_update():
                        logger.info("ConfigService: reloaded config from PG")
                except Exception:
                    logger.exception("ConfigService: config reload poll failed")

        self._reload_thread = threading.Thread(
            target=_poll_loop, daemon=True, name="config-reload"
        )
        self._reload_thread.start()
        logger.info(
            "ConfigService: config reload thread started (interval=%ds)",
            interval_seconds,
        )

    def stop_config_reload(self) -> None:
        """Stop the background config reload thread."""
        self._stop_event.set()
        if self._reload_thread is not None:
            self._reload_thread.join(timeout=5)
            logger.info("ConfigService: config reload thread stopped")

    def get_delegation_manager(self) -> ClaudeDelegationManager:
        """Get the Claude Delegation manager for config operations."""
        return self._delegation_manager

    def load_config(self) -> ServerConfig:
        """
        Load configuration from file + DB merge, or file-only if no DB yet.

        When runtime DB is initialized (solo or cluster), loads bootstrap
        from config.json and merges runtime from DB. Otherwise reads full
        config from file (pre-migration or early bootstrap).

        Returns:
            ServerConfig object with current settings
        """
        config = self.config_manager.load_config()
        if config is None:
            config = self.config_manager.create_default_config()
            self.save_config(config)

        self._config = config

        # If runtime DB is available, merge runtime from DB on top of
        # the bootstrap-only file config. This prevents any caller of
        # load_config() from overwriting the merged config with defaults.
        if self._pool is not None:
            self._load_runtime_from_pg()  # Merges internally
        elif self._sqlite_db_path:
            runtime = self._load_runtime_from_sqlite()
            if runtime:
                self._merge_runtime_config(runtime)

        return self._config

    def get_config(self) -> ServerConfig:
        """
        Get current configuration, loading if necessary.

        Returns:
            ServerConfig object
        """
        if self._config is None:
            self.load_config()
        assert self._config is not None  # load_config() always sets self._config
        return self._config

    def get_claude_integration_config(self):
        """
        Get Claude integration config from server config.

        Returns:
            ClaudeIntegrationConfig object from current server configuration
        """
        config = self.get_config()
        return config.claude_integration_config

    def get_all_settings(self) -> Dict[str, Any]:
        """
        Get all settings as a flat dictionary for UI display.

        Returns:
            Dictionary with all settings flattened for easy access
        """
        config = self.get_config()

        # These are guaranteed to be non-None by ServerConfig.__post_init__
        assert config.cache_config is not None
        assert config.resource_config is not None
        assert config.password_security is not None
        assert config.oidc_provider_config is not None
        assert config.telemetry_config is not None
        # Story #3 - Configuration Consolidation: Assert new config objects
        assert config.search_limits_config is not None
        assert config.golden_repos_config is not None
        # Story #3 - Phase 2: Assert P0/P1 config objects
        assert config.mcp_session_config is not None
        assert config.health_config is not None
        assert config.scip_config is not None
        # Story #3 - Phase 2: Assert P2 config objects (AC12-AC26)
        assert config.git_timeouts_config is not None
        assert config.error_handling_config is not None
        assert config.api_limits_config is not None
        assert config.web_security_config is not None
        # Story #683 AC3: auth_config assert removed (AuthConfig deleted).
        # Story #15 AC3: Assert ClaudeIntegrationConfig is not None
        assert config.claude_integration_config is not None
        # Story #25: Assert MultiSearchLimitsConfig is not None
        assert config.multi_search_limits_config is not None
        # Story #26: Assert BackgroundJobsConfig is not None
        assert config.background_jobs_config is not None

        settings = {
            # Server settings
            "server": {
                "host": config.host,
                "port": config.port,
                "workers": config.workers,
                "log_level": config.log_level,
                "jwt_expiration_minutes": config.jwt_expiration_minutes,
                # Story #22 - Configurable service display name
                "service_display_name": config.service_display_name,
            },
            # Cache settings
            "cache": {
                "index_cache_ttl_minutes": config.cache_config.index_cache_ttl_minutes,
                "index_cache_cleanup_interval": config.cache_config.index_cache_cleanup_interval,
                "index_cache_max_size_mb": config.cache_config.index_cache_max_size_mb,
                "fts_cache_ttl_minutes": config.cache_config.fts_cache_ttl_minutes,
                "fts_cache_cleanup_interval": config.cache_config.fts_cache_cleanup_interval,
                "fts_cache_max_size_mb": config.cache_config.fts_cache_max_size_mb,
                "fts_cache_reload_on_access": config.cache_config.fts_cache_reload_on_access,
                # Payload cache settings (Story #679)
                "payload_preview_size_chars": config.cache_config.payload_preview_size_chars,
                "payload_max_fetch_size_chars": config.cache_config.payload_max_fetch_size_chars,
                "payload_cache_ttl_seconds": config.cache_config.payload_cache_ttl_seconds,
                "payload_cleanup_interval_seconds": config.cache_config.payload_cleanup_interval_seconds,
            },
            # Git operation timeouts
            "timeouts": {
                "git_clone_timeout": config.resource_config.git_clone_timeout,
                "git_pull_timeout": config.resource_config.git_pull_timeout,
                "git_refresh_timeout": config.resource_config.git_refresh_timeout,
                "hnsw_max_elements": config.resource_config.hnsw_max_elements,
                # Story #683 AC7: 8 genuinely-wired advanced timeout fields
                "git_init_conflict_timeout": config.resource_config.git_init_conflict_timeout,
                "git_service_conflict_timeout": config.resource_config.git_service_conflict_timeout,
                "git_service_cleanup_timeout": config.resource_config.git_service_cleanup_timeout,
                "git_service_wait_timeout": config.resource_config.git_service_wait_timeout,
                "git_process_check_timeout": config.resource_config.git_process_check_timeout,
                "git_untracked_file_timeout": config.resource_config.git_untracked_file_timeout,
                "cow_clone_timeout": config.resource_config.cow_clone_timeout,
                "cidx_fix_config_timeout": config.resource_config.cidx_fix_config_timeout,
            },
            # Password security
            "password_security": {
                "min_length": config.password_security.min_length,
                "max_length": config.password_security.max_length,
                "required_char_classes": config.password_security.required_char_classes,
                "min_entropy_bits": config.password_security.min_entropy_bits,
            },
            # Claude CLI integration (Story #15 AC3, Story #20: moved to claude_integration_config)
            "claude_cli": {
                "anthropic_api_key": (
                    config.claude_integration_config.anthropic_api_key[:10] + "***"
                    if config.claude_integration_config.anthropic_api_key
                    else None
                ),
                "voyageai_api_key": (
                    config.claude_integration_config.voyageai_api_key[:6] + "***"
                    if config.claude_integration_config.voyageai_api_key
                    else None
                ),
                "cohere_api_key": (
                    config.claude_integration_config.cohere_api_key[:6] + "***"
                    if config.claude_integration_config.cohere_api_key
                    else None
                ),
                "max_concurrent_claude_cli": config.claude_integration_config.max_concurrent_claude_cli,
                "description_refresh_interval_hours": config.claude_integration_config.description_refresh_interval_hours,
                "description_refresh_enabled": config.claude_integration_config.description_refresh_enabled,
                "research_assistant_timeout_seconds": config.claude_integration_config.research_assistant_timeout_seconds,
                "dependency_map_enabled": config.claude_integration_config.dependency_map_enabled,
                "dependency_map_interval_hours": config.claude_integration_config.dependency_map_interval_hours,
                "dependency_map_pass_timeout_seconds": config.claude_integration_config.dependency_map_pass_timeout_seconds,
                "dependency_map_pass1_max_turns": config.claude_integration_config.dependency_map_pass1_max_turns,
                "dependency_map_pass2_max_turns": config.claude_integration_config.dependency_map_pass2_max_turns,
                "dependency_map_delta_max_turns": config.claude_integration_config.dependency_map_delta_max_turns,
                "refinement_enabled": config.claude_integration_config.refinement_enabled,
                "refinement_interval_hours": config.claude_integration_config.refinement_interval_hours,
                "refinement_domains_per_run": config.claude_integration_config.refinement_domains_per_run,
                "claude_auth_mode": config.claude_integration_config.claude_auth_mode,
                "llm_creds_provider_url": config.claude_integration_config.llm_creds_provider_url,
                "llm_creds_provider_api_key": (
                    config.claude_integration_config.llm_creds_provider_api_key[:6]
                    + "***"
                    if config.claude_integration_config.llm_creds_provider_api_key
                    else None
                ),
                "llm_creds_provider_consumer_id": config.claude_integration_config.llm_creds_provider_consumer_id,
                "dep_map_fact_check_enabled": config.claude_integration_config.dep_map_fact_check_enabled,
                "dep_map_auto_repair_enabled": config.claude_integration_config.dep_map_auto_repair_enabled,
            },
            # OIDC/SSO authentication
            "oidc": {
                "enabled": config.oidc_provider_config.enabled,
                "issuer_url": config.oidc_provider_config.issuer_url,
                "client_id": config.oidc_provider_config.client_id,
                "client_secret": config.oidc_provider_config.client_secret,
                "scopes": config.oidc_provider_config.scopes,
                "email_claim": config.oidc_provider_config.email_claim,
                "username_claim": config.oidc_provider_config.username_claim,
                "use_pkce": config.oidc_provider_config.use_pkce,
                "require_email_verification": config.oidc_provider_config.require_email_verification,
                "enable_jit_provisioning": config.oidc_provider_config.enable_jit_provisioning,
                "default_role": config.oidc_provider_config.default_role,
                "groups_claim": config.oidc_provider_config.groups_claim,
                "group_mappings": config.oidc_provider_config.group_mappings,
            },
            # SCIP workspace cleanup (Story #647, Story #15 AC2: moved to scip_config)
            "scip_cleanup": {
                "scip_workspace_retention_days": config.scip_config.scip_workspace_retention_days,
            },
            # Telemetry configuration (Story #695)
            "telemetry": {
                "enabled": config.telemetry_config.enabled,
                "collector_endpoint": config.telemetry_config.collector_endpoint,
                "collector_protocol": config.telemetry_config.collector_protocol,
                "service_name": config.telemetry_config.service_name,
                "export_traces": config.telemetry_config.export_traces,
                "export_metrics": config.telemetry_config.export_metrics,
                "machine_metrics_enabled": config.telemetry_config.machine_metrics_enabled,
                "machine_metrics_interval_seconds": config.telemetry_config.machine_metrics_interval_seconds,
                "deployment_environment": config.telemetry_config.deployment_environment,
            },
            # Langfuse configuration (Story #136, Story #164)
            "langfuse": {
                "enabled": (
                    config.langfuse_config.enabled if config.langfuse_config else False
                ),
                "public_key": (
                    config.langfuse_config.public_key if config.langfuse_config else ""
                ),
                "secret_key": (
                    config.langfuse_config.secret_key if config.langfuse_config else ""
                ),
                "host": (
                    config.langfuse_config.host
                    if config.langfuse_config
                    else "https://cloud.langfuse.com"
                ),
                "auto_trace_enabled": (
                    config.langfuse_config.auto_trace_enabled
                    if config.langfuse_config
                    else False
                ),
                # Story #164: Langfuse Trace Pull Configuration
                "pull_enabled": (
                    config.langfuse_config.pull_enabled
                    if config.langfuse_config
                    else False
                ),
                "pull_host": (
                    config.langfuse_config.pull_host
                    if config.langfuse_config
                    else "https://cloud.langfuse.com"
                ),
                "pull_projects": (
                    [asdict(p) for p in config.langfuse_config.pull_projects]
                    if config.langfuse_config
                    else []
                ),
                "pull_sync_interval_seconds": (
                    config.langfuse_config.pull_sync_interval_seconds
                    if config.langfuse_config
                    else 300
                ),
                "pull_trace_age_days": (
                    config.langfuse_config.pull_trace_age_days
                    if config.langfuse_config
                    else 30
                ),
                "pull_max_concurrent_observations": (
                    config.langfuse_config.pull_max_concurrent_observations
                    if config.langfuse_config
                    else 5
                ),
            },
            # Claude Delegation configuration (Story #721)
            "claude_delegation": self._get_delegation_settings(),
            # Story #3 - Configuration Consolidation: Migrated settings
            "search_limits": {
                "max_result_size_mb": config.search_limits_config.max_result_size_mb,
                "timeout_seconds": config.search_limits_config.timeout_seconds,
            },
            "golden_repos": {
                "refresh_interval_seconds": config.golden_repos_config.refresh_interval_seconds,
                "analysis_model": config.golden_repos_config.analysis_model,
            },
            # Story #3 - Phase 2: P0/P1 settings
            "mcp_session": {
                "session_ttl_seconds": config.mcp_session_config.session_ttl_seconds,
                "cleanup_interval_seconds": config.mcp_session_config.cleanup_interval_seconds,
            },
            "health": {
                "memory_warning_threshold_percent": config.health_config.memory_warning_threshold_percent,
                "memory_critical_threshold_percent": config.health_config.memory_critical_threshold_percent,
                "disk_warning_threshold_percent": config.health_config.disk_warning_threshold_percent,
                "disk_critical_threshold_percent": config.health_config.disk_critical_threshold_percent,
                "cpu_sustained_threshold_percent": config.health_config.cpu_sustained_threshold_percent,
            },
            "scip": {
                "indexing_timeout_seconds": config.scip_config.indexing_timeout_seconds,
                "scip_generation_timeout_seconds": config.scip_config.scip_generation_timeout_seconds,
                "temporal_stale_threshold_days": config.scip_config.temporal_stale_threshold_days,
                # P3 settings (AC31-AC34)
                "scip_reference_limit": config.scip_config.scip_reference_limit,
                "scip_dependency_depth": config.scip_config.scip_dependency_depth,
                "scip_callchain_max_depth": config.scip_config.scip_callchain_max_depth,
                "scip_callchain_limit": config.scip_config.scip_callchain_limit,
            },
            # Story #3 - Phase 2: P2 settings (AC12-AC14, AC27-AC28)
            "git_timeouts": {
                "git_local_timeout": config.git_timeouts_config.git_local_timeout,
                "git_remote_timeout": config.git_timeouts_config.git_remote_timeout,
                "github_api_timeout": config.git_timeouts_config.github_api_timeout,
                "gitlab_api_timeout": config.git_timeouts_config.gitlab_api_timeout,
            },
            "error_handling": {
                "max_retry_attempts": config.error_handling_config.max_retry_attempts,
                "base_retry_delay_seconds": config.error_handling_config.base_retry_delay_seconds,
                "max_retry_delay_seconds": config.error_handling_config.max_retry_delay_seconds,
            },
            "api_limits": {
                "default_file_read_lines": config.api_limits_config.default_file_read_lines,
                "max_file_read_lines": config.api_limits_config.max_file_read_lines,
                "default_diff_lines": config.api_limits_config.default_diff_lines,
                "max_diff_lines": config.api_limits_config.max_diff_lines,
                "default_log_commits": config.api_limits_config.default_log_commits,
                "max_log_commits": config.api_limits_config.max_log_commits,
                # P3 settings (AC35, AC38-AC39)
                "audit_log_default_limit": config.api_limits_config.audit_log_default_limit,
                "log_page_size_default": config.api_limits_config.log_page_size_default,
                "log_page_size_max": config.api_limits_config.log_page_size_max,
            },
            "web_security": {
                # Story #683 AC2: csrf_max_age_seconds removed (dead field).
                "web_session_timeout_seconds": config.web_security_config.web_session_timeout_seconds,
            },
            # Story #683 AC3: "auth" section removed (AuthConfig deleted entirely).
            # Story #25/29 - Multi-search limits configuration (includes omni settings)
            "multi_search": {
                "multi_search_max_workers": config.multi_search_limits_config.multi_search_max_workers,
                "multi_search_timeout_seconds": config.multi_search_limits_config.multi_search_timeout_seconds,
                "scip_multi_max_workers": config.multi_search_limits_config.scip_multi_max_workers,
                "scip_multi_timeout_seconds": config.multi_search_limits_config.scip_multi_timeout_seconds,
                # Story #29: Omni settings merged from OmniSearchConfig
                "omni_max_workers": config.multi_search_limits_config.omni_max_workers,
                "omni_per_repo_timeout_seconds": config.multi_search_limits_config.omni_per_repo_timeout_seconds,
                "omni_cache_max_entries": config.multi_search_limits_config.omni_cache_max_entries,
                "omni_cache_ttl_seconds": config.multi_search_limits_config.omni_cache_ttl_seconds,
                "omni_default_limit": config.multi_search_limits_config.omni_default_limit,
                "omni_max_limit": config.multi_search_limits_config.omni_max_limit,
                "omni_default_aggregation_mode": config.multi_search_limits_config.omni_default_aggregation_mode,
                "omni_max_results_per_repo": config.multi_search_limits_config.omni_max_results_per_repo,
                "omni_pattern_metacharacters": config.multi_search_limits_config.omni_pattern_metacharacters,
                # Bug #881 Phase 3: wildcard fan-out cap
                "omni_wildcard_expansion_cap": config.multi_search_limits_config.omni_wildcard_expansion_cap,
                # Bug #894: total per-search fan-out cap (after expansion + literal union)
                "omni_max_repos_per_search": config.multi_search_limits_config.omni_max_repos_per_search,
            },
            # Story #26 - Background jobs configuration, Story #27 - SubprocessExecutor max_workers
            # Note: job history retention period moved to data_retention section (Story #400 - AC5)
            "background_jobs": {
                "max_concurrent_background_jobs": config.background_jobs_config.max_concurrent_background_jobs,
                "subprocess_max_workers": config.background_jobs_config.subprocess_max_workers,
            },
            # Story #400 - Unified data retention configuration
            "data_retention": {
                "operational_logs_retention_hours": config.data_retention_config.operational_logs_retention_hours,  # type: ignore[union-attr]
                "audit_logs_retention_hours": config.data_retention_config.audit_logs_retention_hours,  # type: ignore[union-attr]
                "sync_jobs_retention_hours": config.data_retention_config.sync_jobs_retention_hours,  # type: ignore[union-attr]
                "dep_map_history_retention_hours": config.data_retention_config.dep_map_history_retention_hours,  # type: ignore[union-attr]
                "background_jobs_retention_hours": config.data_retention_config.background_jobs_retention_hours,  # type: ignore[union-attr]
                "cleanup_interval_hours": config.data_retention_config.cleanup_interval_hours,  # type: ignore[union-attr]
            },
            # Story #223 - AC4: Indexing configuration
            "indexing": {
                "indexable_extensions": (
                    config.indexing_config.indexable_extensions
                    if config.indexing_config is not None
                    else []
                ),
            },
            # Story #323 - Wiki metadata fields configuration
            # Story #325 - Configurable metadata display order
            "wiki_config": {
                "enable_header_block_parsing": (
                    config.wiki_config.enable_header_block_parsing
                    if config.wiki_config is not None
                    else True
                ),
                "enable_article_number": (
                    config.wiki_config.enable_article_number
                    if config.wiki_config is not None
                    else True
                ),
                "enable_publication_status": (
                    config.wiki_config.enable_publication_status
                    if config.wiki_config is not None
                    else True
                ),
                "enable_views_seeding": (
                    config.wiki_config.enable_views_seeding
                    if config.wiki_config is not None
                    else True
                ),
                "metadata_display_order": (
                    config.wiki_config.metadata_display_order
                    if config.wiki_config is not None
                    else ""
                ),
            },
        }

        # Reranking settings (Story #652)
        rerank_cfg = config.rerank_config or RerankConfig()
        settings["rerank"] = {
            "voyage_reranker_model": rerank_cfg.voyage_reranker_model,
            "cohere_reranker_model": rerank_cfg.cohere_reranker_model,
            "overfetch_multiplier": rerank_cfg.overfetch_multiplier,
        }

        # Memory retrieval settings (Story #883)
        from code_indexer.server.utils.config_manager import MemoryRetrievalConfig

        mem_cfg = config.memory_retrieval_config or MemoryRetrievalConfig()
        settings["memory_retrieval"] = {
            "memory_retrieval_enabled": mem_cfg.memory_retrieval_enabled,
            "memory_voyage_min_score": mem_cfg.memory_voyage_min_score,
            "memory_cohere_min_score": mem_cfg.memory_cohere_min_score,
            "memory_retrieval_k_multiplier": mem_cfg.memory_retrieval_k_multiplier,
            "memory_retrieval_max_body_chars": mem_cfg.memory_retrieval_max_body_chars,
        }

        # Story #885 Phase 5a: Lifecycle analysis timeout configuration (A7a)
        # lifecycle_analysis_config is guaranteed non-None by ServerConfig.__post_init__
        settings["lifecycle_analysis"] = {
            "shell_timeout_seconds": config.lifecycle_analysis_config.shell_timeout_seconds,  # type: ignore[union-attr]
            "outer_timeout_seconds": config.lifecycle_analysis_config.outer_timeout_seconds,  # type: ignore[union-attr]
        }

        # Story #844: Codex CLI integration configuration
        # codex_integration_config is guaranteed non-None by ServerConfig.__post_init__
        assert config.codex_integration_config is not None
        cx_cfg = config.codex_integration_config
        settings["codex_integration"] = {
            "enabled": cx_cfg.enabled,
            "credential_mode": cx_cfg.credential_mode,
            "api_key": (cx_cfg.api_key[:6] + "***" if cx_cfg.api_key else None),
            "lcp_url": cx_cfg.lcp_url,
            "lcp_vendor": cx_cfg.lcp_vendor,
            "codex_weight": cx_cfg.codex_weight,
        }
        backup_cfg = config.cidx_meta_backup_config or CidxMetaBackupConfig()
        settings["cidx_meta_backup"] = {
            "enabled": backup_cfg.enabled,
            "remote_url": backup_cfg.remote_url,
        }

        return settings

    def _get_delegation_settings(self) -> Dict[str, Any]:
        """Get Claude Delegation settings for display (credential masked)."""
        delegation_config = self._delegation_manager.load_config()
        if delegation_config is None:
            delegation_config = ClaudeDelegationConfig()

        return {
            "function_repo_alias": delegation_config.function_repo_alias,
            "claude_server_url": delegation_config.claude_server_url,
            "claude_server_username": delegation_config.claude_server_username,
            "claude_server_credential_type": delegation_config.claude_server_credential_type,
            "is_configured": delegation_config.is_configured,
            "cidx_callback_url": delegation_config.cidx_callback_url,  # Story #720
            "skip_ssl_verify": delegation_config.skip_ssl_verify,  # Allow self-signed certs for E2E
            "guardrails_enabled": delegation_config.guardrails_enabled,  # Story #457
            "delegation_guardrails_repo": delegation_config.delegation_guardrails_repo,  # Story #457
            "delegation_default_engine": delegation_config.delegation_default_engine,  # Story #459
            "delegation_default_mode": delegation_config.delegation_default_mode,  # Story #459
        }

    def update_setting(
        self, category: str, key: str, value: Any, skip_validation: bool = False
    ) -> None:
        """
        Update a single setting.

        Args:
            category: Setting category (server, cache, reindexing, timeouts, password_security)
            key: Setting key within the category
            value: New value for the setting
            skip_validation: If True, skip validation and save (for batch updates)

        Raises:
            ValueError: If category or key is invalid, or value fails validation
        """
        config = self.get_config()

        if category == "server":
            self._update_server_setting(config, key, value)
        elif category == "cache":
            self._update_cache_setting(config, key, value)
        elif category == "timeouts":
            self._update_timeout_setting(config, key, value)
        elif category == "password_security":
            self._update_password_security_setting(config, key, value)
        elif category == "claude_cli":
            self._update_claude_cli_setting(config, key, value)
        elif category == "oidc":
            self._update_oidc_setting(config, key, value)
        elif category == "scip_cleanup":
            self._update_scip_cleanup_setting(config, key, value)
        elif category == "telemetry":
            self._update_telemetry_setting(config, key, value)
        elif category == "langfuse":
            self._update_langfuse_setting(config, key, value)
        # Story #3 - Configuration Consolidation: New categories
        elif category == "search_limits":
            self._update_search_limits_setting(config, key, value)
        elif category == "golden_repos":
            self._update_golden_repos_setting(config, key, value)
        # Story #3 - Phase 2: P0/P1 categories
        elif category == "mcp_session":
            self._update_mcp_session_setting(config, key, value)
        elif category == "health":
            self._update_health_setting(config, key, value)
        elif category == "scip":
            self._update_scip_setting(config, key, value)
        # Story #3 - Phase 2: P2 categories (AC12-AC26)
        elif category == "git_timeouts":
            self._update_git_timeouts_setting(config, key, value)
        elif category == "error_handling":
            self._update_error_handling_setting(config, key, value)
        elif category == "api_limits":
            self._update_api_limits_setting(config, key, value)
        elif category == "web_security":
            self._update_web_security_setting(config, key, value)
        # Story #683 AC3: "auth" category removed (AuthConfig deleted).
        # Story #25/29 - Multi-search limits (includes omni settings)
        elif category == "multi_search":
            self._update_multi_search_setting(config, key, value)
        # Story #26 - Background jobs
        elif category == "background_jobs":
            self._update_background_jobs_setting(config, key, value)
        # Story #400 - Data retention configuration
        elif category == "data_retention":
            self._update_data_retention_setting(config, key, value)
        # Story #223 - AC4: Indexing configuration
        elif category == "indexing":
            self._update_indexing_setting(key, value)
            # _update_indexing_setting saves config internally, so skip normal save below
            return
        # Story #323 - Wiki metadata fields configuration
        elif category == "wiki":
            self._update_wiki_setting(config, key, value)
        # Story #652 - Reranking configuration
        elif category == "rerank":
            self._update_rerank_setting(config, key, value)
        # Story #883 - Memory retrieval configuration
        elif category == "memory_retrieval":
            self._update_memory_retrieval_setting(config, key, value)
        # Story #885 - Lifecycle analysis timeouts
        elif category == "lifecycle_analysis":
            self._update_lifecycle_analysis_setting(config, key, value)
        # Story #844 - Codex CLI integration
        elif category == "codex_integration":
            self._update_codex_integration_setting(config, key, value)
        elif category == "cidx_meta_backup":
            self._update_cidx_meta_backup_setting(config, key, value)
        else:
            raise ValueError(f"Unknown category: {category}")

        # Validate and save (unless skipping for batch updates)
        if not skip_validation:
            self.config_manager.validate_config(config)
            self.save_config(config)
            logger.info(
                "Updated setting %s.%s to %s",
                category,
                key,
                value,
                extra={"correlation_id": get_correlation_id()},
            )
        else:
            # Just update in memory, don't validate or save yet
            logger.debug(
                "Updated setting %s.%s to %s (validation deferred)",
                category,
                key,
                value,
                extra={"correlation_id": get_correlation_id()},
            )

    def _update_server_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a server setting."""
        if key == "host":
            config.host = str(value)
        elif key == "port":
            config.port = int(value)
        elif key == "workers":
            config.workers = int(value)
        elif key == "log_level":
            config.log_level = str(value).upper()
        elif key == "jwt_expiration_minutes":
            config.jwt_expiration_minutes = int(value)
        # Story #22 - Configurable service display name
        elif key == "service_display_name":
            config.service_display_name = str(value)
        else:
            raise ValueError(f"Unknown server setting: {key}")

    def _update_cache_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update a cache setting."""
        cache = config.cache_config
        assert cache is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "index_cache_ttl_minutes":
            cache.index_cache_ttl_minutes = float(value)
        elif key == "index_cache_cleanup_interval":
            cache.index_cache_cleanup_interval = int(value)
        elif key == "index_cache_max_size_mb":
            cache.index_cache_max_size_mb = int(value) if value else None
        elif key == "fts_cache_ttl_minutes":
            cache.fts_cache_ttl_minutes = float(value)
        elif key == "fts_cache_cleanup_interval":
            cache.fts_cache_cleanup_interval = int(value)
        elif key == "fts_cache_max_size_mb":
            cache.fts_cache_max_size_mb = int(value) if value else None
        elif key == "fts_cache_reload_on_access":
            cache.fts_cache_reload_on_access = bool(value)
        # Payload cache settings (Story #679)
        elif key == "payload_preview_size_chars":
            cache.payload_preview_size_chars = int(value)
        elif key == "payload_max_fetch_size_chars":
            cache.payload_max_fetch_size_chars = int(value)
        elif key == "payload_cache_ttl_seconds":
            cache.payload_cache_ttl_seconds = int(value)
        elif key == "payload_cleanup_interval_seconds":
            cache.payload_cleanup_interval_seconds = int(value)
        else:
            raise ValueError(f"Unknown cache setting: {key}")

        # Bug #878 Fix B.2: hot-reload max_cache_size_mb on the matching live
        # cache singleton so operators can bound native HNSW / FTS memory at
        # runtime without a server restart. Fix B.1 seats a default cap at
        # init time; Fix B.2 lets that cap change dynamically.
        #
        # Only the two size-cap keys trigger hot-reload. All other cache
        # settings write through to config only (by design -- see test
        # TestHotReloadScopeIsolation).
        if key == "index_cache_max_size_mb":
            self._hot_reload_cache_size_cap(
                cache_kind="HNSW", new_size_mb=cache.index_cache_max_size_mb
            )
        elif key == "fts_cache_max_size_mb":
            self._hot_reload_cache_size_cap(
                cache_kind="FTS", new_size_mb=cache.fts_cache_max_size_mb
            )

    def _hot_reload_cache_size_cap(
        self, cache_kind: str, new_size_mb: Optional[int]
    ) -> None:
        """
        Bug #878 Fix B.2: propagate a ``max_cache_size_mb`` change to the
        live HNSW or FTS cache singleton.

        Acquires the cache's ``_cache_lock``, overwrites
        ``cache.config.max_cache_size_mb``, and runs ``_enforce_size_limit``
        so any entries that now exceed the new cap are evicted immediately.

        If the cache layer is not importable / the singleton does not exist
        yet, the exception is logged at WARNING and swallowed -- config
        persistence has already happened and we must not leave the caller
        with an incomplete write on the file side.

        Args:
            cache_kind: "HNSW" or "FTS" (drives singleton selection + logs).
            new_size_mb: New cap in MB, or ``None`` to disable the cap.
        """
        try:
            from code_indexer.server.cache import (
                DEFAULT_MAX_CACHE_SIZE_MB,
                get_global_cache,
                get_global_fts_cache,
            )

            if cache_kind == "HNSW":
                cache = get_global_cache()
            elif cache_kind == "FTS":
                cache = get_global_fts_cache()
            else:
                raise ValueError(f"Unknown cache_kind: {cache_kind!r}")

            # Bug #880: when DB value is None (operator cleared the field to
            # "use default"), re-apply the 4096 MiB safety floor to the live
            # singleton.  The invariant from Bug #878 is: runtime cap is NEVER
            # None post-startup.  The DB stores None correctly (meaning "no
            # override"); only the live singleton needs the concrete floor.
            runtime_cap = (
                DEFAULT_MAX_CACHE_SIZE_MB if new_size_mb is None else new_size_mb
            )

            with cache._cache_lock:
                cache.config.max_cache_size_mb = runtime_cap
                cache._enforce_size_limit()

            logger.info(
                "Hot-reloaded %s cache max_cache_size_mb=%s (db_value=%s)",
                cache_kind,
                runtime_cap,
                new_size_mb,
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to hot-reload %s cache max_cache_size_mb=%s: %s",
                cache_kind,
                new_size_mb,
                exc,
                extra={"correlation_id": get_correlation_id()},
            )

    def _update_timeout_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a timeout setting."""
        timeouts = config.resource_config
        assert timeouts is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "git_clone_timeout":
            timeouts.git_clone_timeout = int(value)
        elif key == "git_pull_timeout":
            timeouts.git_pull_timeout = int(value)
        elif key == "git_refresh_timeout":
            timeouts.git_refresh_timeout = int(value)
        elif key == "hnsw_max_elements":
            timeouts.hnsw_max_elements = int(value)
        # Story #683 AC7: 8 genuinely-wired advanced timeout fields
        elif key == "git_init_conflict_timeout":
            timeouts.git_init_conflict_timeout = int(value)
        elif key == "git_service_conflict_timeout":
            timeouts.git_service_conflict_timeout = int(value)
        elif key == "git_service_cleanup_timeout":
            timeouts.git_service_cleanup_timeout = int(value)
        elif key == "git_service_wait_timeout":
            timeouts.git_service_wait_timeout = int(value)
        elif key == "git_process_check_timeout":
            timeouts.git_process_check_timeout = int(value)
        elif key == "git_untracked_file_timeout":
            timeouts.git_untracked_file_timeout = int(value)
        elif key == "cow_clone_timeout":
            timeouts.cow_clone_timeout = int(value)
        elif key == "cidx_fix_config_timeout":
            timeouts.cidx_fix_config_timeout = int(value)
        else:
            raise ValueError(f"Unknown timeout setting: {key}")

    def _update_password_security_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a password security setting."""
        pwd = config.password_security
        assert pwd is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "min_length":
            pwd.min_length = int(value)
        elif key == "max_length":
            pwd.max_length = int(value)
        elif key == "required_char_classes":
            pwd.required_char_classes = int(value)
        elif key == "min_entropy_bits":
            pwd.min_entropy_bits = int(value)
        else:
            raise ValueError(f"Unknown password security setting: {key}")

    def _update_claude_cli_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a Claude CLI setting (Story #15 AC3: use claude_integration_config)."""
        claude_config = config.claude_integration_config
        assert claude_config is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "anthropic_api_key":
            claude_config.anthropic_api_key = str(value) if value else None
        elif key == "max_concurrent_claude_cli":
            claude_config.max_concurrent_claude_cli = int(value)
        elif key == "description_refresh_interval_hours":
            claude_config.description_refresh_interval_hours = int(value)
        elif key == "description_refresh_enabled":
            claude_config.description_refresh_enabled = value in ["true", True, "True"]
        elif key == "research_assistant_timeout_seconds":
            claude_config.research_assistant_timeout_seconds = int(value)
        elif key == "dependency_map_enabled":
            claude_config.dependency_map_enabled = value in ["true", True, "True"]
        elif key == "dependency_map_interval_hours":
            claude_config.dependency_map_interval_hours = max(1, int(value))
        elif key == "dependency_map_pass_timeout_seconds":
            claude_config.dependency_map_pass_timeout_seconds = max(60, int(value))
        elif key == "dependency_map_pass1_max_turns":
            claude_config.dependency_map_pass1_max_turns = max(0, int(value))
        elif key == "dependency_map_pass2_max_turns":
            claude_config.dependency_map_pass2_max_turns = max(5, int(value))
        elif key == "dependency_map_delta_max_turns":
            claude_config.dependency_map_delta_max_turns = max(5, int(value))
        elif key == "refinement_enabled":
            claude_config.refinement_enabled = value in ["true", True, "True"]
        elif key == "refinement_interval_hours":
            claude_config.refinement_interval_hours = max(1, int(value))
        elif key == "refinement_domains_per_run":
            claude_config.refinement_domains_per_run = min(50, max(1, int(value)))
        elif key == "claude_auth_mode":
            allowed = {"api_key", "subscription"}
            str_value = str(value)
            if str_value not in allowed:
                raise ValueError(
                    f"Invalid claude_auth_mode '{value}': must be one of {sorted(allowed)}"
                )
            claude_config.claude_auth_mode = str_value
        elif key == "llm_creds_provider_url":
            claude_config.llm_creds_provider_url = str(value) if value else ""
        elif key == "llm_creds_provider_api_key":
            claude_config.llm_creds_provider_api_key = str(value) if value else ""
        elif key == "llm_creds_provider_consumer_id":
            claude_config.llm_creds_provider_consumer_id = str(value) if value else ""
        elif key == "dep_map_fact_check_enabled":
            claude_config.dep_map_fact_check_enabled = _parse_bool(value)
        elif key == "dep_map_auto_repair_enabled":
            claude_config.dep_map_auto_repair_enabled = _parse_bool(value)
        elif key == "fact_check_timeout_seconds":
            claude_config.fact_check_timeout_seconds = max(
                _MIN_FACT_CHECK_TIMEOUT_SECONDS, int(value)
            )
        elif key == "scheduled_catchup_enabled":
            claude_config.scheduled_catchup_enabled = _parse_bool(value)
        elif key == "scheduled_catchup_interval_minutes":
            claude_config.scheduled_catchup_interval_minutes = max(
                _MIN_SCHEDULED_CATCHUP_INTERVAL_MINUTES, int(value)
            )
        elif key == "cohere_api_key":
            claude_config.cohere_api_key = str(value) if value else None
        else:
            raise ValueError(f"Unknown claude_cli setting: {key}")

    def _update_oidc_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update an OIDC setting."""
        oidc = config.oidc_provider_config
        assert oidc is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "enabled":
            oidc.enabled = value in ["true", True]
        elif key == "issuer_url":
            oidc.issuer_url = str(value)
        elif key == "client_id":
            oidc.client_id = str(value)
        elif key == "client_secret":
            # Only update if value is provided (not empty)
            if value:
                oidc.client_secret = str(value)
        elif key == "scopes":
            # Convert space-separated string to list
            oidc.scopes = (
                str(value).split() if value else ["openid", "profile", "email"]
            )
        elif key == "email_claim":
            oidc.email_claim = str(value)
        elif key == "username_claim":
            oidc.username_claim = str(value)
        elif key == "use_pkce":
            oidc.use_pkce = value in ["true", True]
        elif key == "require_email_verification":
            oidc.require_email_verification = value in ["true", True]
        elif key == "enable_jit_provisioning":
            oidc.enable_jit_provisioning = value in ["true", True]
        elif key == "default_role":
            oidc.default_role = str(value)
        elif key == "groups_claim":
            oidc.groups_claim = str(value)
        elif key == "group_mappings":
            # Parse JSON string, dict (old format), or list (new format)
            import json

            if isinstance(value, (dict, list)):
                oidc.group_mappings = value
            elif isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if not isinstance(parsed, (dict, list)):
                        raise ValueError(
                            f"Invalid JSON for group_mappings: must be dict or list, got {type(parsed)}"
                        )
                    oidc.group_mappings = parsed
                except json.JSONDecodeError:
                    raise ValueError(
                        f"Invalid JSON for group_mappings: {value}. Expected format: [{{'external_group_id': 'guid', 'cidx_group': 'admins'}}]"
                    )
            else:
                raise ValueError(
                    f"Invalid type for group_mappings: {type(value)}. Expected dict, list, or JSON string"
                )
        else:
            raise ValueError(f"Unknown OIDC setting: {key}")

    def _update_scip_cleanup_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a SCIP cleanup setting (Story #647, Story #15 AC2: use scip_config)."""
        scip_config = config.scip_config
        assert scip_config is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "scip_workspace_retention_days":
            scip_config.scip_workspace_retention_days = int(value)
        else:
            raise ValueError(f"Unknown SCIP cleanup setting: {key}")

    def _update_telemetry_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a telemetry setting (Story #695)."""
        telemetry = config.telemetry_config
        assert telemetry is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "enabled":
            telemetry.enabled = value in ["true", True, "True", "1"]
        elif key == "collector_endpoint":
            telemetry.collector_endpoint = str(value)
        elif key == "collector_protocol":
            telemetry.collector_protocol = str(value).lower()
        elif key == "service_name":
            telemetry.service_name = str(value)
        elif key == "export_traces":
            telemetry.export_traces = value in ["true", True, "True", "1"]
        elif key == "export_metrics":
            telemetry.export_metrics = value in ["true", True, "True", "1"]
        elif key == "machine_metrics_enabled":
            telemetry.machine_metrics_enabled = value in ["true", True, "True", "1"]
        elif key == "machine_metrics_interval_seconds":
            telemetry.machine_metrics_interval_seconds = int(value)
        elif key == "deployment_environment":
            telemetry.deployment_environment = str(value)
        else:
            raise ValueError(f"Unknown telemetry setting: {key}")

    def _update_langfuse_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a langfuse setting (Story #136, Story #164)."""
        from ..utils.config_manager import LangfuseConfig, LangfusePullProject

        # Initialize langfuse_config if None
        if config.langfuse_config is None:
            config.langfuse_config = LangfuseConfig()
        langfuse = config.langfuse_config
        if key == "enabled":
            langfuse.enabled = value in ["true", True, "True", "1"]
        elif key == "public_key":
            langfuse.public_key = str(value)
        elif key == "secret_key":
            langfuse.secret_key = str(value)
        elif key == "host":
            langfuse.host = str(value)
        elif key == "auto_trace_enabled":
            langfuse.auto_trace_enabled = value in ["true", True, "True", "1"]
        # Story #164: Langfuse Trace Pull settings
        elif key == "pull_enabled":
            langfuse.pull_enabled = value in ["true", True, "True", "1"]
        elif key == "pull_host":
            langfuse.pull_host = (
                value.strip() if value else "https://cloud.langfuse.com"
            )
        elif key == "pull_sync_interval_seconds":
            val = max(60, min(3600, int(value)))
            langfuse.pull_sync_interval_seconds = val
        elif key == "pull_trace_age_days":
            val = max(1, min(365, int(value)))
            langfuse.pull_trace_age_days = val
        elif key == "pull_max_concurrent_observations":
            val = max(1, min(20, int(value)))
            langfuse.pull_max_concurrent_observations = val
        elif key == "pull_projects":
            import json as _json

            projects_data = _json.loads(value) if isinstance(value, str) else value
            langfuse.pull_projects = [
                LangfusePullProject(**p) if isinstance(p, dict) else p
                for p in projects_data
            ]
        else:
            raise ValueError(f"Unknown langfuse setting: {key}")

    def _update_wiki_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update a wiki metadata configuration setting (Story #323)."""
        from ..utils.config_manager import WikiConfig

        if config.wiki_config is None:
            config.wiki_config = WikiConfig()
        wiki = config.wiki_config
        bool_value = value in ["true", True, "True", "1"]
        if key == "enable_header_block_parsing":
            wiki.enable_header_block_parsing = bool_value
        elif key == "enable_article_number":
            wiki.enable_article_number = bool_value
        elif key == "enable_publication_status":
            wiki.enable_publication_status = bool_value
        elif key == "enable_views_seeding":
            wiki.enable_views_seeding = bool_value
        elif key == "metadata_display_order":
            # String field — store as-is (not a boolean toggle)
            wiki.metadata_display_order = str(value)
        else:
            raise ValueError(f"Unknown wiki setting: {key}")

    def _update_rerank_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a reranking configuration setting (Story #652)."""
        if config.rerank_config is None:
            config.rerank_config = RerankConfig()
        rerank = config.rerank_config
        if key == "voyage_reranker_model":
            rerank.voyage_reranker_model = str(value)
        elif key == "cohere_reranker_model":
            rerank.cohere_reranker_model = str(value)
        elif key == "overfetch_multiplier":
            rerank.overfetch_multiplier = int(value)
        else:
            raise ValueError(f"Unknown rerank setting: {key}")

    def _update_memory_retrieval_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a memory retrieval configuration setting (Story #883)."""
        from code_indexer.server.utils.config_manager import MemoryRetrievalConfig

        if config.memory_retrieval_config is None:
            config.memory_retrieval_config = MemoryRetrievalConfig()
        mem = config.memory_retrieval_config
        if key == "memory_retrieval_enabled":
            mem.memory_retrieval_enabled = _parse_bool(value)
        elif key == "memory_voyage_min_score":
            mem.memory_voyage_min_score = float(value)
        elif key == "memory_cohere_min_score":
            mem.memory_cohere_min_score = float(value)
        elif key == "memory_retrieval_k_multiplier":
            mem.memory_retrieval_k_multiplier = int(value)
        elif key == "memory_retrieval_max_body_chars":
            mem.memory_retrieval_max_body_chars = int(value)
        else:
            raise ValueError(f"Unknown memory_retrieval setting: {key}")

    def _update_lifecycle_analysis_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a lifecycle analysis timeout setting (Story #885 A7a/A7c)."""
        lifecycle = config.lifecycle_analysis_config
        assert lifecycle is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "shell_timeout_seconds":
            lifecycle.shell_timeout_seconds = int(value)
        elif key == "outer_timeout_seconds":
            lifecycle.outer_timeout_seconds = int(value)
        else:
            raise ValueError(f"Unknown lifecycle_analysis setting: {key}")

    def _update_codex_integration_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a Codex CLI integration setting (Story #844).

        Mirrors _update_claude_cli_setting but scoped to CodexIntegrationConfig.
        api_key is preserved when the submitted value is a masked placeholder
        (contains '***') to prevent UI re-saves from wiping the stored key.
        """
        from code_indexer.server.utils.config_manager import CodexIntegrationConfig

        if config.codex_integration_config is None:
            config.codex_integration_config = CodexIntegrationConfig()
        cx = config.codex_integration_config

        if key == "enabled":
            cx.enabled = _parse_bool(value)
        elif key == "credential_mode":
            allowed = {"none", "api_key", "subscription"}
            str_val = str(value)
            if str_val not in allowed:
                raise ValueError(
                    f"Invalid credential_mode '{value}': must be one of {sorted(allowed)}"
                )
            cx.credential_mode = str_val
        elif key == "api_key":
            # Preserve existing key when the submitted value is a masked placeholder
            str_val = str(value) if value else ""
            if "***" not in str_val:
                cx.api_key = str_val if str_val else None
            # else: placeholder submitted — do not overwrite the stored key
        elif key == "lcp_url":
            cx.lcp_url = str(value) if value else None
        elif key == "lcp_vendor":
            cx.lcp_vendor = str(value)
        elif key == "codex_weight":
            weight = float(value)
            if not (0.0 <= weight <= 1.0):
                raise ValueError(
                    f"Invalid codex_weight {weight}: must be in [0.0, 1.0]"
                )
            cx.codex_weight = weight
        else:
            raise ValueError(f"Unknown codex_integration setting: {key}")

    def _update_cidx_meta_backup_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update cidx-meta backup runtime settings."""
        if config.cidx_meta_backup_config is None:
            config.cidx_meta_backup_config = CidxMetaBackupConfig()
        backup = config.cidx_meta_backup_config
        if key == "enabled":
            backup.enabled = _parse_bool(value)
        elif key == "remote_url":
            backup.remote_url = str(value).strip()
        else:
            raise ValueError(f"Unknown cidx_meta_backup setting: {key}")

    def _update_search_limits_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a search limits setting (Story #3 - Configuration Consolidation)."""
        search_limits = config.search_limits_config
        assert search_limits is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "max_result_size_mb":
            search_limits.max_result_size_mb = int(value)
        elif key == "timeout_seconds":
            search_limits.timeout_seconds = int(value)
        else:
            raise ValueError(f"Unknown search limits setting: {key}")

    def _update_golden_repos_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a golden repos setting (Story #3 - Configuration Consolidation)."""
        golden_repos = config.golden_repos_config
        assert golden_repos is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "refresh_interval_seconds":
            golden_repos.refresh_interval_seconds = int(value)
        elif key == "analysis_model":
            if value not in ("opus", "sonnet"):
                raise ValueError(f"Invalid analysis_model: {value}")
            golden_repos.analysis_model = value
        else:
            raise ValueError(f"Unknown golden repos setting: {key}")

    def _update_mcp_session_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an MCP session setting (Story #3 - Phase 2)."""
        mcp_session = config.mcp_session_config
        assert mcp_session is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "session_ttl_seconds":
            mcp_session.session_ttl_seconds = int(value)
        elif key == "cleanup_interval_seconds":
            mcp_session.cleanup_interval_seconds = int(value)
        else:
            raise ValueError(f"Unknown MCP session setting: {key}")

    def _update_health_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a health monitoring setting (Story #3 - Phase 2, AC37)."""
        health = config.health_config
        assert health is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "memory_warning_threshold_percent":
            health.memory_warning_threshold_percent = float(value)
        elif key == "memory_critical_threshold_percent":
            health.memory_critical_threshold_percent = float(value)
        elif key == "disk_warning_threshold_percent":
            health.disk_warning_threshold_percent = float(value)
        elif key == "disk_critical_threshold_percent":
            health.disk_critical_threshold_percent = float(value)
        elif key == "cpu_sustained_threshold_percent":
            health.cpu_sustained_threshold_percent = float(value)
        else:
            raise ValueError(f"Unknown health setting: {key}")

    def _update_scip_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update a SCIP setting (Story #3 - Phase 2)."""
        scip = config.scip_config
        assert scip is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "indexing_timeout_seconds":
            scip.indexing_timeout_seconds = int(value)
        elif key == "scip_generation_timeout_seconds":
            scip.scip_generation_timeout_seconds = int(value)
        elif key == "temporal_stale_threshold_days":
            scip.temporal_stale_threshold_days = int(value)
        # P3 settings (AC31-AC34)
        elif key == "scip_reference_limit":
            scip.scip_reference_limit = int(value)
        elif key == "scip_dependency_depth":
            scip.scip_dependency_depth = int(value)
        elif key == "scip_callchain_max_depth":
            scip.scip_callchain_max_depth = int(value)
        elif key == "scip_callchain_limit":
            scip.scip_callchain_limit = int(value)
        else:
            raise ValueError(f"Unknown SCIP setting: {key}")

    def _update_git_timeouts_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a git timeouts setting (Story #3 - Phase 2, AC12-AC14, AC27-AC28)."""
        git_timeouts = config.git_timeouts_config
        assert git_timeouts is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "git_local_timeout":
            git_timeouts.git_local_timeout = int(value)
        elif key == "git_remote_timeout":
            git_timeouts.git_remote_timeout = int(value)
        elif key == "github_api_timeout":
            git_timeouts.github_api_timeout = int(value)
        elif key == "gitlab_api_timeout":
            git_timeouts.gitlab_api_timeout = int(value)
        else:
            raise ValueError(f"Unknown git timeouts setting: {key}")

    def _update_error_handling_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an error handling setting (Story #3 - Phase 2, AC16-AC18)."""
        error_handling = config.error_handling_config
        assert error_handling is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "max_retry_attempts":
            error_handling.max_retry_attempts = int(value)
        elif key == "base_retry_delay_seconds":
            error_handling.base_retry_delay_seconds = float(value)
        elif key == "max_retry_delay_seconds":
            error_handling.max_retry_delay_seconds = float(value)
        else:
            raise ValueError(f"Unknown error handling setting: {key}")

    def _update_api_limits_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an API limits setting (Story #3 - Phase 2, AC19-AC24, AC35, AC38-AC39)."""
        api_limits = config.api_limits_config
        assert api_limits is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "default_file_read_lines":
            api_limits.default_file_read_lines = int(value)
        elif key == "max_file_read_lines":
            api_limits.max_file_read_lines = int(value)
        elif key == "default_diff_lines":
            api_limits.default_diff_lines = int(value)
        elif key == "max_diff_lines":
            api_limits.max_diff_lines = int(value)
        elif key == "default_log_commits":
            api_limits.default_log_commits = int(value)
        elif key == "max_log_commits":
            api_limits.max_log_commits = int(value)
        # P3 settings (AC35, AC38-AC39)
        elif key == "audit_log_default_limit":
            api_limits.audit_log_default_limit = int(value)
        elif key == "log_page_size_default":
            api_limits.log_page_size_default = int(value)
        elif key == "log_page_size_max":
            api_limits.log_page_size_max = int(value)
        else:
            raise ValueError(f"Unknown API limits setting: {key}")

    def _update_web_security_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a web security setting (Story #3 - Phase 2, AC25-AC26).

        Story #683 AC2: csrf_max_age_seconds removed (dead field).
        """
        web_security = config.web_security_config
        assert web_security is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "web_session_timeout_seconds":
            web_security.web_session_timeout_seconds = int(value)
        else:
            raise ValueError(f"Unknown web security setting: {key}")

    def _update_multi_search_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a multi_search setting (Story #25, Story #29)."""
        multi_search = config.multi_search_limits_config
        assert multi_search is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "multi_search_max_workers":
            multi_search.multi_search_max_workers = int(value)
        elif key == "multi_search_timeout_seconds":
            multi_search.multi_search_timeout_seconds = int(value)
        elif key == "scip_multi_max_workers":
            multi_search.scip_multi_max_workers = int(value)
        elif key == "scip_multi_timeout_seconds":
            multi_search.scip_multi_timeout_seconds = int(value)
        # Story #29: Omni settings merged from OmniSearchConfig
        elif key == "omni_max_workers":
            multi_search.omni_max_workers = int(value)
        elif key == "omni_per_repo_timeout_seconds":
            multi_search.omni_per_repo_timeout_seconds = int(value)
        elif key == "omni_cache_max_entries":
            multi_search.omni_cache_max_entries = int(value)
        elif key == "omni_cache_ttl_seconds":
            multi_search.omni_cache_ttl_seconds = int(value)
        elif key == "omni_default_limit":
            multi_search.omni_default_limit = int(value)
        elif key == "omni_max_limit":
            multi_search.omni_max_limit = int(value)
        elif key == "omni_default_aggregation_mode":
            multi_search.omni_default_aggregation_mode = str(value)
        elif key == "omni_max_results_per_repo":
            multi_search.omni_max_results_per_repo = int(value)
        elif key == "omni_pattern_metacharacters":
            multi_search.omni_pattern_metacharacters = str(value)
        # Bug #881 Phase 3: wildcard fan-out cap
        elif key == "omni_wildcard_expansion_cap":
            multi_search.omni_wildcard_expansion_cap = int(value)
        # Bug #894: total per-search fan-out cap
        elif key == "omni_max_repos_per_search":
            multi_search.omni_max_repos_per_search = int(value)
        else:
            raise ValueError(f"Unknown multi_search setting: {key}")

    def _update_background_jobs_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a background_jobs setting (Story #26, Story #27)."""
        background_jobs = config.background_jobs_config
        assert background_jobs is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "max_concurrent_background_jobs":
            background_jobs.max_concurrent_background_jobs = int(value)
        elif key == "subprocess_max_workers":
            # Story #27: SubprocessExecutor max_workers configuration
            background_jobs.subprocess_max_workers = int(value)
        else:
            raise ValueError(f"Unknown background jobs setting: {key}")

    def _update_data_retention_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a data_retention setting (Story #400)."""
        data_retention = config.data_retention_config
        assert data_retention is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "operational_logs_retention_hours":
            data_retention.operational_logs_retention_hours = int(value)
        elif key == "audit_logs_retention_hours":
            data_retention.audit_logs_retention_hours = int(value)
        elif key == "sync_jobs_retention_hours":
            data_retention.sync_jobs_retention_hours = int(value)
        elif key == "dep_map_history_retention_hours":
            data_retention.dep_map_history_retention_hours = int(value)
        elif key == "background_jobs_retention_hours":
            data_retention.background_jobs_retention_hours = int(value)
        elif key == "cleanup_interval_hours":
            data_retention.cleanup_interval_hours = int(value)
        else:
            raise ValueError(f"Unknown data retention setting: {key}")

    def save_all_settings(self, settings: Dict[str, Dict[str, Any]]) -> None:
        """
        Save all settings at once.

        Args:
            settings: Dictionary with category -> {key: value} structure

        Raises:
            ValueError: If any setting fails validation
        """
        config = self.get_config()

        for category, category_settings in settings.items():
            for key, value in category_settings.items():
                if category == "server":
                    self._update_server_setting(config, key, value)
                elif category == "cache":
                    self._update_cache_setting(config, key, value)
                elif category == "timeouts":
                    self._update_timeout_setting(config, key, value)
                elif category == "password_security":
                    self._update_password_security_setting(config, key, value)
                elif category == "claude_cli":
                    self._update_claude_cli_setting(config, key, value)

        # Validate and save
        self.config_manager.validate_config(config)
        self.save_config(config)
        logger.info(
            "Saved all settings", extra={"correlation_id": get_correlation_id()}
        )

    def get_config_file_path(self) -> str:
        """Get the path to the configuration file."""
        return str(self.config_manager.config_file_path)

    def _update_indexing_setting(self, key: str, value: Any) -> None:
        """
        Update an indexing setting (Story #223 - AC4).

        Handles both list and comma-separated string input for indexable_extensions.
        Normalizes extensions to ensure leading dot and lowercase.
        Saves config to disk immediately.

        Args:
            key: Setting key (only 'indexable_extensions' is supported)
            value: New value (list or comma-separated string)

        Raises:
            ValueError: If key is not recognized
        """
        if key != "indexable_extensions":
            raise ValueError(f"Unknown indexing setting: {key}")

        config = self.get_config()
        indexing = config.indexing_config
        if indexing is None:
            from ..utils.config_manager import IndexingConfig

            indexing = IndexingConfig()
            config.indexing_config = indexing

        if isinstance(value, list):
            parsed_list: List[str] = list(value)
        elif isinstance(value, str):
            parsed_list = [ext.strip() for ext in value.split(",") if ext.strip()]
        else:
            parsed_list = list(value)

        # Normalize: ensure leading dot and lowercase
        normalized: List[str] = []
        for ext in parsed_list:
            ext = str(ext).strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if ext:
                normalized.append(ext)

        indexing.indexable_extensions = normalized
        self.save_config(config)
        logger.info(
            "Updated indexing.indexable_extensions with %d extensions",
            len(normalized),
            extra={"correlation_id": get_correlation_id()},
        )

    def cascade_indexable_extensions_to_repos(self) -> None:
        """
        Cascade server indexable_extensions to all golden repo config files (Story #223 - AC5).

        Writes CLI-format extensions (no leading dots) to each repo's
        .code-indexer/config.json. Continues on individual repo failures.
        """
        config = self.get_config()
        if config.indexing_config is None:
            return
        server_exts = config.indexing_config.indexable_extensions
        cli_exts = [ext.lstrip(".") for ext in server_exts]

        from ..repositories.golden_repo_manager import get_golden_repo_manager

        manager = get_golden_repo_manager()
        if manager is None:
            return

        repos = manager.list_golden_repos()
        for repo in repos:
            alias = repo.get("alias", "")
            try:
                repo_path = manager.get_actual_repo_path(alias)
                cidx_config_path = Path(repo_path) / ".code-indexer" / "config.json"
                if not cidx_config_path.exists():
                    continue
                with open(cidx_config_path, "r") as f:
                    repo_config = json.load(f)
                repo_config["file_extensions"] = cli_exts
                with open(cidx_config_path, "w") as f:
                    json.dump(repo_config, f, indent=2)
                logger.info("Cascaded file_extensions to %s", alias)
            except Exception as e:
                logger.warning("Could not cascade extensions to %s: %s", alias, e)

    def seed_repo_extensions_from_server_config(self, repo_path: str) -> None:
        """
        Seed a newly cloned repo's config with server indexable_extensions (Story #223 - AC6).

        Called after cidx init creates the config, before cidx index runs.
        Does nothing if .code-indexer/config.json does not exist.

        Args:
            repo_path: Filesystem path to the cloned repository
        """
        config = self.get_config()
        if config.indexing_config is None:
            return
        server_exts = config.indexing_config.indexable_extensions
        cli_exts = [ext.lstrip(".") for ext in server_exts]

        cidx_config_path = Path(repo_path) / ".code-indexer" / "config.json"
        if not cidx_config_path.exists():
            return

        try:
            with open(cidx_config_path, "r") as f:
                repo_config = json.load(f)
            repo_config["file_extensions"] = cli_exts
            with open(cidx_config_path, "w") as f:
                json.dump(repo_config, f, indent=2)
            logger.info("Seeded file_extensions from server config for %s", repo_path)
        except Exception as e:
            logger.warning("Could not seed extensions for %s: %s", repo_path, e)

    def sync_repo_extensions_if_drifted(self, repo_path: str) -> None:
        """
        Sync repo config if extensions drifted from server config (Story #223 - AC7).

        Called before refresh indexing. No-op if already in sync or config missing.

        Args:
            repo_path: Filesystem path to the repository
        """
        config = self.get_config()
        if config.indexing_config is None:
            return
        server_exts = config.indexing_config.indexable_extensions
        cli_exts_sorted = sorted([ext.lstrip(".") for ext in server_exts])

        cidx_config_path = Path(repo_path) / ".code-indexer" / "config.json"
        if not cidx_config_path.exists():
            return

        try:
            with open(cidx_config_path, "r") as f:
                repo_config = json.load(f)
            current_exts = sorted(repo_config.get("file_extensions", []))
            if current_exts == cli_exts_sorted:
                return  # Already in sync, do not rewrite
            repo_config["file_extensions"] = [ext.lstrip(".") for ext in server_exts]
            with open(cidx_config_path, "w") as f:
                json.dump(repo_config, f, indent=2)
            logger.info("Synced drifted file_extensions for %s", repo_path)
        except Exception as e:
            logger.warning("Could not sync extensions for %s: %s", repo_path, e)

    def check_config_update(self) -> bool:
        """Check if config version changed in PG (called periodically).

        Returns True if config was reloaded.
        """
        if self._pool is None:
            return False
        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT version FROM server_config WHERE config_key = %s",
                ("runtime",),
            ).fetchone()
        if row and row["version"] != self._db_config_version:
            self._load_runtime_from_pg()
            # Fire change callbacks so services can react (Bug #586)
            for cb in self._on_change_callbacks:
                try:
                    cb(self._config)
                except Exception:
                    logger.exception("Config change callback failed")
            return True
        return False

    @staticmethod
    def _extract_runtime_dict(config: "ServerConfig") -> dict:
        """Extract runtime (non-bootstrap) config as dict."""
        full_dict = asdict(config)
        return {k: v for k, v in full_dict.items() if k not in BOOTSTRAP_KEYS}

    @staticmethod
    def _extract_bootstrap_dict(config: "ServerConfig") -> dict:
        """Extract bootstrap config as dict."""
        full_dict = asdict(config)
        return {k: v for k, v in full_dict.items() if k in BOOTSTRAP_KEYS}

    def _save_runtime_to_pg(self, config: "ServerConfig") -> None:
        """Save runtime config to PostgreSQL."""
        assert self._pool is not None
        from psycopg.rows import dict_row

        runtime_dict = self._extract_runtime_dict(config)

        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            conn.execute(
                "UPDATE server_config SET config_json = %s, "
                "version = version + 1, updated_at = CURRENT_TIMESTAMP, "
                "updated_by = %s WHERE config_key = %s",
                (json.dumps(runtime_dict), UPDATER_WEB_UI, CONFIG_KEY_RUNTIME),
            )
            conn.commit()
            row = conn.execute(
                "SELECT version FROM server_config WHERE config_key = %s",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
            if row:
                self._db_config_version = row["version"]
            else:
                logger.error(
                    "Runtime config row missing from server_config after update"
                )

    def _seed_runtime_to_pg(self) -> None:
        """Seed PG server_config table from current config (first boot)."""
        assert self._pool is not None
        config = self.get_config()
        runtime_dict = self._extract_runtime_dict(config)

        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO server_config (config_key, config_json, version, updated_by) "
                "VALUES (%s, %s, 1, %s) "
                "ON CONFLICT (config_key) DO NOTHING",
                (CONFIG_KEY_RUNTIME, json.dumps(runtime_dict), UPDATER_SEED),
            )
            conn.commit()
            # Finding 4 fix: SELECT actual version -- INSERT may have been a
            # no-op if another node already seeded, so version could be > 1.
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT version FROM server_config WHERE config_key = %s",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
            assert row is not None, (
                "server_config row must exist after INSERT ON CONFLICT DO NOTHING"
            )
            self._db_config_version = row["version"]
        logger.info(
            "ConfigService: seeded runtime config to PostgreSQL (%d keys)",
            len(runtime_dict),
        )

    def _load_runtime_from_pg(self) -> None:
        """Load runtime config from PostgreSQL and merge with bootstrap."""
        assert self._pool is not None
        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT config_json, version FROM server_config WHERE config_key = %s",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()

        if row is None:
            self._seed_runtime_to_pg()
            return

        config_json = row["config_json"]
        runtime_dict = (
            json.loads(config_json) if isinstance(config_json, str) else config_json
        )
        self._db_config_version = int(row["version"])
        self._merge_runtime_config(runtime_dict)

    def _strip_config_file_to_bootstrap(self) -> None:
        """Strip config.json to bootstrap-only keys, backing up original."""
        import shutil

        config = self.get_config()
        full_dict = asdict(config)

        # Check if already stripped
        non_bootstrap = [k for k in full_dict if k not in BOOTSTRAP_KEYS]
        if not non_bootstrap:
            return

        # Create backup (only once)
        backup_dir = Path(self.config_manager.server_dir) / "config-migration-backup"
        backup_file = backup_dir / "config.json.pre-centralization"
        if not backup_file.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.config_manager.config_file_path, str(backup_file))
            logger.info("ConfigService: backed up config.json to %s", backup_file)

        # Write bootstrap-only
        bootstrap_dict = self._extract_bootstrap_dict(config)
        self.config_manager.save_config_dict(bootstrap_dict)
        logger.info(
            "ConfigService: stripped config.json to %d bootstrap keys",
            len(bootstrap_dict),
        )

    def _save_runtime_to_sqlite(self, runtime_dict: dict) -> None:
        """Save runtime config to local SQLite server_config table."""
        import sqlite3

        assert self._sqlite_db_path is not None
        conn = sqlite3.connect(self._sqlite_db_path)
        try:
            conn.execute(
                "INSERT INTO server_config "
                "(config_key, config_json, version, updated_by) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(config_key) DO UPDATE SET "
                "config_json = excluded.config_json, "
                "version = server_config.version + 1, "
                "updated_at = datetime('now'), "
                "updated_by = excluded.updated_by",
                (CONFIG_KEY_RUNTIME, json.dumps(runtime_dict), UPDATER_WEB_UI),
            )
            conn.commit()
            row = conn.execute(
                "SELECT version FROM server_config WHERE config_key = ?",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
            if row:
                self._db_config_version = row[0]
        finally:
            conn.close()

    def _load_runtime_from_sqlite(self) -> Optional[dict]:
        """Load runtime config from local SQLite server_config table."""
        import sqlite3

        if not self._sqlite_db_path or not Path(self._sqlite_db_path).exists():
            return None
        conn = sqlite3.connect(self._sqlite_db_path)
        try:
            row = conn.execute(
                "SELECT config_json, version FROM server_config WHERE config_key = ?",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        self._db_config_version = int(row[1])
        result: dict = json.loads(row[0])
        return result

    def _merge_runtime_config(self, runtime_dict: dict) -> None:
        """Merge runtime dict into current config (runtime fields only).

        Reconstructs a full ServerConfig from a merged dict to correctly
        deserialize nested dataclass fields (Finding 1 fix).  Uses atomic
        reference swap for thread safety (Finding 3 fix).
        """
        config = self.get_config()
        full_dict = asdict(config)
        # Overwrite runtime fields only
        for k, v in runtime_dict.items():
            if k not in BOOTSTRAP_KEYS:
                full_dict[k] = v
        # Story #885 Phase 5b (A7d): log when lifecycle_analysis_config is absent
        # from the stored runtime dict so operators know defaults are being applied
        # on first boot after upgrade (no manual action required).
        if "lifecycle_analysis_config" not in runtime_dict:
            logger.info(
                "ConfigService: lifecycle_analysis_config absent from runtime storage "
                "-- applying defaults (shell=%ds, outer=%ds). "
                "No operator action required.",
                full_dict.get("lifecycle_analysis_config", {}).get(
                    "shell_timeout_seconds", 360
                ),
                full_dict.get("lifecycle_analysis_config", {}).get(
                    "outer_timeout_seconds", 420
                ),
            )
        # Reconstruct ServerConfig through the existing deserialization path
        # which correctly converts nested dicts to dataclass instances
        new_config = self.config_manager._dict_to_server_config(full_dict)
        self._config = new_config  # Atomic reference swap

    def save_config(self, config: ServerConfig) -> None:
        """Save config: runtime to DB (PG or SQLite), bootstrap to file.

        Priority: PG pool > SQLite > full file (legacy).
        """
        runtime_dict = self._extract_runtime_dict(config)

        if self._pool is not None:
            self._save_runtime_to_pg(config)
            bootstrap_dict = self._extract_bootstrap_dict(config)
            self.config_manager.save_config_dict(bootstrap_dict)
        elif self._sqlite_db_path is not None:
            self._save_runtime_to_sqlite(runtime_dict)
            bootstrap_dict = self._extract_bootstrap_dict(config)
            self.config_manager.save_config_dict(bootstrap_dict)
        else:
            self.config_manager.save_config(config)
        self._config = config


# Global service instance
_config_service: Optional[ConfigService] = None


def get_config_service() -> ConfigService:
    """Get or create the global ConfigService instance."""
    global _config_service
    if _config_service is None:
        _config_service = ConfigService()
    return _config_service


def reset_config_service() -> None:
    """
    Reset the global ConfigService singleton.

    This is primarily used for testing to ensure each test gets a fresh
    config service instance with its own server directory.
    """
    global _config_service
    _config_service = None
