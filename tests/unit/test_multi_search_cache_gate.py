"""C6 Phase 3: MultiSearchService caches only repos this node owns.

Owned repos warm the per-pod index cache (bounded to the shard); non-owned repos
and solo mode bypass the cache (hnsw_cache=None) to preserve Bug #881 protection.
"""

from code_indexer.server.multi.multi_search_service import MultiSearchService


class _FakeOwnership:
    def __init__(self, owned):
        self._owned = set(owned)

    def owns(self, alias):
        return alias in self._owned


def _service():
    # Bypass __init__ (spawns a thread pool); we only exercise the cache gate.
    svc = MultiSearchService.__new__(MultiSearchService)
    svc._shard_ownership = None
    return svc


def test_solo_mode_bypasses_cache():
    # No shard ownership -> never cache (Bug #881 protection preserved).
    assert _service()._owns_for_cache("repo") is False


def test_owned_repo_is_cached():
    svc = _service()
    svc.set_shard_ownership(_FakeOwnership(["a", "b"]))
    assert svc._owns_for_cache("a") is True
    assert svc._owns_for_cache("b") is True


def test_non_owned_repo_bypasses_cache():
    svc = _service()
    svc.set_shard_ownership(_FakeOwnership(["a"]))
    assert svc._owns_for_cache("z") is False
