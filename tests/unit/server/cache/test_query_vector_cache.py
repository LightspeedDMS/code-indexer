"""Tests for the fork-interim in-process query-embedding (vector) cache."""

from code_indexer.server.cache.query_vector_cache import (
    QueryVectorCache,
    configure_query_vector_cache,
    get_query_vector_cache,
)


class _Clock:
    """Controllable monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------


def test_make_key_collapses_whitespace_but_preserves_case():
    assert QueryVectorCache.make_key(
        "voyage-ai", "voyage-3", "how  to   reset"
    ) == QueryVectorCache.make_key("voyage-ai", "voyage-3", "how to reset")
    # Embeddings are case-sensitive, so case must NOT be normalized away.
    assert QueryVectorCache.make_key("p", "m", "Reset") != QueryVectorCache.make_key(
        "p", "m", "reset"
    )


def test_make_key_separates_provider_and_model():
    assert QueryVectorCache.make_key("a", "m", "q") != QueryVectorCache.make_key(
        "b", "m", "q"
    )
    assert QueryVectorCache.make_key("p", "a", "q") != QueryVectorCache.make_key(
        "p", "b", "q"
    )


# ---------------------------------------------------------------------------
# get / put / stats
# ---------------------------------------------------------------------------


def test_put_then_get_hit_and_miss_counters():
    c = QueryVectorCache(max_entries=10, ttl_seconds=100.0)
    assert c.get("x") is None  # miss
    c.put("x", [1.0, 2.0])
    assert c.get("x") == [1.0, 2.0]  # hit
    s = c.stats()
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["size"] == 1


def test_ttl_expiry():
    clock = _Clock()
    c = QueryVectorCache(max_entries=10, ttl_seconds=5.0, time_fn=clock)
    c.put("k", [1.0])
    assert c.get("k") == [1.0]
    clock.t = 4.9
    assert c.get("k") == [1.0]  # still fresh
    clock.t = 5.0
    assert c.get("k") is None  # now >= expires_at -> expired + evicted
    assert c.stats()["size"] == 0


def test_lru_eviction_past_capacity():
    c = QueryVectorCache(max_entries=2, ttl_seconds=100.0)
    c.put("a", [1.0])
    c.put("b", [2.0])
    assert c.get("a") == [1.0]  # touch 'a' -> 'b' becomes least-recently-used
    c.put("c", [3.0])  # capacity 2 -> evict LRU ('b')
    assert c.get("b") is None
    assert c.get("a") == [1.0]
    assert c.get("c") == [3.0]


def test_zero_max_entries_disables_cache():
    c = QueryVectorCache(max_entries=0, ttl_seconds=100.0)
    c.put("k", [1.0])
    assert c.get("k") is None
    assert c.stats()["size"] == 0


# ---------------------------------------------------------------------------
# Singleton configuration
# ---------------------------------------------------------------------------


def test_configure_installs_and_clears_singleton():
    try:
        configure_query_vector_cache(enabled=True, max_entries=5, ttl_seconds=10.0)
        cache = get_query_vector_cache()
        assert cache is not None
        cache.put("k", [1.0])
        assert cache.get("k") == [1.0]

        # Disabling clears the singleton so the query path becomes a no-op again.
        configure_query_vector_cache(enabled=False, max_entries=5, ttl_seconds=10.0)
        assert get_query_vector_cache() is None
    finally:
        # Never leak an enabled singleton into other tests.
        configure_query_vector_cache(enabled=False, max_entries=5, ttl_seconds=10.0)


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


def test_config_defaults_disabled():
    from code_indexer.server.utils.config_manager import QueryVectorCacheConfig

    c = QueryVectorCacheConfig()
    assert c.enabled is False
    assert c.max_entries == 10000
    assert c.ttl_seconds == 86400.0


def test_server_config_defaults_the_section():
    from code_indexer.server.utils.config_manager import ServerConfig

    sc = ServerConfig(server_dir="/tmp/x")
    assert sc.query_vector_cache_config is not None
    assert sc.query_vector_cache_config.enabled is False
