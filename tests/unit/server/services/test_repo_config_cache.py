"""Unit tests for RepoConfigCache (Story #1082, Scenarios 2/6/7/10/12).

RepoConfigCache routes a repo path to a NO-TTL cache ONLY when the immutable
predicate proves a .versioned/{alias}/v_* snapshot; everything else (the mutable
base clone returned by get_actual_repo_path Priority-1) uses a SHORT-TTL cache.
Both sub-caches are bounded. The cached value is the parsed Config, byte-identical
to a direct load.

No mocks: real config files on disk drive the loads; a controllable clock drives
deterministic TTL expiry.
"""

import json
import tempfile
from pathlib import Path

from code_indexer.config import ConfigManager
from code_indexer.server.services.query_path_cache import RepoConfigCache


class _Clock:
    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _make_repo(parent: str, codebase_dir: str) -> str:
    """Create a .code-indexer/config.json under parent; return parent path."""
    ci = Path(parent) / ".code-indexer"
    ci.mkdir(parents=True, exist_ok=True)
    (ci / "config.json").write_text(
        json.dumps({"codebase_dir": codebase_dir, "embedding_provider": "voyage-ai"})
    )
    return parent


def test_immutable_versioned_path_cached_with_no_ttl():
    clock = _Clock()
    with tempfile.TemporaryDirectory() as base:
        # Build .../golden-repos/.versioned/myrepo/v_123 with a real config.
        versioned = Path(base) / "golden-repos" / ".versioned" / "myrepo" / "v_123"
        versioned.mkdir(parents=True)
        _make_repo(str(versioned), str(versioned))

        loads = {"n": 0}

        def counting_loader(path: str):
            loads["n"] += 1
            return ConfigManager.create_with_backtrack(Path(path)).get_config()

        cache = RepoConfigCache(
            config_ttl_seconds=10.0,
            config_max_entries=64,
            loader=counting_loader,
            time_fn=clock,
        )

        c1 = cache.get_config(str(versioned))
        clock.advance(10_000_000.0)  # far beyond any TTL
        c2 = cache.get_config(str(versioned))

        assert loads["n"] == 1  # NO TTL: immutable -> loaded once
        assert c1.embedding_provider == "voyage-ai"
        assert c2 is c1


def test_mutable_path_uses_short_ttl_and_reloads_after_expiry():
    clock = _Clock()
    with tempfile.TemporaryDirectory() as base:
        # Mutable base clone path (no .versioned/.../v_* segment).
        repo = Path(base) / "golden-repos" / "myrepo"
        repo.mkdir(parents=True)
        _make_repo(str(repo), str(repo))

        loads = {"n": 0}

        def counting_loader(path: str):
            loads["n"] += 1
            return ConfigManager.create_with_backtrack(Path(path)).get_config()

        cache = RepoConfigCache(
            config_ttl_seconds=10.0,
            config_max_entries=64,
            loader=counting_loader,
            time_fn=clock,
        )

        cache.get_config(str(repo))  # load #1
        clock.advance(5.0)
        cache.get_config(str(repo))  # fresh hit
        assert loads["n"] == 1
        clock.advance(6.0)  # 11s > 10s TTL
        cache.get_config(str(repo))  # reload #2
        assert loads["n"] == 2


def test_invalidate_forces_reload_for_mutable_path():
    with tempfile.TemporaryDirectory() as base:
        repo = Path(base) / "golden-repos" / "myrepo"
        repo.mkdir(parents=True)
        _make_repo(str(repo), str(repo))

        loads = {"n": 0}

        def counting_loader(path: str):
            loads["n"] += 1
            return ConfigManager.create_with_backtrack(Path(path)).get_config()

        cache = RepoConfigCache(
            config_ttl_seconds=300.0, config_max_entries=64, loader=counting_loader
        )

        cache.get_config(str(repo))
        cache.invalidate(str(repo))
        cache.get_config(str(repo))
        assert loads["n"] == 2


