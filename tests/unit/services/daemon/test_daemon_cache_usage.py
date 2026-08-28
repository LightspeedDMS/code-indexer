"""Unit tests proving daemon cache usage bugs and validating fixes.

These tests verify that the daemon service actually uses cached indexes
instead of reloading from disk on every query.

BUGS BEING TESTED:
1. Semantic queries reload HNSW from disk instead of using cache_entry.hnsw_index
2. FTS queries reopen Tantivy index instead of using cache_entry.tantivy_searcher
3. Performance regression: warm cache should be 200x faster than cold cache
"""

import json
from unittest.mock import MagicMock, patch

import pytest


class TestDaemonCacheUsage:
    """Test that daemon actually uses cached indexes instead of reloading from disk."""

    @pytest.fixture
    def mock_project_path(self, tmp_path):
        """Create a mock project with index structure."""
        project_path = tmp_path / "test_project"
        project_path.mkdir()

        # Create .code-indexer structure
        code_indexer_dir = project_path / ".code-indexer"
        code_indexer_dir.mkdir()

        # Bug #1718: _execute_semantic_search verifies project_path's OWN
        # config via ConfigManager.load_verified_config() before doing
        # anything else -- without a genuine config.json here, that call
        # raises ConfigVerificationError and the method returns early,
        # never reaching the cache-bypass code path this test exercises.
        from code_indexer.config import ConfigManager

        ConfigManager(code_indexer_dir / "config.json").create_default_config(
            codebase_dir=project_path
        )

        # Create index directory with collection
        index_dir = code_indexer_dir / "index"
        index_dir.mkdir()
        collection_dir = index_dir / "collection_test"
        collection_dir.mkdir()

        # Create collection metadata
        metadata = {
            "vector_size": 1536,
            "hnsw_index": {"index_rebuild_uuid": "test-version-1"},
        }
        with open(collection_dir / "collection_meta.json", "w") as f:
            json.dump(metadata, f)

        # Create a REAL, pre-existing Tantivy index (with meta.json) rather
        # than an empty directory. Bug #1730 Bug 3: an empty tantivy_index/
        # (no meta.json) makes TantivyIndexManager.initialize_index() take
        # the CREATE branch instead of the Index.open() branch, so a test
        # patching tantivy.Index.open observes zero calls for the WRONG
        # reason and can never detect a production code path that reopens
        # the index from disk on every query.
        tantivy_dir = code_indexer_dir / "tantivy_index"
        tantivy_dir.mkdir()
        try:
            from code_indexer.services.tantivy_index_manager import (
                TantivyIndexManager,
            )

            _fixture_manager = TantivyIndexManager(tantivy_dir)
            _fixture_manager.initialize_index(create_new=True)
            _fixture_manager.add_document(
                {
                    "path": "test.py",
                    "content": "def test_function(): pass",
                    "content_raw": "def test_function(): pass",
                    "identifiers": ["test_function"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "python",
                }
            )
            _fixture_manager.commit()
        except ImportError:
            pass  # Tantivy not installed -- FTS test self-skips.

        return project_path

    @pytest.fixture
    def daemon_service(self):
        """Create daemon service instance."""
        from code_indexer.daemon.service import CIDXDaemonService

        service = CIDXDaemonService()
        return service

    def test_semantic_search_should_use_cached_hnsw_not_call_vector_store_search(
        self, daemon_service, mock_project_path
    ):
        """Semantic search should use the daemon's warm cached HNSW index,
        never backend.get_vector_store_client() (which would reload HNSW
        from disk again, and additionally trips server-only init paths).

        Bug #1730 post-fix assertions (inverted per the issue's own inline
        comments): (1) get_vector_store_client() is never called while the
        cache is warm; (2) the REAL FilesystemVectorStore this fix
        constructs is wired with an hnsw_index_cache whose get_or_load()
        hands back the cached hnsw_index without ever invoking a
        disk-reading loader.
        """
        # Prepare cache with loaded HNSW index
        from code_indexer.daemon.cache import CacheEntry

        cache_entry = CacheEntry(mock_project_path)

        # Create mock HNSW index (the object that must be reused untouched)
        mock_hnsw_index = MagicMock()
        mock_hnsw_index.knn_query = MagicMock(return_value=([0], [0.95]))
        mock_id_mapping = {"0": {"path": "test.py", "content": "test"}}

        cache_entry.set_semantic_indexes(mock_hnsw_index, mock_id_mapping)
        daemon_service.cache_entry = cache_entry

        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
        from code_indexer.backends.backend_factory import BackendFactory
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        init_call_count = [0]

        # A REAL (not MagicMock) FilesystemBackend is required so the fix's
        # isinstance()-gated direct-construction path can actually trigger.
        real_backend = FilesystemBackend(project_root=mock_project_path)
        original_get_vector_store_client = real_backend.get_vector_store_client

        def tracked_get_vector_store_client():
            init_call_count[0] += 1
            return original_get_vector_store_client()

        real_backend.get_vector_store_client = tracked_get_vector_store_client  # type: ignore[method-assign]

        # Let the REAL FilesystemVectorStore.__init__ run (it is cheap --
        # directory setup and instance-attribute assignment only), capturing
        # the constructed instance so we can inspect the wiring afterward.
        # Only .search()/.resolve_collection_name() (the expensive, disk/
        # network-touching behavior, irrelevant to caching) are stubbed.
        captured_instances: list = []
        original_vs_init = FilesystemVectorStore.__init__

        def capturing_init(self, *args, **kwargs):
            original_vs_init(self, *args, **kwargs)
            captured_instances.append(self)

        with patch.object(BackendFactory, "create", return_value=real_backend):
            with (
                patch.object(FilesystemVectorStore, "__init__", capturing_init),
                patch.object(FilesystemVectorStore, "search", return_value=([], {})),
                patch.object(
                    FilesystemVectorStore,
                    "resolve_collection_name",
                    return_value="collection_test",
                ),
                patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
                ) as mock_factory,
            ):
                mock_provider = MagicMock()
                mock_provider.get_embedding.return_value = [0.1] * 1536
                mock_provider.get_current_model.return_value = "voyage-code-3"
                mock_factory.return_value = mock_provider

                daemon_service._execute_semantic_search(
                    str(mock_project_path), "test query", limit=10
                )

        # POST-FIX ASSERTION 1 (inverted from == 1 to == 0).
        assert init_call_count[0] == 0, (
            f"Fixed behavior: backend.get_vector_store_client() should NOT "
            f"be called when cache_entry.hnsw_index is warm, but got "
            f"{init_call_count[0]} calls"
        )

        # POST-FIX ASSERTION 2 (replaces knn_query.assert_called_once(),
        # which is not reachable through a caching-focused unit test --
        # see final report for rationale): the REAL vector store instance
        # must be wired with an hnsw_index_cache that hands the cached
        # hnsw_index straight through, without touching disk.
        assert len(captured_instances) == 1, (
            "Expected exactly one FilesystemVectorStore construction"
        )
        vector_store = captured_instances[0]
        assert vector_store.hnsw_index_cache is not None, (
            "FilesystemVectorStore must be constructed with an "
            "hnsw_index_cache wired to the daemon's warm CacheEntry"
        )

        def _loader_must_not_be_called():
            raise AssertionError(
                "Loader must never be called -- the cache already has a "
                "warm hnsw_index; invoking the loader means the fix is "
                "still reloading from disk"
            )

        returned_index, _ = vector_store.hnsw_index_cache.get_or_load(
            "irrelevant-cache-key", _loader_must_not_be_called
        )
        assert returned_index is mock_hnsw_index, (
            "The cached hnsw_index must be handed straight through to the "
            "vector store, not reloaded or replaced"
        )

    def test_fts_search_should_use_cached_tantivy_searcher_not_reopen_index(
        self, daemon_service, mock_project_path
    ):
        """FAILING TEST: FTS search should use cached Tantivy index, not call tantivy.Index.open().

        EXPECTED BEHAVIOR:
        1. Cache is pre-loaded with Tantivy index in cache_entry.tantivy_index
        2. FTS search uses cache_entry.tantivy_index directly (injected into manager)
        3. Should NOT call tantivy.Index.open() to reopen the index

        ACTUAL BEHAVIOR (BUG):
        - _execute_fts_search() creates new TantivyIndexManager
        - Calls TantivyIndexManager.initialize_index() which calls tantivy.Index.open()
        - cache_entry.tantivy_index exists but is never used
        - Performance: ~200ms instead of ~1ms

        This test proves the bug by checking if tantivy.Index.open() is called,
        which indicates the cached index is being bypassed.
        """
        # Prepare cache with loaded Tantivy index
        from code_indexer.daemon.cache import CacheEntry

        cache_entry = CacheEntry(mock_project_path)

        # Create mock Tantivy index with proper schema attribute
        mock_tantivy_index = MagicMock()
        mock_schema = MagicMock()
        mock_tantivy_index.schema = mock_schema
        mock_tantivy_index.parse_query = MagicMock(return_value=MagicMock())
        mock_tantivy_index.searcher = MagicMock(
            return_value=MagicMock(search=MagicMock(return_value=([], {})))
        )

        mock_tantivy_searcher = MagicMock()

        cache_entry.set_fts_indexes(mock_tantivy_index, mock_tantivy_searcher)
        daemon_service.cache_entry = cache_entry

        # Track if tantivy.Index.open() is called (proves index being reopened)
        try:
            with patch("tantivy.Index.open") as mock_index_open:
                # Execute FTS search
                daemon_service._execute_fts_search(
                    str(mock_project_path), "test query", limit=10
                )

                # CRITICAL ASSERTION: Index.open should NOT be called
                # because we should use cached index directly
                # This will FAIL with original implementation
                assert mock_index_open.call_count == 0, (
                    f"Should use cached Tantivy index, not call Index.open() ({mock_index_open.call_count} times)"
                )

        except ImportError:
            # Tantivy not installed, skip this test
            pytest.skip("Tantivy not installed")
