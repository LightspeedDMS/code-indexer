"""
Factory for creating properly configured GlobalRegistry instances.

Story #713: This factory ensures all server code uses SQLite backend
for GlobalRegistry, eliminating the storage mismatch between
GoldenRepoManager (SQLite) and GlobalRegistry (JSON).

Cluster fix: When running in postgres mode, get_server_global_registry()
wraps the shared BackendRegistry.global_repos backend so all nodes read
and write to the same shared store.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from code_indexer.global_repos.global_registry import GlobalRegistry


class PostgresGlobalRegistryAdapter:
    """
    Thin adapter that exposes the GlobalRegistry interface over a
    GlobalReposPostgresBackend (or any backend implementing the same protocol).

    Used in cluster (postgres) mode so that registry_factory callers get the
    same API regardless of storage backend.
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
