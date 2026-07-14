"""
Factory for creating properly configured GlobalRegistry instances.

Story #713: This factory ensures all server code uses SQLite backend
for GlobalRegistry, eliminating the storage mismatch between
GoldenRepoManager (SQLite) and GlobalRegistry (JSON).

Cluster fix: When running in postgres mode, get_server_global_registry()
wraps the shared BackendRegistry.global_repos backend so all nodes read
and write to the same shared store.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from code_indexer.global_repos.global_registry import (
    RESERVED_GLOBAL_NAMES,
    GlobalRegistry,
    ReservedNameError,
)

logger = logging.getLogger(__name__)


class PostgresGlobalRegistryAdapter:
    """
    Thin adapter that exposes the GlobalRegistry interface over a
    GlobalReposPostgresBackend (or any backend implementing the same protocol).

    Used in cluster (postgres) mode so that registry_factory callers get the
    same API regardless of storage backend.

    Bug #1308 remediation: this adapter must forward EVERY method that
    RefreshScheduler and GlobalActivator invoke on `self.registry` -- not just
    the read-only subset (list_global_repos/get_global_repo/
    update_enable_temporal) that the read/list path happens to use. A partial
    adapter looks correct in cluster-mode read tests but raises AttributeError
    the moment cluster-mode refresh-execution or activation touches a write
    method. See test_adapter_forwards_every_call_site_used_by_scheduler_and_activator
    for the regression guard.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def list_global_repos(self) -> List[Dict[str, Any]]:
        """Return list of repo dicts, mirroring GlobalRegistry.list_global_repos()."""
        repos_dict: Dict[str, Dict[str, Any]] = self._backend.list_repos()
        return list(repos_dict.values())

    def get_global_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        """Return a single repo dict by alias, mirroring GlobalRegistry.get_global_repo()."""
        # _backend is typed Any (supports multiple backend protocols); get_repo() is
        # documented to return Optional[Dict[str, Any]] for all backend implementations.
        return cast(Optional[Dict[str, Any]], self._backend.get_repo(alias_name))

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> None:
        """Delegate to backend's update_enable_temporal()."""
        self._backend.update_enable_temporal(alias_name, enable_temporal)

    def register_global_repo(
        self,
        repo_name: str,
        alias_name: str,
        repo_url: Optional[str],
        index_path: str,
        allow_reserved: bool = False,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None:
        """
        Register a global repository, mirroring GlobalRegistry.register_global_repo().

        Preserves GlobalRegistry's alias-validation semantics (reserved-name
        check and mandatory '-global' suffix) before delegating to
        backend.register_repo() -- the backend method name and argument order
        differ from this method's, which is why a straight passthrough is not
        enough.

        Raises:
            ReservedNameError: If alias_name is a reserved name and allow_reserved=False
            ValueError: If alias_name doesn't end with '-global' suffix
        """
        if not allow_reserved and alias_name in RESERVED_GLOBAL_NAMES:
            purpose = RESERVED_GLOBAL_NAMES[alias_name]
            raise ReservedNameError(
                f"Cannot register repo with name '{alias_name}': "
                f"This name is reserved for {purpose}. "
                f"Choose a different alias name for your repository."
            )

        if not alias_name.lower().endswith("-global"):
            raise ValueError(
                f"Global repo alias must end with '-global' suffix (case-insensitive). "
                f"Got: '{alias_name}', expected: '{repo_name}-global'"
            )

        self._backend.register_repo(
            alias_name=alias_name,
            repo_name=repo_name,
            repo_url=repo_url,
            index_path=index_path,
            enable_temporal=enable_temporal,
            temporal_options=temporal_options,
            enable_scip=enable_scip,
        )

    def unregister_global_repo(self, alias_name: str) -> None:
        """Delegate to backend.delete_repo(), mirroring GlobalRegistry.unregister_global_repo()."""
        self._backend.delete_repo(alias_name)

    def update_refresh_timestamp(self, alias_name: str) -> None:
        """
        Delegate to backend.update_last_refresh() -- the backend method NAME
        DIFFERS from this GlobalRegistry-API method name.
        """
        self._backend.update_last_refresh(alias_name)

    def update_next_refresh(
        self, alias_name: str, next_refresh: Optional[float]
    ) -> None:
        """
        Update the next_refresh timestamp, mirroring
        GlobalRegistry.update_next_refresh(): converts the float Unix
        timestamp to str (backend column is TEXT) before delegating,
        preserving None as a clear-the-value signal.
        """
        next_refresh_str = str(next_refresh) if next_refresh is not None else None
        self._backend.update_next_refresh(alias_name, next_refresh_str)

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> None:
        """Delegate to backend's update_enable_scip()."""
        self._backend.update_enable_scip(alias_name, enable_scip)

    def list_due_repos(self, limit: int, now: float) -> List[Dict[str, Any]]:
        """Delegate to backend.list_due_repos(), mirroring GlobalRegistry.list_due_repos()."""
        return cast(
            List[Dict[str, Any]], self._backend.list_due_repos(limit=limit, now=now)
        )


def get_server_global_registry(
    golden_repos_dir: str,
    server_data_dir: Optional[str] = None,
    backend: Optional[Any] = None,
) -> Any:
    """
    Create a GlobalRegistry instance configured for server mode.

    In SQLite / standalone mode (backend=None), returns a GlobalRegistry
    backed by SQLite (existing behaviour, unchanged).

    In postgres / cluster mode (backend provided), returns a
    PostgresGlobalRegistryAdapter wrapping the shared backend so all
    cluster nodes read and write to the same store.

    Args:
        golden_repos_dir: Path to golden repos directory
        server_data_dir: Path to server data directory (for db_path).
                        If None, derives from golden_repos_dir parent.
                        Ignored when backend is provided.
        backend: Optional GlobalReposBackend (postgres mode).  When
                 supplied the returned adapter delegates all calls to it.

    Returns:
        PostgresGlobalRegistryAdapter (postgres mode) or
        GlobalRegistry (sqlite mode)
    """
    if backend is not None:
        return PostgresGlobalRegistryAdapter(backend)

    golden_repos_path = Path(golden_repos_dir)

    if server_data_dir is None:
        # golden_repos_dir is typically: ~/.cidx-server/data/golden-repos
        # server_data_dir would be: ~/.cidx-server/data
        server_data_dir = str(golden_repos_path.parent)

    db_path = str(Path(server_data_dir) / "cidx_server.db")

    return GlobalRegistry(
        golden_repos_dir=golden_repos_dir,
        use_sqlite=True,
        db_path=db_path,
    )


def resolve_backend_registry_attr(
    attr_name: str, caller_name: str = ""
) -> Tuple[Optional[Any], bool]:
    """
    Inspect the running server's app.state to determine cluster (postgres)
    mode and, if active, the shared BackendRegistry attribute named
    *attr_name* (e.g. "global_repos", "golden_repo_metadata").

    Bug #1308: RefreshScheduler and GlobalActivator used to eagerly bind a
    per-node SQLite GlobalRegistry at construction time, which split-brained
    against the shared PostgreSQL registry that the read/list path already
    used (see GlobalRepoOperations._resolve_registry_backend in
    shared_operations.py, which now delegates here so there is ONE
    implementation). This helper lets any caller defer resolution to
    request/access time -- by then app.state.backend_registry is guaranteed
    to be populated in postgres/cluster mode -- instead of duplicating the
    app.state introspection in every caller.

    Bug #1390: generalized from the original global_repos-only
    resolve_backend_registry_state() to accept any BackendRegistry attribute
    name, so RefreshScheduler's golden_repo_metadata resolution (needed for
    cross-table enable_temporal reconciliation) can reuse the exact same
    deferred-resolution shape instead of duplicating it a second time.

    Bug #1308 remediation item #5 (CLI import cost): looks up
    code_indexer.server.app via `sys.modules.get()` -- a pure dict lookup --
    instead of `from code_indexer.server import app`, which would trigger a
    FRESH (expensive: FastAPI app + all routers + DB pools) import the first
    time this runs in a process. In a real server process the module is
    already loaded by the ASGI entrypoint before any of this code runs, so
    the lookup is free there too. In a pure CLI process (e.g. `cidx global
    activate`, which never imports the server app) the module is simply
    absent from sys.modules, and resolution short-circuits to the SQLite
    fallback with zero import cost.

    Args:
        attr_name: BackendRegistry attribute to read (e.g. "global_repos",
            "golden_repo_metadata").
        caller_name: Optional label included in the startup-window WARNING,
            for diagnosability when multiple callers share this helper.

    Returns:
        Tuple of (backend, postgres_mode_without_backend):
        - backend: the shared backend in postgres/cluster mode, or None in
          solo/CLI mode (no app.state) or if backend_registry is not yet
          populated.
        - postgres_mode_without_backend: True when storage_mode=postgres but
          app.state.backend_registry is not yet set (transient startup
          window). Callers should NOT cache the SQLite fallback in this case
          so the next access re-checks.
    """
    backend = None
    postgres_mode_without_backend = False

    app_module = sys.modules.get("code_indexer.server.app")
    _app = getattr(app_module, "app", None) if app_module is not None else None
    _app_state = getattr(_app, "state", None)
    if _app_state and getattr(_app_state, "storage_mode", None) == "postgres":
        _br = getattr(_app_state, "backend_registry", None)
        if _br is not None:
            backend = getattr(_br, attr_name)
        else:
            postgres_mode_without_backend = True
            logger.warning(
                "%s: storage_mode=postgres but backend_registry not set; "
                "falling back to SQLite",
                caller_name or "resolve_backend_registry_attr",
            )
    return backend, postgres_mode_without_backend


def resolve_backend_registry_state(caller_name: str = "") -> Tuple[Optional[Any], bool]:
    """
    Inspect the running server's app.state to determine cluster (postgres)
    mode and, if active, the shared global_repos backend.

    Thin convenience wrapper over :func:`resolve_backend_registry_attr` for
    the "global_repos" attribute -- preserved as its own function (rather
    than inlining the attr name at every call site) since it predates the
    Bug #1390 generalization and existing callers already depend on this
    exact name/signature.

    Args:
        caller_name: Optional label included in the startup-window WARNING,
            for diagnosability when multiple callers share this helper.

    Returns:
        Tuple of (backend, postgres_mode_without_backend) -- see
        :func:`resolve_backend_registry_attr` for the full contract.
    """
    return resolve_backend_registry_attr("global_repos", caller_name)


def get_server_golden_repo_metadata_backend(
    server_data_dir: str,
    backend: Optional[Any] = None,
) -> Any:
    """
    Return a GoldenRepoMetadataBackend instance configured for server mode.

    Mirrors get_server_global_registry(): in postgres/cluster mode (backend
    provided), the shared backend is returned AS-IS -- no adapter is needed
    here (unlike GlobalRegistry/PostgresGlobalRegistryAdapter) because
    GoldenRepoMetadataPostgresBackend and GoldenRepoMetadataSqliteBackend
    already share the same get_repo()/update_enable_temporal() surface. In
    SQLite/standalone mode (backend=None), constructs a per-node
    GoldenRepoMetadataSqliteBackend against cidx_server.db (the same DB file
    GlobalRegistry's SQLite fallback uses) and ensures its table exists.

    Args:
        server_data_dir: Path to server data directory (contains
            cidx_server.db in solo mode). Ignored when backend is provided.
        backend: Optional GoldenRepoMetadataBackend (postgres mode). When
            supplied, returned unchanged.

    Returns:
        The provided backend (postgres mode) or a new
        GoldenRepoMetadataSqliteBackend (sqlite mode).
    """
    if backend is not None:
        return backend

    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    db_path = str(Path(server_data_dir) / "cidx_server.db")
    sqlite_backend = GoldenRepoMetadataSqliteBackend(db_path)
    sqlite_backend.ensure_table_exists()
    return sqlite_backend
