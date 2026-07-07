"""Regression: FTS cache reload_on_access reload() must NOT hold the global lock.

Holding _cache_lock across tantivy_index.reload() serialized every concurrent
cache hit; on NFS-backed indexes reload() is slow, collapsing FTS query
throughput to ~1 rps at C=16. The reload must happen after the lock is released
so a slow reload on one key never blocks access to another key.
"""

import threading
import time

from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
)


class _SlowReloadIndex:
    """Fake tantivy index whose reload() blocks until released."""

    def __init__(self, gate: threading.Event | None = None):
        self._gate = gate
        self.reloads = 0

    def reload(self):
        self.reloads += 1
        if self._gate is not None:
            # Block until the test releases us (simulates a slow NFS reload).
            self._gate.wait(timeout=5)


def _prime(cache, key, index):
    """Populate the cache for ``key`` with ``index`` via a miss load."""
    cache.get_or_load(key, lambda: (index, "schema"))


def test_slow_reload_does_not_block_other_keys():
    cfg = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=True)
    cache = FTSIndexCache(config=cfg)

    gate = threading.Event()
    slow = _SlowReloadIndex(gate)  # key A: reload() blocks on the gate
    fast = _SlowReloadIndex(None)  # key B: reload() returns immediately
    _prime(cache, "/idx/a", slow)
    _prime(cache, "/idx/b", fast)

    # Thread 1: hit key A -> triggers the blocking reload() (lock must be released
    # before reload, or thread 2 below would be blocked too).
    t1 = threading.Thread(target=lambda: cache.get_or_load("/idx/a", lambda: None))
    t1.start()
    time.sleep(0.2)  # let thread 1 enter reload() and (previously) hold the lock

    # Thread 2: hit key B while A is mid-reload. If reload held the global lock,
    # this would block until the gate opens (>=5s). With the fix it returns fast.
    done = threading.Event()

    def hit_b():
        cache.get_or_load("/idx/b", lambda: None)
        done.set()

    threading.Thread(target=hit_b, daemon=True).start()

    assert done.wait(timeout=2.0), "access to key B blocked by a slow reload on A"

    gate.set()  # release A's reload
    t1.join(timeout=5)
    assert slow.reloads >= 1  # reload still happens on hit when reload_on_access=True


def test_hit_still_reloads_when_enabled():
    cfg = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=True)
    cache = FTSIndexCache(config=cfg)
    idx = _SlowReloadIndex(None)
    _prime(cache, "/idx/x", idx)  # miss (no reload on load)
    cache.get_or_load("/idx/x", lambda: None)  # hit -> reload
    cache.get_or_load("/idx/x", lambda: None)  # hit -> reload
    assert idx.reloads == 2


def test_hit_does_not_reload_when_disabled():
    cfg = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
    cache = FTSIndexCache(config=cfg)
    idx = _SlowReloadIndex(None)
    _prime(cache, "/idx/y", idx)
    cache.get_or_load("/idx/y", lambda: None)
    cache.get_or_load("/idx/y", lambda: None)
    assert idx.reloads == 0
