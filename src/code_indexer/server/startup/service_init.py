"""
Service initialization for CIDX server startup.

Extracted from app.py as part of Story #409 AC5 (app.py modularization).
Contains the initialize_services() function that creates and wires all
server services, returning them as a dict for use by create_app().
"""

import atexit
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Bug #567: Module-level reference to PostgreSQL connection pool for atexit cleanup.
# When pytest crashes or is killed, atexit handlers run and close the pool,
# preventing idle connection accumulation that exhausts max_connections.
_postgres_pool_for_cleanup: Optional[Any] = None


def _cleanup_postgres_pool() -> None:
    """Close the PostgreSQL connection pool on process exit (Bug #567).

    Registered via atexit to ensure connections are released even when
    the process is killed or pytest crashes mid-run.
    """
    global _postgres_pool_for_cleanup
    if _postgres_pool_for_cleanup is not None:
        try:
            _postgres_pool_for_cleanup.close()
        except Exception:
            pass  # Best-effort cleanup on exit
        _postgres_pool_for_cleanup = None


def register_postgres_pool_atexit_cleanup(pool: Any) -> None:
    """Register a PostgreSQL connection pool for atexit cleanup (Bug #567).

    Args:
        pool: ConnectionPool instance to close on process exit.
    """
    global _postgres_pool_for_cleanup
    _postgres_pool_for_cleanup = pool
    atexit.register(_cleanup_postgres_pool)


