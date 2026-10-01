"""Unit tests proving daemon cache usage bugs and validating fixes.

These tests verify that the daemon service actually uses cached indexes
instead of reloading from disk on every query.

BUGS BEING TESTED:
1. Semantic queries reload HNSW from disk instead of using cache_entry.hnsw_index
2. FTS queries reopen Tantivy index instead of using cache_entry.tantivy_searcher
3. Performance regression: warm cache should be 200x faster than cold cache
"""

import json
from pathlib import Path
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

    @pytest.fixture
    def tracked_backend(self, mock_project_path):
        """Real FilesystemBackend with a call-counting get_vector_store_client().

        A REAL (not MagicMock) backend is required so the fix's
        isinstance()-gated direct-construction path can actually trigger.
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        real_backend = FilesystemBackend(project_root=mock_project_path)
        call_count = [0]
        original = real_backend.get_vector_store_client

        def tracked():
            call_count[0] += 1
            return original()

        real_backend.get_vector_store_client = tracked  # type: ignore[method-assign]
        return real_backend, call_count

    @pytest.fixture
    def patched_search_deps(self, tracked_backend):
        """Patch every _execute_semantic_search dependency EXCEPT the code
        under test itself: BackendFactory returns the tracked real backend;
        FilesystemVectorStore.__init__ is wrapped (not replaced) to capture
        every constructed instance; .search() and .resolve_collection_name()
        (disk/network-touching, irrelevant to caching) are stubbed;
        EmbeddingProviderFactory.create() returns a deterministic fake
        provider. resolve_collection_name() is pinned to "collection_test",
        matching mock_project_path's real on-disk collection.

        Yields (call_count, captured_instances).
        """
        from contextlib import ExitStack

        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
        from code_indexer.backends.backend_factory import BackendFactory

        real_backend, call_count = tracked_backend
        captured_instances: list = []
        original_vs_init = FilesystemVectorStore.__init__

        def capturing_init(self, *args, **kwargs):
            original_vs_init(self, *args, **kwargs)
            captured_instances.append(self)

        mock_provider = MagicMock()
        mock_provider.get_embedding.return_value = [0.1] * 1536
        mock_provider.get_current_model.return_value = "voyage-code-3"

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(BackendFactory, "create", return_value=real_backend)
            )
            stack.enter_context(
                patch.object(FilesystemVectorStore, "__init__", capturing_init)
            )
            stack.enter_context(
                patch.object(FilesystemVectorStore, "search", return_value=([], {}))
            )
            stack.enter_context(
                patch.object(
                    FilesystemVectorStore,
                    "resolve_collection_name",
                    return_value="collection_test",
                )
            )
            stack.enter_context(
                patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                    return_value=mock_provider,
                )
            )
            yield call_count, captured_instances

    def _warm_matching_cache_entry(self, mock_project_path):
        """Build a CacheEntry warm for "collection_test" with a known
        hnsw_cache_key, matching what patched_search_deps resolves to.
        """
        from code_indexer.daemon.cache import CacheEntry

        cache_entry = CacheEntry(mock_project_path)
        mock_hnsw_index = MagicMock()
        mock_id_mapping = {"0": {"path": "test.py", "content": "test"}}
        matching_cache_key = "the-exact-key-this-collection-was-cached-under"
        cache_entry.set_semantic_indexes(
            mock_hnsw_index, mock_id_mapping, hnsw_cache_key=matching_cache_key
        )
        cache_entry.collection_name = "collection_test"
        return cache_entry, mock_hnsw_index, matching_cache_key

    def test_semantic_search_uses_cache_for_matching_collection(
        self, daemon_service, mock_project_path, tracked_backend, patched_search_deps
    ):
        """When the cached collection matches the query's own resolved
        collection: never call get_vector_store_client() (inverted from
        the original bug-verification assertion), and the constructed
        vector store's hnsw_index_cache hands back the cached hnsw_index
        for the matching cache_key without ever invoking the loader.
        """
        cache_entry, mock_hnsw_index, matching_key = self._warm_matching_cache_entry(
            mock_project_path
        )
        daemon_service.cache_entry = cache_entry
        _, call_count = tracked_backend
        _, captured_instances = patched_search_deps

        daemon_service._execute_semantic_search(
            str(mock_project_path), "test query", limit=10
        )

        assert call_count[0] == 0, (
            f"get_vector_store_client() should NOT be called when the "
            f"cache is warm and the collection matches, got {call_count[0]}"
        )
        assert len(captured_instances) == 1
        vector_store = captured_instances[0]
        assert vector_store.hnsw_index_cache is not None

        def _loader_must_not_be_called():
            raise AssertionError("Loader must never fire for a matching key")

        returned_index, _ = vector_store.hnsw_index_cache.get_or_load(
            matching_key, _loader_must_not_be_called
        )
        assert returned_index is mock_hnsw_index

    def test_adapter_falls_through_to_loader_for_non_matching_key(
        self, daemon_service, mock_project_path, tracked_backend, patched_search_deps
    ):
        """BLOCKER 1 defense-in-depth: the adapter must never silently
        serve a cached index for a cache_key that does not match the
        collection it was actually cached under -- it must call the
        loader instead, exactly like a genuine cache miss.
        """
        cache_entry, _, _ = self._warm_matching_cache_entry(mock_project_path)
        daemon_service.cache_entry = cache_entry
        _, _ = tracked_backend
        _, captured_instances = patched_search_deps

        daemon_service._execute_semantic_search(
            str(mock_project_path), "test query", limit=10
        )

        vector_store = captured_instances[0]
        loader_calls: list = []

        def _spy_loader():
            loader_calls.append(True)
            return "loaded-from-disk-sentinel", {}

        result = vector_store.hnsw_index_cache.get_or_load(
            "some-other-collections-cache-key", _spy_loader
        )
        assert len(loader_calls) == 1, (
            "A non-matching cache_key must fall through to the loader -- "
            "never structurally possible to silently serve the wrong "
            "collection's cached graph"
        )
        assert result[0] == "loaded-from-disk-sentinel"

    def test_semantic_search_falls_back_to_slow_path_when_collection_mismatches(
        self, daemon_service, mock_project_path, tracked_backend, patched_search_deps
    ):
        """BLOCKER 1 remediation: a warm cache_entry.hnsw_index is NOT
        sufficient to take the fast path -- the cached collection must
        also match the query's own resolved collection ("collection_test",
        per patched_search_deps). When it does not, fall back to the safe,
        slower get_vector_store_client() path exactly once.
        """
        from code_indexer.daemon.cache import CacheEntry

        cache_entry = CacheEntry(mock_project_path)
        cache_entry.set_semantic_indexes(
            MagicMock(), {}, hnsw_cache_key="some-cache-key"
        )
        cache_entry.collection_name = "some_other_collection_entirely"
        daemon_service.cache_entry = cache_entry
        _, call_count = tracked_backend
        patched_search_deps  # noqa: B018 -- fixture applies its patches

        daemon_service._execute_semantic_search(
            str(mock_project_path), "test query", limit=10
        )

        assert call_count[0] == 1, (
            f"Mismatched cached collection must fall back to "
            f"get_vector_store_client() exactly once, got {call_count[0]}"
        )

    @staticmethod
    def _build_two_collections(code_indexer_dir, *names):
        """Create N real on-disk collections (metadata only); return index_dir."""
        index_dir = code_indexer_dir / "index"
        index_dir.mkdir()
        for name in names:
            collection_dir = index_dir / name
            collection_dir.mkdir()
            metadata = {
                "vector_size": 1536,
                "hnsw_index": {"index_rebuild_uuid": f"test-version-{name}"},
            }
            with open(collection_dir / "collection_meta.json", "w") as f:
                json.dump(metadata, f)
        return index_dir

    def test_load_semantic_indexes_warms_configured_collection_not_first_in_list(
        self, daemon_service, tmp_path
    ):
        """BLOCKER 1 remediation (root cause): _load_semantic_indexes must
        warm the CONFIGURED collection (resolve_collection_name() for this
        project's real config), never an arbitrary list_collections()
        order pick. Reproduces the live defect: 2 real collections where
        an unrelated one sorts first and the configured one sorts second.
        """
        project_path = tmp_path / "multi_collection_project"
        project_path.mkdir()
        code_indexer_dir = project_path / ".code-indexer"
        code_indexer_dir.mkdir()

        from code_indexer.config import ConfigManager

        ConfigManager(code_indexer_dir / "config.json").create_default_config(
            codebase_dir=project_path
        )
        # Default config: embedding_provider="voyage-ai", model="voyage-code-3".
        configured_collection = "voyage-code-3"
        wrong_collection = "voyage-multimodal-3"
        self._build_two_collections(
            code_indexer_dir, wrong_collection, configured_collection
        )

        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
        from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
        from code_indexer.storage.id_index_manager import IDIndexManager
        from code_indexer.daemon.cache import CacheEntry

        entry = CacheEntry(project_path)
        with (
            patch.object(
                FilesystemVectorStore,
                "list_collections",
                return_value=[wrong_collection, configured_collection],
            ),
            patch.object(
                HNSWIndexManager, "load_index", return_value=MagicMock()
            ) as mock_load_index,
            patch.object(IDIndexManager, "load_index", return_value={"0": "x"}),
        ):
            daemon_service._load_semantic_indexes(entry)

        assert entry.collection_name == configured_collection, (
            f"Expected the CONFIGURED collection '{configured_collection}', "
            f"got '{entry.collection_name}' "
            f"(list_collections()[0] was '{wrong_collection}')"
        )
        assert mock_load_index.call_count == 1
        assert mock_load_index.call_args.args[0].name == configured_collection
        assert entry.hnsw_cache_key is not None

    def test_ensure_cache_loaded_staleness_check_targets_actual_cached_collection(
        self, daemon_service, tmp_path
    ):
        """FINDING 2 remediation: _ensure_cache_loaded's staleness check
        must resolve collection_path from cache_entry.collection_name (the
        collection ACTUALLY cached), never an arbitrary iterdir()-order
        pick -- otherwise it compares the wrong collection's
        index_rebuild_uuid and can incorrectly invalidate (or fail to
        invalidate) the genuinely cached collection.
        """
        project_path = tmp_path / "staleness_project"
        project_path.mkdir()
        code_indexer_dir = project_path / ".code-indexer"
        code_indexer_dir.mkdir()
        index_dir = self._build_two_collections(
            code_indexer_dir, "aaa_wrong_first", "voyage-code-3"
        )
        # Give the two collections DIFFERENT uuids so a wrong-collection
        # comparison is guaranteed to disagree with the cached value.
        with open(index_dir / "aaa_wrong_first" / "collection_meta.json") as f:
            wrong_meta = json.load(f)
        wrong_meta["hnsw_index"]["index_rebuild_uuid"] = "wrong-collection-uuid"
        with open(index_dir / "aaa_wrong_first" / "collection_meta.json", "w") as f:
            json.dump(wrong_meta, f)

        from code_indexer.daemon.cache import CacheEntry

        cache_entry = CacheEntry(project_path)
        cache_entry.hnsw_index = MagicMock()
        cache_entry.collection_name = "voyage-code-3"
        cache_entry.hnsw_index_version = "test-version-voyage-code-3"
        daemon_service.cache_entry = cache_entry

        original_iterdir = Path.iterdir

        def fake_iterdir(path_self):
            if path_self == index_dir:
                return iter(
                    [index_dir / "aaa_wrong_first", index_dir / "voyage-code-3"]
                )
            return original_iterdir(path_self)

        with patch.object(Path, "iterdir", fake_iterdir):
            daemon_service._ensure_cache_loaded(str(project_path))

        assert daemon_service.cache_entry is cache_entry, (
            "The staleness check compared against the WRONG collection "
            "(iterdir()[0] = 'aaa_wrong_first') and incorrectly "
            "invalidated a genuinely fresh cache for 'voyage-code-3'"
        )

    def test_fts_search_should_use_cached_tantivy_searcher_not_reopen_index(
        self, daemon_service, mock_project_path
    ):
        """FTS search uses the daemon's cached Tantivy index, not tantivy.Index.open().

        FIXED BEHAVIOR (Bug #1730 Bug 2):
        1. Cache is pre-loaded with Tantivy index in cache_entry.tantivy_index
        2. FTS search adopts cache_entry.tantivy_index directly (via
           TantivyIndexManager.open_from_cached_index()) instead of
           constructing a fresh manager and reopening from disk
        3. tantivy.Index.open() is NOT called on this path

        Pre-fix, _execute_fts_search() constructed a new TantivyIndexManager
        per query and called initialize_index(), which always calls
        tantivy.Index.open() (reopening from disk every query, and
        acquiring the exclusive writer lock -- Bug #1233's class of bug).

        This test proves the fix by asserting tantivy.Index.open() is never
        called when the cache is warm.
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
