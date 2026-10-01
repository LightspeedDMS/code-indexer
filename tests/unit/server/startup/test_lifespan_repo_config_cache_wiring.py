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


class TestLifespanRepoConfigLoaderVerifiesTarget:
    """Bug #1690: this closure is the REAL production code path
    search_service.py's `_load_repo_config` delegates to whenever
    app.state.repo_config_cache is wired (the default) -- fixing only
    search_service.py's own direct-load fallback branch would leave
    production still exposed via this loader (the exact
    "registered-but-unwired" trap this codebase documents elsewhere).

    `_repo_config_loader` used to blind-trust
    `ConfigManager.create_with_backtrack(repo_path).get_config()` without
    verifying the resolved config genuinely describes repo_path itself.
    Verified via source inspection (mirrors this file's existing wiring
    guards): the closure body is defined inline inside the async lifespan
    generator, not independently invocable without running full server
    startup.
    """

    def test_loader_uses_load_verified_config(self):
        source = _source()
        def_idx = source.find("def _repo_config_loader(")
        assert def_idx != -1, "_repo_config_loader definition missing"
        # Narrow window: from the def to the next top-level statement
        # (app.state.repo_config_cache = ...) that follows it.
        end_idx = source.find("app.state.repo_config_cache = ", def_idx)
        assert end_idx != -1, "could not bound _repo_config_loader's body"
        loader_body = source[def_idx:end_idx]

        assert "_ConfigManager.load_verified_config(" in loader_body, (
            "_repo_config_loader must route through "
            "ConfigManager.load_verified_config() (Bug #1690) instead of "
            "blind-trusting create_with_backtrack(...).get_config()."
        )
        assert "_ConfigManager.create_with_backtrack(" not in loader_body, (
            "_repo_config_loader must not call create_with_backtrack() "
            "directly anymore -- load_verified_config() wraps it with the "
            "Bug #1690 verification."
        )