def initialize_services() -> Dict[str, Any]:
    """
    Initialize all server services and return them as a dict.

    This function was extracted from create_app() to reduce app.py size.
    It creates all manager/service instances and returns them for use by
    create_app(), which sets module globals and wires them into the FastAPI app.

    Returns:
        Dict with all initialized service instances.
    """
    # Import all dependencies inside function to avoid circular imports
    from code_indexer.server.auth.jwt_manager import JWTManager
    from code_indexer.server.auth.user_manager import UserManager
    from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
    from code_indexer.server.utils.jwt_secret_manager import JWTSecretManager
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.server.repositories.repository_listing_manager import (
        RepositoryListingManager,
    )
    from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
    from code_indexer.server.services.workspace_cleanup_service import (
        WorkspaceCleanupService,
    )
    from code_indexer.server.app_helpers import set_server_start_time
    from code_indexer.server.startup.bootstrap import (
        migrate_legacy_cidx_meta,
        bootstrap_cidx_meta,
        register_langfuse_golden_repos,
    )
    from code_indexer.server.routers.repo_categories import set_category_service

    # Story #526: Initialize server-side HNSW cache at bootstrap for 1800x performance
    from code_indexer.server.cache import get_global_cache, get_global_fts_cache

    _server_hnsw_cache = get_global_cache()
    logger.info(
        f"HNSW index cache initialized (TTL: {_server_hnsw_cache.config.ttl_minutes}min)",
        extra={"correlation_id": get_correlation_id()},
    )

    # Initialize server-side FTS cache for FTS query performance
    _server_fts_cache = get_global_fts_cache()
    logger.info(
        f"FTS index cache initialized (TTL: {_server_fts_cache.config.ttl_minutes}min)",
        extra={"correlation_id": get_correlation_id()},
    )

    # Register index memory provider with metrics collector
    from code_indexer.server.services.system_metrics_collector import (
        get_system_metrics_collector,
    )
    from code_indexer.server.cache import get_total_index_memory_mb

    metrics_collector = get_system_metrics_collector()
    metrics_collector.set_index_memory_provider(get_total_index_memory_mb)
    logger.info(
        "Index memory provider registered with system metrics collector",
        extra={"correlation_id": get_correlation_id()},
    )

    # Initialize exception logger EARLY for server mode
    from code_indexer.utils.exception_logger import ExceptionLogger

    exception_logger = ExceptionLogger.initialize(
        project_root=Path.home(), mode="server"
    )
    exception_logger.install_thread_exception_hook()
    logger.info(
        "ExceptionLogger initialized for server mode",
        extra={"correlation_id": get_correlation_id()},
    )

    # Set server start time for health monitoring (stored in app_helpers module)
    set_server_start_time(datetime.now(timezone.utc).isoformat())

    # Bug #83 Fix: Load config to use jwt_expiration_minutes and password_security
    from code_indexer.server.services.config_service import get_config_service

    config_service = get_config_service()
    server_config = config_service.get_config()

    # Story #528: JWT secret manager init moved after storage mode detection below,
    # so it can use PostgreSQL for cluster-wide JWT secret sharing.

    # Initialize UserManager with server data directory support
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    Path(server_data_dir).mkdir(parents=True, exist_ok=True)
    users_file_path = str(Path(server_data_dir) / "users.json")
    # Compute db_path for SQLite storage (Story #702)
    db_path = Path(server_data_dir) / "data" / "cidx_server.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Initialize SQLite database schema before creating managers (Story #702)
    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    # Bug #583: Wire token blacklist with SQLite path for standalone mode
    from code_indexer.server.app import get_token_blacklist

    get_token_blacklist().set_sqlite_path(str(db_path))

    # Story #578: Initialize SQLite-backed runtime config (unified model).
    # Must happen after schema init (server_config table) and before PG pool.
    config_service.initialize_runtime_db(str(db_path))

    # Epic #408: Cluster mode — determine storage backend
    _storage_mode = "sqlite"
    _backend_registry = None
    data_dir = str(Path(server_data_dir) / "data")
    db_path_str = str(db_path)
    _raw_config: dict = {}
    try:
        import json as _json

        _config_path = Path(server_data_dir) / "config.json"
        if _config_path.exists():
            with open(_config_path) as _cf:
                _raw_config = _json.load(_cf)
            _storage_mode = _raw_config.get("storage_mode", "sqlite")
    except Exception:
        pass  # Default to sqlite on any config read error

    if _storage_mode == "postgres":
        try:
            _postgres_dsn = _raw_config.get("postgres_dsn", "")
            if not _postgres_dsn:
                raise ValueError("postgres_dsn required when storage_mode=postgres")
            from code_indexer.server.storage.factory import StorageFactory

            # Story #519 AC4: Auto-run SQL schema migrations before creating backends
            # Schema must exist before StorageFactory creates PG backend instances.
            try:
                from code_indexer.server.storage.postgres.migrations.runner import (
                    MigrationRunner,
                )

                _migration_runner = MigrationRunner(_postgres_dsn)
                _migrations_applied = _migration_runner.run()
                if _migrations_applied > 0:
                    logger.info(
                        f"Applied {_migrations_applied} SQL schema migration(s)",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except Exception as _mig_err:
                # AC5: Fail-fast — schema must be ready for PG mode
                logger.error(
                    f"FATAL: PostgreSQL schema migration failed: {_mig_err}. "
                    "Server cannot start in cluster mode without a valid schema.",
                    extra={"correlation_id": get_correlation_id()},
                )
                raise

            _backend_registry = StorageFactory.create_backends(
                config=_raw_config,
                data_dir=data_dir,
            )

            # Bug #567: Register atexit handler to close PostgreSQL connection pools
            # on process exit. Prevents connection leaks when pytest crashes or is
            # killed mid-run.
            if _backend_registry.connection_pool is not None:
                register_postgres_pool_atexit_cleanup(_backend_registry.connection_pool)
            # Bug #545: Also close the critical pool on exit.
            if _backend_registry.critical_connection_pool is not None:
                _critical = _backend_registry.critical_connection_pool
                atexit.register(lambda: _critical.close())

            logger.info(
                "Storage mode: PostgreSQL (cluster)",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as _e:
            # Bug #532: NEVER silently fall back to SQLite when postgres is
            # explicitly configured.  A cluster node running on local SQLite
            # would diverge from the shared PG state within minutes.
            logger.critical(
                f"FATAL: PostgreSQL configured but initialization failed: {_e}. "
                "Refusing to start — cluster mode requires a working PostgreSQL connection. "
                "Fix the connection or change storage_mode to 'sqlite' in config.json.",
                extra={"correlation_id": get_correlation_id()},
            )
            raise RuntimeError(
                f"PostgreSQL initialization failed (storage_mode=postgres): {_e}"
            ) from _e
    else:
        from code_indexer.server.storage.factory import StorageFactory

        _backend_registry = StorageFactory.create_backends(
            config={"storage_mode": "sqlite"},
            data_dir=data_dir,
        )
        logger.info(
            "Storage mode: SQLite (standalone)",
            extra={"correlation_id": get_correlation_id()},
        )

    # Bug #575: Wire session manager to DB backend (SQLite or PG) for cluster support.
    # The module-level singleton starts in JSON file mode; set_backend() switches it
    # to use the protocol-backed storage so password-change invalidations are visible
    # across all cluster nodes.
    if _backend_registry is not None and _backend_registry.sessions is not None:
        from code_indexer.server.auth.session_manager import (
            session_manager as _session_mgr,
        )

        _session_mgr.set_backend(_backend_registry.sessions)

    # Story #528: Initialize JWT secret with PG DSN for cluster-wide sharing.
    # In PG mode, JWT secret is stored in shared cluster_secrets table so
    # all nodes sign/verify tokens with the same key.
    _pg_dsn_for_jwt = (
        _raw_config.get("postgres_dsn") if _storage_mode == "postgres" else None
    )
    jwt_secret_manager = JWTSecretManager(pg_dsn=_pg_dsn_for_jwt)
    secret_key = jwt_secret_manager.get_or_create_secret()
    # Bug #83-1 Fix: Use config.jwt_expiration_minutes instead of hardcoded 10
    jwt_manager = JWTManager(
        secret_key=secret_key,
        token_expiration_minutes=server_config.jwt_expiration_minutes,
        algorithm="HS256",
    )

    # Bug #83-2 Fix: Pass password_security_config to UserManager
    user_manager = UserManager(
        users_file_path=users_file_path,
        password_security_config=server_config.password_security,
        use_sqlite=True,
        db_path=str(db_path),
        storage_backend=_backend_registry.users if _backend_registry else None,
    )
    refresh_token_manager = RefreshTokenManager(
        jwt_manager=jwt_manager,
        storage_backend=_backend_registry.refresh_tokens if _backend_registry else None,
    )

    # Initialize OAuth manager
    oauth_db_path = str(Path(server_data_dir) / "oauth.db")
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

    oauth_manager = OAuthManager(
        db_path=oauth_db_path,
        issuer=None,
        user_manager=user_manager,
        storage_backend=_backend_registry.oauth if _backend_registry else None,
    )

    # Story #19: Initialize SCIP audit database eagerly at startup
    # This ensures scip_audit.db exists before health checks run,
    # preventing RED status on fresh installs.
    from code_indexer.server.startup.database_init import initialize_scip_audit_database

    scip_audit_path = initialize_scip_audit_database(server_data_dir)
    if scip_audit_path:
        logger.info(
            f"SCIP audit database initialized: {scip_audit_path}",
            extra={"correlation_id": get_correlation_id()},
        )
    else:
        logger.warning(
            format_error_log(
                "APP-GENERAL-028",
                "SCIP audit database initialization failed (non-blocking)",
                extra={"correlation_id": get_correlation_id()},
            )
        )

    # Initialize SCIPAuditRepository with backend support for cluster mode
    from code_indexer.server.repositories.scip_audit import SCIPAuditRepository

    scip_audit_repository = SCIPAuditRepository(
        storage_backend=_backend_registry.scip_audit if _backend_registry else None,
    )

    # Load server configuration for resource limits and timeouts
    server_config = config_service.get_config()

    # Initialize managers with resource configuration
    # (data_dir and db_path_str already computed above for storage mode detection)

    golden_repo_manager = GoldenRepoManager(
        data_dir=data_dir,
        resource_config=server_config.resource_config,
        db_path=db_path_str,
        storage_backend=_backend_registry.golden_repo_metadata
        if _backend_registry
        else None,
    )
    # Story #311: Instantiate JobTracker before BackgroundJobManager (Epic #261 Story 1B)
    from code_indexer.server.services.job_tracker import JobTracker as _JobTracker

    job_tracker = _JobTracker(
        db_path_str,
        storage_backend=_backend_registry.background_jobs
        if _backend_registry
        else None,
    )
    job_tracker.cleanup_orphaned_jobs_on_startup()

    # Story #313: Inject job_tracker into ClaudeCliManager singleton
    try:
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
        )

        _cli_manager = get_claude_cli_manager()
        if _cli_manager is not None:
            _cli_manager.set_job_tracker(job_tracker)
    except Exception:
        pass  # ClaudeCliManager may not be initialized yet

    # Initialize BackgroundJobManager with SQLite persistence
    background_job_manager = BackgroundJobManager(
        resource_config=server_config.resource_config,
        use_sqlite=True,
        db_path=db_path_str,
        background_jobs_config=server_config.background_jobs_config,
        job_tracker=job_tracker,
        data_retention_config=server_config.data_retention_config,
        storage_backend=_backend_registry.background_jobs
        if _backend_registry
        else None,
    )
    # Inject BackgroundJobManager into GoldenRepoManager for async operations
    golden_repo_manager.background_job_manager = background_job_manager

    # Migration and bootstrap using the main golden_repo_manager instance
    try:
        migrate_legacy_cidx_meta(
            golden_repo_manager, golden_repo_manager.golden_repos_dir
        )
        bootstrap_cidx_meta(golden_repo_manager, golden_repo_manager.golden_repos_dir)
        register_langfuse_golden_repos(
            golden_repo_manager, golden_repo_manager.golden_repos_dir
        )
        logger.info(
            "cidx-meta migration and bootstrap completed",
            extra={"correlation_id": get_correlation_id()},
        )

        # Register cidx-meta-global as write exception (Story #197 AC1/AC4)
        from code_indexer.server.services.file_crud_service import file_crud_service

        cidx_meta_path = Path(golden_repo_manager.golden_repos_dir) / "cidx-meta"
        file_crud_service.register_write_exception("cidx-meta-global", cidx_meta_path)
        # Inject golden_repos_dir for write-mode marker lookup (Story #231)
        file_crud_service.set_golden_repos_dir(
            Path(golden_repo_manager.golden_repos_dir)
        )
        logger.info(
            "Registered cidx-meta-global as write exception for direct editing",
            extra={"correlation_id": get_correlation_id()},
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "APP-GENERAL-011",
                f"Failed to migrate/bootstrap cidx-meta on startup: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )

    activated_repo_manager = ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )

    # Inject ActivatedRepoManager for cascade deletion support
    golden_repo_manager.activated_repo_manager = activated_repo_manager

    # Inject RepoCategoryService for auto-assignment (Story #181)
    from code_indexer.server.services.repo_category_service import RepoCategoryService

    repo_category_service = RepoCategoryService(
        db_path_str,
        storage_backend=_backend_registry.repo_category if _backend_registry else None,
        golden_repo_backend=_backend_registry.golden_repo_metadata
        if _backend_registry
        else None,
    )

    # Wire RepoCategoryService to REST API router (Story #182)
    set_category_service(repo_category_service)
    golden_repo_manager._repo_category_service = repo_category_service

    repository_listing_manager = RepositoryListingManager(
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=activated_repo_manager,
    )
    semantic_query_manager = SemanticQueryManager(
        data_dir=data_dir,
        activated_repo_manager=activated_repo_manager,
        background_job_manager=background_job_manager,
    )

    # Initialize WorkspaceCleanupService for SCIP workspace cleanup (Story #647)
    workspace_cleanup_service = WorkspaceCleanupService(
        config=server_config,
        job_manager=background_job_manager,
        workspace_root="/tmp",  # Standard temp directory for SCIP workspaces
    )

    # Initialize MCP credential manager
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    mcp_credential_manager = MCPCredentialManager(user_manager=user_manager)

    # Initialize MCP self-registration service (Story #203)
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    mcp_registration_service = MCPSelfRegistrationService(
        config_manager=config_service,
        mcp_credential_manager=mcp_credential_manager,
    )

    return {
        "jwt_manager": jwt_manager,
        "user_manager": user_manager,
        "refresh_token_manager": refresh_token_manager,
        "oauth_manager": oauth_manager,
        "golden_repo_manager": golden_repo_manager,
        "background_job_manager": background_job_manager,
        "job_tracker": job_tracker,
        "activated_repo_manager": activated_repo_manager,
        "repository_listing_manager": repository_listing_manager,
        "semantic_query_manager": semantic_query_manager,
        "workspace_cleanup_service": workspace_cleanup_service,
        "mcp_credential_manager": mcp_credential_manager,
        "mcp_registration_service": mcp_registration_service,
        "config_service": config_service,
        "server_config": server_config,
        "data_dir": data_dir,
        "db_path_str": db_path_str,
        "secret_key": secret_key,
        "repo_category_service": repo_category_service,
        "storage_mode": _storage_mode,
        "backend_registry": _backend_registry,
        "_server_hnsw_cache": _server_hnsw_cache,
        "_server_fts_cache": _server_fts_cache,
        "scip_audit_repository": scip_audit_repository,
    }
