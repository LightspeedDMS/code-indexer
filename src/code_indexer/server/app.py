"""FastAPI application for CIDX Server — multi-user semantic code search with JWT auth."""

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

from .auth.jwt_manager import JWTManager
from .auth.user_manager import UserManager
from .auth.refresh_token_manager import RefreshTokenManager
from .repositories.golden_repo_manager import GoldenRepoManager
from .repositories.background_jobs import BackgroundJobManager
from .repositories.activated_repo_manager import ActivatedRepoManager
from .repositories.repository_listing_manager import RepositoryListingManager
from .query.semantic_query_manager import SemanticQueryManager
from .services.workspace_cleanup_service import WorkspaceCleanupService

# Constants for job operations and status
GOLDEN_REPO_ADD_OPERATION = "add_golden_repo"
GOLDEN_REPO_REFRESH_OPERATION = "refresh_golden_repo"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"

# Pydantic models — re-exported for backward compatibility with existing tests and callers.
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
job_tracker: Optional[Any] = None  # Story #311: JobTracker instance
activated_repo_manager: Optional[ActivatedRepoManager] = None
repository_listing_manager: Optional[RepositoryListingManager] = None
semantic_query_manager: Optional[SemanticQueryManager] = None
workspace_cleanup_service: Optional[WorkspaceCleanupService] = None
langfuse_sync_service: Optional[Any] = None  # Story #168: Langfuse trace sync service
_server_hnsw_cache: Optional[Any] = None  # Server-wide HNSW cache (Story #526)
_server_fts_cache: Optional[Any] = None   # Server-wide FTS cache

# Helper functions re-exported from app_helpers.py for backward compatibility.
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

# Story #409 AC5: Bootstrap helpers extracted to startup/bootstrap.py
# Re-exported here for backward compatibility.
from .startup.bootstrap import (
    _detect_repo_root,
    migrate_legacy_cidx_meta,
    bootstrap_cidx_meta,
    register_langfuse_golden_repos,
)

# Token blacklist for logout functionality (Story #491) - MODULE LEVEL
token_blacklist: set[str] = set()


def blacklist_token(jti: str) -> None:
    """Add token JTI to blacklist."""
    token_blacklist.add(jti)


def is_token_blacklisted(jti: str) -> bool:
    """Check if token JTI is blacklisted."""
    return jti in token_blacklist


def create_app():
    """
    Create and configure FastAPI application.

    Returns:
        Configured FastAPI app
    """
    global jwt_manager, user_manager, refresh_token_manager, golden_repo_manager
    global background_job_manager, job_tracker, activated_repo_manager
    global repository_listing_manager, semantic_query_manager
    global _server_hnsw_cache, _server_fts_cache, workspace_cleanup_service

    from .startup.service_init import initialize_services
    from .startup.lifespan import make_lifespan
    from .startup.app_wiring import create_fastapi_app
    from .auth import dependencies

    services = initialize_services()

    # Set module globals for backward compatibility
    jwt_manager = services["jwt_manager"]
    user_manager = services["user_manager"]
    refresh_token_manager = services["refresh_token_manager"]
    golden_repo_manager = services["golden_repo_manager"]
    background_job_manager = services["background_job_manager"]
    job_tracker = services["job_tracker"]
    activated_repo_manager = services["activated_repo_manager"]
    repository_listing_manager = services["repository_listing_manager"]
    semantic_query_manager = services["semantic_query_manager"]
    workspace_cleanup_service = services["workspace_cleanup_service"]
    _server_hnsw_cache = services["_server_hnsw_cache"]
    _server_fts_cache = services["_server_fts_cache"]

    lifespan = make_lifespan(
        background_job_manager=background_job_manager,
        job_tracker=job_tracker,
        golden_repo_manager=golden_repo_manager,
        mcp_registration_service=services["mcp_registration_service"],
        user_manager=user_manager,
        jwt_manager=jwt_manager,
        dependencies=dependencies,
        register_langfuse_golden_repos=register_langfuse_golden_repos,
    )

    app = create_fastapi_app(services, lifespan)
    return app


# Create app instance for uvicorn
app = create_app()
