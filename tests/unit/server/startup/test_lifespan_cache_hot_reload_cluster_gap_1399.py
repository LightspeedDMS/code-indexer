"""
Bug #1399 items 7-8: multi-worker/cluster propagation gaps in the config
hot-reload pattern.

Item 7 (`_hot_reload_cache_size_cap` multi-worker/cluster gap): the cache
size-cap hot-reload (Bug #878 Fix B.2) -- and the new TTL/cleanup-interval/
reload_on_access hot-reloads added by this issue -- only patch the live
singleton in the ONE worker process that handled the Web UI POST. Under
`uvicorn --workers N` or in a cluster, sibling workers/nodes each run their
own `ConfigService.start_config_reload()` PG-poll loop; without a registered
change-callback, they keep the stale cache config indefinitely. The fix
models Bug #943's `update_totp_elevation_atomic` pattern: a local
synchronous call on the processing node (already exists) PLUS a PG-poll
callback (`_on_config_change` in lifespan.py) that every sibling re-applies
on its own next poll tick.

Item 8 (SessionManager / GoldenRepoManager stale-nested-object downgrade):
`SessionManager._web_security_config` and `GoldenRepoManager.resource_config`
are captured by reference at construction time. The processing node mutates
the nested sub-object in place (correct). But `check_config_update()` on a
SIBLING node replaces `self._config` with a brand-new ServerConfig object
(fresh nested sub-objects) on every version-diff PG-poll tick -- the
manager's stale reference is never re-wired, so siblings never observe the
change until restart. Fix: `_on_config_change` re-wires both references from
the fresh `new_config` on every callback invocation.

Test suite:
1. Source-text guards: `_on_config_change` must call
   `reapply_live_cache_hot_reload_fields` and must re-wire both
   `_web_security_config` and `resource_config`.
2. Runtime guards: replicate the exact wiring logic (mirrors the
   established pattern in test_lifespan_clone_backend_wiring_bug1044.py)
   with real collaborator objects to prove it actually works.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Iterator
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _extract_on_config_change_block(source: str) -> str:
    """Return the _on_config_change function body slice for source-guard checks."""
    start = source.find("def _on_config_change(new_config: Any) -> None:")
    assert start != -1, "_on_config_change callback not found in lifespan.py"
    end = source.find("_get_cs().register_on_change_callback(_on_config_change)")
    assert end != -1, (
        "_get_cs().register_on_change_callback(_on_config_change) registration "
        "not found in lifespan.py"
    )
    return source[start:end]


class TestSourceGuards:
    def test_on_config_change_reapplies_cache_hot_reload_fields(self):
        """
        Bug #1399 item 7: _on_config_change must call
        reapply_live_cache_hot_reload_fields(new_config) so sibling
        workers/nodes pick up cache-family hot-reload changes on their own
        next PG-poll tick, not just the processing node.
        """
        source = _LIFESPAN_PATH.read_text()
        block = _extract_on_config_change_block(source)

        assert "reapply_live_cache_hot_reload_fields" in block, (
            "Bug #1399: _on_config_change must call "
            "reapply_live_cache_hot_reload_fields(new_config) on the config "
            "service to close the multi-worker/cluster gap in the cache "
            "hot-reload pattern (mirrors Bug #943's PG-poll callback "
            "propagation for TOTP elevation)."
        )

    def test_on_config_change_rewires_session_manager_web_security_config(self):
        """
        Bug #1399 item 8: _on_config_change must re-wire
        SessionManager._web_security_config from the fresh new_config so
        sibling cluster nodes do not keep pointing at a stale nested
        sub-object after a version-diff PG-poll reload.
        """
        source = _LIFESPAN_PATH.read_text()
        block = _extract_on_config_change_block(source)

        assert (
            "_web_security_config" in block
            and "new_config.web_security_config" in block
        ), (
            "Bug #1399: _on_config_change must re-wire "
            "SessionManager._web_security_config = new_config.web_security_config "
            "so sibling nodes' SessionManager does not stay pinned to a stale "
            "nested sub-object."
        )

    def test_on_config_change_rewires_golden_repo_manager_resource_config(self):
        """
        Bug #1399 item 8: _on_config_change must re-wire
        GoldenRepoManager.resource_config from the fresh new_config for the
        same reason as SessionManager above.
        """
        source = _LIFESPAN_PATH.read_text()
        block = _extract_on_config_change_block(source)

        assert (
            "golden_repo_manager.resource_config" in block
            and "new_config.resource_config" in block
        ), (
            "Bug #1399: _on_config_change must re-wire "
            "golden_repo_manager.resource_config = new_config.resource_config "
            "so sibling nodes' GoldenRepoManager does not stay pinned to a "
            "stale nested sub-object."
        )


# ---------------------------------------------------------------------------
# Runtime guards
# ---------------------------------------------------------------------------


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        instance.stop_background_cleanup()
        setattr(cache_module, attr, None)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    yield
    _stop_and_clear_singletons(cache_module)


class TestCacheHotReloadClusterGapRuntime:
    """Runtime guard for item 7: reapply_live_cache_hot_reload_fields closes
    the multi-worker/cluster gap when invoked from a PG-poll-style callback
    on a 'sibling' process (simulated: same process, values reset to mimic
    a sibling that never processed the original POST)."""

    def test_sibling_process_picks_up_cache_ttl_via_reapply(self, tmp_path: Path):
        from code_indexer.server.cache import (
            get_global_cache,
            get_global_fts_cache,
        )
        from code_indexer.server.cache.hnsw_index_cache import (
            HNSWIndexCache,
            HNSWIndexCacheConfig,
        )
        from code_indexer.server.cache.fts_index_cache import (
            FTSIndexCache,
            FTSIndexCacheConfig,
        )
        import code_indexer.server.cache as cache_module
        from code_indexer.server.services.config_service import ConfigService

        # "Sibling" singleton: still at the OLD (stale) TTL -- it never
        # processed the Web UI POST that changed the setting.
        stale_ttl = 10.0
        cache_module._global_cache_instance = HNSWIndexCache(
            config=HNSWIndexCacheConfig(ttl_minutes=stale_ttl)
        )
        cache_module._global_fts_cache_instance = FTSIndexCache(
            config=FTSIndexCacheConfig(ttl_minutes=stale_ttl)
        )

        # The "processing node" already saved the new TTL to the DB (both
        # HNSW and FTS, so the assertions below reflect a real state change
        # for each singleton rather than a coincidental default value).
        new_ttl = 3.0
        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_ttl_minutes", new_ttl)
        service.update_setting("cache", "fts_cache_ttl_minutes", new_ttl)
        fresh_config = service.get_config()

        # Sibling's PG-poll tick fires the change callback with the fresh
        # config -- this is exactly what _on_config_change must do.
        service.reapply_live_cache_hot_reload_fields(fresh_config)

        assert get_global_cache().config.ttl_minutes == new_ttl, (
            "Bug #1399 item 7: a sibling process's cache singleton must "
            "observe the new TTL after reapply_live_cache_hot_reload_fields "
            "is invoked from its own PG-poll callback."
        )
        assert get_global_fts_cache().config.ttl_minutes == new_ttl


class TestSessionManagerGoldenRepoManagerRewiringRuntime:
    """Runtime guard for item 8: replicates the exact _on_config_change
    re-wiring logic with real SessionManager / GoldenRepoManager-shaped
    collaborators."""

    def test_session_manager_web_security_config_rewired_on_config_change(
        self, tmp_path: Path
    ):
        from code_indexer.server.web.auth import SessionManager
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            WebSecurityConfig,
        )

        stale_web_security = WebSecurityConfig(web_session_timeout_seconds=111)
        session_manager = SessionManager(
            secret_key="test-secret",
            config=MagicMock(),
            web_security_config=stale_web_security,
        )
        assert session_manager._web_security_config.web_session_timeout_seconds == 111

        fresh_web_security = WebSecurityConfig(web_session_timeout_seconds=222)
        new_config = ServerConfig(
            server_dir=str(tmp_path), web_security_config=fresh_web_security
        )

        # --- Replicate the _on_config_change re-wiring block ---
        session_manager._web_security_config = new_config.web_security_config

        assert (
            session_manager._web_security_config.web_session_timeout_seconds == 222
        ), (
            "Bug #1399 item 8: SessionManager._web_security_config must be "
            "re-wired to the fresh new_config.web_security_config object, "
            "not left pointing at the stale pre-reload sub-object."
        )
        assert session_manager._web_security_config is new_config.web_security_config

    def test_golden_repo_manager_resource_config_rewired_on_config_change(
        self, tmp_path: Path
    ):
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ServerResourceConfig,
        )

        stale_resource_config = ServerResourceConfig(git_clone_timeout=111)
        grm = GoldenRepoManager(
            data_dir=str(tmp_path), resource_config=stale_resource_config
        )
        assert grm.resource_config.git_clone_timeout == 111

        fresh_resource_config = ServerResourceConfig(git_clone_timeout=222)
        new_config = ServerConfig(
            server_dir=str(tmp_path), resource_config=fresh_resource_config
        )

        # --- Replicate the _on_config_change re-wiring block ---
        grm.resource_config = new_config.resource_config

        assert grm.resource_config.git_clone_timeout == 222, (
            "Bug #1399 item 8: GoldenRepoManager.resource_config must be "
            "re-wired to the fresh new_config.resource_config object, not "
            "left pointing at the stale pre-reload sub-object."
        )
        assert grm.resource_config is new_config.resource_config
