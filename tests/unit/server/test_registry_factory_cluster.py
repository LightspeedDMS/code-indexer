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
        self.register_repo_calls: list = []
        self.delete_repo_calls: list = []
        self.update_last_refresh_calls: list = []
        self.update_next_refresh_calls: list = []
        self.update_enable_scip_calls: list = []

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
        self.register_repo_calls.append(
            (
                alias_name,
                repo_name,
                repo_url,
                index_path,
                enable_temporal,
                temporal_options,
                enable_scip,
            )
        )
        self._repos[alias_name] = {
            "alias_name": alias_name,
            "repo_name": repo_name,
            "repo_url": repo_url,
            "index_path": index_path,
            "enable_temporal": enable_temporal,
            "temporal_options": temporal_options,
            "enable_scip": enable_scip,
            "next_refresh": None,
        }

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]:
        return self._repos.get(alias_name)

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._repos)

    def delete_repo(self, alias_name: str) -> bool:
        self.delete_repo_calls.append(alias_name)
        if alias_name in self._repos:
            del self._repos[alias_name]
            return True
        return False

    def update_last_refresh(self, alias_name: str) -> bool:
        self.update_last_refresh_calls.append(alias_name)
        return alias_name in self._repos

    def update_enable_temporal(self, alias_name: str, enable_temporal: bool) -> bool:
        self.update_enable_temporal_calls.append((alias_name, enable_temporal))
        if alias_name in self._repos:
            self._repos[alias_name]["enable_temporal"] = enable_temporal
            return True
        return False

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool:
        self.update_enable_scip_calls.append((alias_name, enable_scip))
        if alias_name in self._repos:
            self._repos[alias_name]["enable_scip"] = enable_scip
            return True
        return False

    def update_next_refresh(self, alias_name: str, next_refresh: Optional[str]) -> bool:
        self.update_next_refresh_calls.append((alias_name, next_refresh))
        if alias_name in self._repos:
            self._repos[alias_name]["next_refresh"] = next_refresh
            return True
        return False

    def list_due_repos(self, limit: int, now: float) -> list:
        if limit <= 0:
            return []
        due = []
        for repo in self._repos.values():
            nr_str = repo.get("next_refresh")
            if nr_str is None:
                continue
            try:
                nr = float(nr_str)
            except (ValueError, TypeError):
                continue
            if nr <= now:
                due.append((nr, repo))
        due.sort(key=lambda t: t[0])
        return [repo for _, repo in due[:limit]]

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

    # Sentinel distinguishes "attribute was never set" from "attribute was
    # explicitly set to None" so teardown restores the TRUE prior state
    # (including removing the attribute entirely) instead of leaking
    # "postgres" mode into later tests whenever the saved value happened to
    # be None/unset.
    _unset = object()
    saved_storage_mode = getattr(app_module.app.state, "storage_mode", _unset)
    saved_backend_registry = getattr(app_module.app.state, "backend_registry", _unset)
    saved_golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", _unset)

    def _restore(attr_name: str, saved_value: Any) -> None:
        if saved_value is _unset:
            if hasattr(app_module.app.state, attr_name):
                delattr(app_module.app.state, attr_name)
        else:
            setattr(app_module.app.state, attr_name, saved_value)

    try:
        app_module.app.state.storage_mode = "postgres"
        app_module.app.state.backend_registry = fake_registry
        if golden_repos_dir is not None:
            app_module.app.state.golden_repos_dir = golden_repos_dir
        yield fake_registry
    finally:
        _restore("storage_mode", saved_storage_mode)
        _restore("backend_registry", saved_backend_registry)
        if golden_repos_dir is not None:
            _restore("golden_repos_dir", saved_golden_repos_dir)


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


# ---------------------------------------------------------------------------
# Test 5: Bug #1308 - RefreshScheduler resolves PostgreSQL registry in cluster
# mode instead of an empty per-node SQLite registry bound at construction.
# ---------------------------------------------------------------------------


