"""Strict single-flight + refcounted key-locks for TTLCache (Story #1082 review).

Code-review Finding 1 (HIGH): when ``invalidate()`` or LRU-eviction drops a
key's loader-lock while a loader is still mid-flight, a second concurrent caller
for that SAME key used to create a NEW key-lock and run a SECOND loader -- two
concurrent loaders for one logical miss. That violates the strict single-flight
guarantee (Scenario 13).

The fix is a refcounted key-lock registry (a key-lock with refcount > 0 can
never be reclaimed by invalidate/eviction) plus a per-key invalidate epoch (a
value loaded across an invalidate is never stored as fresh, so the next get
reloads). These tests prove all three properties with REAL threads -- no mocks,
no ``time.sleep`` in production code.

(a) and (c) MUST fail on the pre-fix implementation (no refcount, no
``key_lock_count()``); (b) proves the epoch correctness.
"""

import threading
import time

from code_indexer.server.services.query_path_cache import TTLCache


def test_concurrent_get_invalidate_eviction_one_loader_per_logical_miss():
    """At most ONE loader runs concurrently for the SAME key.

    A per-key barrier-instrumented loader records the maximum number of loaders
    ever simultaneously in-flight for each key. Under concurrent
    get("hot") + invalidate("hot") + real eviction pressure from 200 other
    keys, the refcounted key-lock must keep the per-key maximum at 1 for "hot".

    Only the "hot" loader blocks (so racing "hot" callers pile up on its
    key-lock); cold loaders return immediately so the eviction loop applies
    genuine LRU pressure DURING the in-flight window, exercising the path where
    eviction must not reclaim the in-flight "hot" key-lock.
    """
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}
    bookkeeping = threading.Lock()
    loader_entered = threading.Event()
    release = threading.Event()
    errors: list = []

    def slow_loader(key: str) -> str:
        with bookkeeping:
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
        try:
            if key == "hot":
                loader_entered.set()
                if not release.wait(timeout=10.0):
                    errors.append(RuntimeError("release event never set"))
            return f"loaded-{key}"
        finally:
            with bookkeeping:
                in_flight[key] -= 1

    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=300.0, max_entries=4, loader=slow_loader
    )

    def hot_getter():
        try:
            cache.get("hot")
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    def invalidator():
        # Drop the in-flight key's lock candidate while the loader is blocked.
        # With a non-refcounted registry this lets a sibling getter mint a new
        # key-lock and start a second concurrent loader for "hot".
        if loader_entered.wait(timeout=10.0):
            cache.invalidate("hot")

    def eviction_pressure():
        # Churn many distinct keys to force LRU eviction (max_entries=4) DURING
        # the in-flight window; cold loaders return immediately so this loop
        # runs to completion while "hot" is still blocked.
        for i in range(200):
            try:
                cache.get(f"cold-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=hot_getter) for _ in range(8)]
    threads.append(threading.Thread(target=invalidator))
    threads.append(threading.Thread(target=eviction_pressure))

    for t in threads:
        t.start()

    # Once the first "hot" loader is running, give the racing threads time to
    # try to overlap (and the eviction loop time to churn), then release.
    assert loader_entered.wait(timeout=10.0)
    time.sleep(0.3)
    release.set()

    for t in threads:
        t.join(timeout=10.0)

    assert errors == [], f"workers raised: {errors}"
    # The core single-flight guarantee: never two loaders for "hot" at once.
    assert max_in_flight.get("hot", 0) == 1, (
        f"expected max 1 concurrent loader for 'hot', got {max_in_flight.get('hot', 0)}"
    )


def test_invalidate_during_load_next_get_returns_fresh_not_stale():
    """A value loaded across an invalidate is never served as fresh afterward.

    The loader returns a monotonically increasing version. We let one get()
    start loading version 1, invalidate mid-flight, then release. The caller
    that triggered the load may observe version 1, but because the key was
    invalidated during the load, the value must NOT be cached as fresh -- the
    NEXT get() must reload and return version 2 (the fresh post-invalidate
    value), never the stale version 1.
    """
    version = {"n": 0}
    version_lock = threading.Lock()
    loader_entered = threading.Event()
    release = threading.Event()
    errors: list = []

    def versioned_loader(key: str) -> int:
        with version_lock:
            version["n"] += 1
            mine = version["n"]
        if mine == 1:
            loader_entered.set()
            if not release.wait(timeout=10.0):
                errors.append(RuntimeError("release event never set"))
        return mine

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=versioned_loader
    )

    first = {}

    def first_getter():
        first["val"] = cache.get("k")

    t = threading.Thread(target=first_getter)
    t.start()
    assert loader_entered.wait(timeout=10.0)
    # Invalidate while version-1 load is blocked mid-flight.
    cache.invalidate("k")
    release.set()
    t.join(timeout=10.0)

    assert errors == [], f"loader raised: {errors}"
    # The triggering caller saw what its own loader produced (version 1).
    assert first["val"] == 1
    # But the invalidate happened mid-load -> version 1 must NOT be cached as
    # fresh. The next get reloads and returns the fresh post-invalidate value.
    assert cache.get("k") == 2
    # And that fresh value is now stable.
    assert cache.get("k") == 2


