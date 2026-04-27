"""
Tests for Fix B.2 (Issue #878): Runtime hot-reload of max_cache_size_mb on
live HNSW / FTS cache singletons via ConfigService._update_cache_setting().

Fix B.1 gave the server an opinionated 4096 MB default cap for both caches.
Fix B.2 closes the remaining gap: when an operator later lowers (or raises)
that cap via the Web UI / config_service, the change must also take effect
on the already-running cache singletons -- NOT just on the config file on
disk. Without this, operators have no way to bound native memory at runtime;
they would have to restart the server to pick up a new cap.

Contract under test:

1. After `update_setting("cache", "index_cache_max_size_mb", X)`, the live
   HNSW singleton's ``config.max_cache_size_mb`` equals X.
2. After `update_setting("cache", "fts_cache_max_size_mb", X)`, the live
   FTS singleton's ``config.max_cache_size_mb`` equals X.
3. Lowering the HNSW cap below the current cache footprint triggers
   ``_enforce_size_limit()`` on the live cache so ``total_memory_mb`` drops
   to at most the new cap.
4. Hot-reload is thread-safe: concurrent reads/mutations on the cache
   alongside repeated ``_update_cache_setting`` calls never raise and the
   final cap matches the last update.
5. Updating an unrelated cache setting (e.g. ``index_cache_ttl_minutes``)
   does NOT mutate ``cache.config.max_cache_size_mb``.

Tests use real cache singletons and a real ConfigService (no mocking of
core behavior). Singletons are hermetic: seeded directly in the fixture
with a controlled config so no file / env state from the developer
machine can leak into the tests.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Named constants (avoid magic numbers; document intent)
# ---------------------------------------------------------------------------

# Initial seeded cap (MB) applied to both singletons by the fixture.
# Matches Fix B.1's opinionated default so tests read naturally.
INITIAL_CAP_MB = 4096

# HNSW update target for the basic hot-reload contract test.
HNSW_UPDATED_CAP_MB = 2048

# FTS update target for the basic hot-reload contract test.
FTS_UPDATED_CAP_MB = 1024

# Eviction-test payload: four injected entries of this size...
INJECTED_ENTRY_MB = 100
INJECTED_ENTRY_COUNT = 4
INJECTED_TOTAL_MB = INJECTED_ENTRY_MB * INJECTED_ENTRY_COUNT  # 400 MB

# ...then we lower the cap to this value and expect eviction down to <= this.
EVICTION_TARGET_CAP_MB = 200

# Minimum number of evictions expected for the eviction test:
# 400 MB -> <=200 MB means at least 2 entries of 100 MB must be evicted.
MIN_EXPECTED_EVICTIONS = 2

# Thread-safety test parameters.
CHURN_ENTRY_MB = 5  # size of each churn entry (small; eviction not the point)
CHURN_KEY_SLOTS = 3  # rotating slots for the churn worker
CHURN_SLEEP_SECONDS = 0.001  # back-pressure so we don't peg a CPU core
CHURN_MAX_ITERATIONS = 20_000  # hard upper bound (statically provable)
# Worker self-deadline: never runs longer than this even if main thread hangs.
CHURN_SELF_DEADLINE_SECONDS = 2.5
# Main thread bound: stop issuing updates once this window has elapsed.
MAIN_THREAD_DEADLINE_SECONDS = 1.5
# Pause between hot-reload updates on the main thread.
MAIN_UPDATE_INTERVAL_SECONDS = 0.05
# Join grace period for the churn worker on teardown.
WORKER_JOIN_TIMEOUT_SECONDS = 2.0

# Cap sequence for the thread-safety test. The LAST value is the expected
# final cap on the live singleton.
CAP_SEQUENCE_MB = (1024, 2048, 512, 3072, 256)

# Scope-isolation probe: a distinctive cap we can detect being clobbered.
DISTINCTIVE_CAP_MB = 777
# Unrelated-setting probe value (TTL minutes) that must NOT touch the cap.
UNRELATED_TTL_MINUTES = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    """
    Stop cleanup threads on HNSW / FTS singletons and reset module slots.

    ``stop_background_cleanup()`` is idempotent by construction (it guards
    with ``if self._cleanup_thread and self._cleanup_thread.is_alive()``)
    and ``join(timeout=5)`` does not raise, so no try/except is needed --
    unexpected exceptions propagate and fail teardown loudly rather than
    silently leaving stale singletons behind.
    """
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        instance.stop_background_cleanup()
        setattr(cache_module, attr, None)


def _seed_singletons(cache_module: ModuleType, cap_mb: int) -> None:
    """
    Directly construct HNSW and FTS singletons with an explicit cap, bypassing
    the file / env loading path in ``get_global_cache()`` / ``get_global_fts_cache()``.

    This keeps the tests hermetic from any real ``~/.cidx-server/config.json``
    or environment variables on the developer machine -- no monkey-patching
    of ``Path.home()`` required.
    """
    from code_indexer.server.cache.hnsw_index_cache import (
        HNSWIndexCache,
        HNSWIndexCacheConfig,
    )
    from code_indexer.server.cache.fts_index_cache import (
        FTSIndexCache,
        FTSIndexCacheConfig,
    )

    hnsw_config = HNSWIndexCacheConfig(max_cache_size_mb=cap_mb)
    fts_config = FTSIndexCacheConfig(max_cache_size_mb=cap_mb)

    cache_module._global_cache_instance = HNSWIndexCache(config=hnsw_config)  # type: ignore[attr-defined]  # private module singleton set directly for test isolation
    cache_module._global_fts_cache_instance = FTSIndexCache(config=fts_config)  # type: ignore[attr-defined]  # private module singleton set directly for test isolation


def _install_fake_entry(
    cache, key: str, estimated_mb: int, created_offset_seconds: int = 0
) -> None:
    """
    Inject a synthetic cache entry with a known memory footprint so we can
    exercise ``_enforce_size_limit()`` without having to build real HNSW /
    Tantivy indexes on disk. The entry's ``index_size_bytes`` drives the
    MB accounting that ``_enforce_size_limit`` uses.

    Must be called while holding ``cache._cache_lock``.
    """
    from datetime import datetime, timedelta

    # Lazy import to respect the cache module's internal structure.
    from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCacheEntry

    now = datetime.now() + timedelta(seconds=created_offset_seconds)
    entry = HNSWIndexCacheEntry(
        hnsw_index=object(),  # opaque placeholder: _enforce_size_limit never touches it
        id_mapping={},
        repo_path=key,
        ttl_minutes=cache.config.ttl_minutes,
        index_size_bytes=estimated_mb * 1024 * 1024,
    )
    entry.created_at = now
    entry.last_accessed = now
    cache._cache[key] = entry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_and_seed_singletons(tmp_path: Path) -> Iterator[Path]:
    """
    Reset HNSW and FTS global singletons around each test and seed them
    explicitly with a controlled config (cap = INITIAL_CAP_MB).

    Yields the tmp dir so tests can use it as ConfigService server_dir.
    """
    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    _seed_singletons(cache_module, cap_mb=INITIAL_CAP_MB)
    yield tmp_path
    _stop_and_clear_singletons(cache_module)


# ---------------------------------------------------------------------------
# Test 1: HNSW live singleton cap is updated on hot-reload
# ---------------------------------------------------------------------------


class TestHNSWHotReload:
    """`_update_cache_setting` must update HNSW live singleton's cap."""

    def test_hot_reload_updates_hnsw_live_singleton_cap(
        self, _reset_and_seed_singletons: Path
    ) -> None:
        """
        1. Seeded HNSW singleton has cap = INITIAL_CAP_MB.
        2. Call update_setting("cache", "index_cache_max_size_mb", X).
        3. Assert the LIVE singleton's config.max_cache_size_mb == X.
        """
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons

        cache = get_global_cache()
        assert cache.config.max_cache_size_mb == INITIAL_CAP_MB, (
            "Precondition: fixture must seat the live cap at "
            f"{INITIAL_CAP_MB} MB; got {cache.config.max_cache_size_mb!r}."
        )

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_max_size_mb", HNSW_UPDATED_CAP_MB)

        live_cache = get_global_cache()
        assert live_cache is cache, (
            "Hot-reload must update the existing singleton, not replace it."
        )
        assert live_cache.config.max_cache_size_mb == HNSW_UPDATED_CAP_MB, (
            "Fix B.2: updating index_cache_max_size_mb via ConfigService must "
            "propagate to the live HNSW singleton's config; got "
            f"{live_cache.config.max_cache_size_mb!r}."
        )


