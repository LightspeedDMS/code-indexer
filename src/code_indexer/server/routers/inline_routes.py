"""
Inline route handlers extracted from create_app() in app.py.

Part of Story #409 (app.py modularization) AC2: Route Handlers Extracted to routers/ Package.

All 63 route handler functions that were previously defined as closures inside
create_app() are wrapped in register_inline_routes(). They remain closures over
the function parameters - same pattern, different enclosing scope. Zero changes
to handler logic, paths, methods, response models, or error codes.

Usage in create_app():
    from .routers.inline_routes import register_inline_routes
    register_inline_routes(
        app,
        background_job_manager=background_job_manager,
        ...
    )
"""

# Standard library imports (missing after extraction from app.py)
import logging

# Exception classes missing after extraction from app.py closures

# Service singletons missing after extraction from app.py closures

# Helper function missing after extraction from app.py closures
from fastapi import (
    FastAPI,
)

# Import all model classes used by route handlers

# Import auth dependencies and utilities

# Import logging utilities

# Import services

# Import routers used in register_inline_routes (were module-level imports in app.py)
from ..auth.oauth.routes import router as oauth_router
from ..mcp.protocol import mcp_router
from ..global_routes.routes import router as global_routes_router
from ..global_routes.git_settings import router as git_settings_router
from ..routers.ssh_keys import router as ssh_keys_router
from ..routers.scip_queries import router as scip_queries_router
from ..routers.files import router as files_router
from ..routers.git import router as git_router
from ..routers.indexing import router as indexing_router
from ..routers.cache import router as cache_router
from ..routers.delegation_callbacks import router as delegation_callbacks_router
from ..routers.maintenance_router import router as maintenance_router
from ..routers.api_keys import router as api_keys_router
from ..routers.diagnostics import router as diagnostics_router
from ..routers.research_assistant import router as research_assistant_router
from ..web.mfa_routes import mfa_router
from ..routers.repository_health import router as repository_health_router
from ..routers.activated_repos import router as activated_repos_router
from ..routers.llm_creds import router as llm_creds_router
from ..routers.debug_routes import debug_router
from ..routers.groups import (
    router as groups_router,
    users_router,
    audit_router,
)
from ..routers.repo_categories import router as repo_categories_router
from ..routes.multi_query_routes import router as multi_query_router
from ..routes.scip_multi_routes import router as scip_multi_router
from ..web import (
    web_router,
    user_router,
    login_router,
    api_router,
    init_session_manager,
)
from ..web.repo_category_routes import repo_category_web_router
from ..web.dependency_map_routes import dependency_map_router

# Import helper functions from app_helpers (extracted to break circular import with app.py)

# Import sub-registration functions for extracted route modules
from .inline_auth import register_auth_routes  # noqa: E402
from .inline_mcp_creds import register_mcp_credential_routes  # noqa: E402
from .inline_admin_users import register_admin_user_routes  # noqa: E402
from .inline_admin_ops import register_admin_ops_routes  # noqa: E402
from .inline_jobs import register_job_routes  # noqa: E402
from .inline_misc import register_misc_routes  # noqa: E402
from .inline_query import register_query_routes  # noqa: E402
from .inline_repos import register_repo_routes  # noqa: E402
from .inline_repos_v2 import register_repos_v2_routes  # noqa: E402

# Constants used by route handlers
GOLDEN_REPO_ADD_OPERATION = "add_golden_repo"
GOLDEN_REPO_REFRESH_OPERATION = "refresh_golden_repo"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"

# Module-level logger (was available as a closure variable in app.py)
logger = logging.getLogger(__name__)


