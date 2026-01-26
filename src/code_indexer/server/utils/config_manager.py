"""
Server Configuration Management for CIDX Server.

Handles server configuration creation, validation, environment variable overrides,
and directory structure setup for the CIDX server installation.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class PasswordSecurityConfig:
    """Password strength validation configuration."""

    min_length: int = 12
    max_length: int = 128
    required_char_classes: int = 4
    min_entropy_bits: int = 50
    check_common_passwords: bool = True
    check_personal_info: bool = True
    check_keyboard_patterns: bool = True
    check_sequential_chars: bool = True


@dataclass
class CacheConfig:
    """Cache configuration for HNSW and FTS indexes."""

    # HNSW index cache settings
    index_cache_ttl_minutes: float = 10.0
    index_cache_cleanup_interval: int = 60
    index_cache_max_size_mb: Optional[int] = None

    # FTS index cache settings
    fts_cache_ttl_minutes: float = 10.0
    fts_cache_cleanup_interval: int = 60
    fts_cache_max_size_mb: Optional[int] = None
    fts_cache_reload_on_access: bool = True

    # Payload cache settings (Story #679)
    payload_preview_size_chars: int = 2000
    payload_max_fetch_size_chars: int = 5000
    payload_cache_ttl_seconds: int = 900
    payload_cleanup_interval_seconds: int = 60


@dataclass
class ReindexingConfig:
    """Reindexing trigger and analysis configuration."""

    change_percentage_threshold: float = 80.0
    accuracy_threshold: float = 0.85
    max_index_age_days: int = 30
    batch_size: int = 100
    max_analysis_time_seconds: int = 300
    max_memory_usage_mb: int = 512
    enable_structural_analysis: bool = True
    enable_config_change_detection: bool = True
    enable_corruption_detection: bool = True
    enable_periodic_check: bool = True
    parallel_analysis: bool = True


@dataclass
class ServerResourceConfig:
    """
    Resource limits and timeout configuration for CIDX server.

    All previously hardcoded magic numbers are now externalized here.
    All limits are disabled (set to very high values) to remove constraints.
    """

    # Git operation timeouts (in seconds) - lenient values
    git_clone_timeout: int = 3600  # 1 hour for git clone validation
    git_pull_timeout: int = 3600  # 1 hour for git pull
    git_refresh_timeout: int = 3600  # 1 hour for git refresh
    git_init_conflict_timeout: int = 1800  # 30 minutes for init conflict resolution
    git_service_conflict_timeout: int = (
        1800  # 30 minutes for service conflict resolution
    )
    git_service_cleanup_timeout: int = 300  # 5 minutes for service cleanup
    git_service_wait_timeout: int = 180  # 3 minutes for service cleanup wait
    git_process_check_timeout: int = 30  # 30 seconds for process check
    git_untracked_file_timeout: int = 60  # 1 minute for untracked file check

    # Refresh scheduler timeouts (in seconds)
    cow_clone_timeout: int = 600  # 10 minutes for CoW clone of large repos (11GB)
    git_update_index_timeout: int = 300  # 5 minutes for git update-index --refresh
    git_restore_timeout: int = 300  # 5 minutes for git restore .
    cidx_fix_config_timeout: int = 60  # 1 minute for cidx fix-config
    cidx_index_timeout: int = 3600  # 1 hour for cidx index on large repos

    # NOTE: Artificial resource limits (max_golden_repos, max_repo_size_bytes, max_jobs_per_user)
    # have been REMOVED from the codebase. They were nonsensical limitations that served no purpose.


@dataclass
class AutoWatchConfig:
    """Auto-watch configuration for server file operations - Story #640."""

    auto_watch_enabled: bool = True
    auto_watch_timeout: int = 300  # Timeout in seconds for auto-stop


@dataclass
class OIDCProviderConfig:
    """Single external OIDC provider configuration."""

    enabled: bool = False
    provider_name: str = "SSO"
    issuer_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: Optional[list] = None
    email_claim: str = "email"
    username_claim: str = "preferred_username"
    use_pkce: bool = True
    require_email_verification: bool = True
    enable_jit_provisioning: bool = True
    default_role: str = "normal_user"

    def __post_init__(self):
        if self.scopes is None:
            self.scopes = ["openid", "profile", "email"]


@dataclass
class TelemetryConfig:
    """
    OpenTelemetry configuration for CIDX Server (Story #695).

    Controls telemetry export including traces, metrics, and logs to an
    OpenTelemetry collector endpoint. Disabled by default to ensure
    zero overhead on fresh installations.
    """

    # Core settings
    enabled: bool = False
    collector_endpoint: str = "http://localhost:4317"
    collector_protocol: str = "grpc"  # Options: grpc, http
    service_name: str = "cidx-server"

    # Export settings
    export_traces: bool = True
    export_metrics: bool = True
    export_logs: bool = False

    # Machine metrics settings
    machine_metrics_enabled: bool = True
    machine_metrics_interval_seconds: int = 60

    # Trace sampling
    trace_sample_rate: float = 1.0  # 0.0 to 1.0

    # Deployment environment (development, staging, production)
    deployment_environment: str = "development"


@dataclass
class SearchLimitsConfig:
    """
    Search limits configuration (Story #3 - Configuration Consolidation).

    Migrated from SQLite-based SearchLimitsConfigManager to main config.json.
    Controls maximum result size and timeout for search operations.
    """

    # AC-M1: Maximum result size in megabytes (default 1 MB, range 1-100 MB)
    max_result_size_mb: int = 1
    # AC-M2: Search timeout in seconds (default 30s, range 5-300s)
    timeout_seconds: int = 30

    @property
    def max_size_bytes(self) -> int:
        """Return max result size in bytes."""
        return self.max_result_size_mb * 1024 * 1024


@dataclass
class FileContentLimitsConfig:
    """
    File content limits configuration (Story #3 - Configuration Consolidation).

    Migrated from SQLite-based FileContentLimitsConfigManager to main config.json.
    Controls token budgets for file content operations.
    """

    # AC-M3: Maximum tokens per request (default 5000, range 1000-50000)
    max_tokens_per_request: int = 5000
    # AC-M4: Characters per token ratio (default 4, range 1-10)
    chars_per_token: int = 4

    @property
    def max_chars_per_request(self) -> int:
        """Return max characters per request based on token budget."""
        return self.max_tokens_per_request * self.chars_per_token


@dataclass
class GoldenReposConfig:
    """
    Golden repositories configuration (Story #3 - Configuration Consolidation).

    Migrated from separate global_config.json to main config.json.
    Controls refresh intervals for golden repository synchronization.
    """

    # AC-M5: Refresh interval in seconds (default 3600s/1 hour, minimum 60s)
    refresh_interval_seconds: int = 3600


@dataclass
class McpSessionConfig:
    """
    MCP Session configuration (Story #3 - Phase 2, AC2-AC3).

    Controls MCP session lifecycle settings including TTL and cleanup intervals.
    Migrated from hardcoded constants in session_registry.py.
    """

    # AC2: Session TTL in seconds (default 3600s/1 hour, minimum 300s/5 min)
    session_ttl_seconds: int = 3600
    # AC3: Cleanup interval in seconds (default 900s/15 min, minimum 60s/1 min)
    cleanup_interval_seconds: int = 900


@dataclass
class HealthConfig:
    """
    Health monitoring thresholds configuration (Story #3 - Phase 2, AC4-AC8, AC37).

    Controls resource monitoring thresholds for memory, disk, CPU, and metrics caching.
    Migrated from hardcoded constants in health_service.py.
    """

    # AC4: Memory warning threshold (default 80%, range 50-95%)
    memory_warning_threshold_percent: float = 80.0
    # AC5: Memory critical threshold (default 90%, range 60-99%)
    memory_critical_threshold_percent: float = 90.0
    # AC6: Disk warning threshold (default 80%, range 50-95%)
    disk_warning_threshold_percent: float = 80.0
    # AC7: Disk critical threshold (default 90%, range 60-99%)
    disk_critical_threshold_percent: float = 90.0
    # AC8: CPU sustained threshold (default 95%, range 70-100%)
    cpu_sustained_threshold_percent: float = 95.0
    # AC37: System metrics cache TTL in seconds (default 5s, range 1-60s)
    system_metrics_cache_ttl_seconds: int = 5