def _make_refresh_scheduler(golden_repos_dir: Path):
    """Build a RefreshScheduler with real (non-mock) minimal collaborators and
    NO registry injected, so it exercises the production registry-resolution
    path (Bug #1308)."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.config import ConfigManager

    config_mgr = ConfigManager(golden_repos_dir.parent / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)

    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
    )


def test_refresh_scheduler_resolves_postgres_registry_in_cluster_mode(
    tmp_path: Path,
) -> None:
    """
    Bug #1308: on a freshly-installed cluster node the local SQLite global
    registry is empty, but PostgreSQL global_repos already has the repo.
    RefreshScheduler._resolve_global_alias() (and therefore
    trigger_refresh_for_repo()) must resolve against the shared PostgreSQL
    backend, not raise ValueError against the empty local SQLite registry.
    """
    backend = _make_backend_with_one_repo("typer-global", "typer")

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    scheduler = _make_refresh_scheduler(golden_repos_dir)

    with _app_state_postgres_mode(backend, str(golden_repos_dir)):
        resolved_alias = scheduler._resolve_global_alias("typer")

    assert resolved_alias == "typer-global"


def test_refresh_scheduler_uses_sqlite_registry_in_solo_mode(tmp_path: Path) -> None:
    """
    Regression: outside postgres/cluster mode (no app.state.backend_registry,
    i.e. CLI/solo), RefreshScheduler must still resolve via the local SQLite
    GlobalRegistry exactly as before the Bug #1308 fix.
    """
    from code_indexer.global_repos.global_registry import GlobalRegistry

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    scheduler = _make_refresh_scheduler(golden_repos_dir)

    assert isinstance(scheduler.registry, GlobalRegistry)
    assert scheduler.registry._use_sqlite is True


# ---------------------------------------------------------------------------
# Test 6: Bug #1308 - GlobalActivator resolves PostgreSQL registry in cluster
# mode instead of an empty per-node SQLite registry bound at construction.
# ---------------------------------------------------------------------------


def test_global_activator_resolves_postgres_registry_in_cluster_mode(
    tmp_path: Path,
) -> None:
    """
    Bug #1308: GlobalActivator.is_globally_active() must see repos registered
    only in the shared PostgreSQL global_repos backend, not just the empty
    per-node SQLite registry.
    """
    from code_indexer.global_repos.global_activation import GlobalActivator

    backend = _make_backend_with_one_repo("typer-global", "typer")

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    activator = GlobalActivator(str(golden_repos_dir))

    with _app_state_postgres_mode(backend, str(golden_repos_dir)):
        assert activator.is_globally_active("typer") is True


# ---------------------------------------------------------------------------
# Bug #1308 remediation: PostgresGlobalRegistryAdapter write/execute surface.
#
# The read-only adapter (list_global_repos/get_global_repo/update_enable_temporal)
# is not enough: RefreshScheduler and GlobalActivator call ~9 methods on
# self.registry. Without full forwarding, cluster-mode refresh-execution and
# activation raise AttributeError the moment they touch a write method.
# ---------------------------------------------------------------------------


def test_adapter_register_global_repo_delegates_to_backend_register_repo(
    tmp_path: Path,
) -> None:
    """
    PostgresGlobalRegistryAdapter.register_global_repo() must delegate to
    backend.register_repo(), mapping arguments correctly (GlobalActivator
    calls this at activate_golden_repo() time).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = FakeGlobalReposBackend()
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.register_global_repo(
        repo_name="my-repo",
        alias_name="my-repo-global",
        repo_url="https://example.com/my-repo.git",
        index_path="/data/golden-repos/my-repo",
        enable_temporal=True,
        temporal_options={"max_commits": 10},
    )

    assert len(backend.register_repo_calls) == 1
    (
        alias_name,
        repo_name,
        repo_url,
        index_path,
        enable_temporal,
        temporal_options,
        enable_scip,
    ) = backend.register_repo_calls[0]
    assert alias_name == "my-repo-global"
    assert repo_name == "my-repo"
    assert repo_url == "https://example.com/my-repo.git"
    assert index_path == "/data/golden-repos/my-repo"
    assert enable_temporal is True
    assert temporal_options == {"max_commits": 10}
    assert enable_scip is False
    assert backend.get_repo("my-repo-global") is not None