def test_key_lock_registry_bounded_under_churn():
    """key_lock_count() stays O(max_entries) under ~200 distinct-key churn.

    Each completed get() releases its key-lock refcount, so reclamation keeps
    the registry from growing unbounded. With max_entries=8 and 200 distinct
    keys, the registry must stay within the store bound (max_entries) once all
    gets have completed (no loaders in flight -> every refcount is 0).
    """
    cache: TTLCache[str, str] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=lambda k: k
    )

    for i in range(200):
        cache.get(f"key-{i}")

    # No loaders in flight -> every key-lock refcount is 0 -> reclaimable.
    # The registry must be bounded by the store bound (max_entries), not 200.
    assert cache.key_lock_count() <= cache._max_entries
    assert cache.size() <= cache._max_entries


def test_clear_preserves_pinned_holder_and_invalidates_inflight():
    """clear() while a loader is in-flight keeps the pinned holder and bumps
    its epoch so the in-flight value is not stored as fresh (next get reloads).
    """
    version = {"n": 0}
    version_lock = threading.Lock()
    loader_entered = threading.Event()
    release = threading.Event()
    errors: list = []

    def versioned_loader(key: str) -> int:
        with version_lock:
            version["n"] += 1
            mine = version["n"]
        if mine == 1:
            loader_entered.set()
            if not release.wait(timeout=10.0):
                errors.append(RuntimeError("release event never set"))
        return mine

    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=versioned_loader
    )

    first = {}

    def first_getter():
        first["val"] = cache.get("k")

    t = threading.Thread(target=first_getter)
    t.start()
    assert loader_entered.wait(timeout=10.0)
    # clear() lands while the version-1 load is blocked mid-flight.
    cache.clear()
    release.set()
    t.join(timeout=10.0)

    assert errors == [], f"loader raised: {errors}"
    assert first["val"] == 1
    # Pinned holder was released cleanly -> reclaimed; registry empty at rest.
    assert cache.key_lock_count() == 0
    # The value loaded across clear() was not stored fresh -> next get reloads.
    assert cache.get("k") == 2


def test_idle_key_lock_holders_are_reclaimed():
    """invalidate() and clear() drop idle (refcount==0) holders; at rest the
    key-lock registry is empty.
    """
    cache: TTLCache[str, int] = TTLCache(
        ttl_seconds=300.0, max_entries=8, loader=lambda k: 1
    )

    cache.get("a")
    cache.get("b")
    # All loads complete -> holders are idle and already reclaimed.
    assert cache.key_lock_count() == 0

    # invalidate on a cached key with no in-flight loader: no holder to pin.
    cache.invalidate("a")
    assert cache.key_lock_count() == 0
    assert cache.counters()["invalidate"] == 1

    cache.clear()
    assert cache.key_lock_count() == 0
