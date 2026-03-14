"""
FastAPI app wiring for CIDX server startup.

Extracted from app.py as part of Story #409 AC5 (app.py modularization).
Contains create_fastapi_app() which creates the FastAPI instance, adds
middleware, wires services into app.state, and registers all routes.
"""

import logging
from typing import Any, Callable, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from code_indexer.server.middleware.error_handler import GlobalErrorHandler

logger = logging.getLogger(__name__)


def create_fastapi_app(services: Dict[str, Any], lifespan: Callable) -> FastAPI:
    """
    Create FastAPI app, add middleware, wire services to app.state, register routes.

    Args:
        services: Dict of initialized service instances (from initialize_services())
        lifespan: Async context manager for server startup/shutdown

    Returns:
        Configured FastAPI application instance
    """
    from code_indexer.server.auth import dependencies
    from code_indexer.server.app_state import AppState as _AppState
    from code_indexer.server.routers.inline_routes import register_inline_routes

    # Unpack services
    jwt_manager = services["jwt_manager"]
    user_manager = services["user_manager"]
    refresh_token_manager = services["refresh_token_manager"]
    oauth_manager = services["oauth_manager"]
    golden_repo_manager = services["golden_repo_manager"]
    background_job_manager = services["background_job_manager"]
    job_tracker = services["job_tracker"]
    activated_repo_manager = services["activated_repo_manager"]
    repository_listing_manager = services["repository_listing_manager"]
    semantic_query_manager = services["semantic_query_manager"]
    workspace_cleanup_service = services["workspace_cleanup_service"]
    mcp_credential_manager = services["mcp_credential_manager"]
    mcp_registration_service = services["mcp_registration_service"]
    config_service = services["config_service"]
    server_config = services["server_config"]
    data_dir = services["data_dir"]
    db_path_str = services["db_path_str"]
    secret_key = services["secret_key"]

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
    from code_indexer.server.telemetry.correlation_bridge import CorrelationBridgeMiddleware

    app.add_middleware(CorrelationBridgeMiddleware)

    # Add exception handlers for validation errors that FastAPI catches before middleware
    @app.exception_handler(RequestValidationError)
    def validation_exception_handler(request: Request, exc: RequestValidationError):
        error_data = global_error_handler.handle_validation_error(exc, request)
        return global_error_handler._create_error_response(error_data)

    # Store managers in app.state for access by routes
    app.state.golden_repo_manager = golden_repo_manager
    app.state.background_job_manager = background_job_manager
    app.state.activated_repo_manager = activated_repo_manager
    app.state.repository_listing_manager = repository_listing_manager
    app.state.semantic_query_manager = semantic_query_manager
    app.state.workspace_cleanup_service = workspace_cleanup_service

    # AC4: Attach typed AppState for dependency-injection access by routers
    _app_state = _AppState()
    _app_state.golden_repo_manager = golden_repo_manager
    _app_state.background_job_manager = background_job_manager
    _app_state.activated_repo_manager = activated_repo_manager
    _app_state.repository_listing_manager = repository_listing_manager
    _app_state.semantic_query_manager = semantic_query_manager
    _app_state.workspace_cleanup_service = workspace_cleanup_service
    app.state.app_state = _app_state

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
