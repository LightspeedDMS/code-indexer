from code_indexer.server.middleware.correlation import get_correlation_id, set_correlation_id

"""
FastAPI application for CIDX Server.

Multi-user semantic code search server with JWT authentication and role-based access control.
"""

from contextlib import asynccontextmanager
from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Response,
    Request,
    Query,
    Body,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from typing import Dict, Any, Optional, List, Callable, Literal, Union
import os
import json
from pathlib import Path
import psutil
import logging
import requests  # type: ignore
from datetime import datetime, timezone

# Initialize logger for server module
logger = logging.getLogger(__name__)

from .auth.jwt_manager import JWTManager
from .auth.user_manager import UserManager, UserRole, SSOPasswordChangeError
from .auth import dependencies
from .auth.password_validator import (
    validate_password_complexity,
    get_password_complexity_error_message,
)
from .auth.rate_limiter import password_change_rate_limiter, refresh_token_rate_limiter
from .auth.audit_logger import password_audit_logger
from .auth.session_manager import session_manager
from .auth.timing_attack_prevention import timing_attack_prevention
from .auth.concurrency_protection import (
    password_change_concurrency_protection,
    ConcurrencyConflictError,
)
from .auth.auth_error_handler import auth_error_handler, AuthErrorType
from code_indexer.server.logging_utils import format_error_log
from .utils.jwt_secret_manager import JWTSecretManager
from .middleware.error_handler import GlobalErrorHandler
from .repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepoError,
    GitOperationError,
)
from .repositories.background_jobs import BackgroundJobManager
from .repositories.activated_repo_manager import (
    ActivatedRepoManager,
    ActivatedRepoError,
)
from .repositories.repository_listing_manager import (
    RepositoryListingManager,
    RepositoryListingError,
)
from .query.semantic_query_manager import (
    SemanticQueryManager,
    SemanticQueryError,
)
from .auth.refresh_token_manager import RefreshTokenManager
from .auth.oauth.routes import router as oauth_router
from .mcp.protocol import mcp_router
from .global_routes.routes import router as global_routes_router
from .global_routes.git_settings import router as git_settings_router
from .web import web_router, user_router, login_router, api_router, init_session_manager
from .web.repo_category_routes import repo_category_web_router
from .web.dependency_map_routes import dependency_map_router
from .routers.ssh_keys import router as ssh_keys_router
from .routers.scip_queries import router as scip_queries_router
from .routers.files import router as files_router
from .routers.git import router as git_router
from .routers.indexing import router as indexing_router
from .routers.cache import router as cache_router
from .routers.delegation_callbacks import router as delegation_callbacks_router
from .routers.maintenance_router import router as maintenance_router
from .routers.api_keys import router as api_keys_router
from .routers.diagnostics import router as diagnostics_router
from .routers.research_assistant import router as research_assistant_router
from .routers.repository_health import router as repository_health_router
from .routers.activated_repos import router as activated_repos_router
from .routers.llm_creds import router as llm_creds_router
from .routers.debug_routes import debug_router
from .services.maintenance_service import get_maintenance_state
from .routers.groups import (
    router as groups_router,
    users_router,
    audit_router,
    set_group_manager,
)
from .routers.repo_categories import (
    router as repo_categories_router,
    set_category_service,
)
from .routes.multi_query_routes import router as multi_query_router
from .routes.scip_multi_routes import router as scip_multi_router

# TEMP: Commented for testing bug #751 - cicd has circular import
# from .routes.cicd import router as cicd_router
from .models.branch_models import BranchListResponse
from .models.activated_repository import ActivatedRepository
from .services.branch_service import BranchService
from code_indexer.services.git_topology_service import GitTopologyService
from .validators.composite_repo_validator import CompositeRepoValidator
from .models.api_models import (
    RepositoryStatsResponse,
    FileListQueryParams,
    SemanticSearchRequest,
    SemanticSearchResponse,
    HealthCheckResponse,
    RepositoryStatusSummary,
    ActivatedRepositorySummary,
    AvailableRepositorySummary,
    RecentActivity,
    TemporalIndexOptions,
)
from .models.repository_discovery import (
    RepositoryDiscoveryResponse,
)
from .services.repository_discovery_service import RepositoryDiscoveryError
from .services.stats_service import stats_service
from .services.file_service import file_service
from .services.search_service import search_service
from .services.health_service import health_service
from .services.sqlite_log_handler import SQLiteLogHandler
from .services.workspace_cleanup_service import WorkspaceCleanupService
from .managers.composite_file_listing import _list_composite_files


