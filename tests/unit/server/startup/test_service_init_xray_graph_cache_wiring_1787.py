"""Story #1787 S2 amendment AC14/AC16 (dual-review defect H4/H6
remediation): service_init.py must compose the X-Ray graph cache into the
SAME governor attach-slot HNSW already occupies (never a second unguarded
attach_cache() call that would silently REPLACE HNSW's registration), and
must construct the TTL-cached K-calibration provider once the storage-mode-
appropriate backend is known.

Why a source-inspection test (not a full initialize_services() invocation):
initialize_services() creates DB schemas, spawns background threads,
connects to PostgreSQL, and performs bootstrap git operations -- exercising
it directly would require mocking dozens of external boundaries (the same
rationale test_service_init_node_id_wiring_1400.py documents for the same
function). A source-inspection test is the narrowest reliable check for
this specific wiring invariant. The REAL behavior of the composition itself
(both sub-caches remain governor-visible, neither evicts the other) is
proven executably in test_xray_graph_governor_composite_cache_1787.py.
"""

from __future__ import annotations

from pathlib import Path

_PARENTS_TO_REPO_ROOT = 4
_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)


def _source() -> str:
    return _SERVICE_INIT_PATH.read_text()


class TestAttachCallUsesCompositeNotBareHnsw:
    def test_bare_hnsw_only_attach_cache_call_no_longer_exists(self) -> None:
        """The pre-fix single unguarded attach_cache(_server_hnsw_cache)
        call must be GONE -- it is exactly the call that would silently
        replace HNSW's registration once a second cache needs the slot."""
        assert "_memory_governor.attach_cache(_server_hnsw_cache)" not in _source(), (
            "service_init.py must no longer attach the bare HNSW cache "
            "directly -- it must attach a CompositeLRUCache composing both "
            "HNSW and the X-Ray graph cache (H4/H6 remediation)"
        )

    def test_graph_cache_and_composite_constructed_before_the_sole_attach_call(
        self,
    ) -> None:
        source = _source()
        graph_cache_pos = source.find("_xray_graph_cache = XrayGraphCacheProxy()")
        composite_pos = source.find("_xray_composite_cache = CompositeLRUCache(")
        attach_pos = source.find("_memory_governor.attach_cache(_xray_composite_cache)")

        assert graph_cache_pos != -1, (
            "_xray_graph_cache = XrayGraphCacheProxy() construction not found"
        )
        assert composite_pos != -1, (
            "_xray_composite_cache = CompositeLRUCache( construction not found"
        )
        assert attach_pos != -1, (
            "_memory_governor.attach_cache(_xray_composite_cache) call not found -- "
            "the composite (not the bare HNSW cache) must be the ONE thing "
            "attached to the governor"
        )
        assert graph_cache_pos < composite_pos < attach_pos, (
            "SOURCE-ORDER VIOLATION: the graph cache and composite must be "
            "built before the sole attach_cache() call"
        )
        # Counts the real call site only (`_memory_governor.attach_cache(`) --
        # a bare "attach_cache(" substring also matches this file's own
        # explanatory comments about the single-slot API.
        call_count = source.count("_memory_governor.attach_cache(")
        assert call_count == 1, (
            "there must be EXACTLY ONE _memory_governor.attach_cache() call in "
            "service_init.py -- a second call would silently replace whatever "
            f"was attached first (single-slot governor API), found {call_count}"
        )


class TestCompositeCacheContents:
    def test_composite_cache_composes_both_hnsw_and_graph_cache_at_the_shared_floor(
        self,
    ) -> None:
        source = _source()
        start = source.find("_xray_composite_cache = CompositeLRUCache(")
        assert start != -1, "_xray_composite_cache = CompositeLRUCache( not found"
        end = source.find("\n    )", start)
        call_block = source[start:end]

        assert "_server_hnsw_cache" in call_block, (
            "CompositeLRUCache(...) must include the pre-existing HNSW "
            f"cache so it stays governor-visible. Call block was: {call_block!r}"
        )
        assert "_xray_graph_cache" in call_block, (
            "CompositeLRUCache(...) must include the new X-Ray graph cache. "
            f"Call block was: {call_block!r}"
        )
        assert "floor_per_cache=YELLOW_LRU_FLOOR" in call_block, (
            "CompositeLRUCache(...) must floor each sub-cache at the SAME "
            "constant memory_governor.py's own YELLOW tick uses internally "
            f"-- never a second, duplicated magic number. Call block was: {call_block!r}"
        )


class TestKTtlProviderWiring:
    def test_k_ttl_provider_constructed_after_backend_registry_is_resolved(
        self,
    ) -> None:
        source = _source()
        # Two branches (postgres/sqlite) each assign _backend_registry;
        # the TTL provider must come after BOTH have had the chance to run
        # (i.e. after the LAST such assignment in source order).
        last_backend_registry_pos = source.rfind(
            "_backend_registry = StorageFactory.create_backends("
        )
        ttl_provider_pos = source.find("_xray_k_ttl_provider = TTLCachedKProvider(")

        assert last_backend_registry_pos != -1, (
            "_backend_registry = StorageFactory.create_backends( not found"
        )
        assert ttl_provider_pos != -1, (
            "_xray_k_ttl_provider = TTLCachedKProvider( construction not found"
        )
        assert last_backend_registry_pos < ttl_provider_pos, (
            "SOURCE-ORDER VIOLATION: TTLCachedKProvider must be constructed "
            "AFTER _backend_registry is resolved (it wraps "
            "_backend_registry.xray_k_calibration)"
        )

    def test_k_ttl_provider_wraps_the_backend_registry_calibration_store(
        self,
    ) -> None:
        source = _source()
        start = source.find("_xray_k_ttl_provider = TTLCachedKProvider(")
        assert start != -1, "_xray_k_ttl_provider = TTLCachedKProvider( not found"
        end = source.find("\n    )", start)
        call_block = source[start:end]

        assert "store=_backend_registry.xray_k_calibration" in call_block, (
            "TTLCachedKProvider(...) must wrap _backend_registry.xray_k_calibration "
            f"(the just-wired BackendRegistry field). Call block was: {call_block!r}"
        )


class TestSingletonsInstalled:
    def test_singletons_installed_for_future_call_sites(self) -> None:
        source = _source()
        assert "set_xray_graph_cache(_xray_graph_cache)" in source, (
            "service_init.py must install the process-level singleton via "
            "set_xray_graph_cache() so a future graph-build call site "
            "retrieves the SAME governor-attached instance"
        )
        assert "set_xray_k_provider(_xray_k_ttl_provider)" in source, (
            "service_init.py must install the process-level singleton via "
            "set_xray_k_provider() so a future graph-build call site "
            "retrieves the SAME instance"
        )
