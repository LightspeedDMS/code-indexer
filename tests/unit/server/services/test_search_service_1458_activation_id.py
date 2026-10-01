"""SemanticSearchService threads activation_id through to the constructed
FilesystemVectorStore (Story #1458 AC11).

Real repo directory + real config.json + real BackendFactory/
FilesystemBackend/FilesystemVectorStore construction chain -- only
FilesystemVectorStore.search() itself is monkeypatched (the SAME
established pattern test_search_service_filesystem_params.py already uses)
to avoid needing a real indexed HNSW collection, since the property under
test here is "did activation_id reach the constructed FSV instance",
independent of query result correctness (already proven end-to-end by
test_filesystem_vector_store_1458_activation_cache_key.py).
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.models.api_models import SemanticSearchRequest
from code_indexer.server.services.search_service import SemanticSearchService
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


@pytest.fixture
def test_repo_with_filesystem_backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir) / "test_repo"
        repo_path.mkdir()
        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir()

        config_data = {
            "embedding": {
                "provider": "voyage",
                "model": "voyage-3-large",
                "dimensions": 1024,
            },
            "vector_store": {"provider": "filesystem"},
            "chunking": {
                "chunk_size": 512,
                "chunk_overlap": 128,
                "tree_sitter_config": {"python": {"enabled": True}},
            },
        }
        (config_dir / "config.json").write_text(json.dumps(config_data, indent=2))
        (config_dir / "index").mkdir()

        yield str(repo_path)


class TestSearchRepositoryPathThreadsActivationId:
    def test_activation_id_reaches_constructed_vector_store(
        self, test_repo_with_filesystem_backend
    ):
        repo_path = test_repo_with_filesystem_backend
        search_service = SemanticSearchService()

        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = [0.1] * 1024

        observed_activation_ids = []

        def tracked_search(self, *args, **kwargs):
            observed_activation_ids.append(self.activation_id)
            return [], {}

        with patch.object(FilesystemVectorStore, "search", tracked_search):
            with patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=mock_embedding_service,
            ):
                search_service.search_repository_path(
                    repo_path=repo_path,
                    search_request=SemanticSearchRequest(
                        query="test", limit=5, include_source=True
                    ),
                    activation_id="repo-path-activation-token",
                )

        assert observed_activation_ids == ["repo-path-activation-token"]

    def test_no_activation_id_defaults_to_none(self, test_repo_with_filesystem_backend):
        repo_path = test_repo_with_filesystem_backend
        search_service = SemanticSearchService()

        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = [0.1] * 1024

        observed_activation_ids = []

        def tracked_search(self, *args, **kwargs):
            observed_activation_ids.append(self.activation_id)
            return [], {}

        with patch.object(FilesystemVectorStore, "search", tracked_search):
            with patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=mock_embedding_service,
            ):
                search_service.search_repository_path(
                    repo_path=repo_path,
                    search_request=SemanticSearchRequest(
                        query="test", limit=5, include_source=True
                    ),
                )

        assert observed_activation_ids == [None]


class TestSearchRepositoryPathWithProviderThreadsActivationId:
    def test_activation_id_reaches_constructed_vector_store(
        self, test_repo_with_filesystem_backend
    ):
        repo_path = test_repo_with_filesystem_backend
        search_service = SemanticSearchService()

        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = [0.1] * 1024

        observed_activation_ids = []

        def tracked_search(self, *args, **kwargs):
            observed_activation_ids.append(self.activation_id)
            return [], {}

        with patch.object(FilesystemVectorStore, "search", tracked_search):
            with patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=mock_embedding_service,
            ):
                search_service.search_repository_path_with_provider(
                    repo_path=repo_path,
                    search_request=SemanticSearchRequest(
                        query="test", limit=5, include_source=True
                    ),
                    provider_name="voyage-ai",
                    activation_id="provider-path-activation-token",
                )

        assert observed_activation_ids == ["provider-path-activation-token"]
