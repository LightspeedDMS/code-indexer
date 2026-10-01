"""Tests for Bug #1480 — server-side multimodal query fan-out.

The CIDX server INDEXES multimodal collections (e.g. voyage-multimodal-3,
built during golden-repo registration when a repo has images) but the front
door (POST /api/query and MCP search_code) never queried them — only the
CLI's MultiIndexQueryService did. This gap is fixed at the single injection
point that serves BOTH REST and MCP: SemanticSearchService._perform_semantic_search
(via search_repository_path, which now passes enable_multimodal=True).

Declared test list (exactly 3):
  1. test_code_only_repo_calls_store_search_exactly_once
  2. test_multimodal_repo_calls_store_search_twice_with_isolated_cache_flag
  3. test_multimodal_results_merged_into_final_response

Design under test:
  _perform_semantic_search gains `enable_multimodal: bool = False` (default
  False — every existing direct caller/test is unaffected).
  search_repository_path() now passes enable_multimodal=True.
  When enabled AND a multimodal collection exists on disk AND no precomputed
  vector was supplied, the FilesystemVectorStore branch dispatches through
  MultiIndexQueryService.query_with_separate_kwargs() instead of a single
  store.search() call — forcing no_embedding_cache_shortcut=True on the
  multimodal-collection call while leaving the code-collection call's flag
  untouched (embedding-cache isolation, Bug #1480).

Mocking mirrors test_search_service_precomputed_vector.py's established
pattern: BackendFactory.create, EmbeddingProviderFactory.create, and a
MagicMock(spec=FilesystemVectorStore) store with a side_effect capturing
kwargs per call. The vector store / FilesystemVectorStore class itself is
never mocked at a deeper level than this — only the collaborators external
to the query-path logic under test.
"""

from unittest.mock import MagicMock, patch
import pytest


def _make_mock_config(codebase_dir: str) -> MagicMock:
    """Build a minimal mock config object accepted by _perform_semantic_search.

    Bug #1690: codebase_dir must match the repo_path under test --
    ConfigManager.load_verified_config() (which _load_repo_config now
    routes through) verifies the resolved config.codebase_dir equals the
    requested target directory.
    """
    cfg = MagicMock()
    cfg.embedding_provider = "voyage-ai"
    cfg.codebase_dir = codebase_dir
    return cfg