# ---------------------------------------------------------------------------
# Test 2: FTS live singleton cap is updated on hot-reload
# ---------------------------------------------------------------------------


class TestFTSHotReload:
    """`_update_cache_setting` must update FTS live singleton's cap."""

    def test_hot_reload_updates_fts_live_singleton_cap(
        self, _reset_and_seed_singletons: Path
    ) -> None:
        """Parallel to HNSW test but for the FTS cache."""
        from code_indexer.server.cache import get_global_fts_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons

        fts_cache = get_global_fts_cache()
        assert fts_cache.config.max_cache_size_mb == INITIAL_CAP_MB, (
            "Precondition: fixture must seat the FTS cap at "
            f"{INITIAL_CAP_MB} MB; got {fts_cache.config.max_cache_size_mb!r}."
        )

        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "fts_cache_max_size_mb", FTS_UPDATED_CAP_MB)

        live_fts = get_global_fts_cache()
        assert live_fts is fts_cache, (
            "Hot-reload must update the existing FTS singleton, not replace it."
        )
        assert live_fts.config.max_cache_size_mb == FTS_UPDATED_CAP_MB, (
            "Fix B.2: updating fts_cache_max_size_mb via ConfigService must "
            "propagate to the live FTS singleton's config; got "
            f"{live_fts.config.max_cache_size_mb!r}."
        )


