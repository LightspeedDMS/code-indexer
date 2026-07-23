"""
Bug #1399 CRITICAL item 1: hot-reload the 5 CRITICAL cache-family keys that
were previously silently ignored by the running server:

    - index_cache_ttl_minutes
    - fts_cache_ttl_minutes
    - index_cache_cleanup_interval
    - fts_cache_cleanup_interval
    - fts_cache_reload_on_access

Before this fix, ConfigService._update_cache_setting() only hot-reloaded
max_cache_size_mb (Bug #878 Fix B.2). The other 5 cache-family fields wrote
to the DB-backed ServerConfig object only -- the *running* HNSW/FTS cache
singletons never saw the change, and (per the root-cause analysis) the value
could not even reach the process via config.json on the next restart because
cache keys are excluded from BOOTSTRAP_KEYS.

Design decision (documented per the issue's own explicit ask): TTL changes
are applied EAGERLY to already-cached entries (mirroring the existing
size-cap fix's eager `_enforce_size_limit()` call) -- not just to new
entries going forward. This directly serves the original production
incident (operator lowers the Index Cache TTL to stop repeated cold-reload
storms; the fix must make already-hot repositories start respecting the new,
shorter TTL immediately, not only repos indexed after the change).

Cleanup-interval changes take effect on the *next* background-cleanup
sleep cycle (the cleanup thread reads `self.config.cleanup_interval_seconds`
fresh on every loop iteration -- see hnsw_index_cache.py/fts_index_cache.py
`start_background_cleanup`'s `cleanup_loop()`). This is inherent to the
existing thread design (not degraded by this fix) so no per-entry rewrite is
needed or possible for this field.

reload_on_access is read live on every FTS cache HIT
(`if self.config.reload_on_access: ...`), so a direct `cache.config` mutation
is sufficient and takes effect on the very next access.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

INITIAL_TTL_MINUTES = 10.0
UPDATED_TTL_MINUTES = 2.0

INITIAL_CLEANUP_INTERVAL_SECONDS = 60
UPDATED_CLEANUP_INTERVAL_SECONDS = 5

# Original-incident reproduction: an entry last accessed this many minutes
# ago. Under the INITIAL 10-minute TTL it is NOT expired; under the
# UPDATED (short) TTL below it MUST become expired.
STALE_ENTRY_AGE_MINUTES = 5
SHORT_INCIDENT_TTL_MINUTES = 2.0


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/unit/server/services/test_cache_hot_reload.py)
# ---------------------------------------------------------------------------


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        instance.stop_background_cleanup()
        setattr(cache_module, attr, None)


def _seed_singletons(cache_module: ModuleType) -> None:
    """Seed HNSW/FTS singletons with explicit, hermetic configs."""
    from code_indexer.server.cache.hnsw_index_cache import (
        HNSWIndexCache,
        HNSWIndexCacheConfig,
    )
    from code_indexer.server.cache.fts_index_cache import (
        FTSIndexCache,
        FTSIndexCacheConfig,
    )

    hnsw_config = HNSWIndexCacheConfig(
        ttl_minutes=INITIAL_TTL_MINUTES,
        cleanup_interval_seconds=INITIAL_CLEANUP_INTERVAL_SECONDS,
    )
    fts_config = FTSIndexCacheConfig(
        ttl_minutes=INITIAL_TTL_MINUTES,
        cleanup_interval_seconds=INITIAL_CLEANUP_INTERVAL_SECONDS,
        reload_on_access=True,
    )

    cache_module._global_cache_instance = HNSWIndexCache(config=hnsw_config)  # type: ignore[attr-defined]
    cache_module._global_fts_cache_instance = FTSIndexCache(config=fts_config)  # type: ignore[attr-defined]


def _install_hnsw_entry(
    cache, key: str, ttl_minutes: float, age_minutes: float
) -> None:
    """Install a real HNSWIndexCacheEntry with a controlled ttl/last_accessed."""
    from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCacheEntry

    now = datetime.now()
    entry = HNSWIndexCacheEntry(
        hnsw_index=object(),
        id_mapping={},
        repo_path=key,
        ttl_minutes=ttl_minutes,
        index_size_bytes=1024,
    )
    entry.created_at = now - timedelta(minutes=age_minutes)
    entry.last_accessed = now - timedelta(minutes=age_minutes)
    with cache._cache_lock:
        cache._cache[key] = entry


def _install_fts_entry(cache, key: str, ttl_minutes: float, age_minutes: float) -> None:
    from code_indexer.server.cache.fts_index_cache import FTSIndexCacheEntry

    now = datetime.now()
    entry = FTSIndexCacheEntry(
        tantivy_index=object(),
        schema=object(),
        index_dir=key,
        ttl_minutes=ttl_minutes,
        index_size_bytes=1024,
    )
    entry.created_at = now - timedelta(minutes=age_minutes)
    entry.last_accessed = now - timedelta(minutes=age_minutes)
    with cache._cache_lock:
        cache._cache[key] = entry


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_and_seed_singletons(tmp_path: Path) -> Iterator[Path]:
    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    _seed_singletons(cache_module)
    yield tmp_path
    _stop_and_clear_singletons(cache_module)


# ---------------------------------------------------------------------------
# Test 1: index_cache_ttl_minutes hot-reloads the live HNSW singleton
# ---------------------------------------------------------------------------


class TestHNSWTTLHotReload:
    def test_hot_reload_updates_hnsw_live_ttl(self, _reset_and_seed_singletons: Path):
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()
        assert cache.config.ttl_minutes == INITIAL_TTL_MINUTES

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_ttl_minutes", UPDATED_TTL_MINUTES)

        live_cache = get_global_cache()
        assert live_cache is cache
        assert live_cache.config.ttl_minutes == UPDATED_TTL_MINUTES, (
            "Bug #1399: updating index_cache_ttl_minutes via ConfigService must "
            "propagate to the live HNSW singleton's config; got "
            f"{live_cache.config.ttl_minutes!r}."
        )

    def test_hot_reload_rewrites_already_cached_hnsw_entry_ttl(
        self, _reset_and_seed_singletons: Path
    ):
        """Design decision: already-cached entries' ttl_minutes is rewritten
        eagerly, not just new entries going forward (mirrors the size-cap
        fix's eager `_enforce_size_limit()` call)."""
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()
        _install_hnsw_entry(
            cache, "/fake/repo-a", ttl_minutes=INITIAL_TTL_MINUTES, age_minutes=1
        )

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_ttl_minutes", UPDATED_TTL_MINUTES)

        entry = cache._cache["/fake/repo-a"]
        assert entry.ttl_minutes == UPDATED_TTL_MINUTES, (
            "Bug #1399: hot-reloading index_cache_ttl_minutes must rewrite "
            "the ttl_minutes of already-cached entries, not just new ones; "
            f"got {entry.ttl_minutes!r}."
        )


# ---------------------------------------------------------------------------
# Test 2: fts_cache_ttl_minutes hot-reloads the live FTS singleton
# ---------------------------------------------------------------------------


class TestFTSTTLHotReload:
    def test_hot_reload_updates_fts_live_ttl(self, _reset_and_seed_singletons: Path):
        from code_indexer.server.cache import get_global_fts_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        fts_cache = get_global_fts_cache()
        assert fts_cache.config.ttl_minutes == INITIAL_TTL_MINUTES

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "fts_cache_ttl_minutes", UPDATED_TTL_MINUTES)

        live_fts = get_global_fts_cache()
        assert live_fts is fts_cache
        assert live_fts.config.ttl_minutes == UPDATED_TTL_MINUTES

    def test_hot_reload_rewrites_already_cached_fts_entry_ttl(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_fts_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        fts_cache = get_global_fts_cache()
        _install_fts_entry(
            fts_cache, "/fake/fts-a", ttl_minutes=INITIAL_TTL_MINUTES, age_minutes=1
        )

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "fts_cache_ttl_minutes", UPDATED_TTL_MINUTES)

        entry = fts_cache._cache["/fake/fts-a"]
        assert entry.ttl_minutes == UPDATED_TTL_MINUTES


