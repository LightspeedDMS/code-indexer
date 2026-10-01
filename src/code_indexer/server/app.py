"""FastAPI application for CIDX Server — multi-user semantic code search with JWT auth."""

import logging
import threading
from typing import Dict, Optional, Any

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


# Global managers (initialized in create_app).
#
# Bug #1638: these are ANNOTATION-ONLY (no `= None` binding) so they are
# absent from this module's __dict__ until create_app() actually runs.
# That absence is what lets the module-level __getattr__() below (PEP 562)
# detect "not yet initialized" and defer initialize_services() until one of
# these names -- or `app` -- is genuinely accessed, instead of running it
# unconditionally as an import-time side effect. A bare `import
# code_indexer.server.app` (or a transitive import of it, e.g. via
# `from code_indexer.server import app as app_module`) no longer runs
# ConfigService/SQLite/DependencyLatencyTracker/MCPSelfRegistrationService
# startup or contends for the live server's primary_instance.lock file.
jwt_manager: Optional[JWTManager]
user_manager: Optional[UserManager]
refresh_token_manager: Optional[RefreshTokenManager]
golden_repo_manager: Optional[GoldenRepoManager]
background_job_manager: Optional[BackgroundJobManager]
job_tracker: Optional[Any]  # Story #311: JobTracker instance
activated_repo_manager: Optional[ActivatedRepoManager]
repository_listing_manager: Optional[RepositoryListingManager]
semantic_query_manager: Optional[SemanticQueryManager]
workspace_cleanup_service: Optional[WorkspaceCleanupService]
langfuse_sync_service: Optional[Any]  # Story #168: Langfuse trace sync service
_server_hnsw_cache: Optional[Any]  # Server-wide HNSW cache (Story #526)
_server_fts_cache: Optional[Any]  # Server-wide FTS cache

# Names whose first attribute access on this module should trigger lazy
# app construction (Bug #1638). Kept as a plain module-level set (not a
# function-local literal) so __getattr__ stays a simple membership check.
_LAZY_INIT_ATTRS = frozenset(
    {
        "app",
        "jwt_manager",
        "user_manager",
        "refresh_token_manager",
        "golden_repo_manager",
        "background_job_manager",
        "job_tracker",
        "activated_repo_manager",
        "repository_listing_manager",
        "semantic_query_manager",
        "workspace_cleanup_service",
        "langfuse_sync_service",
        "_server_hnsw_cache",
        "_server_fts_cache",
    }
)

# Bug #1638 remediation (post-review): the lazy-init lock MUST be an RLock,
# not a plain Lock. create_app()'s own execution path (initialize_services()
# -> bootstrap_cidx_meta() -> golden_repo_manager.register_local_repo() ->
# global_activator.activate_golden_repo() -> registry ->
# server/utils/registry_factory.py's resolve_backend_registry_attr() ->
# _running_server_app_state()) calls getattr(app_module, "app", None) on
# THIS SAME MODULE, re-entering __getattr__ on the SAME thread while
# create_app() is still running. A plain Lock would self-deadlock there
# (first boot only -- masked whenever cidx-meta is already bootstrapped,
# which is every dev/test environment). The RLock lets the re-entrant call
# back in; the _initializing sentinel below (not the lock) is what stops it
# from recursively re-running create_app() -- it makes the re-entrant call
# return AttributeError (-> None via getattr(..., None)) instead, which is
# EXACTLY the pre-fix behavior: before this bug existed, `app` was simply
# unbound until the module-level `app = create_app()` assignment completed,
# so any code reading it mid-bootstrap via getattr(..., None) already got
# None.
_lazy_init_lock = threading.RLock()

# Snapshot of every _LAZY_INIT_ATTRS value, taken immediately after the one
# real create_app() call completes. This is the fallback __getattr__ reads
# when a name is momentarily (or permanently, for langfuse_sync_service --
# see Blocker #3 below) absent from globals(). Two concrete cases need it:
#
#   * langfuse_sync_service is listed in _LAZY_INIT_ATTRS but create_app()
#     never actually assigns it via a `global` statement (only lifespan.py's
#     own function scope does). Pre-fix, reading it returned None (its old
#     `= None` default). Without this snapshot, globals()["langfuse_sync_service"]
#     would raise KeyError -- not even AttributeError -- breaking the
#     getattr(module, name, default)/hasattr() protocol.
#   * unittest.mock.patch's teardown: when a test patches a name that was
#     never yet materialized in globals(), mock records local=False and its
#     __exit__ calls delattr(module, name) then hasattr(module, name) to
#     decide whether to also restore via setattr. That hasattr() call
#     re-enters __getattr__ with the name freshly deleted from globals() --
#     without this snapshot fallback, globals()[name] raises KeyError (not
#     AttributeError), which hasattr() does NOT catch, so it propagates and
#     poisons the module (every later read of that name raises KeyError for
#     the rest of the process). The snapshot fallback makes that lookup
#     resolve cleanly instead.
_lazy_values: Dict[str, Any] = {}
_initialized = False
_initializing = False

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


