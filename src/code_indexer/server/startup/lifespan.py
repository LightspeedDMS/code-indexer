"""Lifespan context manager for CIDX server startup and shutdown."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
import anyio.to_thread
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
from code_indexer.server.startup.bootstrap import _detect_repo_root
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


def _make_dep_map_repair_invoker_fn(
    dep_map_dir: Path,
    tracking_backend: Any,
    job_tracker: Any,
    dep_map_service: Any,
) -> Callable[[str], None]:
    """Build the repair invoker closure for DependencyMapService (Story #927).

    Extracted as a module-level factory so the wiring can be tested directly
    (Codex Pass 3 regression guard).

    Story #927 Codex Pass 4-5: the lifespan startup must construct
    DependencyMapService FIRST, then call this factory with the constructed
    service instance, then bind the returned closure via
    ``dependency_map_service.set_repair_invoker_fn(...)`` BEFORE
    ``start_scheduler()``. The full ordering is:
        1. dependency_map_service = DependencyMapService(...)
        2. _dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(...)
        3. dependency_map_service.set_repair_invoker_fn(_dep_map_repair_invoker_fn)
        4. dependency_map_service.start_scheduler()

    This late-binding pattern is required because the closure captures
    ``dep_map_service`` by parameter binding (Python by-value), so the service
    must exist at factory call time.

    Args:
        dep_map_dir: Path to the dependency-map directory (captured by closure).
        tracking_backend: DependencyMapTrackingBackend for job status updates.
        job_tracker: JobTracker for unified job tracking.
        dep_map_service: DependencyMapService instance (provides repo metadata + analyzer).

    Returns:
        A callable(job_id: str) -> None that delegates to _execute_repair_body.
    """

    def _invoker(job_id: str) -> None:
        from code_indexer.server.web.dependency_map_routes import (
            _execute_repair_body,
        )

        _execute_repair_body(
            job_id=job_id,
            output_dir=dep_map_dir,
            tracking_backend=tracking_backend,
            job_tracker=job_tracker,
            activity_journal=None,
            dep_map_service=dep_map_service,  # Story #927 Pass 2: executor needs repo metadata + analyzer
        )

    return _invoker


def _apply_fault_injection_state(app: Any, startup_config: Any) -> None:
    """Wire fault injection state on app.state for both normal and degraded startup.

    Story #746 — Codex architectural review MAJOR finding:
    Previously, the degraded-startup branch (startup_config is None) only set
    app.state.fault_injection_service and forgot app.state.http_client_factory,
    causing AttributeError at request time in api_keys._make_tester() which
    unconditionally reads http_request.app.state.http_client_factory.

    This helper guarantees BOTH attributes are set on every path.
    """
    from code_indexer.server.fault_injection.http_client_factory import (
        HttpClientFactory,
    )
    from code_indexer.server.fault_injection.startup import wire_fault_injection

    if startup_config is None:
        app.state.fault_injection_service = None
        app.state.http_client_factory = HttpClientFactory(fault_injection_service=None)
        return

    wire_fault_injection(app, startup_config)


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
    latency_tracker: Any = None,  # Any: matches existing pattern; avoids circular import from services
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

        # Story #680: Store latency_tracker in app.state for dashboard route access.
        app.state.latency_tracker = latency_tracker

        # Startup: Initialize SQLite log handler FIRST (to capture all startup logs)
        logger.info(
            "Server startup: Initializing SQLite log handler",
            extra={"correlation_id": get_correlation_id()},
        )
        startup_config = None  # Story #746: ensure always defined before try block
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

        # Bootstrap-only: bump anyio threadpool size so concurrent sync handlers
        # do not starve one another. Default 256 vs anyio's built-in default of 40.
        # Runs inside the async lifespan so current_default_thread_limiter() resolves correctly.
        _threadpool_size = (
            getattr(startup_config, "server_threadpool_size", 256)
            if startup_config is not None
            else 256
        )
        if _threadpool_size > 0:
            anyio.to_thread.current_default_thread_limiter().total_tokens = (
                _threadpool_size
            )
            logger.info(
                f"Threadpool sized to {_threadpool_size} tokens (anyio default 40)",
                extra={"correlation_id": get_correlation_id()},
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
                storage_backend=(
                    backend_registry.groups if backend_registry is not None else None
                ),
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
                storage_backend=(
                    backend_registry.audit_log if backend_registry is not None else None
                ),
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

            # Story #510 AC8: Build VersionedSnapshotManager with configured CloneBackend
            # and store it in app.state for use by snapshot-aware lifecycle services.
            try:
                from code_indexer.server.startup.clone_backend_wiring import (
                    build_snapshot_manager,
                )

                versioned_base = str(golden_repos_dir)
                snapshot_manager = build_snapshot_manager(
                    server_config, versioned_base=versioned_base
                )
                app.state.snapshot_manager = snapshot_manager
                logger.info(
                    "Story #510: VersionedSnapshotManager initialized "
                    "(clone_backend=%r, versioned_base=%s)",
                    server_config.clone_backend,
                    versioned_base,
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as snap_exc:
                # Non-fatal: log and continue. Snapshot operations will fail at
                # runtime if clone_backend is misconfigured, but startup proceeds.
                logger.error(
                    format_error_log(
                        "APP-GENERAL-510",
                        f"Failed to initialize VersionedSnapshotManager "
                        f"(clone_backend={getattr(server_config, 'clone_backend', 'unknown')}): "
                        f"{snap_exc}",
                        exc_info=True,
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                app.state.snapshot_manager = None

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
                storage_backend=(
                    backend_registry.payload_cache
                    if backend_registry is not None
                    else None
                ),
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

        # Startup: Initialize Codex CLI credential management (Story #846)
        _codex_shutdown_hook = None
        try:
            from code_indexer.server.startup.codex_cli_startup import (
                initialize_codex_manager_on_startup,
            )

            _codex_shutdown_hook = initialize_codex_manager_on_startup(
                server_config=config_service.get_config(),
                server_data_dir=server_data_dir,
                return_shutdown_hook=True,
            )
            # Hoist to app.state so the shutdown block can reach it (Story #846 CRIT-1).
            app.state.codex_shutdown_hook = _codex_shutdown_hook
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-050",
                    f"Failed to initialize Codex CLI credential management on startup: {e}",
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
                analysis_model=(
                    server_config.golden_repos_config.analysis_model
                    if server_config.golden_repos_config
                    else "opus"
                ),
                job_tracker=job_tracker,
                mcp_registration_service=mcp_registration_service,
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

                # Story #876 D4 — Wire cluster-atomic lifecycle registration
                # hook collaborators into golden_repo_manager.  _refresh_scheduler
                # was wired earlier; the three below complete the quartet so the
                # helper _register_lifecycle_after_registration fires during
                # production registrations instead of staged-rollout skipping.
                from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
                    LifecycleClaudeCliInvoker,
                )

                lifecycle_invoker_singleton = LifecycleClaudeCliInvoker()
                if golden_repo_manager is not None:
                    golden_repo_manager.job_tracker = job_tracker
                    golden_repo_manager.lifecycle_debouncer = cidx_meta_debouncer
                    golden_repo_manager.lifecycle_invoker = lifecycle_invoker_singleton
                    golden_repo_manager.lifecycle_tracking_backend = tracking_backend

                # Story #876 D3 — wire the same quartet onto the description
                # refresh scheduler so refresh_task can route every stale repo
                # through LifecycleBatchRunner.  All four slots are mandatory;
                # a missing slot emits a WARNING and skips the runner
                # (Messi Rule #2 anti-fallback, verified by the D3 test suite).
                description_refresh_scheduler._lifecycle_invoker = (
                    lifecycle_invoker_singleton
                )
                description_refresh_scheduler._golden_repos_dir = Path(golden_repos_dir)
                description_refresh_scheduler._lifecycle_debouncer = cidx_meta_debouncer
                description_refresh_scheduler._refresh_scheduler = refresh_scheduler

                logger.info(
                    "CidxMetaRefreshDebouncer initialized "
                    f"(debounce_seconds={_DEFAULT_DEBOUNCE_SECONDS})",
                    extra={"correlation_id": get_correlation_id()},
                )

                # Wire MemoryStoreService for Story #877 (shared technical memory store).
                # No inner try/except — failures propagate to the outer except at APP-GENERAL-021
                # which logs a warning without blocking startup, consistent with this block's pattern.
                from code_indexer.server.startup.nfs_self_check import (
                    run_nfs_atomic_create_self_check,
                )

                run_nfs_atomic_create_self_check(Path(golden_repos_dir) / "cidx-meta")

                from code_indexer.server.services.memory_store_service_factory import (
                    build_memory_store_service as _build_memory_store_service,
                )
                from code_indexer.server.services.access_filtering_service import (
                    AccessFilteringService as _AccessFilteringService,
                )

                _memory_bundle = _build_memory_store_service(
                    golden_repos_dir=Path(golden_repos_dir),
                    server_data_dir=Path(server_data_dir),
                    refresh_scheduler=refresh_scheduler,
                    refresh_debouncer=cidx_meta_debouncer,
                )
                _memory_store_service = _memory_bundle.service
                _memory_metadata_cache = _memory_bundle.cache
                _memories_dir = _memory_bundle.memories_dir

                app.state.memory_store_service = _memory_store_service
                app.state.memory_metadata_cache = _memory_metadata_cache
                if hasattr(app.state, "app_state") and app.state.app_state is not None:
                    app.state.app_state.memory_store_service = _memory_store_service

                # Story #877 Phase 3-A: reconstruct AccessFilteringService with the
                # shared memory metadata cache so filter_cidx_meta_files() can look up
                # memory file scope/referenced_repo without redundant disk reads.
                # Uses _memories_dir from the bundle — the single authoritative path.
                _access_filtering_service_with_cache = _AccessFilteringService(
                    group_manager,
                    memory_metadata_cache=_memory_metadata_cache,
                    memories_dir=_memories_dir,
                )
                app.state.access_filtering_service = (
                    _access_filtering_service_with_cache
                )
                logger.info(
                    "MemoryStoreService initialized for Story #877 (cache wired to AccessFilteringService)",
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

        # Startup: Initialize Activated Repository Reaper Scheduler (Story #967)
        activated_reaper_scheduler = None
        logger.info(
            "Server startup: Initializing activated reaper scheduler",
            extra={"correlation_id": get_correlation_id()},
        )
        try:
            from code_indexer.server.services.activated_reaper_service import (
                ActivatedReaperService as _ActivatedReaperService,
            )
            from code_indexer.server.services.activated_reaper_scheduler import (
                ActivatedReaperScheduler as _ActivatedReaperScheduler,
            )
            from code_indexer.server.services.config_service import get_config_service

            if not (
                hasattr(golden_repo_manager, "activated_repo_manager")
                and golden_repo_manager.activated_repo_manager is not None
            ):
                raise RuntimeError(
                    "golden_repo_manager.activated_repo_manager is not available"
                )
            _reaper_config_service = get_config_service()
            _reaper_service = _ActivatedReaperService(
                activated_repo_manager=golden_repo_manager.activated_repo_manager,
                background_job_manager=background_job_manager,
                config_service=_reaper_config_service,
            )
            activated_reaper_scheduler = _ActivatedReaperScheduler(
                service=_reaper_service,
                background_job_manager=background_job_manager,
                config_service=_reaper_config_service,
            )
            activated_reaper_scheduler.start()
            app.state.activated_reaper_scheduler = activated_reaper_scheduler
            logger.info(
                "Activated reaper scheduler started",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-036",
                    f"Failed to initialize activated reaper scheduler: {e}",
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

            # Create description refresh tracking backend for lifecycle backfill (Epic #725).
            # This is the DescriptionRefreshTrackingBackend (description_refresh_tracking table),
            # which is separate from DependencyMapTrackingBackend. Must be passed explicitly
            # because DependencyMapService only holds DependencyMapTrackingBackend by default.
            if backend_registry is not None:
                description_refresh_tracking_backend = (
                    backend_registry.description_refresh_tracking
                )
            else:
                from code_indexer.server.storage.sqlite_backends import (
                    DescriptionRefreshTrackingBackend,
                )

                description_refresh_tracking_backend = (
                    DescriptionRefreshTrackingBackend(db_path)
                )

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
                analysis_model=(
                    server_config.golden_repos_config.analysis_model
                    if server_config.golden_repos_config
                    else "opus"
                ),
            )

            # Story #927: cluster dedup pool (None = solo mode)
            _dep_map_pg_pool = (
                (
                    backend_registry.critical_connection_pool
                    or backend_registry.connection_pool
                )
                if storage_mode == "postgres" and backend_registry is not None
                else None
            )

            # Story #927: closures capture dep_map_dir, tracking_backend, job_tracker
            _dep_map_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"

            def _dep_map_health_check_fn():
                from code_indexer.server.services.dep_map_health_detector import (
                    DepMapHealthDetector,
                )

                return DepMapHealthDetector().detect(_dep_map_dir)

            # Story #927 Codex Pass 4: construct service FIRST so the closure can
            # capture the real instance, not the pre-construction None placeholder.
            dependency_map_service = DependencyMapService(
                golden_repos_manager=golden_repos_manager,
                config_manager=config_service,
                tracking_backend=tracking_backend,
                analyzer=analyzer,
                refresh_scheduler=(
                    global_lifecycle_manager.refresh_scheduler
                    if global_lifecycle_manager
                    else None
                ),
                job_tracker=job_tracker,  # Story #312: Unified job tracking (Epic #261)
                description_refresh_tracking_backend=description_refresh_tracking_backend,  # Epic #725
                pg_pool=_dep_map_pg_pool,  # Story #927: cluster dedup lock
                health_check_fn=_dep_map_health_check_fn,  # Story #927: anomaly gate
                repair_invoker_fn=None,  # late-bound below via set_repair_invoker_fn
                storage_mode=storage_mode,  # Story #927 Pass 2: anti-fallback guard
            )

            # Build the closure with the real constructed service instance
            _dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(
                dep_map_dir=_dep_map_dir,
                tracking_backend=tracking_backend,
                job_tracker=job_tracker,
                dep_map_service=dependency_map_service,  # Story #927 Pass 4: now the real instance
            )

            # Late-bind the closure into the service
            dependency_map_service.set_repair_invoker_fn(_dep_map_repair_invoker_fn)

            # Start scheduler — invoker is bound, safe to start
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

                    # Bug #639: Use shared factory to ensure consistent encryption key
                    from code_indexer.server.services.ci_token_manager import (
                        create_token_manager,
                    )

                    token_manager = create_token_manager(
                        server_dir=server_data_dir,
                        db_path=db_path,
                        storage_backend=(
                            backend_registry.ci_tokens
                            if backend_registry is not None
                            else None
                        ),
                        storage_mode=storage_mode,
                    )
                    github_token_data = token_manager.get_token("github")
                    github_token = (
                        github_token_data.token if github_token_data else None
                    )

                    # Get server name for issue identification (Bug #87)
                    server_name = server_config.service_display_name or "Neo"

                    # Bug #585: Pass PG backend when available
                    _sm_backend = (
                        backend_registry.self_monitoring
                        if backend_registry is not None
                        else None
                    )
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
                        storage_backend=_sm_backend,
                    )
                    # Bug #580: Only auto-start in standalone mode.
                    # In cluster mode (postgres), leader election callbacks
                    # handle start/stop so only the leader node runs this.
                    if storage_mode != "postgres":
                        self_monitoring_service.start()
                        logger.info(
                            f"Self-monitoring service started (cadence: {sm_config.cadence_minutes} minutes)",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    else:
                        logger.info(
                            "Self-monitoring service initialized (awaiting leader election)",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    app.state.self_monitoring_service = self_monitoring_service
                    # Store repo_root and github_repo for manual trigger route access (Bug #87)
                    app.state.self_monitoring_repo_root = (
                        str(repo_root) if repo_root else None
                    )
                    app.state.self_monitoring_github_repo = github_repo
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
            # Bug #964 Fix 3: track repos seen in previous cycle for short-circuit.
            _previous_sync_repos: set = set()

            def _on_langfuse_sync_complete():
                """Auto-register new Langfuse folders after sync and generate READMEs.

                Bug #964 Fix 3: skips registration and README generation when no new
                repos were discovered compared to the previous sync cycle, avoiding
                unnecessary work on every cycle when nothing has changed.
                """
                nonlocal _previous_sync_repos
                _callback_start = time.monotonic()

                # langfuse_sync_service is a module-level global assigned after this
                # closure is defined; mypy cannot infer the type from the global
                # declaration alone, hence type: ignore[name-defined] is required.
                current_repos: set = set()
                if langfuse_sync_service is not None:  # type: ignore[name-defined]
                    current_repos = set(
                        langfuse_sync_service._last_modified_repos  # type: ignore[name-defined]
                    )

                # Short-circuit: skip expensive work when no new repos discovered.
                # Only activates after the first cycle (_previous_sync_repos non-empty)
                # so the first run always registers repos even if the set is unchanged.
                if current_repos == _previous_sync_repos and _previous_sync_repos:
                    elapsed = time.monotonic() - _callback_start
                    logger.info(
                        f"_on_sync_complete took {elapsed:.2f}s (short-circuited: no new repos)"
                    )
                    return

                _previous_sync_repos = current_repos

                if golden_repo_manager is not None:
                    register_langfuse_golden_repos(
                        golden_repo_manager, str(golden_repos_dir)
                    )
                # Generate README files for repos that received new/updated traces
                # langfuse_sync_service assigned after closure definition; mypy cannot
                # resolve name from the global declaration, hence type: ignore[name-defined].
                if langfuse_sync_service is not None:  # type: ignore[name-defined]
                    try:
                        from code_indexer.server.services.langfuse_readme_generator import (
                            LangfuseReadmeGenerator,
                        )

                        data_dir = Path(server_data_dir) / "data"
                        gen = LangfuseReadmeGenerator()
                        for (
                            repo_folder,
                            session_ids,
                        ) in langfuse_sync_service._last_modified_sessions_by_repo.items():  # type: ignore[name-defined]
                            repo_path = data_dir / "golden-repos" / repo_folder
                            gen.generate_for_repo(repo_path, session_ids)
                    except Exception as readme_err:
                        logger.error(
                            format_error_log(
                                "APP-GENERAL-030",
                                f"Failed to generate Langfuse READMEs after sync: {readme_err}",
                                exc_info=True,
                                extra={"correlation_id": get_correlation_id()},
                            )
                        )

                elapsed = time.monotonic() - _callback_start
                logger.info(f"_on_sync_complete took {elapsed:.2f}s")

            # Create service with config_getter callable
            langfuse_sync_service = LangfuseTraceSyncService(  # type: ignore[name-defined]
                config_getter=config_service.get_config,
                data_dir=str(Path(server_data_dir) / "data"),
                on_sync_complete=_on_langfuse_sync_complete,
                refresh_scheduler=(
                    global_lifecycle_manager.refresh_scheduler
                    if global_lifecycle_manager
                    else None
                ),
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
                    # Bug #580: Don't start immediately -- leader election
                    # callbacks handle start/stop so only the leader runs this.
                    _cluster_services.append(("reconciliation", _reconciliation))

                    # Bug #582: Distributed job worker for reclaimed jobs
                    from code_indexer.server.services.distributed_job_claimer import (
                        DistributedJobClaimer,
                    )
                    from code_indexer.server.services.distributed_job_worker import (
                        DistributedJobWorkerService,
                    )

                    _job_claimer = DistributedJobClaimer(
                        pool=_cluster_pool, node_id=_node_id
                    )
                    _refresh_sched = (
                        global_lifecycle_manager.refresh_scheduler
                        if global_lifecycle_manager is not None
                        else None
                    )
                    _dist_worker = DistributedJobWorkerService(
                        claimer=_job_claimer,
                        refresh_scheduler=_refresh_sched,
                    )
                    _cluster_services.append(("dist_job_worker", _dist_worker))

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
                    # Story #923: wire ElevatedSessionManager cluster pool
                    from code_indexer.server.auth.elevated_session_manager import (
                        elevated_session_manager,
                    )

                    elevated_session_manager.set_connection_pool(_cluster_pool)
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

                    # Bug #583: Wire cluster pool for token blacklist
                    from code_indexer.server.app import get_token_blacklist

                    get_token_blacklist().set_connection_pool(_cluster_pool)

                    # Bug #577: Wire DelegationJobTracker for cluster
                    from code_indexer.server.services.delegation_job_tracker import (
                        DelegationJobTracker,
                    )

                    DelegationJobTracker.get_instance().set_connection_pool(
                        _cluster_pool
                    )

                    # Bug #581: Sync SSH keys from PG to local ~/.ssh/
                    try:
                        from code_indexer.server.services.ssh_key_sync_service import (
                            SSHKeySyncService,
                        )

                        _ssh_backend = (
                            backend_registry.ssh_keys
                            if backend_registry is not None
                            else None
                        )
                        assert _ssh_backend is not None
                        _ssh_sync = SSHKeySyncService(ssh_keys_backend=_ssh_backend)
                        _sync_result = _ssh_sync.sync()
                        logger.info(
                            "Bug #581: SSH key sync complete: %d written, %d removed, %d unchanged",
                            len(_sync_result.get("written", [])),
                            len(_sync_result.get("removed", [])),
                            len(_sync_result.get("unchanged", [])),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Bug #581: SSH key sync failed (non-fatal): %s", exc
                        )

                    # Bug #586: Sync API keys to local files on config change
                    def _on_config_change(new_config: Any) -> None:
                        try:
                            from code_indexer.server.services.api_key_management import (
                                ApiKeySyncService,
                            )

                            sync_svc = ApiKeySyncService()
                            ci = new_config.claude_integration_config
                            if ci and ci.anthropic_api_key:
                                sync_svc.sync_anthropic_key(ci.anthropic_api_key)
                            if ci and ci.voyage_api_key:
                                sync_svc.sync_voyageai_key(ci.voyage_api_key)
                        except Exception:
                            logger.debug(
                                "Bug #586: API key sync on config change failed",
                                exc_info=True,
                            )

                        # Bug #943 Fix #2: hot-reload elevation timeouts into the live
                        # singleton so PG config-poll changes take effect without restart.
                        try:
                            from code_indexer.server.auth.elevated_session_manager import (
                                elevated_session_manager as _esm,
                            )

                            _esm.update_timeouts(
                                new_config.elevation_idle_timeout_seconds,
                                new_config.elevation_max_age_seconds,
                            )
                        except Exception:
                            logger.warning(
                                "Bug #943: elevation timeout hot-reload on config change failed",
                                exc_info=True,
                            )

                    from code_indexer.server.services.config_service import (
                        get_config_service as _get_cs,
                    )

                    _get_cs().register_on_change_callback(_on_config_change)

                    # Bug #579: PG advisory locks for refresh token rotation
                    from code_indexer.server.app import refresh_token_manager

                    if refresh_token_manager is not None and hasattr(
                        refresh_token_manager, "set_connection_pool"
                    ):
                        refresh_token_manager.set_connection_pool(_cluster_pool)

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

                    # Bug #580: Gate housekeeping services behind leader election.
                    # Only the leader node should run JobReconciliationService
                    # and SelfMonitoringService to avoid redundant work and conflicts.
                    def _on_become_leader():
                        logger.info(
                            "Bug #580: Node became leader -- starting housekeeping services",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        _reconciliation.start()
                        _dist_worker.start()
                        if self_monitoring_service is not None:
                            self_monitoring_service.start()

                    def _on_lose_leadership():
                        logger.info(
                            "Bug #580: Node lost leadership -- stopping housekeeping services",
                            extra={"correlation_id": get_correlation_id()},
                        )
                        try:
                            _reconciliation.stop()
                        except Exception as e:
                            logger.warning(
                                f"Bug #580: Failed to stop reconciliation service: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        try:
                            _dist_worker.stop()
                        except Exception as e:
                            logger.warning(
                                f"Bug #582: Failed to stop distributed job worker: {e}",
                                extra={"correlation_id": get_correlation_id()},
                            )
                        if self_monitoring_service is not None:
                            try:
                                self_monitoring_service.stop()
                            except Exception as e:
                                logger.warning(
                                    f"Bug #580: Failed to stop self-monitoring service: {e}",
                                    extra={"correlation_id": get_correlation_id()},
                                )

                    _leader_election._on_become_leader = _on_become_leader
                    _leader_election._on_lose_leadership = _on_lose_leadership

                    # If already leader (won election during start_monitoring),
                    # start the housekeeping services now.
                    if _leader_election.is_leader:
                        _on_become_leader()

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

        # Story #746: wire fault injection harness from bootstrap config
        _apply_fault_injection_state(app, startup_config)

        # Bug #878 Fix A.2: start the DatabaseConnectionManager cleanup daemon.
        # The former get_connection() piggyback trigger was removed because it
        # lost races against thread churn (RC-3).  A dedicated wall-clock
        # daemon now sweeps stale connections across ALL registered instances
        # on a fixed cadence, decoupled from request/query traffic.
        try:
            DatabaseConnectionManager.start_cleanup_daemon()
            logger.info(
                "DatabaseConnectionManager cleanup daemon started (Bug #878 Fix A.2)",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-034",
                    f"Failed to start DatabaseConnectionManager cleanup daemon: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        yield  # Server is now running

        # Prevent test pollution across repeated TestClient/lifespan cycles:
        # the FastAPI lifespan instantiates a real TOTPService and stores it in
        # the mfa_routes._totp_service singleton on startup. Without this clear,
        # subsequent lifespan cycles in the same Python process would inherit
        # the stale singleton (db_path may point to a deleted SQLite file).
        from code_indexer.server.web.mfa_routes import set_totp_service

        set_totp_service(None)

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

        # Shutdown: Stop DependencyLatencyTracker background flush (Story #680)
        if latency_tracker is not None:
            try:
                latency_tracker.shutdown()
                logger.info(
                    "DependencyLatencyTracker stopped successfully",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:  # broad catch intentional: shutdown must not abort remaining cleanup chain
                logger.error(
                    format_error_log(
                        "APP-GENERAL-027",
                        f"Error stopping DependencyLatencyTracker: {e}",
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

        # Shutdown: Stop activated reaper scheduler (Story #967)
        activated_reaper_scheduler_state = getattr(
            app.state, "activated_reaper_scheduler", None
        )
        if activated_reaper_scheduler_state is not None:
            try:
                activated_reaper_scheduler_state.stop()
                logger.info(
                    "Activated reaper scheduler stopped",
                    extra={"correlation_id": get_correlation_id()},
                )
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-037",
                        f"Error stopping activated reaper scheduler: {e}",
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

        # Codex lease lifecycle shutdown (Story #846 CRIT-1)
        _codex_hook = getattr(app.state, "codex_shutdown_hook", None)
        if _codex_hook is not None:
            try:
                _codex_hook()
                logger.info("Codex lease lifecycle stopped")
            except Exception as exc:
                logger.error("Error stopping Codex lease lifecycle: %s", exc)

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

        # Bug #878 Fix A.2: stop the DatabaseConnectionManager cleanup daemon.
        # Symmetric with the startup call; ensures the daemon thread joins
        # cleanly within DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS so the process
        # exits promptly and does not leak a background sweep thread.
        try:
            DatabaseConnectionManager.stop_cleanup_daemon()
            logger.info(
                "DatabaseConnectionManager cleanup daemon stopped (Bug #878 Fix A.2)",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-035",
                    f"Error stopping DatabaseConnectionManager cleanup daemon: {e}",
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