@dataclass
class ScipConfig:
    """
    SCIP indexing configuration (Story #3 - Phase 2, AC9-AC11, AC31-AC34).

    Controls SCIP indexing timeouts, temporal staleness thresholds, and query limits.
    Migrated from hardcoded constants in activated_repo_index_manager.py and scip_query_engine.py.
    """

    # AC9: Indexing timeout in seconds (default 3600s/1 hour, minimum 300s/5 min)
    indexing_timeout_seconds: int = 3600
    # AC10: SCIP generation timeout in seconds (default 600s/10 min, minimum 60s/1 min)
    scip_generation_timeout_seconds: int = 600
    # AC11: Temporal staleness threshold in days (default 7 days, minimum 1 day)
    temporal_stale_threshold_days: int = 7
    # AC31: SCIP reference limit (default 100, range 10-10000)
    scip_reference_limit: int = 100
    # AC32: SCIP dependency depth (default 3, range 1-20)
    scip_dependency_depth: int = 3
    # AC33: SCIP callchain max depth (default 10, range 1-50)
    scip_callchain_max_depth: int = 10
    # AC34: SCIP callchain limit (default 100, range 1-1000)
    scip_callchain_limit: int = 100
    # Story #15 AC2: SCIP workspace retention (moved from ServerConfig, default 7 days)
    scip_workspace_retention_days: int = 7


@dataclass
class GitTimeoutsConfig:
    """
    Git operation timeouts configuration (Story #3 - Phase 2, AC12-AC15, AC27-AC30).

    Controls timeout values for various git operations.
    Migrated from hardcoded constants in git_operations_service.py and activated_repo_manager.py.
    """

    # AC12-AC13: Local git operation timeout (default 30s, minimum 5s)
    git_local_timeout: int = 30
    # AC14: Remote git operation timeout (default 300s, minimum 30s)
    git_remote_timeout: int = 300
    # AC15: Git command timeout (default 30s, minimum 5s)
    git_command_timeout: int = 30
    # AC15: Git fetch timeout (default 60s, minimum 10s)
    git_fetch_timeout: int = 60
    # AC27: GitHub API timeout (default 30s, range 5-120s)
    github_api_timeout: int = 30
    # AC28: GitLab API timeout (default 30s, range 5-120s)
    gitlab_api_timeout: int = 30
    # AC29: GitHub provider timeout (default 30s, range 5-120s)
    github_provider_timeout: int = 30
    # AC30: GitLab provider timeout (default 30s, range 5-120s)
    gitlab_provider_timeout: int = 30


@dataclass
class ErrorHandlingConfig:
    """
    Error handling and retry configuration (Story #3 - Phase 2, AC16-AC18).

    Controls retry behavior for database and transient errors.
    Migrated from hardcoded constants in error_handler.py.
    """

    # AC16-AC17: Maximum retry attempts (default 3, range 1-10)
    max_retry_attempts: int = 3
    # AC18: Base retry delay in seconds (default 0.1s, range 0.01-5.0s)
    base_retry_delay_seconds: float = 0.1
    # AC18: Maximum retry delay in seconds (default 60s, range 1-300s)
    max_retry_delay_seconds: float = 60.0


@dataclass
class ApiLimitsConfig:
    """
    API response limits configuration (Story #3 - Phase 2, AC19-AC24, AC35, AC38-AC39).

    Controls default and maximum limits for file reading, diff, log operations,
    audit logs, and log aggregator page sizes.
    Migrated from hardcoded constants in file_service.py and git_operations_service.py.
    """

    # AC19-AC20: File read lines (default 500, range 100-5000; max 5000, range 500-50000)
    default_file_read_lines: int = 500
    max_file_read_lines: int = 5000
    # AC21-AC22: Diff lines (default 500, range 100-5000; max 5000, range 500-50000)
    default_diff_lines: int = 500
    max_diff_lines: int = 5000
    # AC23-AC24: Log commits (default 50, range 10-500; max 500, range 50-5000)
    default_log_commits: int = 50
    max_log_commits: int = 500
    # AC35: Audit log default limit (default 100, range 10-1000)
    audit_log_default_limit: int = 100
    # AC38: Log page size default (default 50, range 10-500)
    log_page_size_default: int = 50
    # AC39: Log page size max (default 500, range 100-5000)
    log_page_size_max: int = 500


@dataclass
class WebSecurityConfig:
    """
    Web security configuration (Story #3 - Phase 2, AC25-AC26).

    Controls CSRF token and session timeout settings for the web UI.
    Migrated from hardcoded constants in routes.py and auth.py.
    """

    # AC25-AC26: CSRF max age in seconds (default 600s/10min, range 60-3600s)
    csrf_max_age_seconds: int = 600
    # AC25-AC26: Web session timeout in seconds (default 28800s/8hr, range 1800-86400s)
    web_session_timeout_seconds: int = 28800


@dataclass
class AuthConfig:
    """
    Authentication configuration (Story #3 - Phase 2, AC36).

    Controls OAuth token extension and authentication settings.
    Migrated from hardcoded constants in oauth_service.py.
    """

    # AC36: OAuth extension threshold in hours (default 4hr, range 1-24hr)
    oauth_extension_threshold_hours: int = 4


@dataclass
class IndexingConfig:
    """
    Indexing configuration (Story #15 - AC1).

    Contains settings related to indexing operations that were previously
    misplaced in ScipConfig. These settings are general indexing settings,
    not specific to SCIP.
    """

    # Temporal index staleness threshold in days (moved from ScipConfig)
    temporal_stale_threshold_days: int = 7
    # General indexing timeout in seconds (moved from ScipConfig)
    indexing_timeout_seconds: int = 3600


@dataclass
class ClaudeIntegrationConfig:
    """
    Claude CLI integration configuration (Story #15 - AC3, Story #20, Story #23).

    Contains settings for Claude CLI integration that were previously
    loose settings on ServerConfig, plus VoyageAI API key (Story #20),
    and scheduled catch-up settings (Story #23 - AC6).
    """

    # Anthropic API key for Claude CLI (moved from ServerConfig)
    anthropic_api_key: Optional[str] = None
    # VoyageAI API key for embeddings (Story #20)
    voyageai_api_key: Optional[str] = None
    # Maximum concurrent Claude CLI processes (Story #24: default 2 for resource-constrained systems)
    max_concurrent_claude_cli: int = 2
    # Refresh interval for description generation in hours (moved from ServerConfig)
    description_refresh_interval_hours: int = 24
    # Scheduled catch-up settings (Story #23 - AC6)
    # Enable scheduled background catch-up for repos with fallback descriptions
    scheduled_catchup_enabled: bool = False
    # Interval in minutes for scheduled catch-up scanning (default: 60 = 1 hour)
    scheduled_catchup_interval_minutes: int = 60


@dataclass
class RepositoryConfig:
    """
    Repository configuration (Story #15 - AC4).

    Contains settings for repository operations and PR creation that were
    previously loose settings on ServerConfig.
    """

    # Enable automatic PR creation after SCIP fixes (moved from ServerConfig)
    enable_pr_creation: bool = True
    # Default base branch for PRs (moved from ServerConfig)
    pr_base_branch: str = "main"
    # Default branch for repository operations (moved from ServerConfig)
    default_branch: str = "main"