def test_adapter_register_global_repo_rejects_non_global_suffix(
    tmp_path: Path,
) -> None:
    """
    Preserves GlobalRegistry.register_global_repo()'s alias-validation
    semantics: alias_name must end with '-global' (case-insensitive).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = FakeGlobalReposBackend()
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)

    import pytest

    with pytest.raises(ValueError):
        registry.register_global_repo(
            repo_name="my-repo",
            alias_name="my-repo",  # missing -global suffix
            repo_url=None,
            index_path="/data/golden-repos/my-repo",
        )
    assert len(backend.register_repo_calls) == 0


def test_adapter_unregister_global_repo_delegates_to_backend_delete_repo(
    tmp_path: Path,
) -> None:
    """
    PostgresGlobalRegistryAdapter.unregister_global_repo() must delegate to
    backend.delete_repo() (GlobalActivator.deactivate_golden_repo() calls this).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("gone-global", "gone")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.unregister_global_repo("gone-global")

    assert backend.delete_repo_calls == ["gone-global"]
    assert backend.get_repo("gone-global") is None


def test_adapter_update_refresh_timestamp_delegates_to_update_last_refresh(
    tmp_path: Path,
) -> None:
    """
    PostgresGlobalRegistryAdapter.update_refresh_timestamp() must delegate to
    backend.update_last_refresh() -- the backend method NAME DIFFERS from the
    GlobalRegistry API name (RefreshScheduler._execute_refresh() calls this
    after a successful refresh).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("typer-global", "typer")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.update_refresh_timestamp("typer-global")

    assert backend.update_last_refresh_calls == ["typer-global"]


def test_adapter_update_next_refresh_converts_float_to_str(tmp_path: Path) -> None:
    """
    PostgresGlobalRegistryAdapter.update_next_refresh() must convert the
    float Unix timestamp to str (matching GlobalRegistry.update_next_refresh()
    semantics) before delegating to backend.update_next_refresh(), and pass
    None through unchanged when clearing.
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("typer-global", "typer")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.update_next_refresh("typer-global", 1700000000.5)
    registry.update_next_refresh("typer-global", None)

    assert backend.update_next_refresh_calls[0] == ("typer-global", "1700000000.5")
    assert backend.update_next_refresh_calls[1] == ("typer-global", None)


def test_adapter_update_enable_scip_delegates_to_backend(tmp_path: Path) -> None:
    """PostgresGlobalRegistryAdapter.update_enable_scip() must delegate to backend."""
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("typer-global", "typer")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    registry.update_enable_scip("typer-global", True)

    assert backend.update_enable_scip_calls == [("typer-global", True)]


def test_adapter_list_due_repos_delegates_to_backend(tmp_path: Path) -> None:
    """
    PostgresGlobalRegistryAdapter.list_due_repos() must delegate to
    backend.list_due_repos() (RefreshScheduler's poll loop calls this to find
    repos due for auto-refresh).
    """
    from code_indexer.server.utils.registry_factory import get_server_global_registry

    backend = _make_backend_with_one_repo("typer-global", "typer")
    backend.update_next_refresh("typer-global", "100")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    registry = get_server_global_registry(golden_repos_dir, backend=backend)
    due = registry.list_due_repos(limit=10, now=200.0)

    assert len(due) == 1
    assert due[0]["alias_name"] == "typer-global"


def test_adapter_forwards_every_call_site_used_by_scheduler_and_activator() -> None:
    """
    Completeness sweep (Bug #1308 item #3): for every `self.registry.<method>`
    call site found in refresh_scheduler.py and global_activation.py, the
    PostgresGlobalRegistryAdapter class must expose that method. This is the
    regression guard against a future caller silently reintroducing the
    partial-adapter split-brain (AttributeError in cluster mode only).
    """
    import inspect
    import re

    from code_indexer.global_repos import refresh_scheduler as _refresh_scheduler_mod
    from code_indexer.global_repos import global_activation as _global_activation_mod
    from code_indexer.server.utils.registry_factory import (
        PostgresGlobalRegistryAdapter,
    )

    call_site_pattern = re.compile(r"self\.registry\.([a-zA-Z_][a-zA-Z0-9_]*)\(")

    method_names = set()
    for module in (_refresh_scheduler_mod, _global_activation_mod):
        source = inspect.getsource(module)
        method_names.update(call_site_pattern.findall(source))

    assert method_names, (
        "Expected to find at least one self.registry.<method>() call site"
    )

    missing = [
        m for m in sorted(method_names) if not hasattr(PostgresGlobalRegistryAdapter, m)
    ]
    assert missing == [], (
        f"PostgresGlobalRegistryAdapter is missing methods called by "
        f"RefreshScheduler/GlobalActivator: {missing}"
    )


