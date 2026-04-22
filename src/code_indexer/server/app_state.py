"""
AppState: typed container for all services stored on app.state.

Extracted from app.py as part of Story #409 (app.py modularization) AC4.

All service attributes use Optional[Any] to avoid circular imports at
module load time — concrete types live in their own modules and are
wired at runtime during create_app() / lifespan startup.

Usage in route handlers:
    from code_indexer.server.app_state import get_app_state, AppState
    from fastapi import Depends, Request

    @router.get("/endpoint")
    def my_handler(
        request: Request,
        state: AppState = Depends(get_app_state),
    ):
        mgr = state.background_job_manager
        ...

Usage in create_app() to attach to app:
    from code_indexer.server.app_state import AppState
    app.state.app_state = AppState()
    app.state.app_state.golden_repo_manager = golden_repo_manager
    # ... or populate all at once after init
"""

from typing import Any, Optional, cast
from fastapi import Request


class AppState:
    """
    Typed container for all services registered on FastAPI's app.state.

    Every attribute defaults to None and is populated during startup
    (create_app or lifespan). Using explicit typed attributes rather than
    ad-hoc attribute assignment provides IDE autocompletion and enables
    type checkers to verify service access.

    NOTE: Attributes use Optional[Any] to avoid circular imports. The actual
    types are documented in comments for each attribute.
    """

    # Core repository managers (set in create_app body, before routes)
    golden_repo_manager: Optional[Any]  # GoldenRepoManager
    background_job_manager: Optional[Any]  # BackgroundJobManager
    activated_repo_manager: Optional[Any]  # ActivatedRepoManager
    repository_listing_manager: Optional[Any]  # RepositoryListingManager
    semantic_query_manager: Optional[Any]  # SemanticQueryManager
    workspace_cleanup_service: Optional[Any]  # WorkspaceCleanupService

    # Auth / group management (set in lifespan)
    group_manager: Optional[Any]  # GroupManager
    audit_service: Optional[Any]  # AuditService
    access_filtering_service: Optional[Any]  # AccessFilteringService

    # Global lifecycle / query tracking (set in lifespan)
    global_lifecycle_manager: Optional[Any]  # GlobalLifecycleManager
    query_tracker: Optional[Any]  # QueryTracker
    golden_repos_dir: Optional[str]  # str path to golden repos directory

    # Payload cache (set in lifespan)
    payload_cache: Optional[Any]  # PayloadCache

    # LLM credential lifecycle (set in lifespan)
    llm_lifecycle_service: Optional[Any]  # LLMLeaseLifecycleService

    # Background schedulers / debouncers (set in lifespan)
    scheduled_catchup_service: Optional[Any]  # ScheduledCatchupService
    cidx_meta_debouncer: Optional[Any]  # CidxMetaRefreshDebouncer
    description_refresh_scheduler: Optional[Any]  # DescriptionRefreshScheduler
    data_retention_scheduler: Optional[Any]  # DataRetentionScheduler

    # Dependency map service (set in lifespan)
    dependency_map_service: Optional[Any]  # DependencyMapService

    # Self-monitoring (set in lifespan)
    self_monitoring_service: Optional[Any]  # SelfMonitoringService
    self_monitoring_repo_root: Optional[str]  # str path to repo root
    self_monitoring_github_repo: Optional[str]  # str GitHub repo name

    # Telemetry (set in lifespan)
    telemetry_manager: Optional[Any]  # TelemetryManager
    machine_metrics_exporter: Optional[Any]  # MachineMetricsExporter

    # Langfuse trace sync (set in lifespan)
    langfuse_sync_service: Optional[Any]  # LangfuseTraceSyncService

    # SSH migration result (set in lifespan)
    ssh_migration_result: Optional[Any]  # SSHMigrationResult or None

    # Logging database path (set in lifespan)
    log_db_path: Optional[str]  # str path to SQLite log DB

    # Shared technical memory store (set in lifespan, Story #877)
    # Optional[Any] avoids circular import — concrete type is MemoryStoreService
    # from code_indexer.server.services.memory_store_service (wired at runtime).
    memory_store_service: Optional[Any]  # MemoryStoreService

    def __init__(self) -> None:
        """Initialize all attributes to None."""
        self.golden_repo_manager = None
        self.background_job_manager = None
        self.activated_repo_manager = None
        self.repository_listing_manager = None
        self.semantic_query_manager = None
        self.workspace_cleanup_service = None

        self.group_manager = None
        self.audit_service = None
        self.access_filtering_service = None

        self.global_lifecycle_manager = None
        self.query_tracker = None
        self.golden_repos_dir = None

        self.payload_cache = None
        self.llm_lifecycle_service = None

        self.scheduled_catchup_service = None
        self.cidx_meta_debouncer = None
        self.description_refresh_scheduler = None
        self.data_retention_scheduler = None

        self.dependency_map_service = None

        self.self_monitoring_service = None
        self.self_monitoring_repo_root = None
        self.self_monitoring_github_repo = None

        self.telemetry_manager = None
        self.machine_metrics_exporter = None

        self.langfuse_sync_service = None
        self.ssh_migration_result = None
        self.log_db_path = None
        self.memory_store_service = None


def get_app_state(request: Request) -> AppState:
    """
    FastAPI dependency that extracts the AppState from the request's app.state.

    Usage:
        @router.get("/endpoint")
        def handler(state: AppState = Depends(get_app_state)):
            mgr = state.background_job_manager

    The AppState must be attached to app.state.app_state during create_app().
    """
    return cast(AppState, request.app.state.app_state)
