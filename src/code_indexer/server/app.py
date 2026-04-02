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
from .models.api_models import QueryResultItem as QueryResultItem  # noqa: F401
from .models.query import SemanticQueryRequest as SemanticQueryRequest  # noqa: F401
from .models.query import SemanticQueryResponse as SemanticQueryResponse  # noqa: F401
from .models.repos import (
    ActivateRepositoryRequest as ActivateRepositoryRequest,
)  # noqa: F401
from .models.repos import AddGoldenRepoRequest as AddGoldenRepoRequest  # noqa: F401
from .models.repos import ComponentRepoInfo as ComponentRepoInfo  # noqa: F401
from .models.repos import (
    RepositoryDetailsResponse as RepositoryDetailsResponse,
)  # noqa: F401
from .models.jobs import AddIndexRequest as AddIndexRequest  # noqa: F401
from .models.auth import ChangePasswordRequest as ChangePasswordRequest  # noqa: F401


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
_server_fts_cache: Optional[Any] = None  # Server-wide FTS cache

# Module-level service singletons (imported for backward compat with handlers.py app_module pattern)
from .services.file_service import file_service as file_service  # noqa: F401

# Pydantic models re-exported for backward compatibility

# Helper functions re-exported from app_helpers.py for backward compatibility.
from .app_helpers import (  # noqa: F401
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
from .startup.bootstrap import (  # noqa: F401
    _detect_repo_root,
    migrate_legacy_cidx_meta,
    bootstrap_cidx_meta,
    register_langfuse_golden_repos,
)


# Bug #583: Token blacklist for logout — cluster-aware (DB-backed).
class TokenBlacklist:
    """Token blacklist for JWT logout. In-memory + optional DB backend."""

    def __init__(self) -> None:
        self._local: set = set()
        self._pool: Any = None
        self._sqlite_db_path: Optional[str] = None

    def set_connection_pool(self, pool: Any) -> None:
        self._pool = pool
        logging.getLogger(__name__).info(
            "TokenBlacklist: using PostgreSQL (cluster mode)"
        )

    def set_sqlite_path(self, db_path: str) -> None:
        self._sqlite_db_path = db_path

    def add(self, jti: str) -> None:
        self._local.add(jti)  # Always add to local for fast checks on same node
        if self._pool is not None:
            self._pg_add(jti)
        elif self._sqlite_db_path:
            self._sqlite_add(jti)

    def contains(self, jti: str) -> bool:
        # Check local first (fast path)
        if jti in self._local:
            return True
        # Check DB (cross-node)
        if self._pool is not None:
            return self._pg_contains(jti)
        elif self._sqlite_db_path:
            return self._sqlite_contains(jti)
        return False

    def _pg_add(self, jti: str) -> None:
        assert self._pool is not None
        import time

        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO token_blacklist (jti, blacklisted_at) "
                "VALUES (%s, %s) ON CONFLICT (jti) DO NOTHING",
                (jti, time.time()),
            )
            conn.commit()

    def _pg_contains(self, jti: str) -> bool:
        assert self._pool is not None
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = %s", (jti,)
            ).fetchone()
        return row is not None

    def _sqlite_add(self, jti: str) -> None:
        import sqlite3
        import time

        assert self._sqlite_db_path is not None
        conn = sqlite3.connect(self._sqlite_db_path)
        conn.execute(
            "INSERT OR IGNORE INTO token_blacklist (jti, blacklisted_at) VALUES (?, ?)",
            (jti, time.time()),
        )
        conn.commit()
        conn.close()

    def _sqlite_contains(self, jti: str) -> bool:
        import sqlite3

        assert self._sqlite_db_path is not None
        conn = sqlite3.connect(self._sqlite_db_path)
        row = conn.execute(
            "SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,)
        ).fetchone()
        conn.close()
        return row is not None


# Module-level singleton
_token_blacklist = TokenBlacklist()


def blacklist_token(jti: str) -> None:
    """Add token JTI to blacklist."""
    _token_blacklist.add(jti)


def is_token_blacklisted(jti: str) -> bool:
    """Check if token JTI is blacklisted."""
    return _token_blacklist.contains(jti)


def get_token_blacklist() -> TokenBlacklist:
    """Get the token blacklist singleton (for wiring)."""
    return _token_blacklist


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
        storage_mode=services.get("storage_mode", "sqlite"),
        backend_registry=services.get("backend_registry"),
    )

    app = create_fastapi_app(services, lifespan)
    return app


# Create app instance for uvicorn
app = create_app()
