"""Post-manual-E2E-test production fix for Story #1492 (AC1 collection_meta_cache,
AC3 chunk_store_cache).

A real running server was strace-verified to show ZERO cross-request benefit
from either cache. Root cause: FilesystemVectorStore.__init__ only constructs
a CollectionMetaCache()/ChunkStoreThreadCache() when the caller passes None,
and EVERY production construction site -- including
FilesystemBackend.get_vector_store_client(), which backs the REST/MCP
/api/query and search_code query hot paths -- passed None, so every
per-query FilesystemVectorStore got its own private, single-use cache that
died with it.

This mirrors the EXACT fix Bug #1078 already applied for id_index_cache:
FilesystemBackend.get_vector_store_client() must inject the process-wide
singleton (get_global_collection_meta_cache() / get_global_chunk_store_cache())
when hnsw_index_cache is set (this codebase's established "we are in server
mode" proxy), and must continue to pass None in CLI/solo mode so
FilesystemVectorStore falls back to its own fresh per-instance cache,
byte-identical to today.
"""

from pathlib import Path

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.storage.shared.chunk_store_cache import (
    ChunkStoreThreadCache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.shared.collection_meta_cache import (
    CollectionMetaCache,
    reset_global_collection_meta_cache,
)


def _make_hnsw_cache() -> HNSWIndexCache:
    return HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=60.0))


class TestFilesystemBackendCollectionMetaCacheWiring:
    """FilesystemBackend.get_vector_store_client() injects the global
    CollectionMetaCache singleton in server mode."""

    def setup_method(self) -> None:
        reset_global_collection_meta_cache()

    def teardown_method(self) -> None:
        reset_global_collection_meta_cache()

    def test_server_mode_injects_collection_meta_cache(self, tmp_path: Path) -> None:
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=_make_hnsw_cache(),
        )
        store = backend.get_vector_store_client()
        assert store._collection_meta_cache is not None, (
            "Server mode (hnsw_index_cache set) must inject collection_meta_cache"
        )
        assert isinstance(store._collection_meta_cache, CollectionMetaCache)

    def test_cli_mode_leaves_collection_meta_cache_none_at_construction(
        self, tmp_path: Path
    ) -> None:
        """CLI mode must pass collection_meta_cache=None to the constructor
        (byte-identical to pre-fix behavior) -- FilesystemVectorStore itself
        then falls back to constructing its own fresh, per-instance cache,
        so store._collection_meta_cache is never actually None after
        construction. What must never happen is CLI/solo silently sharing
        the SAME global singleton the server uses."""
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(project_root=tmp_path)
        store1 = backend.get_vector_store_client()
        store2 = backend.get_vector_store_client()
        assert store1._collection_meta_cache is not store2._collection_meta_cache, (
            "CLI mode (no hnsw_index_cache) must NOT share the global "
            "singleton across instances -- each gets its own fresh cache"
        )

    def test_server_mode_uses_global_singleton_across_instances(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=_make_hnsw_cache(),
        )
        store1 = backend.get_vector_store_client()
        store2 = backend.get_vector_store_client()
        assert store1._collection_meta_cache is store2._collection_meta_cache, (
            "Both stores must reference the SAME global CollectionMetaCache "
            "singleton -- this is the actual cross-request fix"
        )


class TestFilesystemBackendChunkStoreCacheWiring:
    """FilesystemBackend.get_vector_store_client() injects the global
    ChunkStoreThreadCache singleton in server mode."""

    def setup_method(self) -> None:
        reset_global_chunk_store_cache()

    def teardown_method(self) -> None:
        reset_global_chunk_store_cache()

    def test_server_mode_injects_chunk_store_cache(self, tmp_path: Path) -> None:
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=_make_hnsw_cache(),
        )
        store = backend.get_vector_store_client()
        assert store._chunk_store_cache is not None, (
            "Server mode (hnsw_index_cache set) must inject chunk_store_cache"
        )
        assert isinstance(store._chunk_store_cache, ChunkStoreThreadCache)

    def test_cli_mode_does_not_share_global_singleton(self, tmp_path: Path) -> None:
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(project_root=tmp_path)
        store1 = backend.get_vector_store_client()
        store2 = backend.get_vector_store_client()
        assert store1._chunk_store_cache is not store2._chunk_store_cache, (
            "CLI mode (no hnsw_index_cache) must NOT share the global "
            "singleton across instances -- each gets its own fresh cache"
        )

    def test_server_mode_uses_global_singleton_across_instances(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        backend = FilesystemBackend(
            project_root=tmp_path,
            hnsw_index_cache=_make_hnsw_cache(),
        )
        store1 = backend.get_vector_store_client()
        store2 = backend.get_vector_store_client()
        assert store1._chunk_store_cache is store2._chunk_store_cache, (
            "Both stores must reference the SAME global ChunkStoreThreadCache "
            "singleton -- this is the actual cross-request fix"
        )