# ---------------------------------------------------------------------------
# Test 3: index_cache_cleanup_interval / fts_cache_cleanup_interval
# ---------------------------------------------------------------------------


class TestCleanupIntervalHotReload:
    def test_hot_reload_updates_hnsw_cleanup_interval(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()
        assert cache.config.cleanup_interval_seconds == INITIAL_CLEANUP_INTERVAL_SECONDS

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "cache", "index_cache_cleanup_interval", UPDATED_CLEANUP_INTERVAL_SECONDS
        )

        live_cache = get_global_cache()
        assert (
            live_cache.config.cleanup_interval_seconds
            == UPDATED_CLEANUP_INTERVAL_SECONDS
        ), (
            "Bug #1399: updating index_cache_cleanup_interval must propagate "
            f"to the live HNSW singleton; got {live_cache.config.cleanup_interval_seconds!r}."
        )

    def test_hot_reload_updates_fts_cleanup_interval(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_fts_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        fts_cache = get_global_fts_cache()
        assert (
            fts_cache.config.cleanup_interval_seconds
            == INITIAL_CLEANUP_INTERVAL_SECONDS
        )

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "cache", "fts_cache_cleanup_interval", UPDATED_CLEANUP_INTERVAL_SECONDS
        )

        live_fts = get_global_fts_cache()
        assert (
            live_fts.config.cleanup_interval_seconds == UPDATED_CLEANUP_INTERVAL_SECONDS
        )