# Constants for job operations and status
GOLDEN_REPO_ADD_OPERATION = "add_golden_repo"
GOLDEN_REPO_REFRESH_OPERATION = "refresh_golden_repo"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"


def _detect_repo_root(start_from_file: bool = True) -> Optional[Path]:
    """
    Detect git repository root directory.

    Tries three strategies in order:
    1. CIDX_REPO_ROOT environment variable (explicit configuration from systemd service)
    2. Walk up from __file__ location (works for development/source installations)
    3. Walk up from current working directory (works for pip-installed packages)

    Args:
        start_from_file: If True, try __file__ location first. For testing, can be False.

    Returns:
        Path to repository root if found, None otherwise.

    Bug Fix: MONITOR-GENERAL-011 - Use explicit CIDX_REPO_ROOT env var for production
    servers to eliminate detection ambiguity.
    """
    repo_root = None

    # Strategy 1: Check CIDX_REPO_ROOT environment variable (set by systemd service)
    # This is the most reliable method for production deployments
    env_repo_root = os.environ.get("CIDX_REPO_ROOT")
    if env_repo_root:
        candidate = Path(env_repo_root).resolve()
        if (candidate / ".git").exists():
            logger.info(
                f"Self-monitoring: Detected repo root from CIDX_REPO_ROOT env var: {candidate}",
                extra={"correlation_id": get_correlation_id()},
            )
            return candidate
        else:
            logger.warning(
                f"Self-monitoring: CIDX_REPO_ROOT set to '{env_repo_root}' but no .git found",
                extra={"correlation_id": get_correlation_id()},
            )

    # Strategy 2: Try __file__-based detection (development/source installations)
    if start_from_file:
        current = Path(__file__).resolve().parent
        while current != current.parent:
            if (current / ".git").exists():
                repo_root = current
                logger.info(
                    f"Self-monitoring: Detected repo root from __file__: {repo_root}",
                    extra={"correlation_id": get_correlation_id()},
                )
                break
            current = current.parent

    # Strategy 3: Fallback to cwd (pip-installed packages on production)
    # If systemd service runs from cloned repo directory, cwd will have .git
    if not repo_root:
        cwd = Path.cwd()
        current = cwd
        while current != current.parent:
            if (current / ".git").exists():
                repo_root = current
                logger.info(
                    f"Self-monitoring: Detected repo root from cwd: {repo_root}",
                    extra={"correlation_id": get_correlation_id()},
                )
                break
            current = current.parent

    return repo_root


# Pydantic models imported from domain submodules (Story #409: app.py modularization)
# Re-exported here for backward compatibility with existing tests and callers.
from .models.auth import (
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    UserInfo,
    CreateUserRequest,
    UpdateUserRequest,
    ChangePasswordRequest,
    UserResponse,
    MessageResponse,
    RegistrationRequest,
    PasswordResetRequest,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    ApiKeyListResponse,
    CreateMCPCredentialRequest,
    CreateMCPCredentialResponse,
    MCPCredentialListResponse,
)
from .models.repos import (
    AddGoldenRepoRequest,
    GoldenRepoInfo,
    ActivateRepositoryRequest,
    ActivatedRepositoryInfo,
    SwitchBranchRequest,
    RepositoryInfo,
    RepositoryDetailsResponse,
    RepositoryListResponse,
    AvailableRepositoryListResponse,
    RepositorySyncResponse,
    BranchInfo,
    RepositoryBranchesResponse,
    RepositoryStatistics,
    GitInfo,
    RepositoryConfiguration,
    RepositoryDetailsV2Response,
    ComponentRepoInfo,
    CompositeRepositoryDetails,
    RepositorySyncRequest,
    SyncProgress,
    SyncJobOptions,
    RepositorySyncJobResponse,
    GeneralRepositorySyncRequest,
)
from .models.jobs import (
    AddIndexRequest,
    AddIndexResponse,
    IndexInfo,
    IndexStatusResponse,
    JobResponse,
    JobStatusResponse,
    JobListResponse,
    JobCancellationResponse,
    JobCleanupResponse,
)
from .models.query import (
    SemanticQueryRequest,
    QueryMetadata,
    SemanticQueryResponse,
    FTSResultItem,
    UnifiedSearchMetadata,
    UnifiedSearchResponse,
)
# Re-export QueryResultItem for backward compatibility
from .models.api_models import QueryResultItem