# ---------------------------------------------------------------------------
# Test 3: Lowering the HNSW cap evicts entries to obey the new limit
# ---------------------------------------------------------------------------


class TestHotReloadTriggersEviction:
    """Hot-reload must invoke `_enforce_size_limit()` on the live cache."""

    def test_hot_reload_triggers_enforce_size_limit_on_hnsw(
        self, _reset_and_seed_singletons: Path
    ) -> None:
        """
        1. Seeded HNSW (cap = INITIAL_CAP_MB = 4096).
        2. Inject INJECTED_ENTRY_COUNT entries of INJECTED_ENTRY_MB each
           (total ~INJECTED_TOTAL_MB) -- fits under the initial cap.
        3. Update cap to EVICTION_TARGET_CAP_MB.
        4. Assert cache.get_stats().total_memory_mb <= EVICTION_TARGET_CAP_MB.
        """
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons

        cache = get_global_cache()
        with cache._cache_lock:
            for i in range(INJECTED_ENTRY_COUNT):
                _install_fake_entry(
                    cache,
                    key=f"/fake/repo-{i}",
                    estimated_mb=INJECTED_ENTRY_MB,
                    created_offset_seconds=i,  # strict LRU ordering
                )

        initial_stats = cache.get_stats()
        assert initial_stats.total_memory_mb == pytest.approx(
            float(INJECTED_TOTAL_MB), abs=1.0
        ), (
            "Precondition: injected entries must total "
            f"~{INJECTED_TOTAL_MB} MB. Got {initial_stats.total_memory_mb}."
        )

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "cache", "index_cache_max_size_mb", EVICTION_TARGET_CAP_MB
        )

        post_stats = cache.get_stats()
        assert post_stats.total_memory_mb <= EVICTION_TARGET_CAP_MB, (
            f"Fix B.2: after lowering the cap to {EVICTION_TARGET_CAP_MB} MB, "
            "the live cache must evict LRU entries down to at most that cap. "
            f"Got total_memory_mb={post_stats.total_memory_mb}, "
            f"cached_repositories={post_stats.cached_repositories}."
        )
        assert post_stats.eviction_count >= MIN_EXPECTED_EVICTIONS, (
            "Eviction counter must reflect the evictions driven by the new cap; "
            f"expected >= {MIN_EXPECTED_EVICTIONS}, "
            f"got eviction_count={post_stats.eviction_count}."
        )


# ---------------------------------------------------------------------------
# Test 4: Hot-reload is thread-safe under concurrent cache mutation
# ---------------------------------------------------------------------------