# Bug #1758: explicit busy-wait timeout (seconds) for every raw sqlite3.connect()
# in TokenBlacklist. Matches the 30s convention DatabaseConnectionManager sets via
# `PRAGMA busy_timeout = 30000` (storage/database_manager.py:1854), so a brief lock
# held by a concurrent process (e.g. an auto-updater-triggered server restart
# briefly overlapping a request) is absorbed by SQLite's own internal wait instead
# of raising 'database is locked' after Python's own 5.0s sqlite3.connect() default.
_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0


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
        # Bug #1758: explicit busy-wait timeout so a brief lock held by a
        # concurrent process (e.g. an auto-updater-triggered server restart
        # briefly overlapping a logout request) is absorbed by SQLite's own
        # internal wait instead of raising 'database is locked' after
        # Python's own 5.0s sqlite3.connect() default.
        conn = sqlite3.connect(
            self._sqlite_db_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
        )
        try:
            conn.execute(
                "INSERT OR IGNORE INTO token_blacklist (jti, blacklisted_at) VALUES (?, ?)",
                (jti, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def _sqlite_contains(self, jti: str) -> bool:
        import sqlite3

        assert self._sqlite_db_path is not None
        # Bug #1758: see _sqlite_add's comment -- same busy-wait timeout.
        conn = sqlite3.connect(
            self._sqlite_db_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
        )
        try:
            row = conn.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def prune_expired(self, ttl_seconds: int) -> int:
        """Delete expired token blacklist entries older than ttl_seconds.

        A row is expired when blacklisted_at + ttl_seconds < now, i.e.
        blacklisted_at < (time.time() - ttl_seconds).

        Also evicts deleted JTIs from the local in-memory set.
        Returns the number of rows deleted.
        """
        import time

        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be >= 0, got {ttl_seconds}")

        cutoff = time.time() - ttl_seconds
        if self._pool is not None:
            deleted, evicted_jtis = self._pg_prune(cutoff)
        elif self._sqlite_db_path:
            deleted, evicted_jtis = self._sqlite_prune(cutoff)
        else:
            return 0

        # Evict deleted JTIs from the local in-memory set
        if evicted_jtis:
            self._local -= evicted_jtis
        return deleted

    def _sqlite_prune(self, cutoff: float) -> "tuple[int, set[str]]":
        """Select expired JTIs then delete them (SQLite).

        Returns (deleted_count, set_of_deleted_jtis).
        SELECT-then-DELETE is used because SQLite does not support
        DELETE ... RETURNING in older versions.
        """
        import sqlite3

        assert self._sqlite_db_path is not None
        # Bug #1758: see _sqlite_add's comment -- same busy-wait timeout.
        conn = sqlite3.connect(
            self._sqlite_db_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
        )
        try:
            rows = conn.execute(
                "SELECT jti FROM token_blacklist WHERE blacklisted_at < ?", (cutoff,)
            ).fetchall()
            evicted: set[str] = {row[0] for row in rows}
            conn.execute(
                "DELETE FROM token_blacklist WHERE blacklisted_at < ?", (cutoff,)
            )
            conn.commit()
            return len(evicted), evicted
        finally:
            conn.close()

    def _pg_prune(self, cutoff: float) -> "tuple[int, set[str]]":
        """Delete expired rows from PostgreSQL using RETURNING to collect JTIs.

        Returns (deleted_count, set_of_deleted_jtis).
        PostgreSQL supports DELETE ... RETURNING so we collect affected JTIs
        in a single statement.
        """
        assert self._pool is not None
        with self._pool.connection() as conn:
            rows = conn.execute(
                "DELETE FROM token_blacklist WHERE blacklisted_at < %s RETURNING jti",
                (cutoff,),
            ).fetchall()
            conn.commit()
        evicted: set[str] = {row[0] for row in rows}
        return len(evicted), evicted


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
        latency_tracker=services.get("latency_tracker"),
    )

    app = create_fastapi_app(services, lifespan)
    return app


def _ensure_initialized() -> None:
    """Run create_app() exactly once, tolerating re-entrant probes.

    MUST be called while `_lazy_init_lock` is already held by the caller
    (an RLock, so the SAME thread re-entering is a cheap no-op re-acquire,
    not a deadlock). An explicit `_initializing` sentinel -- not the lock --
    is what stops a re-entrant call from recursing into a second
    create_app(): it simply returns, leaving `app` and friends absent from
    globals() until the ORIGINAL call finishes. See the module-level
    comment above `_lazy_init_lock` for the full re-entrancy rationale.
    """
    global _initialized, _initializing
    if _initialized or _initializing:
        return
    _initializing = True
    try:
        globals()["app"] = create_app()
        _initialized = True
        # Snapshot every lazy name now that create_app() has run, so
        # __getattr__ has a stable fallback independent of globals()
        # mutation (mock.patch delattr, etc.) -- see _lazy_values above.
        for n in _LAZY_INIT_ATTRS:
            _lazy_values[n] = globals().get(n)
    finally:
        _initializing = False


def __getattr__(name: str) -> Any:
    """PEP 562 lazy module attribute access (Bug #1638).

    `app = create_app()` used to run unconditionally at import time, so a
    bare `import code_indexer.server.app` -- or a transitive import of it,
    e.g. `code_indexer.server.mcp.handlers._utils` does
    `from code_indexer.server import app as app_module` -- ran full service
    initialization (ConfigService load, SQLite golden-repo enumeration,
    DependencyLatencyTracker startup, MCPSelfRegistrationService singleton
    registration, primary_instance.lock contention) as a side effect, with
    no explicit opt-in.

    `__getattr__` is only invoked by Python when normal attribute lookup on
    this module fails (i.e. the name is absent from this module's
    __dict__). Since `app` is no longer assigned at module level, and the
    service globals above are annotation-only, importing this module is
    now inert -- initialize_services() only runs the first time real code
    actually reaches for one of these names (e.g. `app_module.app`,
    `from code_indexer.server.app import golden_repo_manager`, or the
    production `uvicorn code_indexer.server.app:app` entrypoint resolving
    its `app` target), never on a bare import.

    Mid-construction (a re-entrant call arriving while create_app() is
    still executing on this same thread), this deliberately raises
    AttributeError rather than blocking or returning a partial value --
    so `getattr(app_module, "app", None)` from inside create_app()'s own
    call chain correctly yields None, matching pre-fix semantics exactly
    (pre-fix, `app` was genuinely unbound until the module-level assignment
    line completed).

    The entire read (init-if-needed + globals()/_lazy_values lookup) runs
    under `_lazy_init_lock` so a concurrent reader on another thread never
    observes `_initialized`/`_lazy_values` mid-mutation.

    Issue #1659 hazard note -- this mechanism makes hasattr()/getattr()/
    delattr() semantically misleading against this module for any name in
    _LAZY_INIT_ATTRS:

      * hasattr(module, name) for any name in _LAZY_INIT_ATTRS always
        returns True, even before real construction has happened, because
        this __getattr__ synthesizes the value on demand instead of
        raising.
      * getattr(module, name, default) -- even one written defensively,
        expecting the attribute might legitimately be absent -- triggers
        full lazy construction as a side effect on first access (real DB
        reads, background thread spawns), which a caller expecting a cheap
        read-only probe almost certainly does not want.
      * delattr(module, name) bypasses this __getattr__ entirely (PEP 562
        defines no __delattr__ hook, so it is plain __dict__ removal) and
        can raise AttributeError even though hasattr() just reported the
        attribute exists (concrete breakage: issue #1658, caused by the
        _lazy_values snapshot fallback above keeping hasattr()/getattr()
        resolving a name whose real __dict__ entry is already gone).

      A caller that needs a genuinely side-effect-free existence check
      should inspect _initialized/_lazy_values directly instead of calling
      hasattr()/getattr() on this module.
    """
    if name not in _LAZY_INIT_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    with _lazy_init_lock:
        _ensure_initialized()
        if name in globals():
            return globals()[name]
        if _initialized and name in _lazy_values:
            return _lazy_values[name]
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