@dataclass
class MultiSearchLimitsConfig:
    """
    Multi-search limits configuration (Story #25, Story #29 - Consolidation).

    Configures worker limits and timeouts for MultiSearchService, SCIPMultiService,
    and Omni-Search (cross-repository search). Story #29 consolidated OmniSearchConfig
    into this class to eliminate duplicate multi-repo search implementations.

    Now configurable via Web UI Configuration system under "Multi-Search Settings".

    Default values per resource audit recommendation: 2 workers (not 10).
    """

    # MultiSearchService settings
    # Default 2 workers per resource audit (was 10)
    multi_search_max_workers: int = 2
    # Default 30 seconds timeout
    multi_search_timeout_seconds: int = 30

    # SCIPMultiService settings
    # Default 2 workers per resource audit (was 10)
    scip_multi_max_workers: int = 2
    # Default 30 seconds timeout
    scip_multi_timeout_seconds: int = 30

    # Story #29: Omni-Search settings (merged from OmniSearchConfig)
    # These control MCP cross-repository search behavior
    # Prefixed with omni_ to distinguish from standard multi-search settings
    omni_max_workers: int = 10
    omni_per_repo_timeout_seconds: int = 300
    omni_cache_max_entries: int = 100
    omni_cache_ttl_seconds: int = 300
    omni_default_limit: int = 10
    omni_max_limit: int = 1000
    omni_default_aggregation_mode: str = "global"
    omni_max_results_per_repo: int = 100
    omni_max_total_results_before_aggregation: int = 10000
    omni_pattern_metacharacters: str = "*?[]^$+|"


@dataclass
class BackgroundJobsConfig:
    """
    Background jobs configuration (Story #26 - Bug Fix, Story #27).

    Configures concurrent job limits for BackgroundJobManager to prevent
    resource exhaustion when many jobs are submitted simultaneously.
    Now configurable via Web UI Configuration system.

    Default value per resource audit recommendation: 5 concurrent jobs.
    Story #27: Also configures SubprocessExecutor max_workers.
    """

    # Maximum number of concurrent background jobs (default: 5)
    # Jobs exceeding this limit stay in PENDING until a slot is available
    max_concurrent_background_jobs: int = 5

    # Story #27: Maximum concurrent workers for SubprocessExecutor (default: 2)
    # Controls parallelism of subprocess-based operations like regex search
    # Default 2 per resource audit recommendation (was hardcoded to 1)
    subprocess_max_workers: int = 2


@dataclass
class ContentLimitsConfig:
    """
    Unified content limits configuration (Story #32).

    Consolidates all content truncation-related settings into a single configuration
    section. All limits use tokens as the primary unit for consistency.

    This replaces scattered settings from:
    - FileContentLimitsConfig (max_tokens_per_request, chars_per_token)
    - CacheConfig payload settings (cache_ttl_seconds)
    - Various hardcoded limits throughout the codebase
    """

    # Token conversion factor
    # Typical ratio for source code is ~4 characters per token
    chars_per_token: int = 4

    # File content limits (tokens)
    # Maximum tokens for file content retrieval operations
    file_content_max_tokens: int = 50000

    # Git operation limits (tokens)
    # Maximum tokens for git diff output
    git_diff_max_tokens: int = 50000
    # Maximum tokens for git log output
    git_log_max_tokens: int = 50000

    # Search result limits (tokens)
    # Maximum tokens per search result
    search_result_max_tokens: int = 50000

    # Cache settings
    # Time-to-live for cached content in seconds (default: 1 hour)
    cache_ttl_seconds: int = 3600
    # Maximum cache entries before cleanup (default: 10000)
    cache_max_entries: int = 10000


@dataclass
class ServerConfig:
    """
    Server configuration data structure.

    Contains all configurable server settings including networking,
    authentication, logging, cache, reindexing, and resource configurations.
    """

    server_dir: str
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    jwt_expiration_minutes: int = 10
    log_level: str = "INFO"
    # Story #22 - Configurable service display name for MCP protocol
    service_display_name: str = "Neo"
    password_security: Optional[PasswordSecurityConfig] = None
    resource_config: Optional[ServerResourceConfig] = None
    cache_config: Optional[CacheConfig] = None
    reindexing_config: Optional[ReindexingConfig] = None
    auto_watch_config: Optional[AutoWatchConfig] = None
    oidc_provider_config: Optional[OIDCProviderConfig] = None
    telemetry_config: Optional[TelemetryConfig] = None

    # Story #3 - Configuration Consolidation: Migrated settings
    search_limits_config: Optional[SearchLimitsConfig] = None
    file_content_limits_config: Optional[FileContentLimitsConfig] = None
    golden_repos_config: Optional[GoldenReposConfig] = None

    # Story #3 - Phase 2: P0/P1 settings
    mcp_session_config: Optional[McpSessionConfig] = None
    health_config: Optional[HealthConfig] = None
    scip_config: Optional[ScipConfig] = None

    # Story #3 - Phase 2: P2 settings (AC12-AC26)
    git_timeouts_config: Optional[GitTimeoutsConfig] = None
    error_handling_config: Optional[ErrorHandlingConfig] = None
    api_limits_config: Optional[ApiLimitsConfig] = None
    web_security_config: Optional[WebSecurityConfig] = None

    # Story #3 - Phase 2: P3 settings (AC36)
    auth_config: Optional[AuthConfig] = None

    # Story #15 - Configuration Refactoring: Indexing settings
    indexing_config: Optional[IndexingConfig] = None

    # Story #15 AC3 - Configuration Refactoring: Claude integration settings
    claude_integration_config: Optional[ClaudeIntegrationConfig] = None

    # Story #15 AC4 - Configuration Refactoring: Repository settings
    repository_config: Optional[RepositoryConfig] = None

    # Story #25 - Multi-search limits configuration
    multi_search_limits_config: Optional[MultiSearchLimitsConfig] = None

    # Story #26 - Background jobs configuration
    background_jobs_config: Optional[BackgroundJobsConfig] = None

    # Story #32 - Unified content limits configuration
    content_limits_config: Optional[ContentLimitsConfig] = None

    def __post_init__(self):
        """Initialize nested config objects if not provided."""
        if self.password_security is None:
            self.password_security = PasswordSecurityConfig()
        if self.resource_config is None:
            self.resource_config = ServerResourceConfig()
        if self.cache_config is None:
            self.cache_config = CacheConfig()
        if self.reindexing_config is None:
            self.reindexing_config = ReindexingConfig()
        if self.auto_watch_config is None:
            self.auto_watch_config = AutoWatchConfig()
        if self.oidc_provider_config is None:
            self.oidc_provider_config = OIDCProviderConfig()
        if self.telemetry_config is None:
            self.telemetry_config = TelemetryConfig()
        # Story #3 - Configuration Consolidation: Initialize migrated configs
        if self.search_limits_config is None:
            self.search_limits_config = SearchLimitsConfig()
        if self.file_content_limits_config is None:
            self.file_content_limits_config = FileContentLimitsConfig()
        if self.golden_repos_config is None:
            self.golden_repos_config = GoldenReposConfig()
        # Story #3 - Phase 2: Initialize P0/P1 configs
        if self.mcp_session_config is None:
            self.mcp_session_config = McpSessionConfig()
        if self.health_config is None:
            self.health_config = HealthConfig()
        if self.scip_config is None:
            self.scip_config = ScipConfig()
        # Story #3 - Phase 2: Initialize P2 configs (AC12-AC26)
        if self.git_timeouts_config is None:
            self.git_timeouts_config = GitTimeoutsConfig()
        if self.error_handling_config is None:
            self.error_handling_config = ErrorHandlingConfig()
        if self.api_limits_config is None:
            self.api_limits_config = ApiLimitsConfig()
        if self.web_security_config is None:
            self.web_security_config = WebSecurityConfig()
        # Story #3 - Phase 2: Initialize P3 configs (AC36)
        if self.auth_config is None:
            self.auth_config = AuthConfig()
        # Story #15 - Configuration Refactoring: Initialize indexing config
        if self.indexing_config is None:
            self.indexing_config = IndexingConfig()
        # Story #15 AC3 - Configuration Refactoring: Initialize claude integration config
        if self.claude_integration_config is None:
            self.claude_integration_config = ClaudeIntegrationConfig()
        # Story #15 AC4 - Configuration Refactoring: Initialize repository config
        if self.repository_config is None:
            self.repository_config = RepositoryConfig()
        # Story #25 - Initialize multi-search limits config
        if self.multi_search_limits_config is None:
            self.multi_search_limits_config = MultiSearchLimitsConfig()
        # Story #26 - Initialize background jobs config
        if self.background_jobs_config is None:
            self.background_jobs_config = BackgroundJobsConfig()
        # Story #32 - Initialize content limits config
        if self.content_limits_config is None:
            self.content_limits_config = ContentLimitsConfig()