# Global managers (initialized in create_app)
jwt_manager: Optional[JWTManager] = None
user_manager: Optional[UserManager] = None
refresh_token_manager: Optional[RefreshTokenManager] = None
golden_repo_manager: Optional[GoldenRepoManager] = None
background_job_manager: Optional[BackgroundJobManager] = None
job_tracker: Optional[Any] = None  # Story #311: JobTracker instance (Epic #261 Story 1B)
activated_repo_manager: Optional[ActivatedRepoManager] = None
repository_listing_manager: Optional[RepositoryListingManager] = None
semantic_query_manager: Optional[SemanticQueryManager] = None
workspace_cleanup_service: Optional[WorkspaceCleanupService] = None
langfuse_sync_service: Optional[Any] = None  # Story #168: Langfuse trace sync service

# Server startup time: authoritative location is app_helpers._server_start_time
# Use set_server_start_time() / get_server_start_time() from app_helpers to access it.

# Server-wide HNSW cache (Story #526)
_server_hnsw_cache: Optional[Any] = None

# Server-wide FTS cache
_server_fts_cache: Optional[Any] = None


# AC2/Story #409: Helper functions extracted to app_helpers.py (breaks circular import)
# Re-exported here for backward compatibility with any existing imports from app.
from .app_helpers import (
    set_server_start_time,
    get_server_uptime,
    get_server_start_time,
    get_system_resources,
    check_database_health,
    get_recent_errors,
    _apply_rest_semantic_truncation,
    _apply_rest_fts_truncation,
    _execute_repository_sync,
    _find_activated_repository,
    _analyze_component_repo,
    _get_composite_details,
)

# Token blacklist for logout functionality (Story #491) - MODULE LEVEL
token_blacklist: set[str] = set()


def blacklist_token(jti: str) -> None:
    """Add token JTI to blacklist."""
    token_blacklist.add(jti)


def is_token_blacklisted(jti: str) -> bool:
    """Check if token JTI is blacklisted."""
    return jti in token_blacklist


