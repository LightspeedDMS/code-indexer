"""Unit tests for exposed_query_temporal() RPC method.

Tests verify that daemon correctly handles temporal query delegation with
mmap caching, following the IDENTICAL pattern as HEAD collection queries.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

# Mock rpyc before import if not available
try:
    import rpyc
except ImportError:
    sys.modules["rpyc"] = MagicMock()
    sys.modules["rpyc.utils.server"] = MagicMock()
    rpyc = sys.modules["rpyc"]

from src.code_indexer.daemon.service import CIDXDaemonService


class TestExposedQueryTemporal(TestCase):
    """Test exposed_query_temporal() RPC method."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.project_path = Path(self.temp_dir) / "test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)

        # Create temporal collection structure. Provider-aware name matching
        # the mock_config (embedding_provider="voyage-ai", voyage_ai.model=
        # "voyage-code-3") used by every test in this file — the real
        # resolve_temporal_collection_from_config() never resolves to the
        # bare legacy "code-indexer-temporal" name.
        self.temporal_collection_path = (
            self.project_path
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_code_3"
        )
        self.temporal_collection_path.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil

        if Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    def test_service_has_exposed_query_temporal_method(self):
        """CIDXDaemonService should have exposed_query_temporal() method."""
        # Acceptance Criterion 5: exposed_query_temporal() RPC method implemented

        service = CIDXDaemonService()

        assert hasattr(service, "exposed_query_temporal")
        assert callable(service.exposed_query_temporal)

    @patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    def test_exposed_query_temporal_loads_cache_on_first_call(
        self,
        mock_backend_factory,
        mock_config_manager,
        mock_execute_fusion,
    ):
        """exposed_query_temporal() should lazily init vector_store on first
        call and route the query through execute_temporal_query_with_fusion
        (Bug #1302 fix -- shard-aware, correctly-named dispatch)."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        service = CIDXDaemonService()

        # Mock ConfigManager
        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.get_config.return_value = mock_config
        mock_config_manager.create_with_backtrack.return_value = mock_config

        # Mock backend factory
        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        mock_execute_fusion.return_value = TemporalSearchResults(
            results=[],
            query="test",
            filter_type=None,
            filter_value=None,
            total_found=0,
        )

        # Create a mock cache entry
        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path

        # Patch _ensure_cache_loaded to set up our mock
        with patch.object(service, "_ensure_cache_loaded"):
            # Manually set the cache_entry
            service.cache_entry = mock_cache_entry

            # Call exposed_query_temporal
            service.exposed_query_temporal(
                project_path=str(self.project_path),
                query="test query",
                time_range="last-7-days",
                limit=10,
            )

            # Vector store must have been lazily initialized and reused.
            assert service.vector_store is mock_vector_store
            mock_execute_fusion.assert_called_once()

    def test_exposed_query_temporal_returns_error_if_index_missing(self):
        """exposed_query_temporal() should return a typed error (surfaced
        from execute_temporal_query_with_fusion's warning) when no temporal
        collections exist for the resolved embedder."""
        service = CIDXDaemonService()

        # Real config (no mocking of collection-naming/shard-discovery) --
        # temporal collection dir was never created for this test.
        result = service.exposed_query_temporal(
            project_path=str(self.project_path),
            query="test query",
            time_range="last-7-days",
            limit=10,
        )

        # Should return error (typed warning surfaced as "error" for
        # backward compatibility with the CLI's `if "error" in result:`
        # contract).
        assert "error" in result
        assert "No temporal indexes available" in result["error"]
        assert result["results"] == []

    @patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    def test_exposed_query_temporal_forwards_query_params_to_fusion_dispatch(
        self,
        mock_backend_factory,
        mock_config_manager,
        mock_execute_fusion,
    ):
        """exposed_query_temporal() should forward query/time_range/limit/
        filters to execute_temporal_query_with_fusion() correctly."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        service = CIDXDaemonService()

        # Mock ConfigManager
        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.get_config.return_value = mock_config
        mock_config_manager.create_with_backtrack.return_value = mock_config

        # Mock backend factory
        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        mock_execute_fusion.return_value = TemporalSearchResults(
            results=[],
            query="authentication",
            filter_type="time_range",
            filter_value="last-7-days",
            total_found=0,
        )

        # Patch cache_lock to avoid threading issues in unit test
        with patch.object(service, "cache_lock"):
            with patch.object(service, "_ensure_cache_loaded"):
                with patch.object(service, "cache_entry") as mock_cache_entry:
                    mock_cache_entry.project_path = self.project_path

                    # Call exposed_query_temporal
                    service.exposed_query_temporal(
                        project_path=str(self.project_path),
                        query="authentication",
                        time_range="last-7-days",
                        limit=10,
                        languages=["python"],
                        min_score=0.7,
                    )

                    # Verify execute_temporal_query_with_fusion was called
                    mock_execute_fusion.assert_called_once()
                    call_kwargs = mock_execute_fusion.call_args[1]
                    assert call_kwargs["query_text"] == "authentication"
                    # Verify time_range was converted to tuple (daemon converts "last-7-days" → ("YYYY-MM-DD", "YYYY-MM-DD"))
                    assert isinstance(call_kwargs["time_range"], tuple)
                    assert len(call_kwargs["time_range"]) == 2
                    # Both dates should be in YYYY-MM-DD format
                    assert len(call_kwargs["time_range"][0]) == 10  # YYYY-MM-DD
                    assert len(call_kwargs["time_range"][1]) == 10  # YYYY-MM-DD
                    assert call_kwargs["limit"] == 10
                    assert call_kwargs["language"] == "python"

    @patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    def test_exposed_query_temporal_honors_temporal_embedder_override(
        self,
        mock_backend_factory,
        mock_config_manager,
        mock_execute_fusion,
    ):
        """exposed_query_temporal() must thread the temporal_embedder
        override through to execute_temporal_query_with_fusion() (needed for
        --temporal-embedder to have any effect in daemon mode)."""
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        service = CIDXDaemonService()

        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.get_config.return_value = mock_config
        mock_config_manager.create_with_backtrack.return_value = mock_config

        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        mock_execute_fusion.return_value = TemporalSearchResults(
            results=[],
            query="test",
            filter_type=None,
            filter_value=None,
            total_found=0,
        )

        with patch.object(service, "cache_lock"):
            with patch.object(service, "_ensure_cache_loaded"):
                with patch.object(service, "cache_entry") as mock_cache_entry:
                    mock_cache_entry.project_path = self.project_path

                    service.exposed_query_temporal(
                        project_path=str(self.project_path),
                        query="test query",
                        time_range="all",
                        limit=10,
                        temporal_embedder="cohere-embed-v4",
                    )

                    mock_execute_fusion.assert_called_once()
                    call_kwargs = mock_execute_fusion.call_args[1]
                    assert call_kwargs["temporal_embedder"] == "cohere-embed-v4"

    @patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    def test_exposed_query_temporal_lazily_initializes_config_manager_before_first_use(
        self,
        mock_backend_factory,
        mock_config_manager,
        mock_execute_fusion,
    ):
        """Bug #1300: exposed_query_temporal() must lazily init config_manager
        BEFORE it is first used (assert + get_config()), not after.

        Reproduces the real post-__init__ state where self.config_manager is
        None (set in CIDXDaemonService.__init__) and no other exposed method
        has run yet to populate it. Before the fix, this call raised
        AssertionError unconditionally.
        """
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        service = CIDXDaemonService()

        # Real post-__init__ state: config_manager has never been set.
        assert service.config_manager is None

        # Mock ConfigManager.create_with_backtrack — the lazy-init call
        mock_config = MagicMock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.get_config.return_value = mock_config
        mock_config_manager.create_with_backtrack.return_value = mock_config

        # Mock backend factory
        mock_vector_store = MagicMock()
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        mock_execute_fusion.return_value = TemporalSearchResults(
            results=[],
            query="test",
            filter_type=None,
            filter_value=None,
            total_found=0,
        )

        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path

        with patch.object(service, "_ensure_cache_loaded"):
            service.cache_entry = mock_cache_entry

            # Must NOT raise AssertionError (or AttributeError) — the whole
            # point of Bug #1300 is that config_manager is None here.
            result = service.exposed_query_temporal(
                project_path=str(self.project_path),
                query="test query",
                time_range="last-7-days",
                limit=10,
            )

        # Lazy init must have run BEFORE first use, populating config_manager
        mock_config_manager.create_with_backtrack.assert_called_once()
        assert service.config_manager is mock_config
        assert "error" not in result


class TestExposedQueryTemporalShardAwareBug1302(TestCase):
    """Bug #1302: daemon temporal query is shard-blind + uses the wrong
    collection-naming scheme.

    Repro (matches the bug's default-config scenario exactly):
    - config.embedding_provider = "voyage-ai", config.voyage_ai.model =
      "voyage-code-3" (the REGULAR semantic-search scheme)
    - config.temporal.active_embedder = "voyage-context-4" (the per-commit
      temporal embedder scheme actually used to name/write temporal
      collections on disk -- Story #1290/#1242)

    Real per-commit temporal indexing creates a bare bookkeeping directory
    "code-indexer-temporal-voyage_context_4/" (temporal_meta.json only, NO
    HNSW data) plus a quarterly shard directory
    "code-indexer-temporal-voyage_context_4-2026Q3/" that holds the actual
    HNSW data (Story #1242 quarterly sharding). The pre-fix daemon resolved
    the collection name via the WRONG regular-provider/model scheme
    ("code-indexer-temporal-voyage_code_3", which never exists on disk) and,
    even if that were fixed in isolation, would still look for a bare
    hnsw_index.bin directly under the base dir -- which never exists either,
    since real HNSW data lives ONLY inside quarterly shard subdirectories.

    This test proves the fix with a REAL on-disk shard layout and REAL
    (unmocked) shard-discovery machinery (get_overlapping_shards,
    resolve_temporal_collection_name, _discover_provider_shards_with_pruning)
    -- only the heavy embedding-provider construction and vector-search
    engine (TemporalSearchService) are mocked, matching the Messi mocking
    hierarchy's "external service boundary" carve-out.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_path = Path(self.temp_dir) / "test_project"
        self.project_path.mkdir(parents=True, exist_ok=True)

        from code_indexer.config import ConfigManager

        config_path = self.project_path / ".code-indexer" / "config.json"
        self.config_manager = ConfigManager(config_path)
        # Real default config: embedding_provider=voyage-ai,
        # voyage_ai.model=voyage-code-3, temporal.active_embedder=
        # voyage-context-4 -- the exact divergence Bug #1302 describes.
        self.config_manager.create_default_config(self.project_path)

        self.index_dir = self.project_path / ".code-indexer" / "index"

        # Real bookkeeping dir under the CORRECT (active_embedder) scheme --
        # bare dir, NO hnsw_index.bin (Story #1242: bookkeeping only).
        self.correct_base_dir = (
            self.index_dir / "code-indexer-temporal-voyage_context_4"
        )
        self.correct_base_dir.mkdir(parents=True, exist_ok=True)
        (self.correct_base_dir / "temporal_meta.json").write_text(
            json.dumps({"last_commit": "abc123"})
        )

        # Real quarterly shard dir -- this is where the actual per-commit
        # HNSW data lives (Story #1242/#1290 sharding).
        self.shard_dir = (
            self.index_dir / "code-indexer-temporal-voyage_context_4-2026Q3"
        )
        self.shard_dir.mkdir(parents=True, exist_ok=True)

        # WRONG-scheme collection name (regular embedding_provider/model) is
        # deliberately never created -- there is no real data there, ever.
        self.wrong_collection_name = "code-indexer-temporal-voyage_code_3"

    def tearDown(self):
        import shutil

        if Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    def test_exposed_query_temporal_discovers_real_quarterly_shard_not_wrong_collection_name(
        self,
    ):
        """Daemon must resolve config.temporal.active_embedder's shard set,
        never the regular embedding_provider/model scheme, and must reach
        the real quarterly shard directory (not just the bare base dir).
        """
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        service = CIDXDaemonService()

        captured_collection_names: list = []

        class _FakeTemporalSearchService:
            def __init__(
                self,
                config_manager,
                project_root,
                vector_store_client=None,
                embedding_provider=None,
                collection_name=None,
            ):
                captured_collection_names.append(collection_name)

            def query_temporal(self, **kwargs):
                return TemporalSearchResults(
                    results=[],
                    query=kwargs.get("query", ""),
                    filter_type="time_range",
                    filter_value=None,
                    total_found=0,
                )

        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path

        with patch.object(service, "cache_lock"):
            with patch.object(service, "_ensure_cache_loaded"):
                service.cache_entry = mock_cache_entry

                with (
                    patch(
                        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService",
                        _FakeTemporalSearchService,
                    ),
                    patch(
                        "code_indexer.services.temporal.temporal_fusion_dispatch._create_embedding_provider_for_collection",
                        return_value=MagicMock(),
                    ),
                ):
                    result = service.exposed_query_temporal(
                        project_path=str(self.project_path),
                        query="anything",
                        time_range="all",
                        limit=5,
                    )

        assert captured_collection_names, (
            "exposed_query_temporal never reached TemporalSearchService at "
            f"all -- result={result!r}"
        )
        assert self.wrong_collection_name not in captured_collection_names, (
            "Bug #1302 Defect 1: exposed_query_temporal used the WRONG "
            "regular-embedding-provider collection-naming scheme instead of "
            f"config.temporal.active_embedder -- captured="
            f"{captured_collection_names!r}"
        )
        assert (
            "code-indexer-temporal-voyage_context_4-2026Q3" in captured_collection_names
        ), (
            "Bug #1302 Defect 2: exposed_query_temporal is shard-blind -- it "
            "did not discover the real quarterly shard directory -- "
            f"captured={captured_collection_names!r}"
        )
        assert "error" not in result, (
            f"Unexpected error in result: {result.get('error')}"
        )