# ---------------------------------------------------------------------------
# Test 4: fts_cache_reload_on_access
# ---------------------------------------------------------------------------


class TestFTSReloadOnAccessHotReload:
    def test_hot_reload_updates_fts_reload_on_access(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_fts_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        fts_cache = get_global_fts_cache()
        assert fts_cache.config.reload_on_access is True

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "fts_cache_reload_on_access", False)

        live_fts = get_global_fts_cache()
        assert live_fts.config.reload_on_access is False, (
            "Bug #1399: updating fts_cache_reload_on_access must propagate "
            "to the live FTS singleton."
        )


# ---------------------------------------------------------------------------
# Test 5: scope isolation -- new hot-reload keys must not clobber unrelated
# fields on the live singleton (extends TestHotReloadScopeIsolation pattern)
# ---------------------------------------------------------------------------


class TestNewHotReloadScopeIsolation:
    def test_ttl_update_does_not_touch_max_cache_size_mb(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()
        cache.config.max_cache_size_mb = 4096

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_ttl_minutes", UPDATED_TTL_MINUTES)

        assert cache.config.max_cache_size_mb == 4096, (
            "Updating index_cache_ttl_minutes must not perturb max_cache_size_mb."
        )

    def test_cleanup_interval_update_does_not_touch_ttl(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "cache", "index_cache_cleanup_interval", UPDATED_CLEANUP_INTERVAL_SECONDS
        )

        assert cache.config.ttl_minutes == INITIAL_TTL_MINUTES, (
            "Updating index_cache_cleanup_interval must not perturb ttl_minutes."
        )


# ---------------------------------------------------------------------------
# Test 6: Integration test reproducing the original production incident.
#
# "Raising the Index Cache TTL value in the Web UI had no effect" -- here we
# reproduce the inverse (operator LOWERS the TTL to stop cold-reload storms)
# and prove the already-hot entry evicts on the new, shorter schedule
# without a restart.
# ---------------------------------------------------------------------------


class TestOriginalIncidentReproduction:
    def test_lowering_ttl_evicts_already_hot_entry_without_restart(
        self, _reset_and_seed_singletons: Path
    ):
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons
        cache = get_global_cache()

        # Entry accessed 5 minutes ago; under the INITIAL 10-minute TTL it is
        # NOT expired.
        _install_hnsw_entry(
            cache,
            "/fake/evolution-repo",
            ttl_minutes=INITIAL_TTL_MINUTES,
            age_minutes=STALE_ENTRY_AGE_MINUTES,
        )
        entry = cache._cache["/fake/evolution-repo"]
        assert not entry.is_expired(), (
            "Precondition: under the initial 10-minute TTL, a 5-minute-old "
            "entry must not be expired yet."
        )

        # Operator lowers TTL via the Web-UI-equivalent API call.
        service = ConfigService(str(tmp_path))
        service.update_setting(
            "cache", "index_cache_ttl_minutes", SHORT_INCIDENT_TTL_MINUTES
        )

        # The already-cached entry must now be expired under the new TTL
        # (5 minutes old > 2-minute new TTL) -- WITHOUT a server restart.
        entry = cache._cache["/fake/evolution-repo"]
        assert entry.is_expired(), (
            "Bug #1399: after lowering index_cache_ttl_minutes, an "
            "already-cached entry older than the new TTL must report itself "
            "as expired immediately (eager per-entry TTL rewrite)."
        )

        # The background cleanup cycle (simulated directly, no sleep) must
        # evict it.
        cache._cleanup_expired_entries()
        assert "/fake/evolution-repo" not in cache._cache, (
            "Bug #1399: the live HNSW cache entry must evict on the new, "
            "shorter TTL schedule without a restart -- reproducing the fix "
            "for the original production incident."
        )
