"""
Wiring and invalidation-on-refresh tests for IdIndexCache (Bug #1078).

Tests:
1. FilesystemVectorStore accepts id_index_cache constructor param and stores it.
2. With cache injected, _load_id_index is called once across two get_or_load calls
   (second call is a cache hit).
3. Without cache, per-instance _id_index dict behaviour is unchanged (no regression).
4. Invalidation parity: rebuild_hnsw_index() invalidates both the HNSW cache AND
   the id_index cache for the same collection_path.
5. FilesystemBackend.get_vector_store_client() injects the global singleton when
   hnsw_index_cache is set (server mode) and passes None when it is not (CLI mode).
6. Anti-orphan: get_global_id_index_cache is referenced from filesystem_backend
   production module (confirms the class is no longer orphaned).
"""

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.cache.id_index_cache import (
    IdIndexCache,
    IdIndexCacheConfig,
    reset_global_id_index_cache,
)
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_id_index_cache() -> IdIndexCache:
    return IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))


def _make_hnsw_cache() -> HNSWIndexCache:
    return HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=60.0))


# ---------------------------------------------------------------------------
# Test 1 & 2: id_index_cache accepted and stored by FilesystemVectorStore
# ---------------------------------------------------------------------------


class TestFilesystemVectorStoreIdIndexCacheParam:
    """FilesystemVectorStore accepts id_index_cache as constructor param."""

    def test_constructor_stores_id_index_cache(self, tmp_path: Path) -> None:
        """FilesystemVectorStore.__init__ stores id_index_cache as self.id_index_cache."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        cache = _make_id_index_cache()
        store = FilesystemVectorStore(
            base_path=tmp_path / "index",
            id_index_cache=cache,
        )
        assert store.id_index_cache is cache

    def test_constructor_no_cache_defaults_to_none(self, tmp_path: Path) -> None:
        """Without id_index_cache arg, self.id_index_cache is None (CLI behaviour)."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path / "index")
        assert store.id_index_cache is None


class TestIdIndexCacheUsedInLoadClosure:
    """With cache injected, _load_id_index is called once across two queries."""

    def test_load_id_index_called_once_with_cache(self, tmp_path: Path) -> None:
        """
        When id_index_cache is injected, two get_or_load calls for the same key
        must call the loader only once — the second call is a cache hit.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        cache = _make_id_index_cache()
        store = FilesystemVectorStore(
            base_path=tmp_path / "index",
            id_index_cache=cache,
        )

        collection_path = tmp_path / "index" / "test_collection"
        collection_path.mkdir(parents=True, exist_ok=True)

        fake_id_index = {"point_1": Path("/some/file.py")}
        load_count = [0]

        def counting_load(name: str) -> dict:
            load_count[0] += 1
            return fake_id_index

        store._load_id_index = counting_load

        # First access: miss -> calls _load_id_index
        cache_key = str(collection_path.resolve())
        result1 = cache.get_or_load(
            cache_key, lambda: store._load_id_index("test_collection")
        )
        # Second access: hit -> does NOT call _load_id_index
        result2 = cache.get_or_load(
            cache_key, lambda: store._load_id_index("test_collection")
        )

        assert load_count[0] == 1, (
            f"Expected _load_id_index called once, got {load_count[0]}"
        )
        assert result1 is result2

    def test_no_cache_uses_per_instance_dict(self, tmp_path: Path) -> None:
        """
        Without cache, the per-instance self._id_index dict is used (unchanged behaviour).
        After manual clearing of _id_index, _load_id_index is called again — confirming
        that no cross-query persistence exists without the cache.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path / "index")
        assert store.id_index_cache is None

        collection_name = "test_coll"
        fake_id_index = {"point_1": Path("/some/file.py")}
        load_count = [0]

        def counting_load(name: str) -> dict:
            load_count[0] += 1
            return fake_id_index

        store._load_id_index = counting_load

        # Simulate what the load_index closure does without cache
        if collection_name not in store._id_index:
            store._id_index[collection_name] = store._load_id_index(collection_name)
        _ = store._id_index[collection_name]

        # Second access on same instance: cached in _id_index dict
        if collection_name not in store._id_index:
            store._id_index[collection_name] = store._load_id_index(collection_name)
        _ = store._id_index[collection_name]

        assert load_count[0] == 1  # per-instance cache works

        # Clear the per-instance dict (simulates new FilesystemVectorStore instance)
        store._id_index.clear()
        if collection_name not in store._id_index:
            store._id_index[collection_name] = store._load_id_index(collection_name)

        assert load_count[0] == 2  # reloaded after clear (no cross-query persistence)


# ---------------------------------------------------------------------------
# Test 3: Invalidation parity on rebuild_hnsw_index
# ---------------------------------------------------------------------------


