"""Unit tests for the TTLCache primitive (Story #1082).

Covers fresh-hit / miss / expiry / invalidate / clear semantics, bounded LRU
eviction, single-flight concurrency (no thundering herd), NO-TTL mode, and the
exposed hit/miss/reload/invalidate/evict counters.

No mocks: a real threading workload drives the single-flight assertions; a
controllable monotonic clock drives the deterministic TTL-expiry assertions.
"""

import threading
import time

import pytest

from code_indexer.server.services.query_path_cache import TTLCache


class _Clock:
    """Deterministic monotonic clock for TTL-expiry tests (no time.sleep)."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_fresh_hit_does_not_reinvoke_loader():
    calls = {"n": 0}

    def loader(key: str) -> str:
        calls["n"] += 1
        return f"value-for-{key}"

    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=60.0, max_entries=128, loader=loader
    )

    assert cache.get("a") == "value-for-a"
    assert cache.get("a") == "value-for-a"
    assert cache.get("a") == "value-for-a"

    # Loader invoked exactly once -> two subsequent calls were fresh hits.
    assert calls["n"] == 1
    counters = cache.counters()
    assert counters["hit"] == 2
    assert counters["miss"] == 1
    assert counters["reload"] == 1


def test_miss_then_expiry_reloads_after_ttl():
    clock = _Clock()
    calls = {"n": 0}

    def loader(key: str) -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=10.0, max_entries=8, loader=loader, time_fn=clock
    )

    assert cache.get("k") == 1  # miss -> load #1
    clock.advance(5.0)
    assert cache.get("k") == 1  # still fresh -> hit
    clock.advance(6.0)  # total 11s > 10s ttl
    assert cache.get("k") == 2  # expired -> reload #2

    counters = cache.counters()
    assert counters["reload"] == 2
    assert counters["hit"] == 1


def test_invalidate_forces_reload():
    calls = {"n": 0}

    def loader(key: str) -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=loader
    )

    assert cache.get("x") == 1
    cache.invalidate("x")
    assert cache.get("x") == 2  # invalidation forced a reload
    assert cache.counters()["invalidate"] == 1
    assert cache.counters()["reload"] == 2


def test_invalidate_missing_key_is_noop():
    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=lambda k: 1
    )
    # Must not raise.
    cache.invalidate("never-stored")
    assert cache.counters()["invalidate"] == 0


def test_clear_evicts_all_entries():
    calls = {"n": 0}

    def loader(key: str) -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=loader
    )

    cache.get("a")
    cache.get("b")
    assert cache.size() == 2
    cache.clear()
    assert cache.size() == 0
    # After clear, a new get reloads.
    cache.get("a")
    assert calls["n"] == 3


def test_bounded_lru_eviction_never_exceeds_max():
    def loader(key: str) -> str:
        return key

    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=300.0, max_entries=3, loader=loader
    )

    for i in range(10):
        cache.get(f"key-{i}")

    assert cache.size() <= 3
    # At least 7 evictions happened (10 distinct keys, cap 3).
    assert cache.counters()["evict"] >= 7


def test_no_ttl_mode_never_expires():
    clock = _Clock()
    calls = {"n": 0}

    def loader(key: str) -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=None, max_entries=8, loader=loader, time_fn=clock
    )

    assert cache.get("immutable") == 1
    clock.advance(10_000_000.0)
    assert cache.get("immutable") == 1  # still fresh despite huge time jump
    assert calls["n"] == 1


def test_single_flight_runs_loader_once_under_concurrent_cold_miss():
    """A cold key hit concurrently by many threads loads exactly once."""
    load_count = {"n": 0}
    load_lock = threading.Lock()
    in_loader = threading.Event()
    release = threading.Event()

    def slow_loader(key: str) -> str:
        with load_lock:
            load_count["n"] += 1
        in_loader.set()
        # Block so all racing threads pile up on the per-key lock.
        release.wait(timeout=5.0)
        return f"loaded-{key}"

    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=300.0, max_entries=16, loader=slow_loader
    )

    results: list = []
    results_lock = threading.Lock()

    def worker():
        val = cache.get("hot")
        with results_lock:
            results.append(val)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()

    # Wait until the first loader is actually running, give the rest time to queue.
    assert in_loader.wait(timeout=5.0)
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(timeout=5.0)

    assert load_count["n"] == 1  # single-flight: exactly one loader ran
    assert results == ["loaded-hot"] * 12
    assert cache.counters()["reload"] == 1


def test_invalidate_during_in_flight_load_is_safe():
    """Invalidating while a loader is mid-flight must not corrupt the store."""
    in_loader = threading.Event()
    release = threading.Event()

    def slow_loader(key: str) -> str:
        in_loader.set()
        release.wait(timeout=5.0)
        return f"v-{key}"

    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=slow_loader
    )

    out = {}

    def worker():
        out["val"] = cache.get("z")

    t = threading.Thread(target=worker)
    t.start()
    assert in_loader.wait(timeout=5.0)
    # Invalidate while the loader is blocked mid-flight.
    cache.invalidate("z")
    release.set()
    t.join(timeout=5.0)

    assert out["val"] == "v-z"
    # Cache remains usable and consistent after the racing invalidate.
    assert cache.get("z") == "v-z"


def test_rejects_invalid_construction():
    with pytest.raises(ValueError):
        TTLCache(ttl_seconds=60.0, max_entries=0, loader=lambda k: 1)
    with pytest.raises(ValueError):
        TTLCache(ttl_seconds=-1.0, max_entries=8, loader=lambda k: 1)