def _stub_filesystem_store(search_side_effect) -> MagicMock:
    """Return a MagicMock that looks like a FilesystemVectorStore instance."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = MagicMock(spec=FilesystemVectorStore)
    store.search.side_effect = search_side_effect
    store.resolve_collection_name.return_value = "voyage-code-3"
    return store


def _stub_backend(store) -> MagicMock:
    """Return a mock backend whose get_vector_store_client() returns *store*."""
    backend = MagicMock()
    backend.get_vector_store_client.return_value = store
    return backend


def _make_multimodal_collection_dir(repo_path) -> None:
    """Create a real on-disk voyage-multimodal-3 collection directory, exactly
    as MultiIndexQueryService.has_multimodal_index() expects to detect it."""
    from pathlib import Path

    collection_dir = Path(repo_path) / ".code-indexer" / "index" / "voyage-multimodal-3"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        '{"name": "voyage-multimodal-3"}'
    )


class TestSemanticSearchServiceMultimodalFanOut:
    """Verify search_repository_path() fans out to multimodal collections
    when present, and stays byte-identical to pre-fix behavior when absent."""

    @pytest.fixture(autouse=True)
    def _patch_config_manager(self, monkeypatch, tmp_path):
        """Patch ConfigManager, set app.state.http_client_factory, and set a
        fake VOYAGE_API_KEY so MultiIndexQueryService's lazy multimodal
        provider construction (VoyageMultimodalClient.__init__) does not raise
        — the provider object is never actually used to call the network in
        this test, since store.search() itself is fully mocked.

        Bug #1690: mock_cfg.codebase_dir is set to str(tmp_path) -- all test
        methods use repo_path = tmp_path, and
        ConfigManager.load_verified_config() (which _load_repo_config now
        routes through) verifies the resolved config.codebase_dir matches
        the requested repo_path."""
        from code_indexer.server.fault_injection.null_factory import NullFaultFactory
        import code_indexer.server.app as app_module

        monkeypatch.setenv("VOYAGE_API_KEY", "test-key-not-real")

        had_factory = hasattr(app_module.app.state, "http_client_factory")
        original_factory = getattr(app_module.app.state, "http_client_factory", None)
        app_module.app.state.http_client_factory = NullFaultFactory()

        mock_cfg = _make_mock_config(str(tmp_path))
        with patch(
            "code_indexer.server.services.search_service.ConfigManager"
            ".create_with_backtrack"
        ) as mock_cm_cls:
            mock_cm = MagicMock()
            mock_cm.get_config.return_value = mock_cfg
            mock_cm_cls.return_value = mock_cm
            yield

        if had_factory:
            app_module.app.state.http_client_factory = original_factory
        elif hasattr(app_module.app.state, "http_client_factory"):
            del app_module.app.state.http_client_factory

    def test_code_only_repo_calls_store_search_exactly_once(self, tmp_path):
        """Regression proof: a repo with NO multimodal collection must call
        store.search() exactly once, with the same kwargs shape as before this
        fix — the multimodal branch must never trigger for code-only repos."""
        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        store = _stub_filesystem_store(lambda **kwargs: ([], {}))
        backend = _stub_backend(store)
        embedding_service = MagicMock()

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
        ):
            svc = SemanticSearchService()
            request = SemanticSearchRequest(query="auth logic", limit=5)
            svc.search_repository_path(str(tmp_path), request)

        assert store.search.call_count == 1
        call_kwargs = store.search.call_args.kwargs
        assert call_kwargs["embedding_provider"] is embedding_service
        assert call_kwargs["collection_name"] == "voyage-code-3"
        assert call_kwargs["no_embedding_cache_shortcut"] is False

    def test_multimodal_repo_calls_store_search_twice_with_isolated_cache_flag(
        self, tmp_path
    ):
        """When a multimodal collection exists on disk, store.search() must be
        called TWICE (once per collection), and the multimodal-collection call
        must have no_embedding_cache_shortcut=True regardless of the caller's
        own flag, while the code-collection call retains the caller's
        original value unchanged (embedding-cache isolation proof)."""
        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        _make_multimodal_collection_dir(tmp_path)

        captured_calls = []

        def search_side_effect(**kwargs):
            captured_calls.append(kwargs)
            return ([], {})

        store = _stub_filesystem_store(search_side_effect)
        backend = _stub_backend(store)
        embedding_service = MagicMock()

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
        ):
            svc = SemanticSearchService()
            # Caller explicitly requests NO cache bypass for its own request.
            request = SemanticSearchRequest(
                query="auth logic", limit=5, no_embedding_cache_shortcut=False
            )
            svc.search_repository_path(str(tmp_path), request)

        assert store.search.call_count == 2

        code_call = next(
            c for c in captured_calls if c.get("collection_name") == "voyage-code-3"
        )
        multimodal_call = next(
            c
            for c in captured_calls
            if c.get("collection_name") == "voyage-multimodal-3"
        )

        assert code_call["no_embedding_cache_shortcut"] is False, (
            "Code-collection call must retain the caller-supplied "
            "no_embedding_cache_shortcut value unchanged (Story #1108 S4)"
        )
        assert multimodal_call["no_embedding_cache_shortcut"] is True, (
            "Multimodal-collection call must ALWAYS force "
            "no_embedding_cache_shortcut=True (Bug #1480 cache isolation)"
        )

    def test_multimodal_results_merged_into_final_response(self, tmp_path):
        """The final SemanticSearchResponse.results must include entries
        sourced from BOTH the code and multimodal collection searches —
        proving a real fan-out + merge, not a silent single-collection read."""
        from code_indexer.server.models.api_models import SemanticSearchRequest
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        _make_multimodal_collection_dir(tmp_path)

        code_result = {
            "id": "c1",
            "score": 0.9,
            "payload": {
                "path": "src/file1.py",
                "chunk_offset": 0,
                "content": "code content",
            },
        }
        multimodal_result = {
            "id": "m1",
            "score": 0.85,
            "payload": {
                "path": "docs/diagram.png",
                "chunk_offset": 0,
                "content": "image content",
            },
        }

        def search_side_effect(**kwargs):
            if kwargs.get("collection_name") == "voyage-multimodal-3":
                return ([multimodal_result], {})
            return ([code_result], {})

        store = _stub_filesystem_store(search_side_effect)
        backend = _stub_backend(store)
        embedding_service = MagicMock()

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch("code_indexer.server.app._server_hnsw_cache", None),
        ):
            svc = SemanticSearchService()
            request = SemanticSearchRequest(query="diagram", limit=10)
            response = svc.search_repository_path(str(tmp_path), request)

        result_paths = {item.file_path for item in response.results}
        assert result_paths == {"src/file1.py", "docs/diagram.png"}
