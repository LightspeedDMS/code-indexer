"""Lifespan context manager for CIDX server startup and shutdown."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
import logging
import os
from pathlib import Path
from typing import Any, Callable

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
from code_indexer.server.startup.bootstrap import _detect_repo_root

logger = logging.getLogger(__name__)


def make_lifespan(
    background_job_manager: Any,
    job_tracker: Any,
    golden_repo_manager: Any,
    mcp_registration_service: Any,
    user_manager: Any,
    jwt_manager: Any,
    dependencies: Any,
    register_langfuse_golden_repos: Callable,
    storage_mode: str = "sqlite",
    backend_registry: Any = None,
):
    """
    Factory that returns a lifespan context manager bound to the given service instances.

    Parameters are the closure-captured variables previously defined in create_app().
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Lifespan context manager for server startup and shutdown.

        Handles:
        - Startup: Auto-populate meta-directory with repository descriptions
        - Startup: Start global repos background services (QueryTracker, CleanupManager, RefreshScheduler)
        - Shutdown: Stop background services gracefully
        - Shutdown: Clean up resources
        """
        # Get server data directory (used by multiple components)
        server_data_dir = os.environ.get(
            "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
        )
        golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"

        # Story #505/#506: Store storage_mode in app.state early so web routes
        # and MCP handlers can access it without re-reading config.
        app.state.storage_mode = storage_mode

        # Startup: Initialize SQLite log handler FIRST (to capture all startup logs)
        logger.info(
            "Server startup: Initializing SQLite log handler",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            # Load config to get configured log level (Story #38: respect log_level setting)
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config_manager = ServerConfigManager(server_dir_path=server_data_dir)
            startup_config = config_manager.load_config()

            # Convert string log level to logging constant
            log_level_map = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
                "CRITICAL": logging.CRITICAL,
            }
            configured_level = log_level_map.get(
                startup_config.log_level.upper(), logging.INFO
            )

            log_db_path = Path(server_data_dir) / "logs.db"
            sqlite_handler = SQLiteLogHandler(log_db_path)
            sqlite_handler.setLevel(configured_level)
            logging.getLogger().addHandler(sqlite_handler)

            # Set app state for web routes to access
            app.state.log_db_path = log_db_path
            # Store handler reference so LogsBackend can be injected later (Story #500 AC4)
            app.state.sqlite_log_handler = sqlite_handler

            logger.info(
                f"SQLite log handler initialized: {log_db_path} (level: {startup_config.log_level})",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-008",
                    f"Failed to initialize SQLite log handler: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize SQLite database schema and run migrations (Story #702)
        logger.info(
            "Server startup: Initializing SQLite database schema",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.storage.database_manager import DatabaseSchema
            from code_indexer.server.storage.migration_service import MigrationService

            db_path = Path(server_data_dir) / "data" / "cidx_server.db"
            schema = DatabaseSchema(str(db_path))
            schema.initialize_database()

            logger.info(
                f"SQLite database schema initialized: {db_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Run migration of legacy JSON files to SQLite
            # Note: JSON files are in server_data_dir (e.g., ~/.cidx-server/users.json)
            # not in the data subdirectory
            migration = MigrationService(str(server_data_dir), str(db_path))
            if migration.is_migration_needed():
                logger.info(
                    "Legacy JSON files found, running migration to SQLite",
                    extra={"correlation_id": get_correlation_id()},
                )
                migration_results = migration.migrate_all()
                logger.info(
                    f"Migration complete: {migration_results}",
                    extra={"correlation_id": get_correlation_id()},
                )
                # Migrate golden repos metadata.json separately (Story #711)
                # golden_repos_dir is defined earlier in startup
                golden_repos_dir_path = Path(server_data_dir) / "data" / "golden-repos"
                if golden_repos_dir_path.exists():
                    gr_result = migration.migrate_golden_repos_metadata(
                        str(golden_repos_dir_path)
                    )
                    if not gr_result.get("skipped"):
                        logger.info(
                            f"Golden repos metadata migration: {gr_result}",
                            extra={"correlation_id": get_correlation_id()},
                        )

                    # Also migrate global_registry.json from golden-repos subdirectory
                    # This file contains globally activated repos and may be in
                    # golden-repos/ instead of the main data directory
                    global_registry_path = (
                        golden_repos_dir_path / "global_registry.json"
                    )
                    if global_registry_path.exists():
                        gr_global_result = migration.migrate_global_repos_from_path(
                            str(global_registry_path)
                        )
                        if not gr_global_result.get("skipped"):
                            logger.info(
                                f"Global repos migration from golden-repos: {gr_global_result}",
                                extra={"correlation_id": get_correlation_id()},
                            )
            else:
                logger.info(
                    "No legacy JSON files to migrate",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-009",
                    f"Failed to initialize SQLite database or run migrations: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize ApiMetricsService with database for multi-worker support
        logger.info(
            "Server startup: Initializing ApiMetricsService",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.api_metrics_service import (
                api_metrics_service,
            )

            metrics_db_path = Path(server_data_dir) / "data" / "api_metrics.db"
            # Story #531: Pass backend so metrics flow through PG in cluster mode
            _api_backend = (
                backend_registry.api_metrics if backend_registry is not None else None
            )
            api_metrics_service.initialize(
                str(metrics_db_path),
                storage_backend=_api_backend,
            )

            logger.info(
                f"ApiMetricsService initialized: {metrics_db_path}",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-010",
                    f"Failed to initialize ApiMetricsService: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: cidx-meta migration and bootstrap moved to after main GoldenRepoManager initialization
        # (See lines after GoldenRepoManager creation below)

        # Startup: Run SSH key migration (first-time auto-discovery)
        logger.info(
            "Server startup: Checking SSH key migration",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.ssh_startup_migration import (
                run_ssh_migration_on_startup,
            )

            migration_result = run_ssh_migration_on_startup(
                server_data_dir=server_data_dir,
                skip_key_testing=True,  # Skip actual SSH testing during startup
            )

            # Store migration result in app state for Web UI access
            app.state.ssh_migration_result = migration_result

            if migration_result.skipped:
                logger.info(
                    "SSH key migration: Skipped (already completed)",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    f"SSH key migration: Completed - "
                    f"{migration_result.keys_discovered} keys discovered, "
                    f"{migration_result.keys_imported} imported",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-012",
                    f"Failed to run SSH key migration on startup: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            app.state.ssh_migration_result = None

        # Startup: Initialize GroupAccessManager for group-based access control
        logger.info(
            "Server startup: Initializing GroupAccessManager",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.group_access_manager import (
                GroupAccessManager,
            )
            from code_indexer.server.routers.groups import set_group_manager
            from code_indexer.server.auth.audit_logger import password_audit_logger

            groups_db_path = Path(server_data_dir) / "groups.db"
            group_manager = GroupAccessManager(
                groups_db_path,
                storage_backend=backend_registry.groups
                if backend_registry is not None
                else None,
            )
            set_group_manager(group_manager)
            app.state.group_manager = group_manager

            logger.info(
                f"GroupAccessManager initialized: {groups_db_path}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Story #399: Initialize AuditLogService and inject into GroupAccessManager
            # Fail fast on init — AuditLogService is required for correct operation
            from code_indexer.server.services.audit_log_service import (
                AuditLogService,
                migrate_flat_file_to_sqlite,
            )

            audit_service = AuditLogService(
                groups_db_path,
                storage_backend=backend_registry.audit_log
                if backend_registry is not None
                else None,
            )
            app.state.audit_service = audit_service
            group_manager.set_audit_service(audit_service)
            # Inject into the module-level singleton so all log/query
            # calls from password_audit_logger also route to SQLite
            password_audit_logger.set_audit_service(audit_service)

            logger.info(
                "Story #399: AuditLogService initialized and injected into GroupAccessManager",
                extra={"correlation_id": get_correlation_id()},
            )

            # AC4: Migration is recoverable — historical data loss, not functional failure
            try:
                flat_file = Path(server_data_dir) / "password_audit.log"
                migrated, skipped = migrate_flat_file_to_sqlite(
                    flat_file, audit_service
                )
                if migrated > 0 or skipped > 0:
                    logger.info(
                        f"Story #399: Migrated {migrated} entries from password_audit.log, "
                        f"skipped {skipped} unparseable lines",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except Exception as e:
                logger.warning("Flat file migration failed (non-fatal): %s", e)

            # Inject GroupAccessManager into GoldenRepoManager for auto-assignment (Story #706)
            if (
                hasattr(app.state, "golden_repo_manager")
                and app.state.golden_repo_manager
            ):
                app.state.golden_repo_manager.group_access_manager = group_manager
                logger.info(
                    "GroupAccessManager injected into GoldenRepoManager for repo access auto-assignment",
                    extra={"correlation_id": get_correlation_id()},
                )

                # Seed existing golden repos to admins/powerusers groups (migration for upgrades)
                # This is idempotent - auto_assign_golden_repo uses INSERT OR IGNORE
                try:
                    from code_indexer.server.services.group_access_manager import (
                        seed_existing_golden_repos,
                        seed_admin_users,
                    )

                    seeded_count = seed_existing_golden_repos(
                        app.state.golden_repo_manager, group_manager
                    )
                    if seeded_count > 0:
                        logger.info(
                            f"Seeded {seeded_count} existing golden repos to admins/powerusers groups",
                            extra={"correlation_id": get_correlation_id()},
                        )

                    # Seed admin users to admins group (migration for upgrades)
                    # This is idempotent - only assigns users not already in a group
                    admin_seeded = seed_admin_users(
                        dependencies.user_manager, group_manager
                    )
                    if admin_seeded > 0:
                        logger.info(
                            f"Seeded {admin_seeded} admin users to admins group",
                            extra={"correlation_id": get_correlation_id()},
                        )
                except Exception as seed_error:
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-013",
                            f"Failed to seed existing golden repos or admin users: {seed_error}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            # Initialize AccessFilteringService for query-time access filtering (Story #707)
            from code_indexer.server.services.access_filtering_service import (
                AccessFilteringService,
            )

            access_filtering_service = AccessFilteringService(group_manager)
            app.state.access_filtering_service = access_filtering_service
            logger.info(
                "AccessFilteringService initialized for query-time access filtering",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-014",
                    f"Failed to initialize GroupAccessManager: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize and start global repos background services
        logger.info(
            "Server startup: Starting global repos background services",
            extra={"correlation_id": get_correlation_id()},
        )
        global_lifecycle_manager = None
        try:
            from code_indexer.server.lifecycle.global_repos_lifecycle import (
                GlobalReposLifecycleManager,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get server config for resource_config (timeouts, etc.)
            config_service = get_config_service()
            server_config = config_service.get_config()

            global_lifecycle_manager = GlobalReposLifecycleManager(
                str(golden_repos_dir),
                background_job_manager=background_job_manager,
                resource_config=server_config.resource_config,
                job_tracker=job_tracker,
            )
            global_lifecycle_manager.start()

            # Store lifecycle manager in app state for access by query handlers
            app.state.global_lifecycle_manager = global_lifecycle_manager
            app.state.query_tracker = global_lifecycle_manager.query_tracker
            app.state.golden_repos_dir = str(golden_repos_dir)

            # Wire refresh_scheduler into golden_repo_manager so that
            # add_indexes_to_golden_repo() and change_branch() can acquire
            # write locks and perform CoW snapshots (Bug B fix).
            if golden_repo_manager is not None:
                golden_repo_manager._refresh_scheduler = (
                    global_lifecycle_manager.refresh_scheduler
                )

            logger.info(
                "Global repos background services started successfully",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-015",
                    f"Failed to start global repos background services: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize PayloadCache for semantic search result truncation (Story #679)
        payload_cache = None
        logger.info(
            "Server startup: Initializing PayloadCache for semantic search",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.cache.payload_cache import (
                PayloadCache,
                PayloadCacheConfig,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get server config for payload cache settings (Story #679)
            # Use config service to get CacheConfig, with env var overrides applied
            config_service = get_config_service()
            server_config = config_service.get_config()
            payload_cache_config = PayloadCacheConfig.from_server_config(
                server_config.cache_config
            )
            cache_db_path = Path(golden_repos_dir) / ".cache" / "payload_cache.db"
            payload_cache = PayloadCache(
                db_path=cache_db_path,
                config=payload_cache_config,
                storage_backend=backend_registry.payload_cache
                if backend_registry is not None
                else None,
            )
            payload_cache.initialize()
            payload_cache.start_background_cleanup()
            app.state.payload_cache = payload_cache

            logger.info(
                f"PayloadCache initialized: {cache_db_path} "
                f"(preview_size={payload_cache_config.preview_size_chars}, "
                f"ttl={payload_cache_config.cache_ttl_seconds}s)",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-016",
                    f"Failed to initialize PayloadCache: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            # Set payload_cache to None so handlers know it's unavailable
            app.state.payload_cache = None

        # Startup: Auto-seed API keys if server config is blank (Story #20)
        logger.info(
            "Server startup: Checking API key auto-seeding",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.startup.api_key_seeding import (
                seed_api_keys_on_startup,
            )

            seeding_result = seed_api_keys_on_startup(config_service)

            if seeding_result["anthropic_seeded"] or seeding_result["voyageai_seeded"]:
                logger.info(
                    f"API key auto-seeding completed: "
                    f"anthropic={seeding_result['anthropic_seeded']}, "
                    f"voyageai={seeding_result['voyageai_seeded']}",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "API key auto-seeding: No keys needed seeding",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-017",
                    f"Failed to auto-seed API keys on startup: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # --- LLM Lease Lifecycle (subscription credential mode) ---
        llm_lifecycle_service = None
        claude_config = server_config.claude_integration_config
        if claude_config and claude_config.claude_auth_mode == "subscription":
            if (
                claude_config.llm_creds_provider_url
                and claude_config.llm_creds_provider_api_key
            ):
                from code_indexer.server.services.llm_creds_client import LlmCredsClient
                from code_indexer.server.config.llm_lease_state import (
                    LlmLeaseStateManager,
                )
                from code_indexer.server.services.claude_credentials_file_manager import (
                    ClaudeCredentialsFileManager,
                )
                from code_indexer.server.services.llm_lease_lifecycle import (
                    LlmLeaseLifecycleService,
                )

                llm_client = LlmCredsClient(
                    provider_url=claude_config.llm_creds_provider_url,
                    api_key=claude_config.llm_creds_provider_api_key,
                )
                llm_lifecycle_service = LlmLeaseLifecycleService(
                    client=llm_client,
                    state_manager=LlmLeaseStateManager(),
                    credentials_manager=ClaudeCredentialsFileManager(),
                )
                llm_lifecycle_service.start(
                    consumer_id=claude_config.llm_creds_provider_consumer_id
                )
                logger.info(
                    "LLM lease lifecycle started: %s",
                    llm_lifecycle_service.get_status().status.value,
                )
                # Store in app.state so /api/llm-creds/lease-status can read it
                app.state.llm_lifecycle_service = llm_lifecycle_service
            else:
                logger.warning(
                    "Subscription mode enabled but provider URL/API key not configured"
                )

        # Startup: Initialize ClaudeCliManager singleton (Story #23)
        logger.info(
            "Server startup: Initializing ClaudeCliManager",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.startup.claude_cli_startup import (
                initialize_claude_manager_on_startup,
            )

            # Get fresh config (may have been updated by API key seeding)
            server_config = config_service.get_config()

            claude_init_result = initialize_claude_manager_on_startup(
                golden_repos_dir=str(golden_repos_dir),
                server_config=server_config,
                mcp_registration_service=mcp_registration_service,
            )

            if claude_init_result:
                logger.info(
                    "ClaudeCliManager initialization completed",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-018",
                        "ClaudeCliManager initialization failed (smart descriptions may be unavailable)",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-019",
                    f"Failed to initialize ClaudeCliManager on startup: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize Scheduled Catch-Up Service (Story #23, AC6)
        scheduled_catchup_service = None
        logger.info(
            "Server startup: Checking scheduled catch-up service",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.scheduled_catchup_service import (
                ScheduledCatchupService,
            )

            # Get config for scheduled catch-up settings
            server_config = config_service.get_config()
            claude_config = server_config.claude_integration_config

            scheduled_catchup_service = ScheduledCatchupService(
                enabled=claude_config.scheduled_catchup_enabled,
                interval_minutes=claude_config.scheduled_catchup_interval_minutes,
                job_tracker=job_tracker,
            )
            scheduled_catchup_service.start()
            app.state.scheduled_catchup_service = scheduled_catchup_service

            if claude_config.scheduled_catchup_enabled:
                logger.info(
                    f"Scheduled catch-up service started "
                    f"(interval: {claude_config.scheduled_catchup_interval_minutes} minutes)",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "Scheduled catch-up service is disabled (can be enabled in Web UI)",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-020",
                    f"Failed to initialize scheduled catch-up service: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize Description Refresh Scheduler (Story #190)
        description_refresh_scheduler = None
        logger.info(
            "Server startup: Initializing description refresh scheduler",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.description_refresh_scheduler import (
                DescriptionRefreshScheduler,
            )
            from code_indexer.server.services.claude_cli_manager import (
                get_claude_cli_manager,
            )
            from code_indexer.global_repos import meta_description_hook

            # Get config
            server_config = config_service.get_config()
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")  # type: ignore[assignment]

            # Create scheduler instance
            meta_dir = Path(golden_repos_dir) / "cidx-meta"
            description_refresh_scheduler = DescriptionRefreshScheduler(
                db_path=db_path,
                config_manager=config_service,
                claude_cli_manager=get_claude_cli_manager(),
                meta_dir=meta_dir,
                analysis_model=server_config.golden_repos_config.analysis_model
                if server_config.golden_repos_config
                else "opus",
                job_tracker=job_tracker,
            )

            # Inject into meta_description_hook for tracking on repo add/remove
            if backend_registry is not None:
                tracking_backend = backend_registry.description_refresh_tracking
            else:
                from code_indexer.server.storage.sqlite_backends import (
                    DescriptionRefreshTrackingBackend,
                )

                tracking_backend = DescriptionRefreshTrackingBackend(db_path)
            meta_description_hook.set_tracking_backend(tracking_backend)
            meta_description_hook.set_scheduler(description_refresh_scheduler)

            # Inject RefreshScheduler for cidx-meta CoW reindex on repo add/remove (Story #270)
            from code_indexer.global_repos.meta_description_hook import (
                set_refresh_scheduler,
                CidxMetaRefreshDebouncer,
                set_debouncer,
                _DEFAULT_DEBOUNCE_SECONDS,
            )

            refresh_scheduler = (
                global_lifecycle_manager.refresh_scheduler
                if global_lifecycle_manager is not None
                else None
            )
            set_refresh_scheduler(refresh_scheduler)

            # Wire debouncer for coalescing batch-registration refresh triggers (Story #345)
            if refresh_scheduler is not None:
                cidx_meta_debouncer = CidxMetaRefreshDebouncer(
                    refresh_scheduler=refresh_scheduler,
                    debounce_seconds=_DEFAULT_DEBOUNCE_SECONDS,
                )
                set_debouncer(cidx_meta_debouncer)
                app.state.cidx_meta_debouncer = cidx_meta_debouncer
                logger.info(
                    "CidxMetaRefreshDebouncer initialized "
                    f"(debounce_seconds={_DEFAULT_DEBOUNCE_SECONDS})",
                    extra={"correlation_id": get_correlation_id()},
                )

            # Start scheduler (internally checks if enabled)
            description_refresh_scheduler.start()
            app.state.description_refresh_scheduler = description_refresh_scheduler

            if server_config.claude_integration_config.description_refresh_enabled:
                logger.info(
                    f"Description refresh scheduler started "
                    f"(interval: {server_config.claude_integration_config.description_refresh_interval_hours}h)",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "Description refresh scheduler is disabled (can be enabled in Web UI)",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-021",
                    f"Failed to initialize description refresh scheduler: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize Data Retention Scheduler (Story #401)
        data_retention_scheduler = None
        logger.info(
            "Server startup: Initializing data retention scheduler",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.data_retention_scheduler import (
                DataRetentionScheduler as _DataRetentionScheduler,
            )
            from code_indexer.server.services.config_service import get_config_service

            _drp_config_service = get_config_service()
            _drp_log_db_path = Path(server_data_dir) / "logs.db"
            _drp_main_db_path = Path(server_data_dir) / "data" / "cidx_server.db"
            _drp_groups_db_path = Path(server_data_dir) / "groups.db"

            data_retention_scheduler = _DataRetentionScheduler(
                log_db_path=_drp_log_db_path,
                main_db_path=_drp_main_db_path,
                groups_db_path=_drp_groups_db_path,
                config_service=_drp_config_service,
                job_tracker=job_tracker,
                storage_mode=storage_mode,
                backend_registry=backend_registry,
            )
            data_retention_scheduler.start()
            app.state.data_retention_scheduler = data_retention_scheduler
            logger.info(
                "Data retention scheduler started",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-033",
                    f"Failed to initialize data retention scheduler: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize Dependency Map Scheduler (Story #193)
        dependency_map_service = None
        logger.info(
            "Server startup: Initializing dependency map scheduler",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.dependency_map_service import (
                DependencyMapService,
            )
            from code_indexer.global_repos.dependency_map_analyzer import (
                DependencyMapAnalyzer,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get dependencies
            config_service = get_config_service()
            server_config = config_service.get_config()
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")  # type: ignore[assignment]
            golden_repos_manager = golden_repo_manager

            # Create tracking backend
            if backend_registry is not None:
                tracking_backend = backend_registry.dependency_map_tracking
            else:
                from code_indexer.server.storage.sqlite_backends import (
                    DependencyMapTrackingBackend,
                )

                tracking_backend = DependencyMapTrackingBackend(db_path)
            tracking_backend.cleanup_stale_status_on_startup()

            # Bug #383: Clean stale staging directory on server startup.
            # A prior crashed analysis may have left dependency-map.staging/ behind.
            # RefreshScheduler would index it and bake it into versioned snapshots,
            # polluting semantic search. Remove it before the scheduler starts.
            try:
                staging_dir = (
                    Path(golden_repos_dir) / "cidx-meta" / "dependency-map.staging"
                )
                if staging_dir.exists():
                    import shutil as _shutil

                    _shutil.rmtree(staging_dir)
                    logger.info(
                        "Cleaned stale dependency-map.staging directory on startup (Bug #383)",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except Exception as _staging_err:
                logger.debug(
                    f"Staging dir startup cleanup failed (non-fatal): {_staging_err}"
                )

            # Create analyzer
            cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
            analyzer = DependencyMapAnalyzer(
                golden_repos_root=Path(golden_repos_dir),
                cidx_meta_path=cidx_meta_path,
                pass_timeout=server_config.claude_integration_config.dependency_map_pass_timeout_seconds,
                mcp_registration_service=mcp_registration_service,
                analysis_model=server_config.golden_repos_config.analysis_model
                if server_config.golden_repos_config
                else "opus",
            )

            # Create service
            dependency_map_service = DependencyMapService(
                golden_repos_manager=golden_repos_manager,
                config_manager=config_service,
                tracking_backend=tracking_backend,
                analyzer=analyzer,
                refresh_scheduler=global_lifecycle_manager.refresh_scheduler
                if global_lifecycle_manager
                else None,
                job_tracker=job_tracker,  # Story #312: Unified job tracking (Epic #261)
            )

            # Start scheduler (internally checks if enabled)
            dependency_map_service.start_scheduler()
            app.state.dependency_map_service = dependency_map_service

            if server_config.claude_integration_config.dependency_map_enabled:
                logger.info(
                    f"Dependency map scheduler started "
                    f"(interval: {server_config.claude_integration_config.dependency_map_interval_hours}h)",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "Dependency map scheduler is disabled (can be enabled in Web UI)",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.warning(
                format_error_log(
                    "APP-GENERAL-022",
                    f"Failed to initialize dependency map scheduler: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize and start SelfMonitoringService (Epic #71)
        self_monitoring_service = None
        logger.info(
            "Server startup: Checking self-monitoring service",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.self_monitoring.service import (
                SelfMonitoringService,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get configuration
            config_service = get_config_service()
            server_config = config_service.get_config()
            sm_config = server_config.self_monitoring_config

            # Get required paths
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")  # type: ignore[assignment]
            log_db_path_val = str(Path(server_data_dir) / "logs.db")

            # Auto-detect repo root and GitHub repository from git remote
            # The server runs from within the cloned repo, so we can detect this
            github_repo = None

            # Detect repo root (tries __file__ location, then cwd as fallback)
            # Bug MONITOR-GENERAL-011: cwd fallback for pip-installed packages
            repo_root = _detect_repo_root()

            if repo_root:
                # Extract github_repo from git remote
                import re
                import subprocess

                try:
                    git_result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        cwd=str(repo_root),
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if git_result.returncode == 0:
                        url = git_result.stdout.strip()
                        # Extract owner/repo from SSH or HTTPS URL
                        match = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
                        if match:
                            github_repo = match.group(1)
                            logger.info(
                                f"Self-monitoring: Auto-detected GitHub repo '{github_repo}' from git remote",
                                extra={"correlation_id": get_correlation_id()},
                            )
                except Exception as e:
                    logger.warning(
                        f"Self-monitoring: Failed to detect GitHub repo from git remote: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )

            if sm_config.enabled:
                if not github_repo or not repo_root:
                    logger.warning(
                        format_error_log(
                            "MONITOR-GENERAL-010",
                            "Self-monitoring enabled but could not detect GitHub repo from git remote - service disabled",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    app.state.self_monitoring_service = None
                    app.state.self_monitoring_repo_root = None
                    app.state.self_monitoring_github_repo = None
                else:
                    # Get GitHub token for authentication (Bug #87)
                    from code_indexer.server.services.ci_token_manager import (
                        CITokenManager,
                    )

                    # Bug #533: In cluster mode, read JWT secret for shared
                    # encryption key so CI tokens are readable across all nodes.
                    _cluster_secret = None
                    if backend_registry is not None:
                        _jwt_file = Path(server_data_dir) / ".jwt_secret"
                        if _jwt_file.exists():
                            _cluster_secret = _jwt_file.read_text().strip()

                    token_manager = CITokenManager(
                        server_dir_path=server_data_dir,
                        use_sqlite=True,
                        db_path=db_path,
                        storage_backend=backend_registry.ci_tokens
                        if backend_registry is not None
                        else None,
                        cluster_secret=_cluster_secret,
                    )
                    github_token_data = token_manager.get_token("github")
                    github_token = (
                        github_token_data.token if github_token_data else None
                    )

                    # Get server name for issue identification (Bug #87)
                    server_name = server_config.service_display_name or "Neo"

                    self_monitoring_service = SelfMonitoringService(
                        enabled=sm_config.enabled,
                        cadence_minutes=sm_config.cadence_minutes,
                        job_manager=background_job_manager,
                        db_path=db_path,
                        log_db_path=log_db_path_val,
                        github_repo=github_repo,
                        model=sm_config.model,
                        repo_root=str(repo_root),  # For Claude to run in repo context
                        github_token=github_token,
                        server_name=server_name,
                    )
                    self_monitoring_service.start()
                    app.state.self_monitoring_service = self_monitoring_service
                    # Store repo_root and github_repo for manual trigger route access (Bug #87)
                    app.state.self_monitoring_repo_root = (
                        str(repo_root) if repo_root else None
                    )
                    app.state.self_monitoring_github_repo = github_repo
                    logger.info(
                        f"Self-monitoring service started (cadence: {sm_config.cadence_minutes} minutes)",
                        extra={"correlation_id": get_correlation_id()},
                    )
            else:
                logger.info(
                    "Self-monitoring service disabled in configuration",
                    extra={"correlation_id": get_correlation_id()},
                )
                app.state.self_monitoring_service = None
                # Store auto-detected values even when disabled (for manual trigger - Bug #87)
                app.state.self_monitoring_repo_root = (
                    str(repo_root) if repo_root else None
                )
                app.state.self_monitoring_github_repo = github_repo

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "MONITOR-GENERAL-011",
                    f"Failed to start self-monitoring service: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            app.state.self_monitoring_service = None
            # Store auto-detected values even on failure (for manual trigger - Bug #87)
            # Note: repo_root and github_repo may not be in scope if exception occurred early
            try:
                app.state.self_monitoring_repo_root = (
                    str(repo_root) if repo_root else None
                )
                app.state.self_monitoring_github_repo = github_repo
            except NameError:
                # Exception occurred before repo_root/github_repo were defined
                app.state.self_monitoring_repo_root = None
                app.state.self_monitoring_github_repo = None

        # Startup: Initialize TOTP MFA service (Story #559)
        try:
            from code_indexer.server.auth.totp_service import TOTPService
            from code_indexer.server.web.mfa_routes import set_totp_service

            mfa_db = str(Path(server_data_dir) / "data" / "cidx_server.db")
            totp_svc = TOTPService(db_path=mfa_db)
            set_totp_service(totp_svc)
            logger.info(
                "TOTP MFA service initialized",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                "TOTP MFA service initialization failed (MFA unavailable): %s",
                e,
                extra={"correlation_id": get_correlation_id()},
            )

        # Startup: Initialize MCP Session cleanup (Story #731)
        session_registry = None
        logger.info(
            "Server startup: Initializing MCP Session cleanup",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.mcp.session_registry import get_session_registry

            session_registry = get_session_registry()
            session_registry.start_background_cleanup(
                ttl_seconds=3600,  # 1 hour
                cleanup_interval_seconds=900,  # 15 minutes
            )
            logger.info(
                "MCP Session cleanup task started (TTL=3600s, interval=900s)",
                extra={"correlation_id": get_correlation_id()},
            )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-021",
                    f"Failed to initialize MCP Session cleanup: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Startup: Initialize TelemetryManager for OTEL (Story #695)
        telemetry_manager = None
        logger.info(
            "Server startup: Initializing TelemetryManager",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.config_service import get_config_service
            from code_indexer.server.utils.config_manager import ServerConfigManager

            config_service = get_config_service()
            server_config = config_service.get_config()

            # Apply environment variable overrides (e.g., CIDX_TELEMETRY_ENABLED)
            config_manager = ServerConfigManager()
            server_config = config_manager.apply_env_overrides(server_config)

            if (
                server_config.telemetry_config is not None
                and server_config.telemetry_config.enabled
            ):
                # Lazy import telemetry module only when enabled
                from code_indexer.server.telemetry import get_telemetry_manager

                telemetry_manager = get_telemetry_manager(
                    server_config.telemetry_config
                )
                app.state.telemetry_manager = telemetry_manager

                logger.info(
                    f"TelemetryManager initialized: "
                    f"service={server_config.telemetry_config.service_name}, "
                    f"endpoint={server_config.telemetry_config.collector_endpoint}, "
                    f"protocol={server_config.telemetry_config.collector_protocol}",
                    extra={"correlation_id": get_correlation_id()},
                )

                # Initialize MachineMetricsExporter for OTEL (Story #696)
                if server_config.telemetry_config.machine_metrics_enabled:
                    from code_indexer.server.telemetry.machine_metrics import (
                        get_machine_metrics_exporter,
                    )

                    machine_metrics_exporter = get_machine_metrics_exporter(
                        telemetry_manager,
                        machine_metrics_enabled=True,
                    )
                    app.state.machine_metrics_exporter = machine_metrics_exporter

                    logger.info(
                        f"MachineMetricsExporter initialized: "
                        f"{len(machine_metrics_exporter.registered_gauges)} gauges registered",
                        extra={"correlation_id": get_correlation_id()},
                    )
                else:
                    app.state.machine_metrics_exporter = None
                    logger.info(
                        "MachineMetricsExporter: Machine metrics disabled in configuration",
                        extra={"correlation_id": get_correlation_id()},
                    )

                # Initialize FastAPI instrumentation for OTEL (Story #697)
                if server_config.telemetry_config.export_traces:
                    from code_indexer.server.telemetry.instrumentation import (
                        instrument_fastapi,
                    )

                    instrumented = instrument_fastapi(app, telemetry_manager)
                    if instrumented:
                        logger.info(
                            "FastAPI instrumented with OTEL tracing",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        logger.debug(
                            "FastAPI instrumentation skipped",
                            extra={"correlation_id": get_correlation_id()},
                        )
            else:
                # Telemetry disabled - set to None
                app.state.telemetry_manager = None
                app.state.machine_metrics_exporter = None
                logger.info(
                    "TelemetryManager: Telemetry disabled in configuration",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-022",
                    f"Failed to initialize TelemetryManager: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            app.state.telemetry_manager = None
            app.state.machine_metrics_exporter = None

        # Startup: Initialize OIDC authentication if configured
        logger.info(
            "Server startup: Checking OIDC configuration",
            extra={"correlation_id": get_correlation_id()},
        )
        # Always register OIDC routes (routes will handle disabled/unconfigured state)
        from code_indexer.server.auth.oidc import routes as oidc_routes

        app.include_router(oidc_routes.router)

        try:
            from code_indexer.server.services.config_service import get_config_service
            from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
            from code_indexer.server.auth.oidc.state_manager import StateManager

            # Bug #578: Read from ConfigService (has merged runtime from DB),
            # NOT directly from config.json (which is bootstrap-only after migration).
            config = get_config_service().get_config()
            if (
                config
                and hasattr(config, "oidc_provider_config")
                and config.oidc_provider_config
                and config.oidc_provider_config.enabled
            ):
                logger.info(
                    "OIDC is enabled, initializing...",
                    extra={"correlation_id": get_correlation_id()},
                )

                # Use existing user_manager and jwt_manager (defined at module level below)
                # Note: These are defined after the lifespan function, so we reference them here
                state_manager = StateManager()
                oidc_manager = OIDCManager(
                    config=config.oidc_provider_config,
                    user_manager=user_manager,  # Global from module level
                    jwt_manager=jwt_manager,  # Global from module level
                )

                # Initialize OIDC database schema (no network calls)
                # Provider metadata will be discovered lazily on first SSO login attempt
                await oidc_manager.initialize()

                # Inject managers into routes module
                oidc_routes.oidc_manager = oidc_manager
                oidc_routes.state_manager = state_manager
                oidc_routes.server_config = config

                # Inject GroupAccessManager into OIDCManager for SSO provisioning (Story #708)
                if hasattr(app.state, "group_manager") and app.state.group_manager:
                    oidc_manager.group_manager = app.state.group_manager
                    logger.info(
                        "GroupAccessManager injected into OIDCManager for SSO auto-provisioning",
                        extra={"correlation_id": get_correlation_id()},
                    )

                logger.info(
                    "OIDC configured (will initialize on first login)",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "OIDC is not enabled",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-023",
                    f"Failed to initialize OIDC: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            logger.info(
                "OIDC routes registered but manager not initialized - SSO login will return 404 until configured",
                extra={"correlation_id": get_correlation_id()},
            )

        # Startup: Initialize Langfuse Trace Sync Service (Story #168)
        global langfuse_sync_service
        langfuse_sync_service = None  # type: ignore[name-defined]
        logger.info(
            "Server startup: Initializing Langfuse Trace Sync Service",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.langfuse_trace_sync_service import (
                LangfuseTraceSyncService,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get config service for dynamic config access
            config_service = get_config_service()

            # Define callback for auto-registering new Langfuse folders after sync
            def _on_langfuse_sync_complete():
                """Auto-register new Langfuse folders after sync."""
                if golden_repo_manager is not None:
                    register_langfuse_golden_repos(
                        golden_repo_manager, str(golden_repos_dir)
                    )

            # Create service with config_getter callable
            langfuse_sync_service = LangfuseTraceSyncService(  # type: ignore[name-defined]
                config_getter=config_service.get_config,
                data_dir=str(Path(server_data_dir) / "data"),
                on_sync_complete=_on_langfuse_sync_complete,
                refresh_scheduler=global_lifecycle_manager.refresh_scheduler
                if global_lifecycle_manager
                else None,
                job_tracker=job_tracker,
            )

            # Start background sync if pull is enabled
            config = config_service.get_config()
            if config.langfuse_config and config.langfuse_config.pull_enabled:
                langfuse_sync_service.start()  # type: ignore[name-defined]
                logger.info(
                    f"Langfuse Trace Sync Service started (interval={config.langfuse_config.pull_sync_interval_seconds}s, "
                    f"projects={len(config.langfuse_config.pull_projects)})",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                logger.info(
                    "Langfuse pull sync disabled, service initialized but not started",
                    extra={"correlation_id": get_correlation_id()},
                )

            # Store in app state for dashboard access
            app.state.langfuse_sync_service = langfuse_sync_service  # type: ignore[name-defined]

        except Exception as e:
            # Log error but don't block server startup
            logger.error(
                format_error_log(
                    "APP-GENERAL-029",
                    f"Failed to initialize Langfuse Trace Sync Service: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            app.state.langfuse_sync_service = None

        # Startup: Eagerly initialize Langfuse SDK (Story #278)
        # Moves the one-time SDK import + network I/O cost to startup rather
        # than the first MCP request. Failure is non-fatal - server continues.
        logger.info(
            "Server startup: Eagerly initializing Langfuse SDK",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.langfuse_service import (
                get_langfuse_service,
            )

            get_langfuse_service().eager_initialize()
            logger.info(
                "Langfuse SDK eager initialization complete",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                f"Langfuse SDK eager initialization failed (non-fatal): {e}",
                extra={"correlation_id": get_correlation_id()},
            )

        # Make backend_registry available to MCP handlers for BOTH modes.
        # In SQLite mode it contains SQLite backends; in postgres mode, PG backends.
        # MCP handlers use app.state.backend_registry unconditionally.
        app.state.backend_registry = backend_registry

        # Bug #532: Inject DiagnosticsBackend into the module-level diagnostics_service
        # singleton. The singleton is created at import time with no backend; we inject
        # it here after backend_registry is available.
        if backend_registry is not None and hasattr(backend_registry, "diagnostics"):
            from code_indexer.server.routers.diagnostics import (
                diagnostics_service as _diagnostics_service,
            )

            _diagnostics_service._backend = backend_registry.diagnostics
            logger.info(
                "Bug #532: DiagnosticsBackend injected into diagnostics_service singleton",
                extra={"correlation_id": get_correlation_id()},
            )

        # Story #500 AC4: Inject LogsBackend into SQLiteLogHandler for delegated writes.
        # The handler was created earlier (before backend_registry existed); now that
        # backend_registry is available, wire it in so emit() routes through the backend.
        # Story #526: Only call set_logs_backend() if backend not already set at construction.
        if backend_registry is not None and hasattr(backend_registry, "logs"):
            app.state.logs_backend = backend_registry.logs
            if hasattr(app.state, "sqlite_log_handler"):
                _handler = app.state.sqlite_log_handler
                if _handler._logs_backend is None:
                    _handler.set_logs_backend(backend_registry.logs)
                    logger.info(
                        "Story #500 AC4: LogsBackend injected into SQLiteLogHandler",
                        extra={"correlation_id": get_correlation_id()},
                    )
                else:
                    logger.info(
                        "Story #526: LogsBackend already set at construction, skipping set_logs_backend()",
                        extra={"correlation_id": get_correlation_id()},
                    )

        # Epic #408: Start cluster services when in postgres mode
        _cluster_services = []
        if storage_mode == "postgres" and backend_registry is not None:
            try:
                _pg_dsn = ""
                _configured_node_id = ""
                try:
                    import json as _json

                    _cfg_path = Path.home() / ".cidx-server" / "config.json"
                    if _cfg_path.exists():
                        with open(_cfg_path) as _f:
                            _cfg_data = _json.load(_f)
                            _pg_dsn = _cfg_data.get("postgres_dsn", "")
                            _configured_node_id = _cfg_data.get("cluster", {}).get(
                                "node_id", ""
                            )
                except Exception:
                    pass

                if _pg_dsn:
                    # Bug #545: Use the critical pool for heartbeat/reconciliation
                    # so they can't be starved by general HTTP/job traffic.
                    # Falls back to general pool if critical pool unavailable.
                    _cluster_pool = (
                        backend_registry.critical_connection_pool
                        or backend_registry.connection_pool
                    )
                    _node_id = (
                        _configured_node_id
                        if _configured_node_id
                        else f"{os.uname().nodename}-cidx"
                    )

                    # Story #501 AC3: Tag log records with the cluster node ID so
                    # the admin UI can aggregate and filter logs per node.
                    if hasattr(app.state, "sqlite_log_handler"):
                        app.state.sqlite_log_handler.set_node_id(_node_id)
                        logger.info(
                            f"Story #501 AC3: node_id={_node_id!r} injected into SQLiteLogHandler",
                            extra={"correlation_id": get_correlation_id()},
                        )

                    # Leader election
                    from code_indexer.server.services.leader_election_service import (
                        LeaderElectionService,
                    )

                    _leader_election = LeaderElectionService(
                        connection_string=_pg_dsn,
                        node_id=_node_id,
                    )
                    _leader_election.start_monitoring(check_interval=10)
                    _cluster_services.append(("leader_election", _leader_election))

                    # Node heartbeat
                    from code_indexer.server.services.node_heartbeat_service import (
                        NodeHeartbeatService,
                    )

                    _heartbeat = NodeHeartbeatService(
                        pool=_cluster_pool, node_id=_node_id
                    )
                    _heartbeat.set_leader_election(_leader_election)
                    _heartbeat.start()
                    _cluster_services.append(("heartbeat", _heartbeat))

                    # Job reconciliation
                    from code_indexer.server.services.job_reconciliation_service import (
                        JobReconciliationService,
                    )

                    _reconciliation = JobReconciliationService(
                        pool=_cluster_pool, heartbeat_service=_heartbeat
                    )
                    _reconciliation.start()
                    _cluster_services.append(("reconciliation", _reconciliation))

                    # Story #538: Enable PG advisory locks for password changes
                    from code_indexer.server.auth.concurrency_protection import (
                        password_change_concurrency_protection,
                    )

                    password_change_concurrency_protection.set_connection_pool(
                        _cluster_pool
                    )

                    # Epic #556: Wire cluster pools for MFA/security services
                    from code_indexer.server.web.mfa_routes import get_totp_service
                    from code_indexer.server.auth.mfa_challenge import (
                        mfa_challenge_manager,
                    )
                    from code_indexer.server.auth.login_rate_limiter import (
                        login_rate_limiter as _login_rate_limiter_singleton,
                    )

                    _totp_svc = get_totp_service()
                    if _totp_svc is not None:
                        _totp_svc.set_connection_pool(_cluster_pool)
                        logger.info("TOTPService: cluster pool wired")
                    else:
                        logger.warning(
                            "TOTPService not initialized — skipping cluster pool wiring"
                        )

                    mfa_challenge_manager.set_connection_pool(_cluster_pool)
                    _login_rate_limiter_singleton.set_connection_pool(_cluster_pool)

                    # Bug #573: Wire cluster pools for password change
                    # and refresh token rate limiters
                    from code_indexer.server.auth.rate_limiter import (
                        password_change_rate_limiter,
                        refresh_token_rate_limiter,
                    )

                    password_change_rate_limiter.set_connection_pool(_cluster_pool)
                    refresh_token_rate_limiter.set_connection_pool(_cluster_pool)

                    # Bug #574: Wire cluster pools for OAuth rate limiters
                    from code_indexer.server.auth.oauth_rate_limiter import (
                        oauth_token_rate_limiter,
                        oauth_register_rate_limiter,
                    )

                    oauth_token_rate_limiter.set_connection_pool(_cluster_pool)
                    oauth_register_rate_limiter.set_connection_pool(_cluster_pool)

                    # Bug #576: Wire OIDC StateManager for cluster
                    from code_indexer.server.auth.oidc import (
                        routes as _oidc_routes,
                    )

                    if (
                        hasattr(_oidc_routes, "state_manager")
                        and _oidc_routes.state_manager is not None
                    ):
                        _oidc_routes.state_manager.set_connection_pool(_cluster_pool)

                    # Story #578: Centralize runtime config in PostgreSQL
                    from code_indexer.server.services.config_service import (
                        get_config_service,
                    )

                    _config_svc = get_config_service()
                    _config_svc.set_connection_pool(_cluster_pool)
                    _config_svc.start_config_reload(interval_seconds=30)

                    # Bug #587: Wire cluster pool for activated repos
                    if (
                        golden_repo_manager is not None
                        and hasattr(golden_repo_manager, "activated_repo_manager")
                        and golden_repo_manager.activated_repo_manager is not None
                    ):
                        _arm = golden_repo_manager.activated_repo_manager
                        _arm.set_connection_pool(_cluster_pool)
                        # Use server_data_dir as shared NFS base
                        _shared_data = str(Path(server_data_dir) / "data")
                        _arm.set_shared_repos_dir(_shared_data)
                        logger.info("Bug #587: ActivatedRepoManager cluster pool wired")

                    app.state.leader_election = _leader_election
                    # Story #505/#506: Store node_id and postgres_dsn in app.state
                    # so check_health MCP handler and web routes can read them.
                    app.state.node_id = _node_id
                    app.state.postgres_dsn = _pg_dsn

                    # Story #531: Inject cluster node_id into ApiMetricsService
                    # so api_metrics.node_id matches node_metrics.node_id
                    try:
                        from code_indexer.server.services.api_metrics_service import (
                            api_metrics_service as _ams,
                        )

                        _ams.set_node_id(_node_id)
                    except (ImportError, AttributeError) as exc:
                        logger.warning(
                            "Failed to inject node_id into ApiMetricsService: %s",
                            exc,
                        )

                    # Story #505: Initialize DatabaseHealthService singleton with
                    # postgres mode so dashboard shows PostgreSQL health instead
                    # of migrated SQLite databases.
                    from code_indexer.server.services.database_health_service import (
                        get_database_health_service,
                    )

                    get_database_health_service(
                        storage_mode="postgres",
                        postgres_dsn=_pg_dsn,
                    )

                    # Story #527: Initialize HealthCheckService singleton with
                    # postgres mode so _check_database_health uses PG connectivity.
                    from code_indexer.server.services.health_service import (
                        get_health_service,
                    )

                    get_health_service(
                        storage_mode="postgres",
                        postgres_dsn=_pg_dsn,
                    )

                    logger.info(
                        f"Cluster services started: node_id={_node_id}, "
                        f"is_leader={_leader_election.is_leader}",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except Exception as e:
                logger.error(
                    f"Failed to start cluster services: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )

        # Story #492: Start NodeMetricsWriterService (always on, SQLite or postgres)
        # When backend_registry is available (postgres/cluster mode), use its node_metrics
        # backend so NodeMetricsPostgresBackend is used instead of SQLite.
        _node_metrics_writer = None
        try:
            from code_indexer.server.services.node_metrics_writer_service import (
                NodeMetricsWriterService,
            )

            _nm_node_id = None
            try:
                import json as _nm_json

                _nm_cfg_path = Path.home() / ".cidx-server" / "config.json"
                if _nm_cfg_path.exists():
                    with open(_nm_cfg_path) as _nm_f:
                        _nm_cfg = _nm_json.load(_nm_f)
                        _nm_node_id = _nm_cfg.get("cluster", {}).get("node_id") or None
            except Exception as e:
                logger.debug(f"Could not read node_id from config.json: {e}")

            # Use the backend_registry.node_metrics when running in postgres/cluster mode;
            # fall back to creating a dedicated SQLite backend for standalone mode.
            if backend_registry is not None:
                _nm_backend = backend_registry.node_metrics
                logger.info(
                    "NodeMetricsWriterService: using backend_registry.node_metrics "
                    f"(storage_mode={storage_mode})",
                    extra={"correlation_id": get_correlation_id()},
                )
            else:
                from code_indexer.server.storage.sqlite_backends import (
                    NodeMetricsSqliteBackend,
                )

                _nm_db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
                _nm_backend = NodeMetricsSqliteBackend(_nm_db_path)

            _node_metrics_writer = NodeMetricsWriterService(
                backend=_nm_backend,
                node_id=_nm_node_id,
            )
            _node_metrics_writer.start()
            app.state.node_metrics_writer = _node_metrics_writer
            # Store backend in app.state so dashboard-health route can access it
            # without re-creating it on every request.
            app.state.node_metrics_backend = _nm_backend
            logger.info(
                f"NodeMetricsWriterService started (node_id={_node_metrics_writer.node_id})",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-032",
                    f"Failed to start NodeMetricsWriterService: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            app.state.node_metrics_writer = None
            app.state.node_metrics_backend = None

        yield  # Server is now running

        # Story #578: Stop config reload thread before cluster services
        try:
            from code_indexer.server.services.config_service import (
                get_config_service,
            )

            get_config_service().stop_config_reload()
        except Exception:
            logger.debug(
                "Config reload stop failed (expected during shutdown)",
                exc_info=True,
            )

        # Epic #408: Stop cluster services
        for svc_name, svc in reversed(_cluster_services):
            try:
                if hasattr(svc, "stop_monitoring"):
                    svc.stop_monitoring()
                elif hasattr(svc, "stop"):
                    svc.stop()
                elif hasattr(svc, "release_leadership"):
                    svc.release_leadership()
                logger.info(
                    f"Cluster service stopped: {svc_name}",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.warning(
                    f"Error stopping cluster service {svc_name}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )

        # Story #492: Stop NodeMetricsWriterService
        if _node_metrics_writer is not None:
            try:
                _node_metrics_writer.stop()
                logger.info(
                    "NodeMetricsWriterService stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-033",
                        f"Error stopping NodeMetricsWriterService: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop global repos background services BEFORE other cleanup
        logger.info(
            "Server shutdown: Stopping global repos background services",
            extra={"correlation_id": get_correlation_id()},
        )
        if global_lifecycle_manager is not None:
            try:
                global_lifecycle_manager.stop()
                logger.info(
                    "Global repos background services stopped successfully",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-024",
                        f"Error stopping global repos background services: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop PayloadCache background cleanup (Story #679)
        if payload_cache is not None:
            try:
                payload_cache.close()
                logger.info(
                    "PayloadCache stopped successfully",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-025",
                        f"Error stopping PayloadCache: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop MCP Session cleanup (Story #731)
        if session_registry is not None:
            try:
                session_registry.stop_background_cleanup()
                logger.info(
                    "MCP Session cleanup stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-026",
                        f"Error stopping MCP Session cleanup: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop data retention scheduler (Story #401)
        data_retention_scheduler_state = getattr(
            app.state, "data_retention_scheduler", None
        )
        if data_retention_scheduler_state is not None:
            try:
                data_retention_scheduler_state.stop()
                logger.info(
                    "Data retention scheduler stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-034",
                        f"Error stopping data retention scheduler: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop description refresh scheduler (Story #190)
        description_refresh_scheduler_state = getattr(
            app.state, "description_refresh_scheduler", None
        )
        if description_refresh_scheduler_state is not None:
            try:
                description_refresh_scheduler_state.stop()
                logger.info(
                    "Description refresh scheduler stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-027",
                        f"Error stopping description refresh scheduler: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # --- LLM Lease Lifecycle shutdown ---
        _llm_svc = getattr(app.state, "llm_lifecycle_service", None)
        if _llm_svc is not None:
            try:
                _llm_svc.stop()
                logger.info("LLM lease lifecycle stopped")
            except Exception as e:
                logger.error("Error stopping LLM lease lifecycle: %s", e)

        # Shutdown: Stop cidx-meta refresh debouncer (Story #345)
        cidx_meta_debouncer_state = getattr(app.state, "cidx_meta_debouncer", None)
        if cidx_meta_debouncer_state is not None:
            try:
                cidx_meta_debouncer_state.shutdown()
                from code_indexer.global_repos.meta_description_hook import (
                    set_debouncer,
                )

                set_debouncer(None)
                logger.info(
                    "CidxMetaRefreshDebouncer shut down",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-029",
                        f"Error stopping cidx-meta refresh debouncer: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop dependency map scheduler (Story #193)
        dependency_map_service_state = getattr(
            app.state, "dependency_map_service", None
        )
        if dependency_map_service_state is not None:
            try:
                dependency_map_service_state.stop_scheduler()
                logger.info(
                    "Dependency map scheduler stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-028",
                        f"Error stopping dependency map scheduler: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop self-monitoring service (Epic #71)
        if self_monitoring_service is not None:
            try:
                self_monitoring_service.stop()
                logger.info(
                    "Self-monitoring service stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-028",
                        f"Error stopping self-monitoring service: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop Langfuse Trace Sync Service (Story #168)
        if langfuse_sync_service is not None:  # type: ignore[name-defined]
            try:
                langfuse_sync_service.stop()  # type: ignore[name-defined]
                logger.info(
                    "Langfuse Trace Sync Service stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-030",
                        f"Error stopping Langfuse Trace Sync Service: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Stop TelemetryManager and flush pending telemetry (Story #695)
        if telemetry_manager is not None:
            try:
                telemetry_manager.shutdown()
                logger.info(
                    "TelemetryManager stopped successfully",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-027",
                        f"Error stopping TelemetryManager: {e}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

        # Shutdown: Clean up other resources
        logger.info(
            "Server shutdown: Cleaning up resources",
            extra={"correlation_id": get_correlation_id()},
        )

    return lifespan