def test_invalidate_clears_both_immutable_and_mutable_entries():
    with tempfile.TemporaryDirectory() as base:
        versioned = Path(base) / "gr" / ".versioned" / "r" / "v_1"
        versioned.mkdir(parents=True)
        _make_repo(str(versioned), str(versioned))

        loads = {"n": 0}

        def counting_loader(path: str):
            loads["n"] += 1
            return ConfigManager.create_with_backtrack(Path(path)).get_config()

        cache = RepoConfigCache(
            config_ttl_seconds=300.0, config_max_entries=64, loader=counting_loader
        )
        cache.get_config(str(versioned))
        cache.invalidate(str(versioned))
        cache.get_config(str(versioned))
        assert loads["n"] == 2


def test_cached_config_is_byte_identical_to_direct_load():
    with tempfile.TemporaryDirectory() as base:
        repo = Path(base) / "golden-repos" / "myrepo"
        repo.mkdir(parents=True)
        _make_repo(str(repo), str(repo))

        cache = RepoConfigCache(
            config_ttl_seconds=300.0,
            config_max_entries=64,
            loader=lambda p: ConfigManager.create_with_backtrack(Path(p)).get_config(),
        )

        cached = cache.get_config(str(repo))
        direct = ConfigManager.create_with_backtrack(Path(repo)).get_config()
        assert cached.model_dump() == direct.model_dump()


def test_caches_are_bounded():
    with tempfile.TemporaryDirectory() as base:
        cache = RepoConfigCache(
            config_ttl_seconds=300.0,
            config_max_entries=3,
            loader=lambda p: ConfigManager.create_with_backtrack(Path(p)).get_config(),
        )

        # Generate many distinct immutable versioned paths (refresh cycles).
        for i in range(20):
            versioned = Path(base) / "gr" / ".versioned" / "r" / f"v_{i}"
            versioned.mkdir(parents=True)
            _make_repo(str(versioned), str(versioned))
            cache.get_config(str(versioned))

        # Immutable sub-cache must respect its bound (NO-TTL but still bounded).
        assert cache.immutable_size() <= 3


def test_clear_empties_both_subcaches():
    with tempfile.TemporaryDirectory() as base:
        repo = Path(base) / "golden-repos" / "myrepo"
        repo.mkdir(parents=True)
        _make_repo(str(repo), str(repo))
        versioned = Path(base) / "gr" / ".versioned" / "r" / "v_1"
        versioned.mkdir(parents=True)
        _make_repo(str(versioned), str(versioned))

        cache = RepoConfigCache(
            config_ttl_seconds=300.0,
            config_max_entries=64,
            loader=lambda p: ConfigManager.create_with_backtrack(Path(p)).get_config(),
        )
        cache.get_config(str(repo))  # mutable
        cache.get_config(str(versioned))  # immutable
        assert cache.mutable_size() == 1
        assert cache.immutable_size() == 1
        cache.clear()
        assert cache.mutable_size() == 0
        assert cache.immutable_size() == 0


def test_mutable_size_tracks_mutable_entries():
    with tempfile.TemporaryDirectory() as base:
        cache = RepoConfigCache(
            config_ttl_seconds=300.0,
            config_max_entries=64,
            loader=lambda p: ConfigManager.create_with_backtrack(Path(p)).get_config(),
        )
        for i in range(3):
            repo = Path(base) / "golden-repos" / f"r{i}"
            repo.mkdir(parents=True)
            _make_repo(str(repo), str(repo))
            cache.get_config(str(repo))
        assert cache.mutable_size() == 3


def test_counters_exposed_for_telemetry():
    with tempfile.TemporaryDirectory() as base:
        repo = Path(base) / "golden-repos" / "myrepo"
        repo.mkdir(parents=True)
        _make_repo(str(repo), str(repo))

        cache = RepoConfigCache(
            config_ttl_seconds=300.0,
            config_max_entries=64,
            loader=lambda p: ConfigManager.create_with_backtrack(Path(p)).get_config(),
        )
        cache.get_config(str(repo))
        cache.get_config(str(repo))
        counters = cache.counters()
        assert counters["mutable"]["hit"] >= 1
        assert counters["mutable"]["reload"] == 1
        assert "immutable" in counters
