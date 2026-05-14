"""
Unit tests for registry_factory cluster-mode awareness.

Covers the bug where get_server_global_registry() always creates a SQLite-backed
GlobalRegistry, even in cluster (postgres) mode, causing data inconsistency.

Tests:
1. SQLite mode: existing behavior preserved (backward compat)
2. Postgres mode: registry_factory delegates to BackendRegistry.global_repos
3. Postgres mode: list_global_repos REST endpoint returns data from BackendRegistry
4. Postgres mode: update_enable_temporal in repos.py uses BackendRegistry
5. Postgres mode: update_enable_temporal in golden_repo_manager uses BackendRegistry
"""

import contextlib
from pathlib import Path
from unittest.mock import MagicMock
from typing import Any, Dict, Generator, Optional


# ---------------------------------------------------------------------------
# Fake implementations (real data stores, no mocks of logic)
# ---------------------------------------------------------------------------


class FakeGlobalReposBackend:
    """
    In-memory implementation of the GlobalReposBackend protocol.
    Satisfies the protocol via structural subtyping — no inheritance needed.
    """

    def __init__(self) -> None:
        self._repos: Dict[str, Dict[str, Any]] = {}
        self.update_enable_temporal_calls: list = []

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None:
        self._repos[alias_name] = {
            "alias_name": alias_name,
            "repo_name": repo_name,
            "repo_url": repo_url,
            "index_path": index_path,
            "enable_temporal": enable_temporal,
            "temporal_options": temporal_options,
            "enable_scip": enable_scip,
        }

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        return self._repos.get(alias_name)

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._repos)

    def delete_repo(self, alias_name: str) -> bool:
        if alias_name in self._repos:
            del self._repos[alias_name]
            return True
        return False

    def update_last_refresh(self, alias_name: str) -> bool:
        return alias_name in self._repos

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> bool:
        self.update_enable_temporal_calls.append((alias_name, enable_temporal))
        if alias_name in self._repos:
            self._repos[alias_name]["enable_temporal"] = enable_temporal
            return True
        return False

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool:
        return alias_name in self._repos

    def update_next_refresh(self, alias_name: str, next_refresh: Optional[str]) -> bool:
        return alias_name in self._repos

    def close(self) -> None:
        pass


class FakeBackendRegistry:
    """
    Minimal BackendRegistry-like object with a global_repos attribute.
    """

    def __init__(self, global_repos: FakeGlobalReposBackend) -> None:
        self.global_repos = global_repos


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _app_state_postgres_mode(
    backend: FakeGlobalReposBackend, golden_repos_dir: Optional[str] = None
) -> Generator:
    """
    Context manager that temporarily sets app.state to postgres mode with a
    fake backend registry, restoring original state on exit.
    """
    from code_indexer.server import app as app_module

    fake_registry = FakeBackendRegistry(backend)

    saved_storage_mode = getattr(app_module.app.state, "storage_mode", None)
    saved_backend_registry = getattr(app_module.app.state, "backend_registry", None)
    saved_golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", None)

    try:
        app_module.app.state.storage_mode = "postgres"
        app_module.app.state.backend_registry = fake_registry
        if golden_repos_dir is not None:
            app_module.app.state.golden_repos_dir = golden_repos_dir
        yield fake_registry
    finally:
        if saved_storage_mode is not None:
            app_module.app.state.storage_mode = saved_storage_mode
        if saved_backend_registry is not None:
            app_module.app.state.backend_registry = saved_backend_registry
        if saved_golden_repos_dir is not None:
            app_module.app.state.golden_repos_dir = saved_golden_repos_dir


def _make_backend_with_one_repo(
    alias_name: str, repo_name: str
) -> FakeGlobalReposBackend:
    """Build a FakeGlobalReposBackend pre-populated with one repository."""
    backend = FakeGlobalReposBackend()
    backend.register_repo(
        alias_name=alias_name,
        repo_name=repo_name,
        repo_url=None,
        index_path=f"/data/golden-repos/{repo_name}",
    )
    return backend


# ---------------------------------------------------------------------------
# Test 1: SQLite mode - existing behavior unchanged
# ---------------------------------------------------------------------------


