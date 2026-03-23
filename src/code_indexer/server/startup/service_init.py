"""
Service initialization for CIDX server startup.

Extracted from app.py as part of Story #409 AC5 (app.py modularization).
Contains the initialize_services() function that creates and wires all
server services, returning them as a dict for use by create_app().
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


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

    # Initialize authentication managers with persistent JWT secret
    jwt_secret_manager = JWTSecretManager()
    secret_key = jwt_secret_manager.get_or_create_secret()
    # Bug #83-1 Fix: Use config.jwt_expiration_minutes instead of hardcoded 10
    jwt_manager = JWTManager(
        secret_key=secret_key,
        token_expiration_minutes=server_config.jwt_expiration_minutes,
        algorithm="HS256",
    )

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

    # Epic #408: Cluster mode — determine storage backend
    _storage_mode = "sqlite"
    _backend_registry = None
    data_dir = str(Path(server_data_dir) / "data")
    db_path_str = str(db_path)
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

            _backend_registry = StorageFactory.create_backends(
                config=_raw_config,
                data_dir=data_dir,
            )
            logger.info(
                "Storage mode: PostgreSQL (cluster)",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as _e:
            logger.error(
                f"Failed to initialize PostgreSQL backends: {_e}. Falling back to SQLite.",
                extra={"correlation_id": get_correlation_id()},
            )
            _storage_mode = "sqlite"
            _backend_registry = None
    else:
        logger.info(
            "Storage mode: SQLite (standalone)",
            extra={"correlation_id": get_correlation_id()},
        )

    # Bug #83-2 Fix: Pass password_security_config to UserManager
    user_manager = UserManager(
        users_file_path=users_file_path,
        password_security_config=server_config.password_security,
        use_sqlite=True,
        db_path=str(db_path),
        storage_backend=_backend_registry.users if _backend_registry else None,
    )
    refresh_token_manager = RefreshTokenManager(jwt_manager=jwt_manager)

    # Initialize OAuth manager
    oauth_db_path = str(Path(server_data_dir) / "oauth.db")
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

    oauth_manager = OAuthManager(
        db_path=oauth_db_path, issuer=None, user_manager=user_manager
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

    # Load server configuration for resource limits and timeouts
    from code_indexer.server.utils.config_manager import ServerConfigManager

    config_manager = ServerConfigManager(server_dir_path=server_data_dir)
    server_config = config_manager.load_config()
    if server_config is None:
        # Create default config if none exists
        server_config = config_manager.create_default_config()
        config_manager.save_config(server_config)

    # Apply environment variable overrides
    server_config = config_manager.apply_env_overrides(server_config)

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

    job_tracker = _JobTracker(db_path_str)
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
    }