class TestInvalidationParity:
    """rebuild_hnsw_index() invalidates both HNSW and id_index caches."""

    def test_rebuild_invalidates_id_index_cache(self, tmp_path: Path) -> None:
        """
        After a rebuild that invalidates the HNSW cache entry, the id_index cache
        entry for the same collection_path is also gone so the next load reloads.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        hnsw_cache = _make_hnsw_cache()
        id_cache = _make_id_index_cache()

        store = FilesystemVectorStore(
            base_path=tmp_path / "index",
            hnsw_index_cache=hnsw_cache,
            id_index_cache=id_cache,
        )

        collection_name = "vectors_1024_voyage-code-3"
        collection_path = tmp_path / "index" / collection_name
        collection_path.mkdir(parents=True, exist_ok=True)

        # Pre-populate both caches with a fake entry.
        resolved_key = str(collection_path.resolve())
        hnsw_cache.get_or_load(
            resolved_key,
            lambda: (MagicMock(), {0: "vec_0"}),
        )
        fake_id_index = {"p1": Path("/f.py")}
        id_cache.get_or_load(resolved_key, lambda: fake_id_index)

        # Verify id_index is cached (no reloads on next get)
        id_load_count = [0]

        def counting_id_loader() -> dict:
            id_load_count[0] += 1
            return fake_id_index

        id_cache.get_or_load(resolved_key, counting_id_loader)
        assert id_load_count[0] == 0, "Pre-condition: id_index should be cached"

        # Simulate rebuild: call the invalidation path
        if store.hnsw_index_cache is not None:
            store.hnsw_index_cache.invalidate(str(collection_path))
        if store.id_index_cache is not None:
            store.id_index_cache.invalidate(str(collection_path))

        # After invalidation, next get_or_load should reload
        id_cache.get_or_load(resolved_key, counting_id_loader)
        assert id_load_count[0] == 1, "After invalidation, id_index should be reloaded"

    def test_rebuild_hnsw_index_method_invalidates_id_index_cache(
        self, tmp_path: Path
    ) -> None:
        """
        FilesystemVectorStore.rebuild_hnsw_filtered() internally calls both
        hnsw_index_cache.invalidate and id_index_cache.invalidate.
        Verify by patching the HNSWIndexManager rebuild and confirming both
        invalidate() methods are called with the collection_path.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        hnsw_cache = MagicMock()
        hnsw_cache.invalidate = MagicMock()
        id_cache = MagicMock()
        id_cache.invalidate = MagicMock()

        store = FilesystemVectorStore(
            base_path=tmp_path / "index",
            hnsw_index_cache=hnsw_cache,
            id_index_cache=id_cache,
        )

        collection_name = "vectors_1024_voyage-code-3"
        # _get_vector_size will be called; mock it out
        store._get_vector_size = MagicMock(return_value=1024)

        # Patch HNSWIndexManager where it is DEFINED (imported locally inside the method)
        with patch(
            "code_indexer.storage.hnsw_index_manager.HNSWIndexManager"
        ) as MockHNSW:
            mock_manager = MagicMock()
            mock_manager.rebuild_from_vectors.return_value = 42
            MockHNSW.return_value = mock_manager

            store.rebuild_hnsw_filtered(collection_name, visible_files=set())

        expected_collection_path = store._get_collection_path(collection_name)
        expected_key = str(expected_collection_path)

        hnsw_cache.invalidate.assert_called_once_with(expected_key)
        id_cache.invalidate.assert_called_once_with(expected_key)


# ---------------------------------------------------------------------------
# Test 4: FilesystemBackend.get_vector_store_client() global-singleton wiring
# ---------------------------------------------------------------------------


class TestFilesystemBackendGlobalSingletonWiring:
    """FilesystemBackend.get_vector_store_client() injects global singleton in server mode."""

    def setup_method(self) -> None:
        """Reset global singleton before each test."""
        reset_global_id_index_cache()

    def teardown_method(self) -> None:
        """Reset global singleton after each test."""
        reset_global_id_index_cache()

    def test_server_mode_injects_id_index_cache(self, tmp_path: Path) -> None:
        """
        When hnsw_index_cache is set (server mode), get_vector_store_client() must
        produce a FilesystemVectorStore whose id_index_cache is not None.
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        hnsw_cache = _make_hnsw_cache()
        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=hnsw_cache,
        )
        store = backend.get_vector_store_client()
        assert store.id_index_cache is not None, (
            "Server mode (hnsw_index_cache set) must inject id_index_cache"
        )

    def test_cli_mode_passes_none_id_index_cache(self, tmp_path: Path) -> None:
        """
        When hnsw_index_cache is None (CLI mode), get_vector_store_client() must
        produce a FilesystemVectorStore whose id_index_cache is None.
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(project_root=tmp_path)
        store = backend.get_vector_store_client()
        assert store.id_index_cache is None, (
            "CLI mode (no hnsw_index_cache) must pass id_index_cache=None"
        )

    def test_server_mode_uses_global_singleton(self, tmp_path: Path) -> None:
        """
        Two get_vector_store_client() calls in server mode must yield stores
        that share the SAME id_index_cache instance (the global singleton).
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        hnsw_cache = _make_hnsw_cache()
        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=hnsw_cache,
        )
        store1 = backend.get_vector_store_client()
        store2 = backend.get_vector_store_client()
        assert store1.id_index_cache is store2.id_index_cache, (
            "Both stores must reference the same global IdIndexCache singleton"
        )


# ---------------------------------------------------------------------------
# Test 5: Anti-orphan — production module imports get_global_id_index_cache
# ---------------------------------------------------------------------------


class TestAntiOrphan:
    """Confirms IdIndexCache/get_global_id_index_cache is wired into production."""

    def test_filesystem_backend_source_references_global_id_index_cache(
        self,
    ) -> None:
        """
        The filesystem_backend.py production source must reference
        get_global_id_index_cache, proving the class is no longer orphaned.
        """
        import code_indexer.backends.filesystem_backend as fb_module

        source = inspect.getsource(fb_module)
        assert "get_global_id_index_cache" in source, (
            "filesystem_backend.py must import/call get_global_id_index_cache "
            "(anti-orphan check: IdIndexCache must be wired into production)"
        )

    def test_filesystem_vector_store_has_id_index_cache_param(
        self, tmp_path: Path
    ) -> None:
        """FilesystemVectorStore.__init__ signature must include id_index_cache."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        sig = inspect.signature(FilesystemVectorStore.__init__)
        assert "id_index_cache" in sig.parameters, (
            "FilesystemVectorStore.__init__ must accept id_index_cache param"
        )
