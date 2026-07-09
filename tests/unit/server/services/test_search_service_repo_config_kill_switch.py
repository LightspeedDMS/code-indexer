"""Kill-switch integration for the repo-config cache (Story #1082 review).

Code-review Finding 2 (MEDIUM): the existing wiring test patches the
``_get_repo_config_cache`` accessor itself. This test instead drives
``search_service._load_repo_config()`` through the REAL accessor by setting and
clearing ``app.state.repo_config_cache`` -- exactly the lever the server uses to
turn the cache on (lifespan wires a ``RepoConfigCache``) or off (kill-switch /
not wired -> ``None``).

- Kill-switch OFF / not wired (``app.state.repo_config_cache = None``):
  ``_load_repo_config`` loads config DIRECTLY on every call (no cache).
- Wired (``app.state.repo_config_cache`` is a real ``RepoConfigCache``):
  the loader runs ONCE for a repo path; subsequent calls are served from cache.

No mocks of the code under test: a real ``config.json`` on disk drives loads and
the registry is a real ``RepoConfigCache``. Only ``app.state`` is manipulated --
the production accessor (`_get_repo_config_cache`) is exercised unmodified.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.app import app as _app
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
def _restore_app_state():
    """Snapshot/restore app.state.repo_config_cache around each test."""
    had = hasattr(_app.state, "repo_config_cache")
    prev = getattr(_app.state, "repo_config_cache", None)
    yield
    if had:
        _app.state.repo_config_cache = prev
    elif hasattr(_app.state, "repo_config_cache"):
        delattr(_app.state, "repo_config_cache")


def test_kill_switch_off_loads_directly_every_call(_restore_app_state):
    # Kill-switch OFF: the cache registry is not wired on app.state.
    _app.state.repo_config_cache = None

    # Sanity: the REAL accessor reports no registry.
    assert ss._get_repo_config_cache() is None

    with tempfile.TemporaryDirectory() as base:
        repo = _make_repo(base)
        c1 = ss._load_repo_config(repo)
        c2 = ss._load_repo_config(repo)
        # No cache -> each call is a fresh parse: distinct objects, equal content.
        assert c1 is not c2
        assert c1.model_dump() == c2.model_dump()


def test_kill_switch_on_serves_from_cache_loader_once(_restore_app_state):
    loads = {"n": 0}

    def counting_loader(path: str):
        loads["n"] += 1
        return ConfigManager.create_with_backtrack(Path(path)).get_config()

    registry = RepoConfigCache(
        config_ttl_seconds=300.0, config_max_entries=64, loader=counting_loader
    )
    # Kill-switch ON: wire the real registry onto app.state.
    _app.state.repo_config_cache = registry

    # Sanity: the REAL accessor now returns the wired registry.
    assert ss._get_repo_config_cache() is registry

    with tempfile.TemporaryDirectory() as base:
        repo = _make_repo(base)
        c1 = ss._load_repo_config(repo)
        c2 = ss._load_repo_config(repo)
        c3 = ss._load_repo_config(repo)
        # Loader ran exactly once; later calls were cache hits (same object).
        assert loads["n"] == 1
        assert c1 is c2 is c3
