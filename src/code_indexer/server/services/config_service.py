"""
Configuration Service for CIDX Server Admin UI.

Provides a high-level interface for reading and updating server configuration.
All settings persist to ~/.cidx-server/config.json via ServerConfigManager.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
from typing import Any, Dict, Optional

from ..utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
)
from ..config.delegation_config import ClaudeDelegationManager, ClaudeDelegationConfig

logger = logging.getLogger(__name__)


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

    def get_delegation_manager(self) -> ClaudeDelegationManager:
        """Get the Claude Delegation manager for config operations."""
        return self._delegation_manager

    def load_config(self) -> ServerConfig:
        """
        Load configuration from disk or create default.

        Returns:
            ServerConfig object with current settings
        """
        config = self.config_manager.load_config()
        if config is None:
            config = self.config_manager.create_default_config()
            # Save the default config so it persists
            self.config_manager.save_config(config)

        self._config = config
        return config

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
        assert config.file_content_limits_config is not None
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
        # Story #3 - Phase 2: Assert P3 config objects (AC36)
        assert config.auth_config is not None
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
                "cidx_index_timeout": config.resource_config.cidx_index_timeout,
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
                "max_concurrent_claude_cli": config.claude_integration_config.max_concurrent_claude_cli,
                "description_refresh_interval_hours": config.claude_integration_config.description_refresh_interval_hours,
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
                "export_logs": config.telemetry_config.export_logs,
                "machine_metrics_enabled": config.telemetry_config.machine_metrics_enabled,
                "machine_metrics_interval_seconds": config.telemetry_config.machine_metrics_interval_seconds,
                "trace_sample_rate": config.telemetry_config.trace_sample_rate,
                "deployment_environment": config.telemetry_config.deployment_environment,
            },
            # Claude Delegation configuration (Story #721)
            "claude_delegation": self._get_delegation_settings(),
            # Story #3 - Configuration Consolidation: Migrated settings
            "search_limits": {
                "max_result_size_mb": config.search_limits_config.max_result_size_mb,
                "timeout_seconds": config.search_limits_config.timeout_seconds,
            },
            "file_content_limits": {
                "max_tokens_per_request": config.file_content_limits_config.max_tokens_per_request,
                "chars_per_token": config.file_content_limits_config.chars_per_token,
            },
            "golden_repos": {
                "refresh_interval_seconds": config.golden_repos_config.refresh_interval_seconds,
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
                # P3 settings (AC37)
                "system_metrics_cache_ttl_seconds": config.health_config.system_metrics_cache_ttl_seconds,
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
                "csrf_max_age_seconds": config.web_security_config.csrf_max_age_seconds,
                "web_session_timeout_seconds": config.web_security_config.web_session_timeout_seconds,
            },
            # Story #3 - Phase 2: P3 settings (AC36)
            "auth": {
                "oauth_extension_threshold_hours": config.auth_config.oauth_extension_threshold_hours,
            },
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
                "omni_max_total_results_before_aggregation": config.multi_search_limits_config.omni_max_total_results_before_aggregation,
                "omni_pattern_metacharacters": config.multi_search_limits_config.omni_pattern_metacharacters,
            },
            # Story #26 - Background jobs configuration, Story #27 - SubprocessExecutor max_workers
            "background_jobs": {
                "max_concurrent_background_jobs": config.background_jobs_config.max_concurrent_background_jobs,
                "subprocess_max_workers": config.background_jobs_config.subprocess_max_workers,
            },
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
        # Story #3 - Configuration Consolidation: New categories
        elif category == "search_limits":
            self._update_search_limits_setting(config, key, value)
        elif category == "file_content_limits":
            self._update_file_content_limits_setting(config, key, value)
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
        # Story #3 - Phase 2: P3 categories (AC36)
        elif category == "auth":
            self._update_auth_setting(config, key, value)
        # Story #25/29 - Multi-search limits (includes omni settings)
        elif category == "multi_search":
            self._update_multi_search_setting(config, key, value)
        # Story #26 - Background jobs
        elif category == "background_jobs":
            self._update_background_jobs_setting(config, key, value)
        else:
            raise ValueError(f"Unknown category: {category}")

        # Validate and save (unless skipping for batch updates)
        if not skip_validation:
            self.config_manager.validate_config(config)
            self.config_manager.save_config(config)
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
        elif key == "cidx_index_timeout":
            timeouts.cidx_index_timeout = int(value)
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
        elif key == "export_logs":
            telemetry.export_logs = value in ["true", True, "True", "1"]
        elif key == "machine_metrics_enabled":
            telemetry.machine_metrics_enabled = value in ["true", True, "True", "1"]
        elif key == "machine_metrics_interval_seconds":
            telemetry.machine_metrics_interval_seconds = int(value)
        elif key == "trace_sample_rate":
            telemetry.trace_sample_rate = float(value)
        elif key == "deployment_environment":
            telemetry.deployment_environment = str(value)
        else:
            raise ValueError(f"Unknown telemetry setting: {key}")

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

    def _update_file_content_limits_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a file content limits setting (Story #3 - Configuration Consolidation)."""
        file_content_limits = config.file_content_limits_config
        assert (
            file_content_limits is not None
        )  # Guaranteed by ServerConfig.__post_init__
        if key == "max_tokens_per_request":
            file_content_limits.max_tokens_per_request = int(value)
        elif key == "chars_per_token":
            file_content_limits.chars_per_token = int(value)
        else:
            raise ValueError(f"Unknown file content limits setting: {key}")

    def _update_golden_repos_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a golden repos setting (Story #3 - Configuration Consolidation)."""
        golden_repos = config.golden_repos_config
        assert golden_repos is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "refresh_interval_seconds":
            golden_repos.refresh_interval_seconds = int(value)
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
        # P3 settings (AC37)
        elif key == "system_metrics_cache_ttl_seconds":
            health.system_metrics_cache_ttl_seconds = int(value)
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
        """Update a web security setting (Story #3 - Phase 2, AC25-AC26)."""
        web_security = config.web_security_config
        assert web_security is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "csrf_max_age_seconds":
            web_security.csrf_max_age_seconds = int(value)
        elif key == "web_session_timeout_seconds":
            web_security.web_session_timeout_seconds = int(value)
        else:
            raise ValueError(f"Unknown web security setting: {key}")

    def _update_auth_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update an auth setting (Story #3 - Phase 2, AC36)."""
        auth = config.auth_config
        assert auth is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "oauth_extension_threshold_hours":
            auth.oauth_extension_threshold_hours = int(value)
        else:
            raise ValueError(f"Unknown auth setting: {key}")

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
        elif key == "omni_max_total_results_before_aggregation":
            multi_search.omni_max_total_results_before_aggregation = int(value)
        elif key == "omni_pattern_metacharacters":
            multi_search.omni_pattern_metacharacters = str(value)
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
        self.config_manager.save_config(config)
        logger.info(
            "Saved all settings", extra={"correlation_id": get_correlation_id()}
        )

    def get_config_file_path(self) -> str:
        """Get the path to the configuration file."""
        return str(self.config_manager.config_file_path)


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
