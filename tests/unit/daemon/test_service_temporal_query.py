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
        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    @patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory")
    def test_exposed_query_temporal_loads_cache_on_first_call(
        self,
        mock_embedding_factory,
        mock_backend_factory,
        mock_config_manager,
        mock_temporal_search,
    ):
        """exposed_query_temporal() should load temporal cache on first call."""
        # Acceptance Criterion 6: Temporal cache loading and management

        service = CIDXDaemonService()

        # Create collection metadata
        metadata = {"hnsw_index": {"index_rebuild_uuid": "uuid-123"}}
        metadata_file = self.temporal_collection_path / "collection_meta.json"
        metadata_file.write_text(json.dumps(metadata))

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

        # Mock embedding provider
        mock_embedding_provider = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding_provider

        # Mock TemporalSearchService
        mock_search_service = MagicMock()
        mock_search_result = MagicMock()
        mock_search_result.results = []
        mock_search_result.query = "test"
        mock_search_result.filter_type = None
        mock_search_result.filter_value = None
        mock_search_result.total_found = 0
        mock_search_result.performance = {}
        mock_search_result.warning = None
        mock_search_service.query_temporal.return_value = mock_search_result
        mock_temporal_search.return_value = mock_search_service

        # Create a mock cache entry
        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path
        mock_cache_entry.temporal_hnsw_index = None
        mock_cache_entry.is_temporal_stale_after_rebuild.return_value = False

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

            # Verify load_temporal_indexes was called
            mock_cache_entry.load_temporal_indexes.assert_called_once()

    @patch(
        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    @patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory")
    def test_exposed_query_temporal_returns_error_if_index_missing(
        self,
        mock_embedding_factory,
        mock_backend_factory,
        mock_config_manager,
        mock_temporal_search,
    ):
        """exposed_query_temporal() should return error if temporal index doesn't exist."""
        # Acceptance Criterion 5: Error handling

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

        # Mock embedding provider
        mock_embedding_provider = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding_provider

        # Temporal collection doesn't exist (delete it)
        if self.temporal_collection_path.exists():
            import shutil

            shutil.rmtree(self.temporal_collection_path)

        # Call exposed_query_temporal
        result = service.exposed_query_temporal(
            project_path=str(self.project_path),
            query="test query",
            time_range="last-7-days",
            limit=10,
        )

        # Should return error
        assert "error" in result
        assert "Temporal index not found" in result["error"]
        assert result["results"] == []

    @patch(
        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    @patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory")
    def test_exposed_query_temporal_integrates_with_temporal_search_service(
        self,
        mock_embedding_factory,
        mock_backend_factory,
        mock_config_manager,
        mock_temporal_search,
    ):
        """exposed_query_temporal() should use TemporalSearchService for queries."""
        # Acceptance Criterion 7: Time-range filtering integration

        service = CIDXDaemonService()

        # Create collection metadata
        metadata = {"hnsw_index": {"index_rebuild_uuid": "uuid-123"}}
        metadata_file = self.temporal_collection_path / "collection_meta.json"
        metadata_file.write_text(json.dumps(metadata))

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

        # Mock embedding provider
        mock_embedding_provider = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding_provider

        # Mock TemporalSearchService
        mock_search_service = MagicMock()
        mock_search_result = MagicMock()
        mock_search_result.results = []
        mock_search_result.query = "test"
        mock_search_result.filter_type = "time_range"
        mock_search_result.filter_value = "last-7-days"
        mock_search_result.total_found = 0
        mock_search_result.performance = {}
        mock_search_result.warning = None
        mock_search_service.query_temporal.return_value = mock_search_result
        mock_temporal_search.return_value = mock_search_service

        # Patch cache_lock to avoid threading issues in unit test
        with patch.object(service, "cache_lock"):
            with patch.object(service, "_ensure_cache_loaded"):
                with patch.object(service, "cache_entry") as mock_cache_entry:
                    mock_cache_entry.temporal_hnsw_index = MagicMock()
                    mock_cache_entry.is_temporal_stale_after_rebuild.return_value = (
                        False
                    )

                    # Call exposed_query_temporal
                    service.exposed_query_temporal(
                        project_path=str(self.project_path),
                        query="authentication",
                        time_range="last-7-days",
                        limit=10,
                        languages=["python"],
                        min_score=0.7,
                    )

                    # Verify TemporalSearchService.query_temporal was called
                    mock_search_service.query_temporal.assert_called_once()
                    call_kwargs = mock_search_service.query_temporal.call_args[1]
                    assert call_kwargs["query"] == "authentication"
                    # Verify time_range was converted to tuple (daemon converts "last-7-days" → ("YYYY-MM-DD", "YYYY-MM-DD"))
                    assert isinstance(call_kwargs["time_range"], tuple)
                    assert len(call_kwargs["time_range"]) == 2
                    # Both dates should be in YYYY-MM-DD format
                    assert len(call_kwargs["time_range"][0]) == 10  # YYYY-MM-DD
                    assert len(call_kwargs["time_range"][1]) == 10  # YYYY-MM-DD
                    assert call_kwargs["limit"] == 10
                    assert call_kwargs["language"] == [
                        "python"
                    ]  # Parameter name is 'language' not 'languages'
                    assert call_kwargs["min_score"] == 0.7

    @patch(
        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    @patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory")
    def test_exposed_query_temporal_reloads_cache_if_stale(
        self,
        mock_embedding_factory,
        mock_backend_factory,
        mock_config_manager,
        mock_temporal_search,
    ):
        """exposed_query_temporal() should reload cache if rebuild detected."""
        # Acceptance Criterion 4: temporal_index_version tracking

        service = CIDXDaemonService()

        # Create collection metadata
        metadata = {"hnsw_index": {"index_rebuild_uuid": "uuid-new"}}
        metadata_file = self.temporal_collection_path / "collection_meta.json"
        metadata_file.write_text(json.dumps(metadata))

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

        # Mock embedding provider
        mock_embedding_provider = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding_provider

        # Mock TemporalSearchService
        mock_search_service = MagicMock()
        mock_search_result = MagicMock()
        mock_search_result.results = []
        mock_search_result.query = "test"
        mock_search_result.filter_type = None
        mock_search_result.filter_value = None
        mock_search_result.total_found = 0
        mock_search_result.performance = {}
        mock_search_result.warning = None
        mock_search_service.query_temporal.return_value = mock_search_result
        mock_temporal_search.return_value = mock_search_service

        # Create a mock cache entry with stale cache
        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path
        mock_cache_entry.temporal_hnsw_index = MagicMock()  # Already loaded
        mock_cache_entry.is_temporal_stale_after_rebuild.return_value = (
            True  # But stale
        )

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

            # Verify invalidate_temporal and load_temporal_indexes were called
            mock_cache_entry.invalidate_temporal.assert_called_once()
            mock_cache_entry.load_temporal_indexes.assert_called()

    @patch(
        "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
    )
    @patch("code_indexer.config.ConfigManager")
    @patch("code_indexer.backends.backend_factory.BackendFactory")
    @patch("code_indexer.services.embedding_factory.EmbeddingProviderFactory")
    def test_exposed_query_temporal_lazily_initializes_config_manager_before_first_use(
        self,
        mock_embedding_factory,
        mock_backend_factory,
        mock_config_manager,
        mock_temporal_search,
    ):
        """Bug #1300: exposed_query_temporal() must lazily init config_manager
        BEFORE it is first used (assert + get_config()), not after.

        Reproduces the real post-__init__ state where self.config_manager is
        None (set in CIDXDaemonService.__init__ line 88) and no other exposed
        method has run yet to populate it. Before the fix, this call raised
        AssertionError at service.py:413 unconditionally.
        """
        service = CIDXDaemonService()

        # Real post-__init__ state: config_manager has never been set.
        assert service.config_manager is None

        # Create collection metadata so the temporal path proceeds
        metadata = {"hnsw_index": {"index_rebuild_uuid": "uuid-123"}}
        metadata_file = self.temporal_collection_path / "collection_meta.json"
        metadata_file.write_text(json.dumps(metadata))

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

        # Mock embedding provider
        mock_embedding_provider = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding_provider

        # Mock TemporalSearchService
        mock_search_service = MagicMock()
        mock_search_result = MagicMock()
        mock_search_result.results = []
        mock_search_result.query = "test"
        mock_search_result.filter_type = None
        mock_search_result.filter_value = None
        mock_search_result.total_found = 0
        mock_search_result.performance = {}
        mock_search_result.warning = None
        mock_search_service.query_temporal.return_value = mock_search_result
        mock_temporal_search.return_value = mock_search_service

        mock_cache_entry = MagicMock()
        mock_cache_entry.project_path = self.project_path
        mock_cache_entry.temporal_hnsw_index = None
        mock_cache_entry.is_temporal_stale_after_rebuild.return_value = False

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