def test_global_activator_uses_sqlite_registry_in_solo_mode(tmp_path: Path) -> None:
    """
    Regression: outside postgres/cluster mode, GlobalActivator must still use
    the local SQLite GlobalRegistry exactly as before the Bug #1308 fix.
    """
    from code_indexer.global_repos.global_activation import GlobalActivator
    from code_indexer.global_repos.global_registry import GlobalRegistry

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    activator = GlobalActivator(str(golden_repos_dir))

    assert isinstance(activator.registry, GlobalRegistry)
    assert activator.registry._use_sqlite is True


# ---------------------------------------------------------------------------
# Bug #1308 remediation item #3: write-path integration through the REAL
# RefreshScheduler/GlobalActivator in cluster mode (not just the adapter
# directly) -- proves the full wiring end-to-end.
# ---------------------------------------------------------------------------


def test_scheduler_registry_list_due_repos_and_update_next_refresh_in_cluster_mode(
    tmp_path: Path,
) -> None:
    """
    A real RefreshScheduler (no injected registry) constructed while
    storage_mode=postgres must resolve scheduler.registry to the postgres
    adapter, and scheduler.registry.list_due_repos()/update_next_refresh()
    (used by the scheduler's own poll loop) must reach the shared backend.
    """
    backend = _make_backend_with_one_repo("typer-global", "typer")
    backend.update_next_refresh("typer-global", "100")

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    scheduler = _make_refresh_scheduler(golden_repos_dir)

    with _app_state_postgres_mode(backend, str(golden_repos_dir)):
        due = scheduler.registry.list_due_repos(limit=10, now=200.0)
        assert len(due) == 1
        assert due[0]["alias_name"] == "typer-global"

        scheduler.registry.update_next_refresh("typer-global", 999.0)

    assert backend.update_next_refresh_calls[-1] == ("typer-global", "999.0")


def test_execute_refresh_reaches_update_refresh_timestamp_in_cluster_mode(
    tmp_path: Path,
) -> None:
    """
    Bug #1308: RefreshScheduler._execute_refresh() must reach
    registry.update_refresh_timestamp() -> backend.update_last_refresh() for a
    repo registered ONLY in the shared PostgreSQL backend (empty local
    SQLite) -- proving the full write path (not just the read path) works in
    cluster mode. Mirrors the existing
    test_execute_refresh_calls_index_source_then_create_snapshot pattern
    (tests/unit/global_repos/test_refresh_scheduler_index_source_first.py),
    mocking only the heavy indexing/git internals -- registry.
    update_refresh_timestamp is left UNMOCKED so it reaches the real fake
    backend.
    """
    import json as _json
    from unittest.mock import patch, MagicMock

    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    backend = FakeGlobalReposBackend()
    alias_name = "exec-refresh-cluster-global"
    source_repo = golden_repos_dir / "exec-refresh-cluster"
    source_repo.mkdir(parents=True, exist_ok=True)
    backend.register_repo(
        alias_name=alias_name,
        repo_name="exec-refresh-cluster",
        repo_url="git@github.com:org/repo.git",
        index_path=str(source_repo),
    )

    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(exist_ok=True)
    (aliases_dir / f"{alias_name}.json").write_text(
        _json.dumps({"target_path": str(source_repo)})
    )

    scheduler = _make_refresh_scheduler(golden_repos_dir)

    fake_snapshot_path = str(
        golden_repos_dir / ".versioned" / "exec-refresh-cluster" / "v_1234567890"
    )

    def mock_create_snapshot(alias_name, source_path):
        Path(fake_snapshot_path).mkdir(parents=True, exist_ok=True)
        return fake_snapshot_path

    with _app_state_postgres_mode(backend, str(golden_repos_dir)):
        with patch.object(scheduler, "_index_source"):
            with patch.object(
                scheduler, "_create_snapshot", side_effect=mock_create_snapshot
            ):
                with patch.object(scheduler.alias_manager, "swap_alias"):
                    with patch.object(scheduler.cleanup_manager, "schedule_cleanup"):
                        with patch.object(
                            scheduler, "_detect_existing_indexes", return_value={}
                        ):
                            with patch.object(
                                scheduler, "_reconcile_registry_with_filesystem"
                            ):
                                with patch(
                                    "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
                                ) as mock_gpu:
                                    mock_updater = MagicMock()
                                    mock_updater.has_changes.return_value = True
                                    mock_updater.get_source_path.return_value = str(
                                        source_repo
                                    )
                                    mock_gpu.return_value = mock_updater

                                    result = scheduler._execute_refresh(alias_name)

    assert result["success"] is True
    assert backend.update_last_refresh_calls == [alias_name], (
        "RefreshScheduler._execute_refresh() must reach "
        "registry.update_refresh_timestamp() -> backend.update_last_refresh() "
        "in cluster mode (Bug #1308)."
    )


