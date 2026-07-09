"""Story #1082 Scenario 2: per-query config reload leaves the hot path.

search_service._load_repo_config(repo_path) must consult the app.state
repo_config_cache registry when present (so config.json is NOT re-parsed per
query), and load directly when no registry is wired (CLI / in-process / unit).

No mocks of the code under test: a real config on disk drives loads; the
registry is the real RepoConfigCache.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.services import search_service as ss
from code_indexer.server.services.query_path_cache import RepoConfigCache


def _make_repo(parent: str) -> str:
    ci = Path(parent) / ".code-indexer"
    ci.mkdir(parents=True, exist_ok=True)
    (ci / "config.json").write_text(
        json.dumps({"codebase_dir": parent, "embedding_provider": "voyage-ai"})
    )
    return parent


@pytest.fixture
def _no_registry(monkeypatch):
    monkeypatch.setattr(ss, "_get_repo_config_cache", lambda: None)
    yield


@pytest.fixture
def _registry_with_counter(monkeypatch):
    loads = {"n": 0}

    def loader(path: str):
        loads["n"] += 1
        return ConfigManager.create_with_backtrack(Path(path)).get_config()

    registry = RepoConfigCache(
        config_ttl_seconds=300.0, config_max_entries=64, loader=loader
    )
    monkeypatch.setattr(ss, "_get_repo_config_cache", lambda: registry)
    return registry, loads


def test_loads_directly_when_no_registry(_no_registry):
    with tempfile.TemporaryDirectory() as base:
        repo = _make_repo(base)
        c1 = ss._load_repo_config(repo)
        c2 = ss._load_repo_config(repo)
        # No cache -> each load is a fresh parse (distinct objects), but equal.
        assert c1.model_dump() == c2.model_dump()
        assert c1 is not c2


def test_uses_registry_when_present(_registry_with_counter):
    registry, loads = _registry_with_counter
    with tempfile.TemporaryDirectory() as base:
        repo = _make_repo(base)
        c1 = ss._load_repo_config(repo)
        c2 = ss._load_repo_config(repo)
        assert loads["n"] == 1  # config.json parsed once, not per call
        assert c1 is c2  # same cached object reused


def test_registry_invalidation_forces_reload(_registry_with_counter):
    registry, loads = _registry_with_counter
    with tempfile.TemporaryDirectory() as base:
        repo = _make_repo(base)
        ss._load_repo_config(repo)
        registry.invalidate(repo)
        ss._load_repo_config(repo)
        assert loads["n"] == 2
