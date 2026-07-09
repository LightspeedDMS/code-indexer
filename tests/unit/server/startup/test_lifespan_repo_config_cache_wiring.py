"""Story #1082: lifespan must wire the drift-safe repo-config cache.

The server query path used to re-parse config.json + re-run path resolve() on
EVERY request. Story #1082 installs ONE RepoConfigCache at startup, exposed on
app.state.repo_config_cache, sized/TTL'd from named CacheConfig knobs, so the
per-query config reload leaves the GIL-bound hot path.

Source-text + source-order guards (mirrors the query-executor wiring guard).
"""

from __future__ import annotations

from pathlib import Path

_PARENTS_TO_REPO_ROOT = 4
_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


class TestLifespanRepoConfigCacheWiringSource:
    def test_repo_config_cache_constructed(self):
        assert "RepoConfigCache(" in _source(), (
            "lifespan.py must construct a RepoConfigCache."
        )

    def test_repo_config_cache_exposed_on_app_state(self):
        assert "app.state.repo_config_cache = " in _source(), (
            "lifespan.py must expose 'app.state.repo_config_cache = ...'."
        )

    def test_repo_config_cache_sized_from_named_knobs(self):
        source = _source()
        assert "repo_config_cache_ttl_seconds" in source, (
            "RepoConfigCache TTL must come from the named knob, not a literal."
        )
        assert "repo_config_cache_max_entries" in source, (
            "RepoConfigCache bound must come from the named knob, not a literal."
        )


class TestLifespanRepoConfigCacheWiringOrder:
    def test_construction_before_yield(self):
        source = _source()
        create_idx = source.find("app.state.repo_config_cache = ")
        yield_idx = source.find("yield  # Server is now running")
        assert create_idx != -1, "repo_config_cache wiring statement missing"
        assert yield_idx != -1, "lifespan yield marker missing"
        assert create_idx < yield_idx, (
            "repo_config_cache must be wired before the lifespan yield (startup)."
        )
