"""
Configuration Service for CIDX Server Admin UI.

Provides a high-level interface for reading and updating server configuration.
All settings persist to ~/.cidx-server/config.json via ServerConfigManager.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import copy
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    runtime_checkable,
)

from ..config.delegation_config import ClaudeDelegationManager, ClaudeDelegationConfig
from ..utils.config_manager import (
    CidxMetaBackupConfig,
    LifecycleAnalysisConfig,
    RerankConfig,
    ServerConfig,
    ServerConfigManager,
)
from ..auto_update.deployment_executor import (
    APPLIED_LAUNCH_CONFIG_PATH,
    LAUNCH_CONFIG_PATH,
    RESTART_SIGNAL_PATH,
    read_execstart_flags,
)
from .db_outage_throttle import DbOutageThrottle

logger = logging.getLogger(__name__)


@dataclass
class ExtensionDrift:
    """Describes which file extensions were added or removed relative to server config.

    Returned by sync_repo_extensions_if_drifted() when drift is detected.
    Both sets contain bare extension names without leading dots (e.g. 'py', 'jsonl').
    """

    added: Set[str] = field(default_factory=set)
    removed: Set[str] = field(default_factory=set)


def _activated_reaper_settings(config: ServerConfig) -> Dict[str, Any]:
    """Return activated_reaper settings dict from ServerConfig (Story #967)."""
    reaper = config.activated_reaper_config
    assert reaper is not None  # Guaranteed by ServerConfig.__post_init__
    return {
        "ttl_days": reaper.ttl_days,
        "cadence_hours": reaper.cadence_hours,
    }


def _hnsw_orphan_sweep_settings(config: ServerConfig) -> Dict[str, Any]:
    """Return hnsw_orphan_sweep settings dict from ServerConfig (Story #1397).

    Surfaces all 5 fields of HNSWOrphanRepairSweepConfig for the Web UI
    Config screen -- extends Story #1360's config object (enabled,
    batch_size, tick_interval_minutes) with the new operating-hours window
    fields.
    """
    sweep = config.hnsw_orphan_repair_sweep_config
    assert sweep is not None  # Guaranteed by ServerConfig.__post_init__
    return {
        "enabled": sweep.enabled,
        "batch_size": sweep.batch_size,
        "tick_interval_minutes": sweep.tick_interval_minutes,
        "operating_hours_start_utc": sweep.operating_hours_start_utc,
        "operating_hours_end_utc": sweep.operating_hours_end_utc,
    }


def _search_timeouts_settings(config: ServerConfig) -> Dict[str, Any]:
    """Return search_timeouts settings dict from ServerConfig (Issue #1398).

    Surfaces all 5 fields of SearchTimeoutsConfig for the Web UI Config
    screen -- consolidates the previously hardcoded MCP handler timeouts
    (search_code / default / write_mode) and the embedding-provider /
    reranker HTTP timeouts into one validated, editable section.
    """
    st = config.search_timeouts_config
    assert st is not None  # Guaranteed by ServerConfig.__post_init__
    return {
        "search_code_handler_timeout_seconds": st.search_code_handler_timeout_seconds,
        "default_handler_timeout_seconds": st.default_handler_timeout_seconds,
        "write_mode_handler_timeout_seconds": st.write_mode_handler_timeout_seconds,
        "embedding_provider_timeout_seconds": st.embedding_provider_timeout_seconds,
        "reranker_timeout_seconds": st.reranker_timeout_seconds,
        # Story #1400 CRITICAL 5: async-hybrid temporal query inline
        # sync-wait window (float seconds).
        "temporal_inline_wait_seconds": st.temporal_inline_wait_seconds,
    }


def _embedding_stats_settings(config: ServerConfig) -> Dict[str, Any]:
    """Return embedding_stats settings dict from ServerConfig (Story #1418
    Phase 3).

    Surfaces EmbeddingStatsConfig's 3 fields (enabled kill-switch, writer
    flush cadence, retention window) for the Web UI Config screen.
    """
    es = config.embedding_stats_config
    assert es is not None  # Guaranteed by ServerConfig.__post_init__
    return {
        "enabled": es.enabled,
        "flush_interval_seconds": es.flush_interval_seconds,
        "retention_days": es.retention_days,
    }


@runtime_checkable
class ElevationManagerProtocol(Protocol):
    """Structural protocol for the ElevatedSessionManager hot-reload interface.

    Allows update_totp_elevation_atomic to accept the real singleton without
    importing ElevatedSessionManager (avoids circular import) and without
    weakening type safety via Any.
    """

    def update_timeouts(
        self, idle_timeout_seconds: int, max_age_seconds: int
    ) -> None: ...


# Story #578: Keys that must stay in local config.json (chicken-and-egg: needed
# before PG pool exists).  Everything else is "runtime" and lives in PG in
# cluster mode.
#
# Story #1197: host, port, workers, log_level are moved to runtime config so
# they can be shared cluster-wide via the runtime DB row.  They are removed
# from BOOTSTRAP_KEYS here.  Story #1197 also kept a one-release transition
# allow-list (TRANSITION_PRESERVE_KEYS) so the four keys survived the
# first-boot strip and every subsequent save_config() call, giving old nodes
# in a rolling upgrade a config.json fallback.  Story #1196 (next-release
# cleanup) has removed that allow-list entirely -- the operator confirmed all
# cluster nodes are on the new release, so the runtime DB / launch.json /
# applied_launch.json are now the sole source for these four settings.
BOOTSTRAP_KEYS = frozenset(
    {
        "server_dir",
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
        "server_threadpool_size",  # startup threadpool config
        "pace_maker_clone_path",  # Story #997 - bootstrap-only (written by auto-updater pre-DB)
        "mcp_dispatch_pool_size",  # Story #1009 - asyncio default executor size
        "query_executor_pool_size",  # perf - shared query executor size (created at startup, pre-DB)
        "enable_predeactivation_leak_scan",  # Story #1032 - bootstrap flag to restore pre-flight leak scan
        "orphan_trash_sweep_per_startup_cap",  # Story #1032 HIGH #3 - cap startup orphan sweep entries
        "clone_backend",  # Story #510 / #1034 - needed pre-DB by build_snapshot_manager() at lifespan startup
        "cow_daemon",  # Story #510 / #1034 - daemon config for CowDaemonBackend wiring at startup
    }
)
CONFIG_KEY_RUNTIME = "runtime"
UPDATER_WEB_UI = "web-ui"
UPDATER_SEED = "config-seed"

# Story #1200 FIX-6: number of consecutive pending-restart polls before emitting
# a rate-limited WARNING (approx 5 min at 30s poll interval).
PENDING_RESTART_WARN_THRESHOLD = 10

# Bug #875: minimum bounds for new claude_cli settings
_MIN_FACT_CHECK_TIMEOUT_SECONDS = 60
_MIN_SCHEDULED_CATCHUP_INTERVAL_MINUTES = 1


def _parse_bool(value: Any) -> bool:
    """Return True when value is the string 'true', 'True', or the boolean True."""
    return value in ["true", True, "True"]


# Bug #943: module-level exports so routes.py can coerce the
# elevation_enforcement_enabled form field without importing ConfigService.
# The ConfigService class defines identically-valued class attributes; those
# shadow these names for self._ access (standard Python class scoping).
_TOTP_TRUTHY: frozenset = frozenset({"true", "on", "1"})
_TOTP_FALSY: frozenset = frozenset({"false", "off", "0"})


class ConfigService:
    """
    Service for managing server configuration.

    Provides methods for loading, updating, and saving server configuration
    with validation. All changes persist to ~/.cidx-server/config.json.
    """

    def __init__(
        self,
        server_dir_path: Optional[str] = None,
        config_manager: Optional["ServerConfigManager"] = None,
    ):
        """
        Initialize the configuration service.

        Args:
            server_dir_path: Optional path to server directory.
                           Defaults to ~/.cidx-server
            config_manager: Optional pre-built ServerConfigManager instance.
                          When provided, server_dir_path is ignored for config
                          loading (but still used for ClaudeDelegationManager).
                          Primarily useful for unit tests.
        """
        if config_manager is not None:
            self.config_manager = config_manager
        else:
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
        # Story #1200 FIX-6: consecutive pending-restart poll counter + rate-limit flag
        self._pending_restart_poll_count: int = 0
        self._pending_restart_warned: bool = False
        # Bug #1249: collapse a PG-outage error storm into a single ERROR +
        # DEBUG follow-ups instead of logging a fresh traceback every tick.
        # Shared across both try/except blocks in start_config_reload's poll loop.
        self._db_throttle = DbOutageThrottle(service_name="ConfigService")
        # Bug #1335: memoized raw config.json snapshot captured the FIRST time
        # _backfill_launch_keys_from_execstart runs -- i.e. BEFORE
        # _strip_config_file_to_bootstrap has ever had a chance to strip
        # host/port/workers off disk within this process. None means "not
        # captured yet"; a captured value is a dict (possibly empty).
        self._bootstrap_launch_keys_snapshot: Optional[Dict[str, Any]] = None
        # Story #1400 CRITICAL 6: guards update_settings_atomic's
        # validate-copy-then-publish sequence (deep-copy live config ->
        # apply updates to the copy -> validate the copy -> publish only on
        # success). Never held during I/O beyond the deep-copy/apply/
        # validate/save sequence itself.
        self._config_update_lock: threading.RLock = threading.RLock()

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
            self._backfill_launch_keys_from_execstart(config)  # Bug #1232: gap-fill
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
            while not self._stop_event.wait(
                timeout=self._db_throttle.next_wait_seconds(interval_seconds)
            ):
                try:
                    if self.check_config_update():
                        logger.info("ConfigService: reloaded config from PG")
                    self._db_throttle.on_db_success(logger)
                except Exception as exc:
                    # Bug #1249: a connectivity error is throttled (single
                    # ERROR transition, DEBUG follow-ups); anything else
                    # still logs normally every time.
                    if not self._db_throttle.on_db_error(exc, logger):
                        logger.exception("ConfigService: config reload poll failed")
                # Story #1200 AC3 CRITICAL-C1: check_pending_launch_restart fires
                # on EVERY interval, independent of whether the version changed.
                # NOT a callback — callbacks only fire on version-diff edge.
                try:
                    self.check_pending_launch_restart()
                    # NOTE: no on_db_success() here — check_pending_launch_restart()
                    # already swallows its own DB-read failures internally at
                    # DEBUG (see _read_raw_launch_snapshot) and never raises a
                    # connectivity exception. Calling on_db_success() here would
                    # always fire regardless of real DB health and incorrectly
                    # reset the outage counter set by check_config_update's
                    # failure in the same tick (Bug #1249 wiring correctness).
                except Exception as exc:
                    if not self._db_throttle.on_db_error(exc, logger):
                        logger.exception(
                            "ConfigService: check_pending_launch_restart poll failed"
                        )

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
            self.config_manager.save_config(config)

        # If runtime DB is available, merge runtime from DB on top of the
        # bootstrap config.  Pass bootstrap config as base_config so that
        # self._config is NOT published until after the full merge completes
        # (Bug #998: prevents concurrent get_config() from seeing transient
        # bootstrap defaults during the merge window).
        if self._pool is not None:
            self._load_runtime_from_pg(base_config=config)
        elif self._sqlite_db_path:
            runtime = self._load_runtime_from_sqlite()
            if runtime:
                self._merge_runtime_config(runtime, base_config=config)
            else:
                self._config = config
        else:
            self._config = config

        assert self._config is not None  # All branches above set self._config
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
                # Story #1213 Story 2: memory-governor runtime knobs
                "memory_governor_enabled": config.cache_config.memory_governor_enabled,
                "memory_governor_yellow_pct": config.cache_config.memory_governor_yellow_pct,
                "memory_governor_red_pct": config.cache_config.memory_governor_red_pct,
                "memory_governor_hysteresis_pct": config.cache_config.memory_governor_hysteresis_pct,
                "memory_governor_red_min_dwell_seconds": config.cache_config.memory_governor_red_min_dwell_seconds,
                "memory_governor_sample_interval_seconds": config.cache_config.memory_governor_sample_interval_seconds,
                "memory_governor_swap_forces_red": config.cache_config.memory_governor_swap_forces_red,
                "memory_governor_rss_inflation_factor": config.cache_config.memory_governor_rss_inflation_factor,
                "memory_governor_swap_pswpin_red_threshold": config.cache_config.memory_governor_swap_pswpin_red_threshold,
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
            # Bug #943: TOTP step-up elevation runtime config
            "totp_elevation": {
                "elevation_enforcement_enabled": config.elevation_enforcement_enabled,
                "elevation_idle_timeout_seconds": config.elevation_idle_timeout_seconds,
                "elevation_max_age_seconds": config.elevation_max_age_seconds,
            },
            # Story #997: Pace-maker mode enforcement
            "pace_maker": {
                "pace_maker_mode": config.pace_maker_mode,
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
                "externally_managed": config.golden_repos_config.externally_managed,
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
            # Story #967 - Activated repository reaper configuration
            "activated_reaper": _activated_reaper_settings(config),
            # Story #1397 - HNSW orphan-repair sweep Web UI configuration
            "hnsw_orphan_sweep": _hnsw_orphan_sweep_settings(config),
            # Issue #1398 - Query & search timeouts Web UI configuration
            "search_timeouts": _search_timeouts_settings(config),
            # Story #1418 Phase 3 - Embedding & reranker call tracking config
            "embedding_stats": _embedding_stats_settings(config),
            # Story #977 - X-Ray precision AST-aware code search configuration
            "xray": {
                "xray_timeout_seconds": config.xray_config.xray_timeout_seconds,  # type: ignore[union-attr]
                "xray_worker_threads": config.xray_config.xray_worker_threads,  # type: ignore[union-attr]
            },
            # Story #223 - AC4: Indexing configuration
            "indexing": {
                "indexable_extensions": (
                    config.indexing_config.indexable_extensions
                    if config.indexing_config is not None
                    else []
                ),
                # Story #1158: parallel requests display wiring
                "voyage_ai_parallel_requests": (
                    config.indexing_config.voyage_ai_parallel_requests
                    if config.indexing_config is not None
                    else 8
                ),
                "cohere_parallel_requests": (
                    config.indexing_config.cohere_parallel_requests
                    if config.indexing_config is not None
                    else 8
                ),
                "temporal_parallel_requests": (
                    config.indexing_config.temporal_parallel_requests
                    if config.indexing_config is not None
                    else None
                ),
                # Story #1290: per-commit temporal embedder config display wiring
                "temporal_embedders": (
                    config.indexing_config.temporal_embedders
                    if config.indexing_config is not None
                    else ["voyage-context-4"]
                ),
                "temporal_active_embedder": (
                    config.indexing_config.temporal_active_embedder
                    if config.indexing_config is not None
                    else "voyage-context-4"
                ),
                "temporal_aggregation_chunk_chars": (
                    config.indexing_config.temporal_aggregation_chunk_chars
                    if config.indexing_config is not None
                    else 4096
                ),
                # Story #1412: golden/server temporal all-branches gate display wiring
                "temporal_all_branches_enabled": (
                    config.indexing_config.temporal_all_branches_enabled
                    if config.indexing_config is not None
                    else False
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
            # Issue #1159 — Search event log settings
            "search_event_log": {
                "search_event_log_retention_days": config.search_event_log_retention_days,
            },
            # Issue #1160 — Export retention settings
            "export": {
                "export_retention_days": config.export_retention_days,
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

        # Query embedding cache settings (Story #1107 S3)
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        qec_cfg = config.query_embedding_cache_config or QueryEmbeddingCacheConfig()
        settings["query_embedding_cache"] = {
            "query_embedding_cache_enabled": qec_cfg.query_embedding_cache_enabled,
            "query_embedding_cache_max_entries": qec_cfg.query_embedding_cache_max_entries,
            "query_embedding_cache_voyage_mode": qec_cfg.query_embedding_cache_voyage_mode,
            "query_embedding_cache_voyage_anchor_tokens": qec_cfg.query_embedding_cache_voyage_anchor_tokens,
            "query_embedding_cache_voyage_audit_sample_rate": qec_cfg.query_embedding_cache_voyage_audit_sample_rate,
            "query_embedding_cache_cohere_mode": qec_cfg.query_embedding_cache_cohere_mode,
            "query_embedding_cache_cohere_anchor_tokens": qec_cfg.query_embedding_cache_cohere_anchor_tokens,
            "query_embedding_cache_cohere_audit_sample_rate": qec_cfg.query_embedding_cache_cohere_audit_sample_rate,
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

    def _apply_setting(
        self, config: ServerConfig, category: str, key: str, value: Any
    ) -> bool:
        """
        Apply one (category, key, value) update to `config` in place.

        Story #1400 CRITICAL 6: extracted from the former update_setting
        dispatch body so callers (update_settings_atomic) can apply updates
        to a CANDIDATE copy of the config, never the live object directly.
        Every branch below is unchanged from the pre-#1400 dispatch.

        Returns:
            True if `config` was mutated and should participate in the
            standard validate-then-publish flow. False for the "indexing"
            category, a pre-existing special case that persists itself
            internally (_update_indexing_setting) against the live config
            and must not be re-validated/re-published by the generic
            atomic flow.

        Raises:
            ValueError: If category or key is invalid, or value fails
                per-field validation performed inline by the per-category
                helper (full cross-field validation happens later, in
                config_manager.validate_config, against the CANDIDATE).
        """
        if category == "server":
            self._update_server_setting(config, key, value)
        elif category == "cache":
            self._update_cache_setting(config, key, value)
        elif category == "timeouts":
            self._update_timeout_setting(config, key, value)
        elif category == "password_security":
            self._update_password_security_setting(config, key, value)
        elif category == "totp_elevation":
            self._update_totp_elevation_setting(config, key, value)
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
        # Story #967 - Activated repository reaper configuration
        elif category == "activated_reaper":
            self._update_activated_reaper_setting(config, key, value)
        # Story #1397 - HNSW orphan-repair sweep Web UI configuration
        elif category == "hnsw_orphan_sweep":
            self._update_hnsw_orphan_sweep_setting(config, key, value)
        # Issue #1398 - Query & search timeouts Web UI configuration
        elif category == "search_timeouts":
            self._update_search_timeouts_setting(config, key, value)
        # Story #977 - X-Ray precision AST-aware code search configuration
        elif category == "xray":
            self._update_xray_setting(config, key, value)
        # Story #223 - AC4: Indexing configuration
        elif category == "indexing":
            self._update_indexing_setting(key, value)
            # _update_indexing_setting saves config internally, so the
            # caller must skip the normal validate-then-publish flow.
            return False
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
        elif category == "pace_maker":
            self._update_pace_maker_setting(config, key, value)
        # Story #1107 S3 - Query embedding cache runtime configuration
        elif category == "query_embedding_cache":
            self._update_query_embedding_cache_setting(config, key, value)
        # Issue #1159 - Search event log retention
        elif category == "search_event_log":
            self._update_search_event_log_setting(config, key, value)
        # Issue #1160 - Export retention
        elif category == "export":
            self._update_export_setting(config, key, value)
        # Story #1418 Phase 3 - Embedding & reranker call tracking config
        elif category == "embedding_stats":
            self._update_embedding_stats_setting(config, key, value)
        else:
            raise ValueError(f"Unknown category: {category}")
        return True

    def _log_applied_updates(self, updates: Sequence[Tuple[str, str, Any]]) -> None:
        """Log every non-"indexing" update after a successful atomic publish."""
        for category, key, value in updates:
            if category == "indexing":
                continue
            logger.info(
                "Updated setting %s.%s to %s",
                category,
                key,
                value,
                extra={"correlation_id": get_correlation_id()},
            )

    def update_settings_atomic(
        self, updates: Sequence[Tuple[str, str, Any]]
    ) -> ServerConfig:
        """
        Apply a batch of (category, key, value) updates as ONE atomic unit.

        Story #1400 CRITICAL 6: validate a deep-copied CANDIDATE and publish
        atomically only on full success, so a rejected update can never
        leave the shared live config mutated in place. The "indexing"
        category is a pre-existing special case that self-persists against
        the live config, independent of this batch's atomicity.

        Raises:
            ValueError: invalid category/key, or the candidate fails
                config_manager.validate_config.
        """
        with self._config_update_lock:
            live = self.get_config()
            candidate = copy.deepcopy(live)
            has_candidate_updates = False
            for category, key, value in updates:
                if category == "indexing":
                    self._apply_setting(live, category, key, value)
                    continue
                if self._apply_setting(candidate, category, key, value):
                    has_candidate_updates = True

            if has_candidate_updates:
                self.config_manager.validate_config(candidate)
                self.save_config(candidate)
                self._log_applied_updates(updates)

            return self.get_config()

    def update_setting(
        self, category: str, key: str, value: Any, skip_validation: bool = False
    ) -> None:
        """
        Update a single setting.

        Story #1400 CRITICAL 6: delegates to update_settings_atomic, which
        validates against a COPY and publishes atomically only on success.
        `skip_validation` is now a no-op kept only for call-site signature
        compatibility -- the old "mutate first, validate later" deferred
        path (which could leave a rejected value live) has been retired.

        Raises:
            ValueError: If category or key is invalid, or value fails validation
        """
        self.update_settings_atomic([(category, key, value)])

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
        # Story #1079 Phase E - embedding coalescer runtime settings. Tunable via
        # the existing config-update path (no separate HTML widget — mirrors how
        # query_provider_max_concurrency is a top-level runtime field). The kill
        # switch (coalesce_enabled=False) and caps hot-reload because the helper /
        # registry read them live.
        elif key == "coalesce_enabled":
            config.coalesce_enabled = _parse_bool(value)
        elif key == "coalesce_max_batch_size":
            config.coalesce_max_batch_size = int(value)
        elif key == "coalesce_k_min":
            config.coalesce_k_min = int(value)
        elif key == "coalesce_k_max":
            config.coalesce_k_max = int(value)
        else:
            raise ValueError(f"Unknown server setting: {key}")

    def _update_cache_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update a cache setting."""
        cache = config.cache_config
        assert cache is not None  # Guaranteed by ServerConfig.__post_init__
        # Bug #1396: blank means "no override, use default" for these
        # fields, matching the size-cap fields' existing blank-tolerance
        # idiom below (int(value) if value else None).
        DEFAULT_CACHE_TTL_MINUTES = 10.0
        DEFAULT_CACHE_CLEANUP_INTERVAL = 60
        DEFAULT_PAYLOAD_PREVIEW_SIZE_CHARS = 2000
        DEFAULT_PAYLOAD_MAX_FETCH_SIZE_CHARS = 5000
        DEFAULT_PAYLOAD_CACHE_TTL_SECONDS = 900
        DEFAULT_PAYLOAD_CLEANUP_INTERVAL_SECONDS = 60
        if key == "index_cache_ttl_minutes":
            cache.index_cache_ttl_minutes = (
                float(value) if value else DEFAULT_CACHE_TTL_MINUTES
            )
        elif key == "index_cache_cleanup_interval":
            # Bug #1396: blank means "no override, use default".
            cache.index_cache_cleanup_interval = (
                int(value) if value else DEFAULT_CACHE_CLEANUP_INTERVAL
            )
        elif key == "index_cache_max_size_mb":
            cache.index_cache_max_size_mb = int(value) if value else None
        elif key == "fts_cache_ttl_minutes":
            cache.fts_cache_ttl_minutes = (
                float(value) if value else DEFAULT_CACHE_TTL_MINUTES
            )
        elif key == "fts_cache_cleanup_interval":
            # Bug #1396: blank means "no override, use default".
            cache.fts_cache_cleanup_interval = (
                int(value) if value else DEFAULT_CACHE_CLEANUP_INTERVAL
            )
        elif key == "fts_cache_max_size_mb":
            cache.fts_cache_max_size_mb = int(value) if value else None
        elif key == "fts_cache_reload_on_access":
            cache.fts_cache_reload_on_access = bool(value)
        # Payload cache settings (Story #679).
        # Bug #1396: blank means "no override, use default" for all four.
        elif key == "payload_preview_size_chars":
            cache.payload_preview_size_chars = (
                int(value) if value else DEFAULT_PAYLOAD_PREVIEW_SIZE_CHARS
            )
        elif key == "payload_max_fetch_size_chars":
            cache.payload_max_fetch_size_chars = (
                int(value) if value else DEFAULT_PAYLOAD_MAX_FETCH_SIZE_CHARS
            )
        elif key == "payload_cache_ttl_seconds":
            cache.payload_cache_ttl_seconds = (
                int(value) if value else DEFAULT_PAYLOAD_CACHE_TTL_SECONDS
            )
        elif key == "payload_cleanup_interval_seconds":
            cache.payload_cleanup_interval_seconds = (
                int(value) if value else DEFAULT_PAYLOAD_CLEANUP_INTERVAL_SECONDS
            )
        # Story #1213 Story 2: Memory-governor runtime knobs (hot-reload).
        elif key == "memory_governor_enabled":
            cache.memory_governor_enabled = _parse_bool(value)
        elif key == "memory_governor_yellow_pct":
            new_yellow = float(value)
            if new_yellow <= 0:
                raise ValueError(
                    f"memory_governor_yellow_pct must be > 0, got {new_yellow}"
                )
            if new_yellow >= cache.memory_governor_red_pct:
                raise ValueError(
                    f"memory_governor_yellow_pct ({new_yellow}) must be less than "
                    f"memory_governor_red_pct ({cache.memory_governor_red_pct})"
                )
            cache.memory_governor_yellow_pct = new_yellow
        elif key == "memory_governor_red_pct":
            new_red = float(value)
            if new_red > 100.0:
                raise ValueError(
                    f"memory_governor_red_pct must be <= 100, got {new_red}"
                )
            if cache.memory_governor_yellow_pct >= new_red:
                raise ValueError(
                    f"memory_governor_red_pct ({new_red}) must be greater than "
                    f"memory_governor_yellow_pct ({cache.memory_governor_yellow_pct})"
                )
            cache.memory_governor_red_pct = new_red
        elif key == "memory_governor_hysteresis_pct":
            new_hyst = float(value)
            yellow = cache.memory_governor_yellow_pct
            red = cache.memory_governor_red_pct
            max_allowed = min(yellow, 100.0 - red)
            if new_hyst >= max_allowed:
                raise ValueError(
                    f"memory_governor_hysteresis_pct ({new_hyst}) must be < "
                    f"min(yellow={yellow}, 100-red={100.0 - red}) = {max_allowed}"
                )
            cache.memory_governor_hysteresis_pct = new_hyst
        elif key == "memory_governor_red_min_dwell_seconds":
            cache.memory_governor_red_min_dwell_seconds = int(value)
        elif key == "memory_governor_sample_interval_seconds":
            cache.memory_governor_sample_interval_seconds = float(value)
        elif key == "memory_governor_swap_forces_red":
            cache.memory_governor_swap_forces_red = _parse_bool(value)
        elif key == "memory_governor_rss_inflation_factor":
            cache.memory_governor_rss_inflation_factor = float(value)
        elif key == "memory_governor_swap_pswpin_red_threshold":
            # Bug #1396: blank means "no override" -- fall back to the
            # CacheConfig dataclass default documented in config_manager.py
            # (memory_governor_swap_pswpin_red_threshold: int = 100) instead
            # of crashing on int('').
            DEFAULT_SWAP_PSWPIN_RED_THRESHOLD = 100
            if value:
                new_thr = int(value)
                if new_thr < 0:
                    raise ValueError(
                        f"memory_governor_swap_pswpin_red_threshold must be >= 0 "
                        f"(non-negative), got {new_thr}"
                    )
            else:
                new_thr = DEFAULT_SWAP_PSWPIN_RED_THRESHOLD
            cache.memory_governor_swap_pswpin_red_threshold = new_thr
        else:
            raise ValueError(f"Unknown cache setting: {key}")

        # Bug #878 Fix B.2: hot-reload max_cache_size_mb on the matching live
        # cache singleton so operators can bound native HNSW / FTS memory at
        # runtime without a server restart. Fix B.1 seats a default cap at
        # init time; Fix B.2 lets that cap change dynamically.
        #
        # Bug #1399: extends the same hot-reload pattern to the 4 other
        # CRITICAL cache-family keys (TTL x2, cleanup interval x2) plus
        # fts_cache_reload_on_access. All other cache settings (payload
        # cache, memory-governor watermarks handled by their own live-read
        # path) write through to config only (by design -- see test
        # TestHotReloadScopeIsolation / TestNewHotReloadScopeIsolation).
        if key == "index_cache_max_size_mb":
            self._hot_reload_cache_size_cap(
                cache_kind="HNSW", new_size_mb=cache.index_cache_max_size_mb
            )
        elif key == "fts_cache_max_size_mb":
            self._hot_reload_cache_size_cap(
                cache_kind="FTS", new_size_mb=cache.fts_cache_max_size_mb
            )
        elif key == "index_cache_ttl_minutes":
            self._hot_reload_cache_ttl_minutes(
                cache_kind="HNSW", new_ttl_minutes=cache.index_cache_ttl_minutes
            )
        elif key == "fts_cache_ttl_minutes":
            self._hot_reload_cache_ttl_minutes(
                cache_kind="FTS", new_ttl_minutes=cache.fts_cache_ttl_minutes
            )
        elif key == "index_cache_cleanup_interval":
            self._hot_reload_cache_cleanup_interval(
                cache_kind="HNSW",
                new_interval_seconds=cache.index_cache_cleanup_interval,
            )
        elif key == "fts_cache_cleanup_interval":
            self._hot_reload_cache_cleanup_interval(
                cache_kind="FTS",
                new_interval_seconds=cache.fts_cache_cleanup_interval,
            )
        elif key == "fts_cache_reload_on_access":
            self._hot_reload_fts_reload_on_access(cache.fts_cache_reload_on_access)

    @staticmethod
    def _resolve_live_cache(cache_kind: str) -> Any:
        """Return the live HNSW or FTS cache singleton for *cache_kind*.

        Shared by all cache-family hot-reload helpers (Bug #1399 anti-
        duplication: 5 call sites now need this same singleton lookup that
        previously existed only once, inline, in _hot_reload_cache_size_cap).

        Raises:
            ValueError: cache_kind is neither "HNSW" nor "FTS".
        """
        from code_indexer.server.cache import get_global_cache, get_global_fts_cache

        if cache_kind == "HNSW":
            return get_global_cache()
        elif cache_kind == "FTS":
            return get_global_fts_cache()
        raise ValueError(f"Unknown cache_kind: {cache_kind!r}")

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
            from code_indexer.server.cache import DEFAULT_MAX_CACHE_SIZE_MB

            cache = self._resolve_live_cache(cache_kind)

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

    def _hot_reload_cache_ttl_minutes(
        self, cache_kind: str, new_ttl_minutes: float
    ) -> None:
        """
        Bug #1399 CRITICAL fix: propagate an ``index_cache_ttl_minutes`` /
        ``fts_cache_ttl_minutes`` change to the live HNSW or FTS cache
        singleton.

        Design decision (documented per the issue): already-cached entries'
        ``ttl_minutes`` is rewritten EAGERLY under the cache lock, mirroring
        the size-cap fix's eager ``_enforce_size_limit()`` call -- not left
        to apply only to entries loaded after the change. This directly
        addresses the original production incident: an operator lowering
        the TTL to stop repeated cold-reload storms needs already-hot
        repositories to start respecting the new, shorter TTL immediately.

        Swallows and logs (WARNING) any failure, matching the established
        fail-soft contract of _hot_reload_cache_size_cap -- config
        persistence has already happened by the time this runs.
        """
        try:
            cache = self._resolve_live_cache(cache_kind)
            with cache._cache_lock:
                cache.config.ttl_minutes = new_ttl_minutes
                for entry in cache._cache.values():
                    entry.ttl_minutes = new_ttl_minutes

            logger.info(
                "Hot-reloaded %s cache ttl_minutes=%s (%d cached entries rewritten)",
                cache_kind,
                new_ttl_minutes,
                len(cache._cache),
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to hot-reload %s cache ttl_minutes=%s: %s",
                cache_kind,
                new_ttl_minutes,
                exc,
                extra={"correlation_id": get_correlation_id()},
            )

    def _hot_reload_cache_cleanup_interval(
        self, cache_kind: str, new_interval_seconds: int
    ) -> None:
        """
        Bug #1399 CRITICAL fix: propagate an ``index_cache_cleanup_interval``
        / ``fts_cache_cleanup_interval`` change to the live HNSW or FTS cache
        singleton.

        Design decision: unlike TTL, there is no per-entry state to rewrite
        here -- the background cleanup thread reads
        ``self.config.cleanup_interval_seconds`` fresh on every loop
        iteration (see ``start_background_cleanup``'s ``cleanup_loop()`` in
        hnsw_index_cache.py / fts_index_cache.py). Writing the new value
        onto ``cache.config`` is therefore sufficient: the CURRENT sleep
        (already in progress under the old interval) finishes on schedule,
        and every SUBSEQUENT cycle uses the new interval -- no restart
        required, effective within at most one old-interval-length window.
        """
        try:
            cache = self._resolve_live_cache(cache_kind)
            with cache._cache_lock:
                cache.config.cleanup_interval_seconds = new_interval_seconds

            logger.info(
                "Hot-reloaded %s cache cleanup_interval_seconds=%s",
                cache_kind,
                new_interval_seconds,
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to hot-reload %s cache cleanup_interval_seconds=%s: %s",
                cache_kind,
                new_interval_seconds,
                exc,
                extra={"correlation_id": get_correlation_id()},
            )

    def _hot_reload_fts_reload_on_access(self, new_value: bool) -> None:
        """
        Bug #1399 CRITICAL fix: propagate ``fts_cache_reload_on_access`` to
        the live FTS cache singleton.

        The FTS cache HIT path reads ``self.config.reload_on_access`` fresh
        on every access (see ``FTSIndexCache.get_or_load``), so mutating
        ``cache.config`` under the cache lock is sufficient -- the change
        takes effect on the very next FTS cache access.
        """
        try:
            cache = self._resolve_live_cache("FTS")
            with cache._cache_lock:
                cache.config.reload_on_access = new_value

            logger.info(
                "Hot-reloaded FTS cache reload_on_access=%s",
                new_value,
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to hot-reload FTS cache reload_on_access=%s: %s",
                new_value,
                exc,
                extra={"correlation_id": get_correlation_id()},
            )

    def reapply_live_cache_hot_reload_fields(self, config: "ServerConfig") -> None:
        """
        Bug #1399 item 7 (multi-worker/cluster gap): re-apply every
        live-reloadable cache-family field from *config* onto the live
        HNSW/FTS singletons in THIS process.

        ``_hot_reload_cache_size_cap`` (and the new TTL/cleanup/reload
        helpers above) only patch the singleton in the ONE worker process
        that handled the Web UI POST. Under ``uvicorn --workers N`` or in a
        cluster, sibling workers/nodes each run their own
        ``ConfigService.start_config_reload`` PG-poll loop and their own
        cache singletons -- without this method they keep the stale value
        indefinitely (even across a restart, since cache keys never reach
        config.json -- see BOOTSTRAP_KEYS).

        This method is registered as a PG config-change callback (mirrors
        Bug #943's ``update_totp_elevation_atomic`` pattern: a local
        synchronous hot-reload call on the processing node, PLUS this
        PG-poll callback so every sibling worker/node re-applies the same
        fresh values on its own next poll tick). Never raises -- each
        per-field helper already swallows its own failures.
        """
        cache = config.cache_config
        assert cache is not None  # Guaranteed by ServerConfig.__post_init__
        self._hot_reload_cache_size_cap(
            cache_kind="HNSW", new_size_mb=cache.index_cache_max_size_mb
        )
        self._hot_reload_cache_size_cap(
            cache_kind="FTS", new_size_mb=cache.fts_cache_max_size_mb
        )
        self._hot_reload_cache_ttl_minutes(
            cache_kind="HNSW", new_ttl_minutes=cache.index_cache_ttl_minutes
        )
        self._hot_reload_cache_ttl_minutes(
            cache_kind="FTS", new_ttl_minutes=cache.fts_cache_ttl_minutes
        )
        self._hot_reload_cache_cleanup_interval(
            cache_kind="HNSW", new_interval_seconds=cache.index_cache_cleanup_interval
        )
        self._hot_reload_cache_cleanup_interval(
            cache_kind="FTS", new_interval_seconds=cache.fts_cache_cleanup_interval
        )
        self._hot_reload_fts_reload_on_access(cache.fts_cache_reload_on_access)

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

    # Bug #943: TOTP elevation setting bounds
    _TOTP_IDLE_MIN: int = 60
    _TOTP_IDLE_MAX: int = 3600
    _TOTP_MAX_AGE_MIN: int = 300
    _TOTP_MAX_AGE_MAX: int = 7200
    _TOTP_TRUTHY: frozenset = frozenset({"true", "on", "1"})
    _TOTP_FALSY: frozenset = frozenset({"false", "off", "0"})

    def _update_totp_elevation_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a TOTP step-up elevation setting (Bug #943).

        Validation rules:
        - elevation_enforcement_enabled: coerce to bool; raise ValueError on unknown string.
        - elevation_idle_timeout_seconds: int in [_TOTP_IDLE_MIN, _TOTP_IDLE_MAX];
          also verify current max_age >= new idle (bidirectional cross-field).
        - elevation_max_age_seconds: int in [_TOTP_MAX_AGE_MIN, _TOTP_MAX_AGE_MAX]
          and >= current idle_timeout.
        """
        if key == "elevation_enforcement_enabled":
            if isinstance(value, bool):
                config.elevation_enforcement_enabled = value
            else:
                str_val = str(value).lower()
                if str_val in self._TOTP_TRUTHY:
                    config.elevation_enforcement_enabled = True
                elif str_val in self._TOTP_FALSY:
                    config.elevation_enforcement_enabled = False
                else:
                    raise ValueError(
                        f"Invalid value for elevation_enforcement_enabled: {value!r}. "
                        "Accepted: true/on/1 or false/off/0."
                    )
        elif key == "elevation_idle_timeout_seconds":
            int_val = int(value)
            if not (self._TOTP_IDLE_MIN <= int_val <= self._TOTP_IDLE_MAX):
                raise ValueError(
                    f"elevation_idle_timeout_seconds must be between "
                    f"{self._TOTP_IDLE_MIN} and {self._TOTP_IDLE_MAX}, got {int_val}"
                )
            if config.elevation_max_age_seconds < int_val:
                raise ValueError(
                    f"elevation_idle_timeout_seconds ({int_val}) must not exceed "
                    f"elevation_max_age_seconds ({config.elevation_max_age_seconds})"
                )
            config.elevation_idle_timeout_seconds = int_val
        elif key == "elevation_max_age_seconds":
            int_val = int(value)
            if not (self._TOTP_MAX_AGE_MIN <= int_val <= self._TOTP_MAX_AGE_MAX):
                raise ValueError(
                    f"elevation_max_age_seconds must be between "
                    f"{self._TOTP_MAX_AGE_MIN} and {self._TOTP_MAX_AGE_MAX}, got {int_val}"
                )
            if int_val < config.elevation_idle_timeout_seconds:
                raise ValueError(
                    f"elevation_max_age_seconds ({int_val}) must be >= "
                    f"elevation_idle_timeout_seconds ({config.elevation_idle_timeout_seconds})"
                )
            config.elevation_max_age_seconds = int_val
        else:
            raise ValueError(f"Unknown totp_elevation setting: {key}")

    def _validate_totp_elevation_tuple(
        self, enabled: bool, idle: int, max_age: int
    ) -> None:
        """Validate the final (enabled, idle, max_age) tuple for atomic saves.

        Called by update_totp_elevation_atomic so the whole batch is checked
        against the *new* values rather than field-by-field against on-disk state.
        Raises ValueError on any out-of-range value or cross-field violation.
        """
        if not isinstance(enabled, bool):
            raise ValueError(
                f"elevation_enforcement_enabled must be bool, got {enabled!r}"
            )
        if not (self._TOTP_IDLE_MIN <= idle <= self._TOTP_IDLE_MAX):
            raise ValueError(
                f"elevation_idle_timeout_seconds must be between "
                f"{self._TOTP_IDLE_MIN} and {self._TOTP_IDLE_MAX}, got {idle}"
            )
        if not (self._TOTP_MAX_AGE_MIN <= max_age <= self._TOTP_MAX_AGE_MAX):
            raise ValueError(
                f"elevation_max_age_seconds must be between "
                f"{self._TOTP_MAX_AGE_MIN} and {self._TOTP_MAX_AGE_MAX}, got {max_age}"
            )
        if max_age < idle:
            raise ValueError(
                f"elevation_max_age_seconds ({max_age}) must be >= "
                f"elevation_idle_timeout_seconds ({idle})"
            )

    def _apply_totp_elevation_to_config(
        self,
        config: "ServerConfig",
        enabled: bool,
        idle: int,
        max_age: int,
    ) -> tuple:
        """Stage new totp_elevation values on config; return original snapshot."""
        original = (
            config.elevation_enforcement_enabled,
            config.elevation_idle_timeout_seconds,
            config.elevation_max_age_seconds,
        )
        config.elevation_enforcement_enabled = enabled
        config.elevation_idle_timeout_seconds = idle
        config.elevation_max_age_seconds = max_age
        return original

    def _rollback_totp_elevation(
        self,
        config: "ServerConfig",
        original: tuple,
        exc: Exception,
    ) -> None:
        """Restore original totp_elevation values and log the failure."""
        config.elevation_enforcement_enabled = original[0]
        config.elevation_idle_timeout_seconds = original[1]
        config.elevation_max_age_seconds = original[2]
        logger.error(
            "totp_elevation atomic save failed; rolled back to "
            "enabled=%s idle=%ds max_age=%ds. Error: %s",
            original[0],
            original[1],
            original[2],
            exc,
            extra={"correlation_id": get_correlation_id()},
        )

    def update_totp_elevation_atomic(
        self,
        enabled: bool,
        idle_timeout_seconds: int,
        max_age_seconds: int,
        session_manager: Optional["ElevationManagerProtocol"] = None,
    ) -> None:
        """Atomically validate, save, and hot-reload totp_elevation (Bug #943).

        Fix #1: validates the final tuple so idle > old_max_age is not rejected.
        Fix #3: rolls back all 3 in-memory fields on save failure.
        Fix #2: calls session_manager.update_timeouts() only when provided.
        Logs INFO on success so operators can confirm hot-reload occurred.
        """
        self._validate_totp_elevation_tuple(
            enabled, idle_timeout_seconds, max_age_seconds
        )
        config = self.get_config()
        original = self._apply_totp_elevation_to_config(
            config, enabled, idle_timeout_seconds, max_age_seconds
        )
        try:
            self.save_config(config)
        except Exception as exc:
            self._rollback_totp_elevation(config, original, exc)
            raise  # MESSI Rule 13 — propagate, never swallow
        if session_manager is not None:
            session_manager.update_timeouts(idle_timeout_seconds, max_age_seconds)
        logger.info(
            "totp_elevation atomic save: enabled=%s idle=%ds max_age=%ds",
            enabled,
            idle_timeout_seconds,
            max_age_seconds,
            extra={"correlation_id": get_correlation_id()},
        )

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
            claude_config.dependency_map_pass2_max_turns = max(0, int(value))
        elif key == "dependency_map_delta_max_turns":
            claude_config.dependency_map_delta_max_turns = max(0, int(value))
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

    def _update_query_embedding_cache_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a query embedding cache configuration setting (Story #1107 S3)."""
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        if config.query_embedding_cache_config is None:
            config.query_embedding_cache_config = QueryEmbeddingCacheConfig()
        qec = config.query_embedding_cache_config

        if key == "query_embedding_cache_enabled":
            qec.query_embedding_cache_enabled = _parse_bool(value)
        elif key == "query_embedding_cache_max_entries":
            qec.query_embedding_cache_max_entries = int(value)
        elif key == "query_embedding_cache_voyage_mode":
            _valid_modes = {"off", "shadow", "on"}
            str_val = str(value)
            if str_val not in _valid_modes:
                raise ValueError(
                    f"Invalid voyage mode '{value}': must be one of {sorted(_valid_modes)}"
                )
            qec.query_embedding_cache_voyage_mode = str_val
        elif key == "query_embedding_cache_voyage_anchor_tokens":
            # Empty string or None means "inherit global" (reset per-provider override).
            if value is None or value == "":
                qec.query_embedding_cache_voyage_anchor_tokens = None
            else:
                qec.query_embedding_cache_voyage_anchor_tokens = int(value)
        elif key == "query_embedding_cache_voyage_audit_sample_rate":
            qec.query_embedding_cache_voyage_audit_sample_rate = float(value)
        elif key == "query_embedding_cache_cohere_mode":
            _valid_modes = {"off", "shadow", "on"}
            str_val = str(value)
            if str_val not in _valid_modes:
                raise ValueError(
                    f"Invalid cohere mode '{value}': must be one of {sorted(_valid_modes)}"
                )
            qec.query_embedding_cache_cohere_mode = str_val
        elif key == "query_embedding_cache_cohere_anchor_tokens":
            # Empty string or None means "inherit global" (reset per-provider override).
            if value is None or value == "":
                qec.query_embedding_cache_cohere_anchor_tokens = None
            else:
                qec.query_embedding_cache_cohere_anchor_tokens = int(value)
        elif key == "query_embedding_cache_cohere_audit_sample_rate":
            qec.query_embedding_cache_cohere_audit_sample_rate = float(value)
        else:
            raise ValueError(f"Unknown query_embedding_cache setting: {key}")

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

    def _update_pace_maker_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update pace-maker runtime settings (Story #997).

        Validation rules:
        - pace_maker_mode: must be one of "disabled", "on", "off" (case-insensitive).
        """
        if key == "pace_maker_mode":
            valid = {"disabled", "on", "off"}
            str_val = str(value).lower()
            if str_val not in valid:
                raise ValueError(
                    f"Invalid pace_maker_mode: {value!r}. Accepted: disabled, on, off."
                )
            config.pace_maker_mode = str_val
        else:
            raise ValueError(f"Unknown pace_maker setting: {key}")

    def _update_search_event_log_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update search event log runtime settings (Issue #1159).

        Validation rules:
        - search_event_log_retention_days: integer in [1, 3650].
        """
        if key == "search_event_log_retention_days":
            days = int(value)
            if not (1 <= days <= 3650):
                raise ValueError(
                    f"search_event_log_retention_days must be between 1 and 3650, got {days}."
                )
            config.search_event_log_retention_days = days
        else:
            raise ValueError(f"Unknown search_event_log setting: {key}")

    def _update_export_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update export runtime settings (Issue #1160).

        Validation rules:
        - export_retention_days: integer in [1, 3650].
        """
        if key == "export_retention_days":
            days = int(value)
            if not (1 <= days <= 3650):
                raise ValueError(
                    f"export_retention_days must be between 1 and 3650, got {days}."
                )
            config.export_retention_days = days
        else:
            raise ValueError(f"Unknown export setting: {key}")

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
        elif key == "externally_managed":
            golden_repos.externally_managed = value in ["true", True, "True"]
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
        if key == "temporal_stale_threshold_days":
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
        elif key == "temporal_lane_concurrency":
            # Story #1400 CRITICAL 1: temporal-lane worker pool size
            # (restart-required -- see RESTART_REQUIRED_FIELDS).
            background_jobs.temporal_lane_concurrency = int(value)
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

    def _update_activated_reaper_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an activated_reaper setting (Story #967)."""
        reaper = config.activated_reaper_config
        assert reaper is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "ttl_days":
            reaper.ttl_days = int(value)
        elif key == "cadence_hours":
            reaper.cadence_hours = int(value)
        else:
            raise ValueError(f"Unknown activated_reaper setting: {key}")

    def _update_hnsw_orphan_sweep_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an hnsw_orphan_sweep setting (Story #1397).

        `enabled` is coerced via the shared `_parse_bool` helper -- the Web
        UI submits an explicit "true"/"false" string (boolean <select>, not
        a checkbox), so `_parse_bool("false")` must persist False rather
        than silently no-op (the "enabled-checkbox trap" the issue warns
        about). The remaining 4 fields are plain integers.
        """
        sweep = config.hnsw_orphan_repair_sweep_config
        assert sweep is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "enabled":
            sweep.enabled = _parse_bool(value)
        elif key == "batch_size":
            sweep.batch_size = int(value)
        elif key == "tick_interval_minutes":
            sweep.tick_interval_minutes = int(value)
        elif key == "operating_hours_start_utc":
            sweep.operating_hours_start_utc = int(value)
        elif key == "operating_hours_end_utc":
            sweep.operating_hours_end_utc = int(value)
        else:
            raise ValueError(f"Unknown hnsw_orphan_sweep setting: {key}")

    def _update_search_timeouts_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update a search_timeouts setting (Issue #1398).

        All 5 fields are plain integers (seconds). Range validation happens
        later in config_manager.validate_config(), called by update_setting()
        after this method returns (unless skip_validation=True for batch
        updates).
        """
        st = config.search_timeouts_config
        assert st is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "search_code_handler_timeout_seconds":
            st.search_code_handler_timeout_seconds = int(value)
        elif key == "default_handler_timeout_seconds":
            st.default_handler_timeout_seconds = int(value)
        elif key == "write_mode_handler_timeout_seconds":
            st.write_mode_handler_timeout_seconds = int(value)
        elif key == "embedding_provider_timeout_seconds":
            st.embedding_provider_timeout_seconds = int(value)
        elif key == "reranker_timeout_seconds":
            st.reranker_timeout_seconds = int(value)
        elif key == "temporal_inline_wait_seconds":
            # Story #1400 CRITICAL 5/6: the ONE float field among six --
            # sub-second precision (e.g. 0.001) is the documented E2E lever
            # for deterministically forcing the async-hybrid handoff path.
            st.temporal_inline_wait_seconds = float(value)
        else:
            raise ValueError(f"Unknown search_timeouts setting: {key}")

    def _update_embedding_stats_setting(
        self, config: ServerConfig, key: str, value: Any
    ) -> None:
        """Update an embedding_stats setting (Story #1418 Phase 3).

        Range validation (flush_interval_seconds > 0, retention_days > 0)
        happens later in config_manager.validate_config(), called by
        update_setting() after this method returns (unless
        skip_validation=True for batch updates).
        """
        es = config.embedding_stats_config
        assert es is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "enabled":
            es.enabled = _parse_bool(value)
        elif key == "flush_interval_seconds":
            es.flush_interval_seconds = float(value)
        elif key == "retention_days":
            es.retention_days = int(value)
        else:
            raise ValueError(f"Unknown embedding_stats setting: {key}")

    def _update_xray_setting(self, config: ServerConfig, key: str, value: Any) -> None:
        """Update an X-Ray setting (Story #977)."""
        xray = config.xray_config
        assert xray is not None  # Guaranteed by ServerConfig.__post_init__
        if key == "xray_timeout_seconds":
            xray.xray_timeout_seconds = int(value)
        elif key == "xray_worker_threads":
            xray.xray_worker_threads = int(value)
            try:
                from code_indexer.server.mcp.handlers.xray import (
                    _get_xray_cell_limiter as _gcl,
                )

                _cl = _gcl()
                if _cl is not None:
                    _cl.set_limit(int(value))
            except Exception as exc:
                logger.warning(
                    "Failed to live-reload xray cell limiter for "
                    "xray_worker_threads=%s: %s",
                    value,
                    exc,
                )
        else:
            raise ValueError(f"Unknown xray setting: {key}")

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
                elif category == "totp_elevation":
                    self._update_totp_elevation_setting(config, key, value)
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
        # Story #1158: Bounds for parallel_requests fields (embedding + temporal).
        _MIN_PARALLEL = 1
        _MAX_PARALLEL = 32

        def _clamp_parallel(raw: Any, field_name: str) -> int:
            """Parse raw value and clamp to [_MIN_PARALLEL, _MAX_PARALLEL]."""
            try:
                return max(_MIN_PARALLEL, min(_MAX_PARALLEL, int(raw)))
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid value for {field_name}: must be a valid integer"
                )

        config = self.get_config()
        indexing = config.indexing_config
        if indexing is None:
            from ..utils.config_manager import IndexingConfig

            indexing = IndexingConfig()
            config.indexing_config = indexing

        # Story #1158 - AC1: Embedding API parallelism (required, clamped [1, 32])
        if key in ("voyage_ai_parallel_requests", "cohere_parallel_requests"):
            setattr(indexing, key, _clamp_parallel(value, key))
            self.save_config(config)
            logger.info(
                "Updated indexing.%s to %d",
                key,
                getattr(indexing, key),
                extra={"correlation_id": get_correlation_id()},
            )
            return

        # Story #1158 - AC2: Temporal git-diff parallelism (optional: None or clamped [1, 32])
        if key == "temporal_parallel_requests":
            if value is None or (isinstance(value, str) and value.strip() == ""):
                indexing.temporal_parallel_requests = None
            else:
                indexing.temporal_parallel_requests = _clamp_parallel(value, key)
            self.save_config(config)
            logger.info(
                "Updated indexing.temporal_parallel_requests to %s",
                indexing.temporal_parallel_requests,
                extra={"correlation_id": get_correlation_id()},
            )
            return

        # Story #1290: per-commit temporal embedder registry (Web UI Config
        # Screen exposure of TemporalConfig.embedders/active_embedder/
        # aggregation_chunk_chars -- seeded into repo config.json by
        # config_seeding.py, no environment variables).
        if key == "temporal_embedders":
            if isinstance(value, list):
                embedders = [str(v).strip() for v in value if str(v).strip()]
            else:
                embedders = [v.strip() for v in str(value).split(",") if v.strip()]
            if not embedders:
                raise ValueError("temporal_embedders must not be empty")
            indexing.temporal_embedders = embedders
            self.save_config(config)
            logger.info(
                "Updated indexing.temporal_embedders to %s",
                indexing.temporal_embedders,
                extra={"correlation_id": get_correlation_id()},
            )
            return

        if key == "temporal_active_embedder":
            active = str(value).strip()
            if not active:
                raise ValueError("temporal_active_embedder must not be empty")
            if active not in indexing.temporal_embedders:
                raise ValueError(
                    f"temporal_active_embedder {active!r} must be a member of "
                    f"temporal_embedders {indexing.temporal_embedders!r}"
                )
            indexing.temporal_active_embedder = active
            self.save_config(config)
            logger.info(
                "Updated indexing.temporal_active_embedder to %s",
                indexing.temporal_active_embedder,
                extra={"correlation_id": get_correlation_id()},
            )
            return

        if key == "temporal_aggregation_chunk_chars":
            try:
                chars = int(value)
            except (ValueError, TypeError):
                raise ValueError(
                    "temporal_aggregation_chunk_chars must be a valid integer"
                )
            if chars <= 0:
                raise ValueError(
                    "temporal_aggregation_chunk_chars must be a positive integer"
                )
            indexing.temporal_aggregation_chunk_chars = chars
            self.save_config(config)
            logger.info(
                "Updated indexing.temporal_aggregation_chunk_chars to %d",
                indexing.temporal_aggregation_chunk_chars,
                extra={"correlation_id": get_correlation_id()},
            )
            return

        # Story #1412: golden/server temporal all-branches gate (default OFF).
        if key == "temporal_all_branches_enabled":
            indexing.temporal_all_branches_enabled = _parse_bool(value)
            self.save_config(config)
            logger.info(
                "Updated indexing.temporal_all_branches_enabled to %s",
                indexing.temporal_all_branches_enabled,
                extra={"correlation_id": get_correlation_id()},
            )
            return

        if key != "indexable_extensions":
            raise ValueError(f"Unknown indexing setting: {key}")

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

    def sync_repo_extensions_if_drifted(
        self, repo_path: str
    ) -> Optional[ExtensionDrift]:
        """
        Sync repo config if extensions drifted from server config (Story #223 - AC7).

        Called before refresh indexing. No-op if already in sync or config missing.
        Returns ExtensionDrift describing which extensions were added/removed when drift
        is detected, or None when already in sync or when repo config is absent/unreadable.

        Args:
            repo_path: Filesystem path to the repository

        Returns:
            ExtensionDrift with added/removed sets if drift was detected, None otherwise.
        """
        config = self.get_config()
        if config.indexing_config is None:
            return None
        server_exts = config.indexing_config.indexable_extensions
        server_exts_bare: Set[str] = {ext.lstrip(".") for ext in server_exts}
        cli_exts_sorted = sorted(server_exts_bare)

        cidx_config_path = Path(repo_path) / ".code-indexer" / "config.json"
        if not cidx_config_path.exists():
            return None

        try:
            with open(cidx_config_path, "r") as f:
                repo_config = json.load(f)
            current_exts_bare: Set[str] = set(repo_config.get("file_extensions", []))
            current_exts_sorted = sorted(current_exts_bare)
            if current_exts_sorted == cli_exts_sorted:
                return None  # Already in sync, do not rewrite
            added = server_exts_bare - current_exts_bare
            removed = current_exts_bare - server_exts_bare
            repo_config["file_extensions"] = [ext.lstrip(".") for ext in server_exts]
            with open(cidx_config_path, "w") as f:
                json.dump(repo_config, f, indent=2)
            logger.info(
                "Synced drifted file_extensions for %s (%d added, %d removed)",
                repo_path,
                len(added),
                len(removed),
            )
            return ExtensionDrift(added=added, removed=removed)
        except Exception as e:
            logger.warning("Could not sync extensions for %s: %s", repo_path, e)
            return None

    def check_config_update(self) -> bool:
        """Check if config version changed in PG (called periodically).

        Returns True if config was reloaded.
        """
        if self._pool is None:
            return False
        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
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

    def _read_raw_launch_generation(self) -> int:
        """Read launch_restart_generation from raw runtime row (COALESCE 0).

        Story #1198 AC1: Story 4 adds launch_restart_generation to the runtime
        row.  Until then every row returns 0 via this COALESCE fallback.
        Logs DEBUG when the fallback is taken due to an exception.
        """
        import sqlite3 as _sqlite3

        try:
            if self._pool is not None:
                # Cluster mode: read from shared PG row (MUST be first — in
                # cluster mode _sqlite_db_path is also set by initialize_runtime_db,
                # so checking SQLite first would silently return the per-node
                # local value instead of the shared generation.  Every other DB
                # method in this class uses _pool-first order.)
                from psycopg.rows import dict_row

                with self._pool.connection() as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        row = cur.execute(
                            "SELECT config_json FROM server_config WHERE config_key = %s",
                            (CONFIG_KEY_RUNTIME,),
                        ).fetchone()
                if row:
                    raw = row["config_json"]
                    data: dict = json.loads(raw) if isinstance(raw, str) else raw
                    return int(data.get("launch_restart_generation", 0))
            elif self._sqlite_db_path and Path(self._sqlite_db_path).exists():
                with _sqlite3.connect(self._sqlite_db_path) as conn:
                    row = conn.execute(
                        "SELECT config_json FROM server_config WHERE config_key = ?",
                        (CONFIG_KEY_RUNTIME,),
                    ).fetchone()
                if row:
                    data = json.loads(row[0])
                    return int(data.get("launch_restart_generation", 0))
        except Exception:
            logger.debug(
                "ConfigService._read_raw_launch_generation: DB read failed; "
                "defaulting launch_restart_generation to 0",
                exc_info=True,
            )
        return 0

    def _capture_bootstrap_launch_keys_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return (and memoize) config.json's raw content as it was BEFORE any
        first-boot stripping (Bug #1335).

        _backfill_launch_keys_from_execstart is invoked up to twice in a
        single first-boot sequence: once from initialize_runtime_db while
        config.json still holds the operator's explicit launch keys, and
        again later from _seed_runtime_to_pg -- by which time
        _strip_config_file_to_bootstrap has ALREADY removed those keys from
        disk. Re-reading config.json on that second call would always see
        the keys as "absent" and wrongly gap-fill them from an unrelated
        systemd unit's ExecStart flags (the #1324 symptom).

        Memoizing the FIRST read on this ConfigService instance -- which
        always precedes any strip within one process's lifetime, since both
        callers read config.json (directly or via this snapshot) before
        _strip_config_file_to_bootstrap ever runs -- preserves the true
        bootstrap intent for every later call.

        Returns None if config.json could not be read (missing/corrupt) on
        the first attempt; callers must skip the gap-fill entirely in that
        case, matching the pre-#1335 error-handling behavior.
        """
        if self._bootstrap_launch_keys_snapshot is not None:
            return self._bootstrap_launch_keys_snapshot
        try:
            config_path = Path(self.config_manager.config_file_path)
            raw_config = (
                json.loads(config_path.read_text()) if config_path.exists() else {}
            )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "ConfigService: unable to read raw config.json for ExecStart "
                "backfill at first-boot seed; skipping gap-fill: %s",
                exc,
            )
            return None
        self._bootstrap_launch_keys_snapshot = raw_config
        return raw_config

    def _backfill_launch_keys_from_execstart(self, config: "ServerConfig") -> None:
        """Gap-fill host/port/workers from the live ExecStart at first-boot seed.

        Bug #1232 correct fix layer: called ONLY at first-boot centralization
        (initialize_runtime_db / _seed_runtime_to_pg), NOT in materialize_launch_config.

        Precedence — gap-fill only, never overrides explicit config.json values:
          1. key present in config.json  → leave config value unchanged (operator intent).
          2. key absent from config.json → fill from live ExecStart when available.
          3. neither config.json nor ExecStart → keep ServerConfig default + WARNING.

        Bug #1335: "present in config.json" is answered from the memoized
        bootstrap snapshot (_capture_bootstrap_launch_keys_snapshot), NOT a
        fresh disk read -- a prior _strip_config_file_to_bootstrap() call in
        this same process may have already removed these keys from disk,
        which must not be misread as "operator never set them."

        Mutates the passed config object AND self._config so that the subsequent
        _extract_runtime_dict (seeds the DB row) and _strip_config_file_to_bootstrap
        (which re-reads via get_config()) both see the corrected values.
        """
        raw_config = self._capture_bootstrap_launch_keys_snapshot()
        if raw_config is None:
            return

        execstart = read_execstart_flags()

        for key in ("host", "port", "workers"):
            if key in raw_config:
                # Explicit operator value in config.json — leave as-is.
                continue
            exec_val = execstart.get(key)
            if exec_val is not None:
                # Gap-fill from ExecStart (key absent from config.json).
                setattr(config, key, exec_val)
            else:
                # Neither config.json nor ExecStart — keep ServerConfig default.
                logger.warning(
                    "ConfigService: launch key '%s' absent from config.json and "
                    "live ExecStart; seeding runtime DB with ServerConfig default "
                    "%r (Bug #1232)",
                    key,
                    getattr(config, key),
                )

        # Publish mutated config so _extract_runtime_dict and
        # _strip_config_file_to_bootstrap (which calls get_config()) both see
        # the corrected values.
        self._config = config

    def materialize_launch_config(self) -> bool:
        """Write launch.json with current target launch parameters.

        Story #1198 AC1: Materializes {workers, log_level, host, port,
        target_restart_generation} to LAUNCH_CONFIG_PATH atomically via
        tempfile + os.replace.  Does NOT write applied_launch.json.

        Bug #1232: host/port/workers come directly from the desired state
        (self._config), which reflects the admin's intent. Gap-filling from the
        live ExecStart happens ONLY at first-boot seed via
        _backfill_launch_keys_from_execstart(), NOT here — so admin changes via
        the Web UI are always honored.

        Returns:
            True on success, False on any failure (fail-soft — never raises).
        """
        import os
        import tempfile

        try:
            config = self.get_config()
            target_generation = self._read_raw_launch_generation()
            payload = {
                "workers": config.workers,
                "log_level": config.log_level,
                "host": config.host,
                "port": config.port,
                "target_restart_generation": target_generation,
            }
            launch_path = LAUNCH_CONFIG_PATH
            launch_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=launch_path.parent, prefix=".launch_tmp_"
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, launch_path)
            return True
        except Exception:
            logger.warning(
                "ConfigService: failed to materialize launch.json",
                exc_info=True,
            )
            return False

    def _read_raw_launch_snapshot(self) -> Optional[dict]:
        """Read host/port/workers/log_level + launch_restart_generation from raw row.

        Story #1200 AC1/AC3: returns all five launch fields in one SELECT so
        both the per-poll generation check and any startup read share the same
        helper.  COALESCE 0 for absent generation.  Returns None when no DB is
        configured or the row does not exist.
        """
        import sqlite3 as _sqlite3

        try:
            if self._pool is not None:
                from psycopg.rows import dict_row

                with self._pool.connection() as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        row = cur.execute(
                            "SELECT config_json FROM server_config"
                            " WHERE config_key = %s",
                            (CONFIG_KEY_RUNTIME,),
                        ).fetchone()
                if row is None:
                    return None
                raw = row["config_json"]
                data: dict = json.loads(raw) if isinstance(raw, str) else raw
            elif self._sqlite_db_path and Path(self._sqlite_db_path).exists():
                with _sqlite3.connect(self._sqlite_db_path) as conn:
                    row = conn.execute(
                        "SELECT config_json FROM server_config WHERE config_key = ?",
                        (CONFIG_KEY_RUNTIME,),
                    ).fetchone()
                if row is None:
                    return None
                data = json.loads(row[0])
            else:
                return None
        except Exception:
            logger.debug(
                "ConfigService._read_raw_launch_snapshot: DB read failed",
                exc_info=True,
            )
            return None

        return {
            "workers": data.get("workers"),
            "log_level": data.get("log_level"),
            "host": data.get("host"),
            "port": data.get("port"),
            "launch_restart_generation": int(
                data.get("launch_restart_generation") or 0
            ),
        }

    def bump_launch_restart_generation(self) -> None:
        """Atomically increment launch_restart_generation in the DB row (+ version).

        Story #1200 AC1: single SQL statement — no asdict() round-trip.
        MAJOR-M3: does NOT advance self._db_config_version so the bumping node's
        next poll detects the new version and self-signals via
        check_pending_launch_restart().

        SQLite path exists for unit-test modeling only (real solo route never bumps;
        see AC7/FIX-5).
        """
        import sqlite3 as _sqlite3

        if self._pool is not None:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE server_config SET "
                    "config_json = jsonb_set("
                    "  config_json,"
                    "  '{launch_restart_generation}',"
                    "  (COALESCE((config_json->>'launch_restart_generation')::int, 0)"
                    "   + 1)::text::jsonb"
                    "),"
                    "version = version + 1 "
                    "WHERE config_key = %s",
                    (CONFIG_KEY_RUNTIME,),
                )
                conn.commit()
            # MAJOR-M3: do NOT read back and update self._db_config_version here.
            logger.info(
                "ConfigService: bumped launch_restart_generation in PG "
                "(cluster-wide restart requested)"
            )
            return

        if self._sqlite_db_path and Path(self._sqlite_db_path).exists():
            with _sqlite3.connect(self._sqlite_db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT config_json FROM server_config WHERE config_key = ?",
                    (CONFIG_KEY_RUNTIME,),
                ).fetchone()
                if row is not None:
                    data = json.loads(row[0])
                    data["launch_restart_generation"] = (
                        int(data.get("launch_restart_generation") or 0) + 1
                    )
                    conn.execute(
                        "UPDATE server_config "
                        "SET config_json = ?, version = version + 1 "
                        "WHERE config_key = ?",
                        (json.dumps(data), CONFIG_KEY_RUNTIME),
                    )
                conn.commit()
            # MAJOR-M3: do NOT update self._db_config_version.
            logger.info("ConfigService: bumped launch_restart_generation in SQLite")

    def check_pending_launch_restart(self) -> None:
        """Per-poll check: if target generation > applied, materialize then signal.

        Story #1200 AC3: called on EVERY poll interval independent of version
        changes (CRITICAL-C1).  NOT a callback — registered callbacks only fire
        on version-diff edge events.

        Cluster-only when a PG pool exists (mirrors existing poll gate).
        SQLite path included so unit tests can exercise the logic without PG.

        Logic:
          1. Read target generation from raw DB snapshot.
          2. Read applied_restart_generation from APPLIED_LAUNCH_CONFIG_PATH
             (written by auto-updater post-restart, Story #1199).
          3. If target > applied (PENDING):
             a. Materialize launch.json (Story #1198).
             b. Only if materialize succeeded, write RESTART_SIGNAL_PATH.
             c. Increment consecutive pending counter; emit ONE rate-limited
                WARNING after PENDING_RESTART_WARN_THRESHOLD polls (FIX-6).
          4. If applied >= target (converged): reset counter + rate-limit flag.

        Does NOT write applied_launch.json (that is the auto-updater's job).
        """
        from datetime import datetime as _datetime

        snapshot = self._read_raw_launch_snapshot()
        if snapshot is None:
            return

        target = snapshot["launch_restart_generation"]

        # Read applied generation from applied_launch.json (COALESCE 0 on absent)
        applied = 0
        try:
            if APPLIED_LAUNCH_CONFIG_PATH.exists():
                raw = json.loads(APPLIED_LAUNCH_CONFIG_PATH.read_text())
                applied = int(raw.get("applied_restart_generation") or 0)
        except Exception:
            logger.debug(
                "ConfigService: could not read applied_launch.json; "
                "treating applied generation as 0",
                exc_info=True,
            )

        if target <= applied:
            # Converged — reset FIX-6 tracking
            self._pending_restart_poll_count = 0
            self._pending_restart_warned = False
            return

        # PENDING: target > applied
        self._pending_restart_poll_count += 1

        # FIX-6: emit one rate-limited WARNING after threshold consecutive polls
        if (
            self._pending_restart_poll_count > PENDING_RESTART_WARN_THRESHOLD
            and not self._pending_restart_warned
        ):
            logger.warning(
                "ConfigService: launch_restart_generation target=%d > applied=%d "
                "for %d consecutive polls — node has not yet been restarted by "
                "the auto-updater; check cidx-auto-update service status.",
                target,
                applied,
                self._pending_restart_poll_count,
            )
            self._pending_restart_warned = True

        # AC3: materialize FIRST; only signal if materialize succeeded
        ok = self.materialize_launch_config()
        if not ok:
            logger.warning(
                "ConfigService: materialize_launch_config() failed; "
                "skipping RESTART_SIGNAL_PATH write (will retry next poll)"
            )
            return

        try:
            signal_data = {
                "timestamp": _datetime.now().isoformat(),
                "reason": "launch_restart_generation",
                "target_restart_generation": target,
                "applied_restart_generation": applied,
            }
            RESTART_SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESTART_SIGNAL_PATH.write_text(json.dumps(signal_data))
            logger.info(
                "ConfigService: wrote RESTART_SIGNAL_PATH "
                "(target_generation=%d, applied_generation=%d)",
                target,
                applied,
            )
        except Exception:
            logger.warning(
                "ConfigService: failed to write RESTART_SIGNAL_PATH",
                exc_info=True,
            )

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
        """Save runtime config to PG (AC2: preserves launch_restart_generation).

        AC2 race-safe: launch_restart_generation is preserved via a single atomic
        UPDATE using jsonb_set(), which reads the CURRENT row's generation value
        inside the same statement.  This eliminates the lost-update race that a
        separate SELECT -> Python patch -> UPDATE sequence would have under
        PostgreSQL READ COMMITTED isolation.

        Only launch_restart_generation is re-injected from the current row — all
        other keys come from the new runtime_dict derived from the dataclass, so
        intentionally dropped keys are NOT resurrected.
        """
        assert self._pool is not None
        from psycopg.rows import dict_row

        runtime_dict = self._extract_runtime_dict(config)
        # runtime_dict intentionally excludes launch_restart_generation (not a
        # dataclass field), so jsonb_set below re-injects it from the current row.
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "UPDATE server_config"
                    " SET config_json = jsonb_set("
                    "         %s::jsonb,"
                    "         '{launch_restart_generation}',"
                    "         to_jsonb(COALESCE("
                    "             (config_json->>'launch_restart_generation')::int,"
                    "             0"
                    "         ))"
                    "     ),"
                    "     version = version + 1,"
                    "     updated_at = CURRENT_TIMESTAMP,"
                    "     updated_by = %s"
                    " WHERE config_key = %s",
                    (json.dumps(runtime_dict), UPDATER_WEB_UI, CONFIG_KEY_RUNTIME),
                )
                conn.commit()
                version_row = cur.execute(
                    "SELECT version FROM server_config WHERE config_key = %s",
                    (CONFIG_KEY_RUNTIME,),
                ).fetchone()
            if version_row:
                self._db_config_version = version_row["version"]
        self.materialize_launch_config()  # AC3: re-materialize after PG save

    def _seed_runtime_to_pg(self) -> None:
        """Seed PG server_config table from current config (first boot)."""
        assert self._pool is not None
        config = self.get_config()
        self._backfill_launch_keys_from_execstart(config)  # Bug #1232: gap-fill
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
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
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

    def _load_runtime_from_pg(self, base_config: Optional[ServerConfig] = None) -> None:
        """Load runtime config from PostgreSQL and merge with bootstrap.

        Args:
            base_config: If provided, used as the merge base so self._config
                is not published until after the merge completes (Bug #998).
        """
        assert self._pool is not None
        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    "SELECT config_json, version FROM server_config WHERE config_key = %s",
                    (CONFIG_KEY_RUNTIME,),
                ).fetchone()

        if row is None:
            self._seed_runtime_to_pg()
            if base_config is not None:
                self._config = base_config
            return

        config_json = row["config_json"]
        runtime_dict = (
            json.loads(config_json) if isinstance(config_json, str) else config_json
        )
        self._db_config_version = int(row["version"])
        self._merge_runtime_config(runtime_dict, base_config=base_config)

    def _strip_config_file_to_bootstrap(self) -> None:
        """Strip config.json to bootstrap-only keys, backing up original.

        Story #1196 (next-release cleanup): the Story #1197 AC3 transition
        allow-list (TRANSITION_PRESERVE_KEYS) has been removed, so the strip
        set is once again simply (all keys) − BOOTSTRAP_KEYS.  The four launch
        keys (host/port/workers/log_level) no longer survive this strip -- the
        runtime DB / launch.json / applied_launch.json are the sole source now
        that all cluster nodes are confirmed on the new release.
        """
        import shutil

        config = self.get_config()
        full_dict = asdict(config)

        # Check if already stripped (nothing left to strip beyond bootstrap keys)
        non_kept = [k for k in full_dict if k not in BOOTSTRAP_KEYS]
        if not non_kept:
            return

        # Create backup (only once)
        backup_dir = Path(self.config_manager.server_dir) / "config-migration-backup"
        backup_file = backup_dir / "config.json.pre-centralization"
        if not backup_file.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.config_manager.config_file_path, str(backup_file))
            logger.info("ConfigService: backed up config.json to %s", backup_file)

        # Write bootstrap keys only.
        kept_dict = {k: v for k, v in full_dict.items() if k in BOOTSTRAP_KEYS}
        self.config_manager.save_config_dict(kept_dict)
        logger.info(
            "ConfigService: stripped config.json to %d bootstrap keys",
            len(kept_dict),
        )

    def _save_runtime_to_sqlite(self, runtime_dict: dict) -> None:
        """Save runtime config to SQLite (AC2: preserves launch_restart_generation).

        SQLite uses BEGIN IMMEDIATE which serialises the transaction, so the
        SELECT-then-UPDATE here is safe against concurrent writes on the same
        node (SQLite is single-writer).  The PG path uses an atomic jsonb_set
        UPDATE instead; see _save_runtime_to_pg.
        """
        import sqlite3

        assert self._sqlite_db_path is not None
        conn = sqlite3.connect(self._sqlite_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_row = conn.execute(
                "SELECT config_json FROM server_config WHERE config_key = ?",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
            current_config = json.loads(existing_row[0]) if existing_row else {}
            generation = int(current_config.get("launch_restart_generation") or 0)
            preserved_dict = dict(runtime_dict)
            preserved_dict["launch_restart_generation"] = generation
            conn.execute(
                "INSERT INTO server_config"
                "    (config_key, config_json, version, updated_by)"
                "    VALUES (?, ?, 1, ?)"
                "    ON CONFLICT(config_key) DO UPDATE SET"
                "        config_json = excluded.config_json,"
                "        version = server_config.version + 1,"
                "        updated_at = datetime('now'),"
                "        updated_by = excluded.updated_by",
                (CONFIG_KEY_RUNTIME, json.dumps(preserved_dict), UPDATER_WEB_UI),
            )
            conn.commit()
            row = conn.execute(
                "SELECT version FROM server_config WHERE config_key=?",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
            if row:
                self._db_config_version = row[0]
        finally:
            conn.close()
        self.materialize_launch_config()

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

    def _merge_runtime_config(
        self, runtime_dict: dict, base_config: Optional[ServerConfig] = None
    ) -> None:
        """Merge runtime dict into current config (runtime fields only).

        Reconstructs a full ServerConfig from a merged dict to correctly
        deserialize nested dataclass fields (Finding 1 fix).  Uses atomic
        reference swap for thread safety (Finding 3 fix).

        Args:
            runtime_dict: Runtime key/value pairs to merge on top of base.
            base_config: If provided, used as the merge base instead of
                calling get_config().  Pass this during load_config() so that
                self._config is not published until after the merge completes
                (Bug #998 atomicity fix).
        """
        config = base_config if base_config is not None else self.get_config()
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

        Story #1196 (next-release cleanup): the Story #1197 AC6 transition
        write-path inclusion (_extract_bootstrap_dict_with_transition) has been
        removed -- config.json writes use plain _extract_bootstrap_dict() again,
        so the four launch keys (host/port/workers/log_level) are no longer
        written to config.json on a settings-save.
        """
        runtime_dict = self._extract_runtime_dict(config)

        # Story #1198 CRITICAL-2 fix: update the cache BEFORE the save calls so
        # that materialize_launch_config() (called inside _save_runtime_to_pg /
        # _save_runtime_to_sqlite) reads the NEW config via get_config(), not the
        # stale cached value from before this save.
        self._config = config

        if self._pool is not None:
            self._save_runtime_to_pg(config)
            file_dict = self._extract_bootstrap_dict(config)
            self.config_manager.save_config_dict(file_dict)
        elif self._sqlite_db_path is not None:
            self._save_runtime_to_sqlite(runtime_dict)
            file_dict = self._extract_bootstrap_dict(config)
            self.config_manager.save_config_dict(file_dict)
        else:
            self.config_manager.save_config(config)


# Global service instance
_config_service: Optional[ConfigService] = None


def get_config_service() -> ConfigService:
    """Get or create the global ConfigService instance."""
    global _config_service
    if _config_service is None:
        _config_service = ConfigService()
    return _config_service


def set_config_service(svc: ConfigService) -> None:
    """
    Inject an existing ConfigService as the global singleton.

    Intended for integration tests that need the real singleton wired to a
    specific server directory without process-level mocking.  Always pair
    with a ``reset_config_service()`` call in teardown.
    """
    global _config_service
    _config_service = svc


def reset_config_service() -> None:
    """
    Reset the global ConfigService singleton.

    This is primarily used for testing to ensure each test gets a fresh
    config service instance with its own server directory.
    """
    global _config_service
    _config_service = None