def register_inline_routes(
    app: FastAPI,
    background_job_manager,
    golden_repo_manager,
    activated_repo_manager,
    repository_listing_manager,
    semantic_query_manager,
    workspace_cleanup_service,
    jwt_manager,
    user_manager,
    refresh_token_manager,
    oauth_manager,
    mcp_credential_manager,
    mcp_registration_service,
    config_service,
    server_config,
    data_dir: str,
    db_path_str: str,
    job_tracker,
    secret_key: str = "",
) -> None:
    """
    Register all inline route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        background_job_manager: BackgroundJobManager instance
        golden_repo_manager: GoldenRepoManager instance
        activated_repo_manager: ActivatedRepoManager instance
        repository_listing_manager: RepositoryListingManager instance
        semantic_query_manager: SemanticQueryManager instance
        workspace_cleanup_service: WorkspaceCleanupService instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        refresh_token_manager: RefreshTokenManager instance
        oauth_manager: OAuthManager instance
        mcp_credential_manager: MCPCredentialManager instance
        mcp_registration_service: MCPSelfRegistrationService instance
        config_service: ConfigService instance
        server_config: ServerConfig instance
        data_dir: Server data directory path
        db_path_str: Database path string
        job_tracker: JobTracker instance
    """

    # Delegate auth and API key routes to extracted module
    register_auth_routes(
        app,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        refresh_token_manager=refresh_token_manager,
    )

    # Delegate MCP credential routes to extracted module
    register_mcp_credential_routes(
        app,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        mcp_credential_manager=mcp_credential_manager,
        mcp_registration_service=mcp_registration_service,
    )

    # Delegate admin user management routes to extracted module
    register_admin_user_routes(
        app,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        refresh_token_manager=refresh_token_manager,
        db_path_str=db_path_str,
    )

    # Delegate admin operations routes to extracted module
    register_admin_ops_routes(
        app,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
        workspace_cleanup_service=workspace_cleanup_service,
        config_service=config_service,
        server_config=server_config,
        data_dir=data_dir,
        job_tracker=job_tracker,
    )

    # Delegate misc/system routes to extracted module
    register_misc_routes(
        app,
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=activated_repo_manager,
        config_service=config_service,
        server_config=server_config,
        data_dir=data_dir,
        background_job_manager=background_job_manager,
        user_manager=user_manager,
    )

    # Delegate job management routes to extracted module
    register_job_routes(
        app,
        jwt_manager=jwt_manager,
        user_manager=user_manager,
        background_job_manager=background_job_manager,
        job_tracker=job_tracker,
    )

    # Delegate query route to extracted module
    register_query_routes(
        app,
        semantic_query_manager=semantic_query_manager,
        activated_repo_manager=activated_repo_manager,
    )

    # Delegate /api/repos/* routes to extracted module
    register_repo_routes(
        app,
        activated_repo_manager=activated_repo_manager,
        golden_repo_manager=golden_repo_manager,
        repository_listing_manager=repository_listing_manager,
        background_job_manager=background_job_manager,
    )

    # Delegate /api/repositories/* routes to extracted module
    register_repos_v2_routes(
        app,
        activated_repo_manager=activated_repo_manager,
        repository_listing_manager=repository_listing_manager,
        background_job_manager=background_job_manager,
    )

    # Mount OAuth 2.1 routes
    app.include_router(oauth_router)
    app.include_router(mcp_router)
    app.include_router(global_routes_router)
    app.include_router(git_settings_router, prefix="/api")
    app.include_router(ssh_keys_router)
    app.include_router(scip_queries_router)
    app.include_router(files_router)
    app.include_router(git_router)
    app.include_router(indexing_router)
    app.include_router(cache_router)
    app.include_router(multi_query_router)
    app.include_router(scip_multi_router)
    # TEMP: Commented for testing bug #751
    # app.include_router(cicd_router)
    app.include_router(groups_router)
    app.include_router(users_router)
    app.include_router(audit_router)
    app.include_router(repo_categories_router)
    app.include_router(delegation_callbacks_router)
    app.include_router(maintenance_router)
    app.include_router(api_keys_router)
    app.include_router(llm_creds_router)
    app.include_router(diagnostics_router)
    app.include_router(research_assistant_router)
    app.include_router(mfa_router)  # Story #559: MFA setup UI
    app.include_router(repository_health_router)
    app.include_router(activated_repos_router)
    app.include_router(debug_router)

    # Mount Web Admin UI routes and static files
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path as PathLib

    # Initialize session manager for web UI
    init_session_manager(secret_key, server_config, server_config.web_security_config)

    # Mount static files for web UI
    # NOTE: __file__ is in routers/, so use .parent.parent to reach server/ root
    web_static_dir = PathLib(__file__).parent.parent / "web" / "static"
    if web_static_dir.exists():
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(web_static_dir)),
            name="admin_static",
        )

    # Include web router with /admin prefix
    app.include_router(web_router, prefix="/admin", tags=["admin"])

    # Include repo category management router with /admin prefix (Story #180)
    app.include_router(repo_category_web_router, prefix="/admin", tags=["admin"])

    # Include dependency map router with /admin prefix (Story #212)
    app.include_router(dependency_map_router, prefix="/admin", tags=["admin"])

    # Include wiki router (Stories #280-#283)
    from ..wiki.routes import wiki_router

    # NOTE: __file__ is in routers/, so use .parent.parent to reach server/ root
    wiki_static_dir = PathLib(__file__).parent.parent / "wiki" / "static"
    if wiki_static_dir.exists():
        app.mount(
            "/wiki/_static",
            StaticFiles(directory=str(wiki_static_dir)),
            name="wiki_static",
        )
    app.include_router(wiki_router, prefix="/wiki", tags=["wiki"])

    # Include user router with /user prefix for non-admin self-service
    app.include_router(user_router, prefix="/user", tags=["user"])

    # Include login router at root level for unified authentication
    app.include_router(login_router, tags=["authentication"])

    # Include API router for public API endpoints (Story #89)
    app.include_router(api_router, prefix="/api", tags=["api"])
