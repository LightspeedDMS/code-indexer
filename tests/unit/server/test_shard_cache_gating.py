"""Shard-aware index-cache gating in the query managers.

Semantic (single-repo) fails OPEN: solo mode caches every repo. Multi/omni
fan-out stays cache-OFF in solo mode (Bug #881) and only warms the cache for
OWNED repos under sharding. Both must tolerate __new__-built instances.
"""

from unittest.mock import MagicMock

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.server.multi.multi_search_service import MultiSearchService


def _sem():
    return SemanticQueryManager.__new__(SemanticQueryManager)


def _multi():
    return MultiSearchService.__new__(MultiSearchService)


def _ownership(owned: set):
    o = MagicMock()
    o.owns.side_effect = lambda alias: alias in owned
    return o


# ---- semantic: fail-open ----


def test_semantic_solo_caches_every_repo():
    m = _sem()  # __new__, no _shard_ownership set -> getattr default None
    assert m._owns_for_cache("any-repo") is True


def test_semantic_sharded_caches_only_owned():
    m = _sem()
    m.set_shard_ownership(_ownership({"repo-a"}))
    assert m._owns_for_cache("repo-a") is True
    assert m._owns_for_cache("repo-b") is False


# ---- multi: cache-off in solo, on for owned ----


def test_multi_solo_never_caches():
    m = _multi()  # __new__ -> getattr default None
    assert m._owns_for_cache("any-repo") is False


def test_multi_sharded_caches_only_owned():
    m = _multi()
    m.set_shard_ownership(_ownership({"repo-a"}))
    assert m._owns_for_cache("repo-a") is True
    assert m._owns_for_cache("repo-b") is False