def migrate_legacy_cidx_meta(golden_repo_manager, golden_repos_dir: str) -> None:
    """
    Migrate cidx-meta from legacy special-case to regular golden repo.

    This is a REGISTRY-ONLY migration. No file movement occurs because:
    - Versioning is for auto-refresh (cidx-meta has no remote)
    - cidx-meta stays at golden-repos/cidx-meta/ (NOT moved to .versioned/)

    Legacy detection scenarios:
    1. cidx-meta directory exists BUT not in metadata.json -> register it
    2. cidx-meta exists in metadata.json with repo_url=None -> update to local://cidx-meta

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"

    # Scenario 1: Directory exists but NOT registered in metadata.json
    if cidx_meta_path.exists() and not golden_repo_manager.golden_repo_exists(
        "cidx-meta"
    ):
        logger.info(
            "Detected legacy cidx-meta (directory exists, not in metadata.json)",
            extra={"correlation_id": get_correlation_id()},
        )
        logger.info(
            "Migrating to regular golden repo (registry-only, no file movement)",
            extra={"correlation_id": get_correlation_id()},
        )

        # Use standard registration path (Story #175)
        golden_repo_manager.register_local_repo(
            alias="cidx-meta",
            folder_path=cidx_meta_path,
            fire_lifecycle_hooks=False,  # ClaudeCliManager not initialized at startup
        )
        logger.info(
            "Legacy cidx-meta migrated via register_local_repo",
            extra={"correlation_id": get_correlation_id()},
        )

    # Scenario 2: Registered with repo_url=None (old special marker)
    elif golden_repo_manager.golden_repo_exists("cidx-meta"):
        repo = golden_repo_manager.get_golden_repo("cidx-meta")
        if repo and repo.repo_url is None:
            logger.info(
                "Detected legacy cidx-meta (repo_url=None in metadata.json)",
                extra={"correlation_id": get_correlation_id()},
            )

            # Update repo_url from None to local://cidx-meta
            repo.repo_url = "local://cidx-meta"
            # Persist to storage backend (SQLite)
            golden_repo_manager._sqlite_backend.update_repo_url(
                "cidx-meta", "local://cidx-meta"
            )
            logger.info(
                "Legacy cidx-meta migrated: repo_url updated to local://cidx-meta",
                extra={"correlation_id": get_correlation_id()},
            )


def bootstrap_cidx_meta(golden_repo_manager, golden_repos_dir: str) -> None:
    """
    Bootstrap cidx-meta as a regular golden repo on fresh installations.

    Creates cidx-meta directory, registers it with local://cidx-meta URL,
    and initializes the CIDX index structure.
    This is idempotent - safe to call multiple times.

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    # Import dependencies
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
    from datetime import datetime, timezone
    import subprocess

    # Create directory structure
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    cidx_meta_path.mkdir(parents=True, exist_ok=True)

    # Check if cidx-meta already exists
    already_registered = golden_repo_manager.golden_repo_exists("cidx-meta")

    if not already_registered:
        logger.info(
            "Bootstrapping cidx-meta as regular golden repo",
            extra={"correlation_id": get_correlation_id()},
        )

        # Initialize CIDX index structure if not already done
        if not (cidx_meta_path / ".code-indexer").exists():
            try:
                logger.info(
                    "Initializing cidx-meta index structure",
                    extra={"correlation_id": get_correlation_id()},
                )
                subprocess.run(
                    ["cidx", "init"],
                    cwd=str(cidx_meta_path),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info(
                    "Successfully initialized cidx-meta index structure",
                    extra={"correlation_id": get_correlation_id()},
                )
            except subprocess.CalledProcessError as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-004",
                        f"Failed to initialize cidx-meta: {e.stderr if e.stderr else str(e)}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Continue with registration even if init fails - don't break server startup
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-005",
                        f"Unexpected error during cidx-meta initialization: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Continue with registration even if init fails - don't break server startup

        # Use standard registration path (Story #175)
        golden_repo_manager.register_local_repo(
            alias="cidx-meta",
            folder_path=cidx_meta_path,
            fire_lifecycle_hooks=False,  # ClaudeCliManager not initialized at startup
        )
        logger.info(
            "Bootstrapped cidx-meta via register_local_repo",
            extra={"correlation_id": get_correlation_id()},
        )


def register_langfuse_golden_repos(golden_repo_manager: "GoldenRepoManager", golden_repos_dir: str) -> None:
    """
    Register any unregistered Langfuse trace folders as golden repos.

    Scans golden-repos/ for langfuse_* directories and registers them
    using register_local_repo() standard path. Idempotent.

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    golden_repos_path = Path(golden_repos_dir)
    if not golden_repos_path.exists():
        return

    for folder in sorted(golden_repos_path.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("langfuse_"):
            continue

        alias = folder.name

        # Use standard registration path (Story #175)
        newly_registered = golden_repo_manager.register_local_repo(
            alias=alias,
            folder_path=folder,
            fire_lifecycle_hooks=False,  # No cidx-meta description for trace folders
        )

        if newly_registered:
            logger.info(
                f"Auto-registered Langfuse folder as golden repo: {alias}",
                extra={"correlation_id": get_correlation_id()},
            )


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.

    Returns:
        Configured FastAPI app
    """
    global jwt_manager, user_manager, refresh_token_manager, golden_repo_manager, background_job_manager, job_tracker, activated_repo_manager, repository_listing_manager, semantic_query_manager, _server_hnsw_cache, _server_fts_cache

    # Story #526: Initialize server-side HNSW cache at bootstrap for 1800x performance
    # Import and initialize global cache instance
    from .cache import get_global_cache, get_global_fts_cache

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
    from .services.system_metrics_collector import get_system_metrics_collector
    from .cache import get_total_index_memory_mb
    metrics_collector = get_system_metrics_collector()
    metrics_collector.set_index_memory_provider(get_total_index_memory_mb)
    logger.info(
        "Index memory provider registered with system metrics collector",
        extra={"correlation_id": get_correlation_id()},
    )

    # Initialize exception logger EARLY for server mode
    from ..utils.exception_logger import ExceptionLogger

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

    # Define lifespan context manager for startup/shutdown events
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
            api_metrics_service.initialize(str(metrics_db_path))

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

            groups_db_path = Path(server_data_dir) / "groups.db"
            group_manager = GroupAccessManager(groups_db_path)
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

            audit_service = AuditLogService(groups_db_path)
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
                migrated, skipped = migrate_flat_file_to_sqlite(flat_file, audit_service)
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
                db_path=cache_db_path, config=payload_cache_config
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
            if claude_config.llm_creds_provider_url and claude_config.llm_creds_provider_api_key:
                from code_indexer.server.services.llm_creds_client import LlmCredsClient
                from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager
                from code_indexer.server.services.claude_credentials_file_manager import ClaudeCredentialsFileManager
                from code_indexer.server.services.llm_lease_lifecycle import LlmLeaseLifecycleService

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
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")

            # Create scheduler instance
            meta_dir = Path(golden_repos_dir) / "cidx-meta"
            description_refresh_scheduler = DescriptionRefreshScheduler(
                db_path=db_path,
                config_manager=config_service,
                claude_cli_manager=get_claude_cli_manager(),
                meta_dir=meta_dir,
                analysis_model=server_config.golden_repos_config.analysis_model if server_config.golden_repos_config else "opus",
                job_tracker=job_tracker,
            )

            # Inject into meta_description_hook for tracking on repo add/remove
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
            from code_indexer.server.storage.sqlite_backends import (
                DependencyMapTrackingBackend,
            )
            from code_indexer.server.services.config_service import get_config_service

            # Get dependencies
            config_service = get_config_service()
            server_config = config_service.get_config()
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
            golden_repos_manager = golden_repo_manager

            # Create tracking backend
            tracking_backend = DependencyMapTrackingBackend(db_path)
            tracking_backend.cleanup_stale_status_on_startup()

            # Bug #383: Clean stale staging directory on server startup.
            # A prior crashed analysis may have left dependency-map.staging/ behind.
            # RefreshScheduler would index it and bake it into versioned snapshots,
            # polluting semantic search. Remove it before the scheduler starts.
            try:
                staging_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map.staging"
                if staging_dir.exists():
                    import shutil as _shutil
                    _shutil.rmtree(staging_dir)
                    logger.info(
                        "Cleaned stale dependency-map.staging directory on startup (Bug #383)",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except Exception as _staging_err:
                logger.debug(f"Staging dir startup cleanup failed (non-fatal): {_staging_err}")

            # Create analyzer
            cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
            analyzer = DependencyMapAnalyzer(
                golden_repos_root=Path(golden_repos_dir),
                cidx_meta_path=cidx_meta_path,
                pass_timeout=server_config.claude_integration_config.dependency_map_pass_timeout_seconds,
                mcp_registration_service=mcp_registration_service,
                analysis_model=server_config.golden_repos_config.analysis_model if server_config.golden_repos_config else "opus",
            )

            # Create service
            dependency_map_service = DependencyMapService(
                golden_repos_manager=golden_repos_manager,
                config_manager=config_service,
                tracking_backend=tracking_backend,
                analyzer=analyzer,
                refresh_scheduler=global_lifecycle_manager.refresh_scheduler if global_lifecycle_manager else None,
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
            db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")
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

                    token_manager = CITokenManager(
                        server_dir_path=server_data_dir,
                        use_sqlite=True,
                        db_path=db_path,
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
                        prompt_template=sm_config.prompt_template,
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
            from code_indexer.server.utils.config_manager import ServerConfigManager
            from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
            from code_indexer.server.auth.oidc.state_manager import StateManager

            config_manager = ServerConfigManager(server_dir_path=server_data_dir)
            config = config_manager.load_config()
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
        langfuse_sync_service = None
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
            langfuse_sync_service = LangfuseTraceSyncService(
                config_getter=config_service.get_config,
                data_dir=str(Path(server_data_dir) / "data"),
                on_sync_complete=_on_langfuse_sync_complete,
                refresh_scheduler=global_lifecycle_manager.refresh_scheduler if global_lifecycle_manager else None,
                job_tracker=job_tracker,
            )

            # Start background sync if pull is enabled
            config = config_service.get_config()
            if config.langfuse_config and config.langfuse_config.pull_enabled:
                langfuse_sync_service.start()
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
            app.state.langfuse_sync_service = langfuse_sync_service

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

        yield  # Server is now running

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
                from code_indexer.global_repos.meta_description_hook import set_debouncer
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
        if langfuse_sync_service is not None:
            try:
                langfuse_sync_service.stop()
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

    # Create FastAPI app with metadata and lifespan
    app = FastAPI(
        title="CIDX Multi-User Server",
        description="Multi-user semantic code search server with JWT authentication",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Add CORS middleware for Claude.ai OAuth compatibility
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://claude.ai",
            "https://claude.com",
            "https://www.anthropic.com",
            "https://api.anthropic.com",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Add global error handler middleware
    global_error_handler = GlobalErrorHandler()
    app.add_middleware(GlobalErrorHandler)

    # Add correlation ID bridge middleware for OTEL tracing (Story #697)
    from code_indexer.server.telemetry.correlation_bridge import (
        CorrelationBridgeMiddleware,
    )

    app.add_middleware(CorrelationBridgeMiddleware)

    # Add exception handlers for validation errors that FastAPI catches before middleware
    @app.exception_handler(RequestValidationError)
    def validation_exception_handler(request: Request, exc: RequestValidationError):
        error_data = global_error_handler.handle_validation_error(exc, request)
        return global_error_handler._create_error_response(error_data)

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
    # Bug #83-2 Fix: Pass password_security_config to UserManager
    user_manager = UserManager(
        users_file_path=users_file_path,
        password_security_config=server_config.password_security,
        use_sqlite=True,
        db_path=str(db_path),
    )
    refresh_token_manager = RefreshTokenManager(jwt_manager=jwt_manager)

    # Initialize OAuth manager
    oauth_db_path = str(Path(server_data_dir) / "oauth.db")
    from .auth.oauth.oauth_manager import OAuthManager

    oauth_manager = OAuthManager(
        db_path=oauth_db_path, issuer=None, user_manager=user_manager
    )

    # Story #19: Initialize SCIP audit database eagerly at startup
    # This ensures scip_audit.db exists before health checks run,
    # preventing RED status on fresh installs.
    from .startup.database_init import initialize_scip_audit_database

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
    from .utils.config_manager import ServerConfigManager

    config_manager = ServerConfigManager(server_dir_path=server_data_dir)
    server_config = config_manager.load_config()
    if server_config is None:
        # Create default config if none exists
        server_config = config_manager.create_default_config()
        config_manager.save_config(server_config)

    # Apply environment variable overrides
    server_config = config_manager.apply_env_overrides(server_config)

    # Initialize managers with resource configuration
    data_dir = str(Path(server_data_dir) / "data")
    # Story #711: Use SQLite for golden repo metadata storage (db_path already defined above as Path)
    db_path_str = str(db_path)
    golden_repo_manager = GoldenRepoManager(
        data_dir=data_dir,
        resource_config=server_config.resource_config,
        db_path=db_path_str,
    )
    # Story #311: Instantiate JobTracker before BackgroundJobManager (Epic #261 Story 1B)
    from code_indexer.server.services.job_tracker import (
        JobTracker as _JobTracker,
    )
    job_tracker = _JobTracker(db_path_str)
    job_tracker.cleanup_orphaned_jobs_on_startup()

    # Story #313: Inject job_tracker into ClaudeCliManager singleton
    try:
        from code_indexer.server.services.claude_cli_manager import get_claude_cli_manager
        _cli_manager = get_claude_cli_manager()
        if _cli_manager is not None:
            _cli_manager.set_job_tracker(job_tracker)
    except Exception:
        pass  # ClaudeCliManager may not be initialized yet

    # Initialize BackgroundJobManager with SQLite persistence (Bug fix: Jobs not showing in Dashboard)
    background_job_manager = BackgroundJobManager(
        resource_config=server_config.resource_config,
        use_sqlite=True,
        db_path=db_path_str,
        background_jobs_config=server_config.background_jobs_config,
        job_tracker=job_tracker,
        data_retention_config=server_config.data_retention_config,
    )
    # Inject BackgroundJobManager into GoldenRepoManager for async operations
    golden_repo_manager.background_job_manager = background_job_manager

    # Migration and bootstrap using the main golden_repo_manager instance
    try:
        migrate_legacy_cidx_meta(golden_repo_manager, golden_repo_manager.golden_repos_dir)
        bootstrap_cidx_meta(golden_repo_manager, golden_repo_manager.golden_repos_dir)
        register_langfuse_golden_repos(golden_repo_manager, golden_repo_manager.golden_repos_dir)
        logger.info(
            "cidx-meta migration and bootstrap completed",
            extra={"correlation_id": get_correlation_id()},
        )

        # Register cidx-meta-global as write exception (Story #197 AC1/AC4)
        from code_indexer.server.services.file_crud_service import file_crud_service
        cidx_meta_path = Path(golden_repo_manager.golden_repos_dir) / "cidx-meta"
        file_crud_service.register_write_exception("cidx-meta-global", cidx_meta_path)
        # Inject golden_repos_dir for write-mode marker lookup (Story #231)
        file_crud_service.set_golden_repos_dir(Path(golden_repo_manager.golden_repos_dir))
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
    repo_category_service = RepoCategoryService(db_path_str)

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
    global workspace_cleanup_service
    workspace_cleanup_service = WorkspaceCleanupService(
        config=server_config,
        job_manager=background_job_manager,
        workspace_root="/tmp",  # Standard temp directory for SCIP workspaces
    )

    # Store managers in app.state for access by routes
    app.state.golden_repo_manager = golden_repo_manager
    app.state.background_job_manager = background_job_manager
    app.state.activated_repo_manager = activated_repo_manager
    app.state.repository_listing_manager = repository_listing_manager
    app.state.semantic_query_manager = semantic_query_manager
    app.state.workspace_cleanup_service = workspace_cleanup_service

    # AC4: Attach typed AppState for dependency-injection access by routers
    from .app_state import AppState as _AppState
    _app_state = _AppState()
    _app_state.golden_repo_manager = golden_repo_manager
    _app_state.background_job_manager = background_job_manager
    _app_state.activated_repo_manager = activated_repo_manager
    _app_state.repository_listing_manager = repository_listing_manager
    _app_state.semantic_query_manager = semantic_query_manager
    _app_state.workspace_cleanup_service = workspace_cleanup_service
    app.state.app_state = _app_state

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

    # Set global dependencies
    dependencies.jwt_manager = jwt_manager
    dependencies.user_manager = user_manager
    dependencies.oauth_manager = oauth_manager
    dependencies.mcp_credential_manager = mcp_credential_manager

    # Seed initial admin user
    user_manager.seed_initial_admin()

    # AC2/AC4: Store ALL closure-captured variables on app.state so extracted
    # router modules can access them via request.app.state.XXX without closures.
    app.state.jwt_manager = jwt_manager
    app.state.user_manager = user_manager
    app.state.refresh_token_manager = refresh_token_manager
    app.state.oauth_manager = oauth_manager
    app.state.mcp_credential_manager = mcp_credential_manager
    app.state.mcp_registration_service = mcp_registration_service
    app.state.config_service = config_service
    app.state.server_config = server_config
    app.state.data_dir = data_dir
    app.state.db_path_str = db_path_str
    app.state.job_tracker = job_tracker

    # AC2: Route handlers extracted to routers/inline_routes.py (Story #409)
    from .routers.inline_routes import register_inline_routes
    register_inline_routes(
        app,
        background_job_manager=background_job_manager,
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=activated_repo_manager,
        repository_listing_manager=repository_listing_manager,
        semantic_query_manager=semantic_query_manager,
        workspace_cleanup_service=workspace_cleanup_service,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        refresh_token_manager=refresh_token_manager,
        oauth_manager=oauth_manager,
        mcp_credential_manager=mcp_credential_manager,
        mcp_registration_service=mcp_registration_service,
        config_service=config_service,
        server_config=server_config,
        data_dir=data_dir,
        db_path_str=db_path_str,
        job_tracker=job_tracker,
        secret_key=secret_key,
    )


    # Initialize self-monitoring app.state attributes to None (Bug #87 fix)
    # These will be updated during lifespan startup if self-monitoring is enabled
    # Ensures manual trigger route can always access these attributes
    app.state.self_monitoring_repo_root = None
    app.state.self_monitoring_github_repo = None

    return app


# Create app instance
app = create_app()  # ENABLED: Required for uvicorn to load the app
# Note: This was temporarily enabled for manual testing