class ServerConfigManager:
    """
    Manages CIDX server configuration.

    Handles configuration creation, validation, file persistence,
    environment variable overrides, and server directory setup.
    """

    def __init__(self, server_dir_path: Optional[str] = None):
        """
        Initialize server configuration manager.

        Args:
            server_dir_path: Path to server directory (defaults to CIDX_SERVER_DATA_DIR env var or ~/.cidx-server)
        """
        if server_dir_path:
            self.server_dir = Path(server_dir_path)
        else:
            # Honor CIDX_SERVER_DATA_DIR environment variable
            default_dir = os.environ.get(
                "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
            )
            self.server_dir = Path(default_dir)

        self.config_file_path = self.server_dir / "config.json"

    def create_default_config(self) -> ServerConfig:
        """
        Create default server configuration.

        Returns:
            ServerConfig with default values
        """
        return ServerConfig(server_dir=str(self.server_dir))

    def save_config(self, config: ServerConfig) -> None:
        """
        Save configuration to file.

        Args:
            config: ServerConfig object to save
        """
        # Ensure server directory exists
        self.server_dir.mkdir(parents=True, exist_ok=True)

        # Convert config to dictionary and save as JSON
        config_dict = asdict(config)

        with open(self.config_file_path, "w") as f:
            json.dump(config_dict, f, indent=2)

    def load_config(self) -> Optional[ServerConfig]:
        """
        Load configuration from file.

        Returns:
            ServerConfig if file exists and is valid, None otherwise

        Raises:
            ValueError: If configuration file is malformed
        """
        if not self.config_file_path.exists():
            return None

        try:
            with open(self.config_file_path, "r") as f:
                config_dict = json.load(f)

            # Ensure server_dir is set if missing from file
            if "server_dir" not in config_dict:
                config_dict["server_dir"] = str(self.server_dir)

            # Convert nested password_security dict to PasswordSecurityConfig
            if "password_security" in config_dict and isinstance(
                config_dict["password_security"], dict
            ):
                config_dict["password_security"] = PasswordSecurityConfig(
                    **config_dict["password_security"]
                )

            # Convert nested resource_config dict to ServerResourceConfig
            if "resource_config" in config_dict and isinstance(
                config_dict["resource_config"], dict
            ):
                config_dict["resource_config"] = ServerResourceConfig(
                    **config_dict["resource_config"]
                )

            # Story #32: Migration from old config format to content_limits_config
            # Must run BEFORE any conversions so we can read raw dict values
            if "content_limits_config" not in config_dict:
                migrated_config = {}

                # Migrate from file_content_limits_config
                if "file_content_limits_config" in config_dict:
                    old_file_limits = config_dict["file_content_limits_config"]
                    if isinstance(old_file_limits, dict):
                        if "chars_per_token" in old_file_limits:
                            migrated_config["chars_per_token"] = old_file_limits[
                                "chars_per_token"
                            ]
                        if "max_tokens_per_request" in old_file_limits:
                            migrated_config["file_content_max_tokens"] = old_file_limits[
                                "max_tokens_per_request"
                            ]

                # Migrate from cache_config payload settings
                if "cache_config" in config_dict:
                    old_cache = config_dict["cache_config"]
                    if isinstance(old_cache, dict):
                        if "payload_cache_ttl_seconds" in old_cache:
                            migrated_config["cache_ttl_seconds"] = old_cache[
                                "payload_cache_ttl_seconds"
                            ]

                # Create content_limits_config with migrated values
                if migrated_config:
                    config_dict["content_limits_config"] = migrated_config

            # Convert nested cache_config dict to CacheConfig
            if "cache_config" in config_dict and isinstance(
                config_dict["cache_config"], dict
            ):
                config_dict["cache_config"] = CacheConfig(**config_dict["cache_config"])

            # Convert nested reindexing_config dict to ReindexingConfig
            if "reindexing_config" in config_dict and isinstance(
                config_dict["reindexing_config"], dict
            ):
                config_dict["reindexing_config"] = ReindexingConfig(
                    **config_dict["reindexing_config"]
                )

            # Story #29: Migrate old omni_search_config to multi_search_limits_config
            if "omni_search_config" in config_dict and isinstance(
                config_dict["omni_search_config"], dict
            ):
                old_omni = config_dict.pop("omni_search_config")
                # Initialize multi_search_limits_config dict if needed
                if "multi_search_limits_config" not in config_dict:
                    config_dict["multi_search_limits_config"] = {}
                if isinstance(config_dict["multi_search_limits_config"], dict):
                    # Map old field names to new omni_ prefixed names
                    field_mapping = {
                        "max_workers": "omni_max_workers",
                        "per_repo_timeout_seconds": "omni_per_repo_timeout_seconds",
                        "cache_max_entries": "omni_cache_max_entries",
                        "cache_ttl_seconds": "omni_cache_ttl_seconds",
                        "default_limit": "omni_default_limit",
                        "max_limit": "omni_max_limit",
                        "default_aggregation_mode": "omni_default_aggregation_mode",
                        "max_results_per_repo": "omni_max_results_per_repo",
                        "max_total_results_before_aggregation": "omni_max_total_results_before_aggregation",
                        "pattern_metacharacters": "omni_pattern_metacharacters",
                    }
                    for old_key, new_key in field_mapping.items():
                        if old_key in old_omni:
                            config_dict["multi_search_limits_config"][new_key] = old_omni[old_key]

            # Convert nested oidc_provider_config dict to OIDCProviderConfig
            if "oidc_provider_config" in config_dict and isinstance(
                config_dict["oidc_provider_config"], dict
            ):
                config_dict["oidc_provider_config"] = OIDCProviderConfig(
                    **config_dict["oidc_provider_config"]
                )

            # Convert nested telemetry_config dict to TelemetryConfig
            if "telemetry_config" in config_dict and isinstance(
                config_dict["telemetry_config"], dict
            ):
                config_dict["telemetry_config"] = TelemetryConfig(
                    **config_dict["telemetry_config"]
                )

            # Story #3 - Configuration Consolidation: Convert migrated config dicts
            # Convert nested search_limits_config dict to SearchLimitsConfig
            if "search_limits_config" in config_dict and isinstance(
                config_dict["search_limits_config"], dict
            ):
                config_dict["search_limits_config"] = SearchLimitsConfig(
                    **config_dict["search_limits_config"]
                )

            # Convert nested file_content_limits_config dict to FileContentLimitsConfig
            if "file_content_limits_config" in config_dict and isinstance(
                config_dict["file_content_limits_config"], dict
            ):
                config_dict["file_content_limits_config"] = FileContentLimitsConfig(
                    **config_dict["file_content_limits_config"]
                )

            # Convert nested golden_repos_config dict to GoldenReposConfig
            if "golden_repos_config" in config_dict and isinstance(
                config_dict["golden_repos_config"], dict
            ):
                config_dict["golden_repos_config"] = GoldenReposConfig(
                    **config_dict["golden_repos_config"]
                )

            # Story #3 - Phase 2: Convert P0/P1 config dicts
            # Convert nested mcp_session_config dict to McpSessionConfig
            if "mcp_session_config" in config_dict and isinstance(
                config_dict["mcp_session_config"], dict
            ):
                config_dict["mcp_session_config"] = McpSessionConfig(
                    **config_dict["mcp_session_config"]
                )

            # Convert nested health_config dict to HealthConfig
            if "health_config" in config_dict and isinstance(
                config_dict["health_config"], dict
            ):
                config_dict["health_config"] = HealthConfig(
                    **config_dict["health_config"]
                )

            # Convert nested scip_config dict to ScipConfig
            if "scip_config" in config_dict and isinstance(
                config_dict["scip_config"], dict
            ):
                config_dict["scip_config"] = ScipConfig(**config_dict["scip_config"])

            # Story #3 - Phase 2: Convert P2 config dicts (AC12-AC26)
            # Convert nested git_timeouts_config dict to GitTimeoutsConfig
            if "git_timeouts_config" in config_dict and isinstance(
                config_dict["git_timeouts_config"], dict
            ):
                config_dict["git_timeouts_config"] = GitTimeoutsConfig(
                    **config_dict["git_timeouts_config"]
                )

            # Convert nested error_handling_config dict to ErrorHandlingConfig
            if "error_handling_config" in config_dict and isinstance(
                config_dict["error_handling_config"], dict
            ):
                config_dict["error_handling_config"] = ErrorHandlingConfig(
                    **config_dict["error_handling_config"]
                )

            # Convert nested api_limits_config dict to ApiLimitsConfig
            if "api_limits_config" in config_dict and isinstance(
                config_dict["api_limits_config"], dict
            ):
                config_dict["api_limits_config"] = ApiLimitsConfig(
                    **config_dict["api_limits_config"]
                )

            # Convert nested web_security_config dict to WebSecurityConfig
            if "web_security_config" in config_dict and isinstance(
                config_dict["web_security_config"], dict
            ):
                config_dict["web_security_config"] = WebSecurityConfig(
                    **config_dict["web_security_config"]
                )

            # Story #3 - Phase 2: Convert P3 config dicts (AC36)
            # Convert nested auth_config dict to AuthConfig
            if "auth_config" in config_dict and isinstance(
                config_dict["auth_config"], dict
            ):
                config_dict["auth_config"] = AuthConfig(**config_dict["auth_config"])

            # Story #15 - Configuration Refactoring: Convert indexing_config
            if "indexing_config" in config_dict and isinstance(
                config_dict["indexing_config"], dict
            ):
                config_dict["indexing_config"] = IndexingConfig(
                    **config_dict["indexing_config"]
                )

            # Story #15 AC2 Migration: Move scip_workspace_retention_days to scip_config
            if "scip_workspace_retention_days" in config_dict:
                retention_days = config_dict.pop("scip_workspace_retention_days")
                if "scip_config" not in config_dict:
                    config_dict["scip_config"] = {}
                if isinstance(config_dict["scip_config"], dict):
                    config_dict["scip_config"][
                        "scip_workspace_retention_days"
                    ] = retention_days
                elif isinstance(config_dict["scip_config"], ScipConfig):
                    config_dict["scip_config"].scip_workspace_retention_days = (
                        retention_days
                    )

            # Story #15 AC2: Final conversion of scip_config after migration
            # This handles the case where scip_config was created by migration above
            if "scip_config" in config_dict and isinstance(
                config_dict["scip_config"], dict
            ):
                config_dict["scip_config"] = ScipConfig(**config_dict["scip_config"])

            # Story #15 AC3 Migration: Move Claude CLI settings to claude_integration_config
            claude_settings_keys = [
                "anthropic_api_key",
                "max_concurrent_claude_cli",
                "description_refresh_interval_hours",
            ]
            claude_settings = {}
            for key in claude_settings_keys:
                if key in config_dict:
                    claude_settings[key] = config_dict.pop(key)
            if claude_settings:
                if "claude_integration_config" not in config_dict:
                    config_dict["claude_integration_config"] = {}
                if isinstance(config_dict["claude_integration_config"], dict):
                    config_dict["claude_integration_config"].update(claude_settings)
                elif isinstance(
                    config_dict["claude_integration_config"], ClaudeIntegrationConfig
                ):
                    for key, value in claude_settings.items():
                        setattr(config_dict["claude_integration_config"], key, value)

            # Story #15 AC3: Convert claude_integration_config dict to ClaudeIntegrationConfig
            if "claude_integration_config" in config_dict and isinstance(
                config_dict["claude_integration_config"], dict
            ):
                config_dict["claude_integration_config"] = ClaudeIntegrationConfig(
                    **config_dict["claude_integration_config"]
                )

            # Story #15 AC4 Migration: Move repository settings to repository_config
            repo_settings_keys = [
                "enable_pr_creation",
                "pr_base_branch",
                "default_branch",
            ]
            repo_settings = {}
            for key in repo_settings_keys:
                if key in config_dict:
                    repo_settings[key] = config_dict.pop(key)
            if repo_settings:
                if "repository_config" not in config_dict:
                    config_dict["repository_config"] = {}
                if isinstance(config_dict["repository_config"], dict):
                    config_dict["repository_config"].update(repo_settings)
                elif isinstance(config_dict["repository_config"], RepositoryConfig):
                    for key, value in repo_settings.items():
                        setattr(config_dict["repository_config"], key, value)

            # Story #15 AC4: Convert repository_config dict to RepositoryConfig
            if "repository_config" in config_dict and isinstance(
                config_dict["repository_config"], dict
            ):
                config_dict["repository_config"] = RepositoryConfig(
                    **config_dict["repository_config"]
                )

            # Story #25: Convert multi_search_limits_config dict to MultiSearchLimitsConfig
            if "multi_search_limits_config" in config_dict and isinstance(
                config_dict["multi_search_limits_config"], dict
            ):
                config_dict["multi_search_limits_config"] = MultiSearchLimitsConfig(
                    **config_dict["multi_search_limits_config"]
                )

            # Story #26: Convert background_jobs_config dict to BackgroundJobsConfig
            if "background_jobs_config" in config_dict and isinstance(
                config_dict["background_jobs_config"], dict
            ):
                config_dict["background_jobs_config"] = BackgroundJobsConfig(
                    **config_dict["background_jobs_config"]
                )

            # Story #32: Convert content_limits_config dict to ContentLimitsConfig
            # (Migration from old format happens earlier, before file_content_limits_config conversion)
            if "content_limits_config" in config_dict and isinstance(
                config_dict["content_limits_config"], dict
            ):
                config_dict["content_limits_config"] = ContentLimitsConfig(
                    **config_dict["content_limits_config"]
                )

            return ServerConfig(**config_dict)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse configuration file: {e}")
        except TypeError as e:
            raise ValueError(f"Invalid configuration format: {e}")

    def apply_env_overrides(self, config: ServerConfig) -> ServerConfig:
        """
        Apply environment variable overrides to configuration.

        Supported environment variables:
        - CIDX_SERVER_HOST: Override host setting
        - CIDX_SERVER_PORT: Override port setting
        - CIDX_JWT_EXPIRATION_MINUTES: Override JWT expiration
        - CIDX_LOG_LEVEL: Override log level

        Args:
            config: Base configuration to apply overrides to

        Returns:
            Updated configuration with environment overrides
        """
        # Host override
        if host_env := os.environ.get("CIDX_SERVER_HOST"):
            config.host = host_env

        # Port override
        if port_env := os.environ.get("CIDX_SERVER_PORT"):
            try:
                config.port = int(port_env)
            except ValueError:
                logging.warning(
                    f"Invalid CIDX_SERVER_PORT environment variable value '{port_env}'. Using default port {config.port}"
                )

        # JWT expiration override
        if jwt_exp_env := os.environ.get("CIDX_JWT_EXPIRATION_MINUTES"):
            try:
                config.jwt_expiration_minutes = int(jwt_exp_env)
            except ValueError:
                logging.warning(
                    f"Invalid CIDX_JWT_EXPIRATION_MINUTES environment variable value '{jwt_exp_env}'. Using default {config.jwt_expiration_minutes} minutes"
                )

        # Log level override
        if log_level_env := os.environ.get("CIDX_LOG_LEVEL"):
            config.log_level = log_level_env.upper()

        # SCIP workspace retention days override (Story #647 - AC1, Story #15 AC2: use scip_config)
        if retention_env := os.environ.get("CIDX_SCIP_WORKSPACE_RETENTION_DAYS"):
            try:
                assert config.scip_config is not None  # Guaranteed by __post_init__
                config.scip_config.scip_workspace_retention_days = int(retention_env)
            except ValueError:
                assert config.scip_config is not None  # Guaranteed by __post_init__
                logging.warning(
                    f"Invalid CIDX_SCIP_WORKSPACE_RETENTION_DAYS environment variable value '{retention_env}'. Using default {config.scip_config.scip_workspace_retention_days} days"
                )

        # Telemetry environment variable overrides (Story #695)
        # Assert telemetry_config is not None (guaranteed by __post_init__)
        assert config.telemetry_config is not None
        if telemetry_enabled_env := os.environ.get("CIDX_TELEMETRY_ENABLED"):
            config.telemetry_config.enabled = telemetry_enabled_env.lower() in (
                "true",
                "1",
                "yes",
            )

        if collector_endpoint_env := os.environ.get("CIDX_OTEL_COLLECTOR_ENDPOINT"):
            config.telemetry_config.collector_endpoint = collector_endpoint_env

        if collector_protocol_env := os.environ.get("CIDX_OTEL_COLLECTOR_PROTOCOL"):
            config.telemetry_config.collector_protocol = collector_protocol_env.lower()

        if service_name_env := os.environ.get("CIDX_OTEL_SERVICE_NAME"):
            config.telemetry_config.service_name = service_name_env

        if trace_sample_rate_env := os.environ.get("CIDX_OTEL_TRACE_SAMPLE_RATE"):
            try:
                config.telemetry_config.trace_sample_rate = float(trace_sample_rate_env)
            except ValueError:
                logging.warning(
                    f"Invalid CIDX_OTEL_TRACE_SAMPLE_RATE environment variable value '{trace_sample_rate_env}'. Using default {config.telemetry_config.trace_sample_rate}"
                )

        if deployment_env := os.environ.get("CIDX_DEPLOYMENT_ENVIRONMENT"):
            config.telemetry_config.deployment_environment = deployment_env

        return config

    def validate_config(self, config: ServerConfig) -> None:
        """
        Validate configuration settings.

        Args:
            config: Configuration to validate

        Raises:
            ValueError: If any configuration value is invalid
        """
        # Validate port range
        if not (1 <= config.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {config.port}")

        # Validate JWT expiration
        if config.jwt_expiration_minutes <= 0:
            raise ValueError(
                f"JWT expiration must be greater than 0, got {config.jwt_expiration_minutes}"
            )

        # Validate log level
        valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if config.log_level.upper() not in valid_log_levels:
            raise ValueError(
                f"Log level must be one of {valid_log_levels}, got {config.log_level}"
            )

        # Validate max_concurrent_claude_cli (Story #15 AC3: use claude_integration_config)
        assert (
            config.claude_integration_config is not None
        )  # Guaranteed by __post_init__
        if config.claude_integration_config.max_concurrent_claude_cli < 1:
            raise ValueError(
                f"max_concurrent_claude_cli must be greater than 0, got {config.claude_integration_config.max_concurrent_claude_cli}"
            )

        # Validate SCIP workspace retention days (Story #647 - AC1, Story #15 AC2: use scip_config)
        assert config.scip_config is not None  # Guaranteed by __post_init__
        if not (1 <= config.scip_config.scip_workspace_retention_days <= 365):
            raise ValueError(
                f"scip_workspace_retention_days must be between 1 and 365, got {config.scip_config.scip_workspace_retention_days}"
            )

        # Validate description_refresh_interval_hours (Story #15 AC3: use claude_integration_config)
        if config.claude_integration_config.description_refresh_interval_hours < 1:
            raise ValueError(
                f"description_refresh_interval_hours must be greater than 0, got {config.claude_integration_config.description_refresh_interval_hours}"
            )

        # Validate OIDC configuration
        if config.oidc_provider_config and config.oidc_provider_config.enabled:
            if not config.oidc_provider_config.issuer_url:
                raise ValueError("OIDC issuer_url is required when OIDC is enabled")
            if not config.oidc_provider_config.client_id:
                raise ValueError("OIDC client_id is required when OIDC is enabled")
            # Validate issuer_url format
            if not config.oidc_provider_config.issuer_url.startswith(
                ("http://", "https://")
            ):
                raise ValueError(
                    f"OIDC issuer_url must start with http:// or https://, got {config.oidc_provider_config.issuer_url}"
                )

            # Validate JIT provisioning requirements
            if config.oidc_provider_config.enable_jit_provisioning:
                if not config.oidc_provider_config.email_claim:
                    raise ValueError(
                        "OIDC email_claim is required when JIT provisioning is enabled"
                    )
                if not config.oidc_provider_config.username_claim:
                    raise ValueError(
                        "OIDC username_claim is required when JIT provisioning is enabled"
                    )

        # Validate telemetry configuration (Story #695)
        if config.telemetry_config:
            # Validate trace_sample_rate (0.0 to 1.0)
            if not (0.0 <= config.telemetry_config.trace_sample_rate <= 1.0):
                raise ValueError(
                    f"trace_sample_rate must be between 0.0 and 1.0, got {config.telemetry_config.trace_sample_rate}"
                )

            # Validate collector_protocol
            valid_protocols = {"grpc", "http"}
            if (
                config.telemetry_config.collector_protocol.lower()
                not in valid_protocols
            ):
                raise ValueError(
                    f"collector_protocol must be one of {valid_protocols}, got {config.telemetry_config.collector_protocol}"
                )

            # Validate machine_metrics_interval_seconds
            if config.telemetry_config.machine_metrics_interval_seconds < 1:
                raise ValueError(
                    f"machine_metrics_interval_seconds must be >= 1, got {config.telemetry_config.machine_metrics_interval_seconds}"
                )

        # Validate search_limits_config (Story #3 - Phase 1, AC-M1, AC-M2)
        if config.search_limits_config:
            # Validate max_result_size_mb (1-100 MB range)
            if not (1 <= config.search_limits_config.max_result_size_mb <= 100):
                raise ValueError(
                    f"max_result_size_mb must be between 1 and 100, got {config.search_limits_config.max_result_size_mb}"
                )
            # Validate timeout_seconds (5-300 seconds range)
            if not (5 <= config.search_limits_config.timeout_seconds <= 300):
                raise ValueError(
                    f"timeout_seconds must be between 5 and 300, got {config.search_limits_config.timeout_seconds}"
                )

        # Validate file_content_limits_config (Story #3 - Phase 1, AC-M3, AC-M4)
        if config.file_content_limits_config:
            # Validate max_tokens_per_request (1000-50000 tokens range)
            if not (
                1000
                <= config.file_content_limits_config.max_tokens_per_request
                <= 50000
            ):
                raise ValueError(
                    f"max_tokens_per_request must be between 1000 and 50000, got {config.file_content_limits_config.max_tokens_per_request}"
                )
            # Validate chars_per_token (1-10 range)
            if not (1 <= config.file_content_limits_config.chars_per_token <= 10):
                raise ValueError(
                    f"chars_per_token must be between 1 and 10, got {config.file_content_limits_config.chars_per_token}"
                )

        # Validate golden_repos_config (Story #3 - Phase 1, AC-M5)
        if config.golden_repos_config:
            # Validate refresh_interval_seconds (minimum 60 seconds)
            if config.golden_repos_config.refresh_interval_seconds < 60:
                raise ValueError(
                    f"refresh_interval_seconds must be >= 60, got {config.golden_repos_config.refresh_interval_seconds}"
                )

        # Validate health_config (Story #3 - Phase 2, AC37)
        if config.health_config:
            # AC37: system_metrics_cache_ttl_seconds range 1-60
            if not (1 <= config.health_config.system_metrics_cache_ttl_seconds <= 60):
                raise ValueError(
                    f"system_metrics_cache_ttl_seconds must be between 1 and 60, got {config.health_config.system_metrics_cache_ttl_seconds}"
                )

        # Validate scip_config (Story #3 - Phase 2, AC31-AC34)
        if config.scip_config:
            # AC31: scip_reference_limit range 10-10000
            if not (10 <= config.scip_config.scip_reference_limit <= 10000):
                raise ValueError(
                    f"scip_reference_limit must be between 10 and 10000, got {config.scip_config.scip_reference_limit}"
                )
            # AC32: scip_dependency_depth range 1-20
            if not (1 <= config.scip_config.scip_dependency_depth <= 20):
                raise ValueError(
                    f"scip_dependency_depth must be between 1 and 20, got {config.scip_config.scip_dependency_depth}"
                )
            # AC33: scip_callchain_max_depth range 1-50
            if not (1 <= config.scip_config.scip_callchain_max_depth <= 50):
                raise ValueError(
                    f"scip_callchain_max_depth must be between 1 and 50, got {config.scip_config.scip_callchain_max_depth}"
                )
            # AC34: scip_callchain_limit range 1-1000
            if not (1 <= config.scip_config.scip_callchain_limit <= 1000):
                raise ValueError(
                    f"scip_callchain_limit must be between 1 and 1000, got {config.scip_config.scip_callchain_limit}"
                )

        # Validate git_timeouts_config (Story #3 - Phase 2, AC12-AC15, AC27-AC30)
        if config.git_timeouts_config:
            # AC13: git_local_timeout minimum 5 seconds
            if config.git_timeouts_config.git_local_timeout < 5:
                raise ValueError(
                    f"git_local_timeout must be >= 5, got {config.git_timeouts_config.git_local_timeout}"
                )
            # AC14: git_remote_timeout minimum 30 seconds
            if config.git_timeouts_config.git_remote_timeout < 30:
                raise ValueError(
                    f"git_remote_timeout must be >= 30, got {config.git_timeouts_config.git_remote_timeout}"
                )
            # AC15: git_command_timeout minimum 5 seconds
            if config.git_timeouts_config.git_command_timeout < 5:
                raise ValueError(
                    f"git_command_timeout must be >= 5, got {config.git_timeouts_config.git_command_timeout}"
                )
            # AC15: git_fetch_timeout minimum 10 seconds
            if config.git_timeouts_config.git_fetch_timeout < 10:
                raise ValueError(
                    f"git_fetch_timeout must be >= 10, got {config.git_timeouts_config.git_fetch_timeout}"
                )
            # AC27: github_api_timeout range 5-120 seconds
            if not (5 <= config.git_timeouts_config.github_api_timeout <= 120):
                raise ValueError(
                    f"github_api_timeout must be between 5 and 120, got {config.git_timeouts_config.github_api_timeout}"
                )
            # AC28: gitlab_api_timeout range 5-120 seconds
            if not (5 <= config.git_timeouts_config.gitlab_api_timeout <= 120):
                raise ValueError(
                    f"gitlab_api_timeout must be between 5 and 120, got {config.git_timeouts_config.gitlab_api_timeout}"
                )
            # AC29: github_provider_timeout range 5-120 seconds
            if not (5 <= config.git_timeouts_config.github_provider_timeout <= 120):
                raise ValueError(
                    f"github_provider_timeout must be between 5 and 120, got {config.git_timeouts_config.github_provider_timeout}"
                )
            # AC30: gitlab_provider_timeout range 5-120 seconds
            if not (5 <= config.git_timeouts_config.gitlab_provider_timeout <= 120):
                raise ValueError(
                    f"gitlab_provider_timeout must be between 5 and 120, got {config.git_timeouts_config.gitlab_provider_timeout}"
                )

        # Validate error_handling_config (Story #3 - Phase 2, AC16-AC18)
        if config.error_handling_config:
            # AC17: max_retry_attempts range 1-10
            if not (1 <= config.error_handling_config.max_retry_attempts <= 10):
                raise ValueError(
                    f"max_retry_attempts must be between 1 and 10, got {config.error_handling_config.max_retry_attempts}"
                )
            # AC18: base_retry_delay_seconds range 0.01-5.0
            if not (
                0.01 <= config.error_handling_config.base_retry_delay_seconds <= 5.0
            ):
                raise ValueError(
                    f"base_retry_delay_seconds must be between 0.01 and 5.0, got {config.error_handling_config.base_retry_delay_seconds}"
                )
            # AC18: max_retry_delay_seconds range 1-300
            if not (1 <= config.error_handling_config.max_retry_delay_seconds <= 300):
                raise ValueError(
                    f"max_retry_delay_seconds must be between 1 and 300, got {config.error_handling_config.max_retry_delay_seconds}"
                )

        # Validate api_limits_config (Story #3 - Phase 2, AC19-AC24, AC35, AC38-AC39)
        if config.api_limits_config:
            # AC20: default_file_read_lines range 100-5000
            if not (100 <= config.api_limits_config.default_file_read_lines <= 5000):
                raise ValueError(
                    f"default_file_read_lines must be between 100 and 5000, got {config.api_limits_config.default_file_read_lines}"
                )
            # AC20: max_file_read_lines range 500-50000
            if not (500 <= config.api_limits_config.max_file_read_lines <= 50000):
                raise ValueError(
                    f"max_file_read_lines must be between 500 and 50000, got {config.api_limits_config.max_file_read_lines}"
                )
            # AC21-22: default_diff_lines range 100-5000
            if not (100 <= config.api_limits_config.default_diff_lines <= 5000):
                raise ValueError(
                    f"default_diff_lines must be between 100 and 5000, got {config.api_limits_config.default_diff_lines}"
                )
            # AC21-22: max_diff_lines range 500-50000
            if not (500 <= config.api_limits_config.max_diff_lines <= 50000):
                raise ValueError(
                    f"max_diff_lines must be between 500 and 50000, got {config.api_limits_config.max_diff_lines}"
                )
            # AC23-24: default_log_commits range 10-500
            if not (10 <= config.api_limits_config.default_log_commits <= 500):
                raise ValueError(
                    f"default_log_commits must be between 10 and 500, got {config.api_limits_config.default_log_commits}"
                )
            # AC23-24: max_log_commits range 50-5000
            if not (50 <= config.api_limits_config.max_log_commits <= 5000):
                raise ValueError(
                    f"max_log_commits must be between 50 and 5000, got {config.api_limits_config.max_log_commits}"
                )
            # AC35: audit_log_default_limit range 10-1000
            if not (10 <= config.api_limits_config.audit_log_default_limit <= 1000):
                raise ValueError(
                    f"audit_log_default_limit must be between 10 and 1000, got {config.api_limits_config.audit_log_default_limit}"
                )
            # AC38: log_page_size_default range 10-500
            if not (10 <= config.api_limits_config.log_page_size_default <= 500):
                raise ValueError(
                    f"log_page_size_default must be between 10 and 500, got {config.api_limits_config.log_page_size_default}"
                )
            # AC39: log_page_size_max range 100-5000
            if not (100 <= config.api_limits_config.log_page_size_max <= 5000):
                raise ValueError(
                    f"log_page_size_max must be between 100 and 5000, got {config.api_limits_config.log_page_size_max}"
                )

        # Validate web_security_config (Story #3 - Phase 2, AC25-AC26)
        if config.web_security_config:
            # AC26: csrf_max_age_seconds range 60-3600
            if not (60 <= config.web_security_config.csrf_max_age_seconds <= 3600):
                raise ValueError(
                    f"csrf_max_age_seconds must be between 60 and 3600, got {config.web_security_config.csrf_max_age_seconds}"
                )
            # AC26: web_session_timeout_seconds range 1800-86400
            if not (
                1800 <= config.web_security_config.web_session_timeout_seconds <= 86400
            ):
                raise ValueError(
                    f"web_session_timeout_seconds must be between 1800 and 86400, got {config.web_security_config.web_session_timeout_seconds}"
                )

        # Validate auth_config (Story #3 - Phase 2, AC36)
        if config.auth_config:
            # AC36: oauth_extension_threshold_hours range 1-24
            if not (1 <= config.auth_config.oauth_extension_threshold_hours <= 24):
                raise ValueError(
                    f"oauth_extension_threshold_hours must be between 1 and 24, got {config.auth_config.oauth_extension_threshold_hours}"
                )

        # Validate multi_search_limits_config (Story #25, Story #29)
        if config.multi_search_limits_config:
            # multi_search_max_workers range 1-50
            if not (1 <= config.multi_search_limits_config.multi_search_max_workers <= 50):
                raise ValueError(
                    f"multi_search_max_workers must be between 1 and 50, got {config.multi_search_limits_config.multi_search_max_workers}"
                )
            # multi_search_timeout_seconds range 5-600
            if not (5 <= config.multi_search_limits_config.multi_search_timeout_seconds <= 600):
                raise ValueError(
                    f"multi_search_timeout_seconds must be between 5 and 600, got {config.multi_search_limits_config.multi_search_timeout_seconds}"
                )
            # scip_multi_max_workers range 1-50
            if not (1 <= config.multi_search_limits_config.scip_multi_max_workers <= 50):
                raise ValueError(
                    f"scip_multi_max_workers must be between 1 and 50, got {config.multi_search_limits_config.scip_multi_max_workers}"
                )
            # scip_multi_timeout_seconds range 5-600
            if not (5 <= config.multi_search_limits_config.scip_multi_timeout_seconds <= 600):
                raise ValueError(
                    f"scip_multi_timeout_seconds must be between 5 and 600, got {config.multi_search_limits_config.scip_multi_timeout_seconds}"
                )

            # Story #29: Validate omni settings (merged from OmniSearchConfig)
            # omni_max_workers range 1-100
            if not (1 <= config.multi_search_limits_config.omni_max_workers <= 100):
                raise ValueError(
                    f"omni_max_workers must be between 1 and 100, got {config.multi_search_limits_config.omni_max_workers}"
                )
            # omni_per_repo_timeout_seconds range 1-3600
            if not (1 <= config.multi_search_limits_config.omni_per_repo_timeout_seconds <= 3600):
                raise ValueError(
                    f"omni_per_repo_timeout_seconds must be between 1 and 3600, got {config.multi_search_limits_config.omni_per_repo_timeout_seconds}"
                )
            # omni_cache_max_entries range 1-10000
            if not (1 <= config.multi_search_limits_config.omni_cache_max_entries <= 10000):
                raise ValueError(
                    f"omni_cache_max_entries must be between 1 and 10000, got {config.multi_search_limits_config.omni_cache_max_entries}"
                )
            # omni_cache_ttl_seconds range 1-86400
            if not (1 <= config.multi_search_limits_config.omni_cache_ttl_seconds <= 86400):
                raise ValueError(
                    f"omni_cache_ttl_seconds must be between 1 and 86400, got {config.multi_search_limits_config.omni_cache_ttl_seconds}"
                )
            # omni_default_limit range 1-1000
            if not (1 <= config.multi_search_limits_config.omni_default_limit <= 1000):
                raise ValueError(
                    f"omni_default_limit must be between 1 and 1000, got {config.multi_search_limits_config.omni_default_limit}"
                )
            # omni_max_limit range 1-10000
            if not (1 <= config.multi_search_limits_config.omni_max_limit <= 10000):
                raise ValueError(
                    f"omni_max_limit must be between 1 and 10000, got {config.multi_search_limits_config.omni_max_limit}"
                )
            # omni_default_aggregation_mode must be "global" or "per_repo"
            valid_omni_modes = {"global", "per_repo"}
            if config.multi_search_limits_config.omni_default_aggregation_mode not in valid_omni_modes:
                raise ValueError(
                    f"omni_default_aggregation_mode must be one of {valid_omni_modes}, got {config.multi_search_limits_config.omni_default_aggregation_mode}"
                )
            # omni_max_results_per_repo range 1-10000
            if not (1 <= config.multi_search_limits_config.omni_max_results_per_repo <= 10000):
                raise ValueError(
                    f"omni_max_results_per_repo must be between 1 and 10000, got {config.multi_search_limits_config.omni_max_results_per_repo}"
                )
            # omni_max_total_results_before_aggregation range 1-100000
            if not (1 <= config.multi_search_limits_config.omni_max_total_results_before_aggregation <= 100000):
                raise ValueError(
                    f"omni_max_total_results_before_aggregation must be between 1 and 100000, got {config.multi_search_limits_config.omni_max_total_results_before_aggregation}"
                )

        # Validate background_jobs_config (Story #26, Story #27)
        if config.background_jobs_config:
            # max_concurrent_background_jobs range 1-100
            if not (1 <= config.background_jobs_config.max_concurrent_background_jobs <= 100):
                raise ValueError(
                    f"max_concurrent_background_jobs must be between 1 and 100, got {config.background_jobs_config.max_concurrent_background_jobs}"
                )
            # Story #27: subprocess_max_workers range 1-50
            if not (1 <= config.background_jobs_config.subprocess_max_workers <= 50):
                raise ValueError(
                    f"subprocess_max_workers must be between 1 and 50, got {config.background_jobs_config.subprocess_max_workers}"
                )

        # Validate content_limits_config (Story #32)
        if config.content_limits_config:
            # chars_per_token range 1-10
            if not (1 <= config.content_limits_config.chars_per_token <= 10):
                raise ValueError(
                    f"chars_per_token must be between 1 and 10, got {config.content_limits_config.chars_per_token}"
                )
            # file_content_max_tokens range 1000-200000
            if not (1000 <= config.content_limits_config.file_content_max_tokens <= 200000):
                raise ValueError(
                    f"file_content_max_tokens must be between 1000 and 200000, got {config.content_limits_config.file_content_max_tokens}"
                )
            # git_diff_max_tokens range 1000-200000
            if not (1000 <= config.content_limits_config.git_diff_max_tokens <= 200000):
                raise ValueError(
                    f"git_diff_max_tokens must be between 1000 and 200000, got {config.content_limits_config.git_diff_max_tokens}"
                )
            # git_log_max_tokens range 1000-200000
            if not (1000 <= config.content_limits_config.git_log_max_tokens <= 200000):
                raise ValueError(
                    f"git_log_max_tokens must be between 1000 and 200000, got {config.content_limits_config.git_log_max_tokens}"
                )
            # search_result_max_tokens range 1000-200000
            if not (1000 <= config.content_limits_config.search_result_max_tokens <= 200000):
                raise ValueError(
                    f"search_result_max_tokens must be between 1000 and 200000, got {config.content_limits_config.search_result_max_tokens}"
                )
            # cache_ttl_seconds minimum 60 seconds
            if config.content_limits_config.cache_ttl_seconds < 60:
                raise ValueError(
                    f"cache_ttl_seconds must be >= 60, got {config.content_limits_config.cache_ttl_seconds}"
                )
            # cache_max_entries range 100-100000
            if not (100 <= config.content_limits_config.cache_max_entries <= 100000):
                raise ValueError(
                    f"cache_max_entries must be between 100 and 100000, got {config.content_limits_config.cache_max_entries}"
                )

    def create_server_directories(self) -> None:
        """
        Create necessary server directories.

        Creates:
        - Main server directory
        - logs/ subdirectory
        - data/ subdirectory
        """
        # Create main server directory
        self.server_dir.mkdir(parents=True, exist_ok=True)

        # Create logs directory
        logs_dir = self.server_dir / "logs"
        logs_dir.mkdir(exist_ok=True)

        # Create data directory
        data_dir = self.server_dir / "data"
        data_dir.mkdir(exist_ok=True)