class TestHotReloadThreadSafety:
    """Repeated hot-reload calls must coexist with concurrent cache mutation."""

    def test_hot_reload_is_thread_safe(self, _reset_and_seed_singletons: Path) -> None:
        """
        Spawn a bounded-iteration background thread that continuously mutates
        the HNSW cache (put/remove cycle under `_cache_lock`) while the main
        thread issues several `_update_cache_setting` calls with different
        sizes.

        Worker termination has TWO independent bounds:
          * ``CHURN_MAX_ITERATIONS``: statically provable upper bound on loop.
          * ``CHURN_SELF_DEADLINE_SECONDS``: wall-clock deadline that fires
            even if ``stop_flag`` is never set (paranoid safety net).

        Success = (a) no exception raised in either thread, and (b) the
        final cap on the live singleton matches the last update value.
        """
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons

        cache = get_global_cache()
        service = ConfigService(str(tmp_path))
        stop_flag = threading.Event()
        worker_exc: list[BaseException] = []

        def churn() -> None:
            """Mutate the cache under its lock, with statically bounded iterations."""
            try:
                worker_deadline = time.monotonic() + CHURN_SELF_DEADLINE_SECONDS
                for i in range(CHURN_MAX_ITERATIONS):
                    if stop_flag.is_set():
                        return
                    if time.monotonic() > worker_deadline:
                        return
                    key = f"/fake/churn-{i % CHURN_KEY_SLOTS}"
                    with cache._cache_lock:
                        _install_fake_entry(cache, key=key, estimated_mb=CHURN_ENTRY_MB)
                    with cache._cache_lock:
                        cache._cache.pop(key, None)
                    time.sleep(CHURN_SLEEP_SECONDS)
            except BaseException as exc:  # noqa: BLE001
                worker_exc.append(exc)

        worker = threading.Thread(target=churn, name="cache-churn", daemon=True)
        worker.start()

        # Issue a bounded sequence of hot-reload updates on the main thread.
        deadline = time.monotonic() + MAIN_THREAD_DEADLINE_SECONDS
        try:
            for cap in CAP_SEQUENCE_MB:
                if time.monotonic() > deadline:
                    break
                service.update_setting("cache", "index_cache_max_size_mb", cap)
                time.sleep(MAIN_UPDATE_INTERVAL_SECONDS)
        finally:
            stop_flag.set()
            worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

        assert not worker_exc, (
            f"Background churn thread must not raise under hot-reload; "
            f"got {worker_exc!r}."
        )
        assert not worker.is_alive(), (
            "Background churn thread must exit cleanly within "
            f"{WORKER_JOIN_TIMEOUT_SECONDS}s after stop_flag set."
        )
        final_expected = CAP_SEQUENCE_MB[-1]
        assert cache.config.max_cache_size_mb == final_expected, (
            "Fix B.2 thread-safety: final cap must reflect the last update "
            f"value {final_expected!r}; got {cache.config.max_cache_size_mb!r}."
        )


# ---------------------------------------------------------------------------
# Test 5: Unrelated cache settings must NOT touch max_cache_size_mb
# ---------------------------------------------------------------------------


class TestHotReloadScopeIsolation:
    """Only max_cache_size_mb updates should trigger hot-reload of the cap."""

    def test_hot_reload_ignores_other_cache_settings(
        self, _reset_and_seed_singletons: Path
    ) -> None:
        """
        Updating an unrelated cache setting (ttl minutes) must not perturb
        the live singleton's ``max_cache_size_mb``. This guards against an
        overly broad hot-reload hook that re-overwrites the cap on every
        cache-category update.
        """
        from code_indexer.server.cache import get_global_cache
        from code_indexer.server.services.config_service import ConfigService

        tmp_path = _reset_and_seed_singletons

        cache = get_global_cache()
        service = ConfigService(str(tmp_path))
        service.update_setting("cache", "index_cache_max_size_mb", DISTINCTIVE_CAP_MB)
        assert cache.config.max_cache_size_mb == DISTINCTIVE_CAP_MB, (
            "Precondition: hot-reload must seat the distinctive cap before "
            f"the scope-isolation check. Got {cache.config.max_cache_size_mb!r}."
        )

        # Update an unrelated setting; the cap must remain DISTINCTIVE_CAP_MB.
        service.update_setting(
            "cache", "index_cache_ttl_minutes", UNRELATED_TTL_MINUTES
        )

        assert cache.config.max_cache_size_mb == DISTINCTIVE_CAP_MB, (
            "Fix B.2 scope: updating a non-size cache setting must not "
            f"clobber the live singleton's cap; expected {DISTINCTIVE_CAP_MB}, "
            f"got {cache.config.max_cache_size_mb!r}."
        )