# ---------------------------------------------------------------------------
# Bug #1308 remediation item #5: CLI must never import code_indexer.server.app
# just to discover "not in server mode -> use SQLite".
# ---------------------------------------------------------------------------


def test_resolve_backend_registry_state_never_imports_server_app() -> None:
    """
    resolve_backend_registry_state() must NOT trigger a FRESH import of
    code_indexer.server.app (which builds the whole FastAPI app -- routers,
    DB pools, etc. -- expensive and previously unpaid by GlobalActivator's
    CLI usage, since it used to eagerly bind SQLite with no import at all).

    Simulates a pristine CLI-only process by removing
    code_indexer.server.app from sys.modules first. A `from
    code_indexer.server import app` statement would re-populate sys.modules;
    a `sys.modules.get()` lookup would not -- so the module staying absent
    proves no import was triggered.
    """
    import sys

    from code_indexer.server.utils.registry_factory import (
        resolve_backend_registry_state,
    )

    saved = sys.modules.pop("code_indexer.server.app", None)
    try:
        backend, postgres_mode_without_backend = resolve_backend_registry_state()

        assert "code_indexer.server.app" not in sys.modules, (
            "resolve_backend_registry_state() must not import "
            "code_indexer.server.app -- CLI processes must never pay that "
            "cost just to discover 'not in server mode, use SQLite' (Bug "
            "#1308 remediation item #5)."
        )
        assert backend is None
        assert postgres_mode_without_backend is False
    finally:
        if saved is not None:
            sys.modules["code_indexer.server.app"] = saved


def test_resolve_backend_registry_state_still_works_when_app_already_imported(
    tmp_path: Path,
) -> None:
    """
    Regression: when code_indexer.server.app IS already loaded (real server
    process, or a test that imported it), resolution must still correctly
    read app.state.storage_mode/backend_registry -- the CLI-import-cost fix
    must not break the real server-mode resolution path.
    """
    from code_indexer.server.utils.registry_factory import (
        resolve_backend_registry_state,
    )

    backend = _make_backend_with_one_repo("typer-global", "typer")
    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    with _app_state_postgres_mode(backend, golden_repos_dir):
        resolved_backend, postgres_mode_without_backend = (
            resolve_backend_registry_state()
        )

    assert resolved_backend is backend
    assert postgres_mode_without_backend is False


# ---------------------------------------------------------------------------
# Codex code-review finding on Bug #1308: RefreshScheduler.registry getter
# must not raise an incidental AttributeError('golden_repos_dir') when read
# on a bare object.__new__(RefreshScheduler) instance (before __init__ ran
# and before .registry was ever set).
# ---------------------------------------------------------------------------


def test_bare_scheduler_registry_getter_resolves_postgres_without_init(
    tmp_path: Path,
) -> None:
    """
    A bare RefreshScheduler built via object.__new__ (no __init__, so
    `golden_repos_dir` was never assigned) must still resolve `.registry` to
    the PostgreSQL adapter when a cluster backend is available -- the
    backend path doesn't need golden_repos_dir at all, so it must not crash.
    """
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    backend = _make_backend_with_one_repo("typer-global", "typer")

    scheduler = object.__new__(RefreshScheduler)

    with _app_state_postgres_mode(backend):
        resolved = scheduler.registry

    repos = resolved.list_global_repos()
    assert len(repos) == 1
    assert repos[0]["alias_name"] == "typer-global"


def test_bare_scheduler_registry_getter_raises_clear_error_in_solo_without_init() -> (
    None
):
    """
    A bare RefreshScheduler built via object.__new__ (no golden_repos_dir,
    no app.state cluster backend) must raise a clear, explicit RuntimeError
    from `.registry` -- NOT the incidental
    AttributeError('golden_repos_dir') that used to leak out of the lazy
    SQLite-fallback resolution path.
    """
    import pytest

    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)

    with pytest.raises(RuntimeError, match="golden_repos_dir"):
        _ = scheduler.registry