def test_sqlite_mode_returns_sqlite_backed_registry(tmp_path: Path) -> None:
    """
    In SQLite mode, get_server_global_registry() returns a GlobalRegistry
    backed by SQLite (existing behavior, must not regress).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry
    from code_indexer.global_repos.global_registry import GlobalRegistry

    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir)

    assert isinstance(registry, GlobalRegistry)
    assert registry._use_sqlite is True
    assert registry._sqlite_backend is not None


# ---------------------------------------------------------------------------
# Test 2: Postgres mode - registry_factory wraps the postgres backend
# ---------------------------------------------------------------------------


def test_postgres_mode_returns_postgres_backed_adapter(tmp_path: Path) -> None:
    """
    When a GlobalReposBackend (backend=) is provided, get_server_global_registry()
    returns an object whose list_global_repos() delegates to backend.list_repos().
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("my-repo-global", "my-repo")

    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)

    repos = registry.list_global_repos()
    assert isinstance(repos, list)
    assert len(repos) == 1
    assert repos[0]["alias_name"] == "my-repo-global"


def test_postgres_mode_update_enable_temporal_delegates_to_backend(
    tmp_path: Path,
) -> None:
    """
    When a backend is provided, update_enable_temporal() on the returned
    adapter delegates to the backend, not to SQLite.
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("my-repo-global", "my-repo")

    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.update_enable_temporal("my-repo-global", True)

    assert len(backend.update_enable_temporal_calls) == 1
    assert backend.update_enable_temporal_calls[0] == ("my-repo-global", True)


# ---------------------------------------------------------------------------
# Test 3: Postgres mode - list_global_repos REST endpoint
# ---------------------------------------------------------------------------


def test_list_global_repos_endpoint_uses_backend_registry_in_postgres_mode(
    tmp_path: Path,
) -> None:
    """
    GET /global/repos in postgres mode must return data from
    app.state.backend_registry.global_repos, not from SQLite.
    """
    from fastapi.testclient import TestClient
    from code_indexer.server.app import app
    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.auth.user_manager import UserRole

    backend = _make_backend_with_one_repo("pg-repo-global", "pg-repo")

    mock_user = MagicMock()
    mock_user.username = "testadmin"
    mock_user.role = UserRole.ADMIN

    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    app.dependency_overrides[get_current_user] = lambda: mock_user
    try:
        with TestClient(app, raise_server_exceptions=True) as client:
            with _app_state_postgres_mode(backend, golden_repos_dir):
                response = client.get("/global/repos")

            assert response.status_code == 200
            data = response.json()
            assert "repos" in data
            aliases = [r.get("alias") or r.get("alias_name") for r in data["repos"]]
            assert "pg-repo-global" in aliases, (
                f"Expected 'pg-repo-global' in aliases from postgres backend, got: {aliases}"
            )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test 4: Postgres mode - repos.py MCP handler update_enable_temporal
# ---------------------------------------------------------------------------


def test_repos_handler_update_enable_temporal_uses_postgres_backend(
    tmp_path: Path,
) -> None:
    """
    _set_enable_temporal_flag() in repos.py, when
    storage_mode == 'postgres', must call backend_registry.global_repos
    update_enable_temporal(), not create a new SQLite GlobalRegistry.
    """
    backend = _make_backend_with_one_repo("test-repo-global", "test-repo")

    grm_mock = MagicMock()
    grm_mock.data_dir = str(tmp_path)
    grm_mock._sqlite_backend = MagicMock()
    grm_mock._sqlite_backend.update_enable_temporal.return_value = True
    grm_mock.golden_repos = {"test-repo": MagicMock()}

    from code_indexer.server import app as app_module

    saved_grm = getattr(app_module, "golden_repo_manager", None)

    with _app_state_postgres_mode(backend):
        try:
            app_module.golden_repo_manager = grm_mock

            from code_indexer.server.mcp.handlers.repos import (
                _set_enable_temporal_flag,
            )

            _set_enable_temporal_flag("test-repo")
        finally:
            if saved_grm is not None:
                app_module.golden_repo_manager = saved_grm
            elif hasattr(app_module, "golden_repo_manager"):
                delattr(app_module, "golden_repo_manager")

    assert len(backend.update_enable_temporal_calls) == 1
    alias_called, flag_called = backend.update_enable_temporal_calls[0]
    assert alias_called == "test-repo-global"
    assert flag_called is True
