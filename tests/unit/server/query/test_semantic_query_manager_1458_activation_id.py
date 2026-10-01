"""SemanticQueryManager threads activation_id from an activated-repo query
down to SemanticSearchService (Story #1458 AC11).

Real SemanticQueryManager + real `_search_single_repository` dispatch logic
-- only the SemanticSearchService.search_repository_path[_with_provider]
boundary (which needs a real indexed collection to run to completion) is
monkeypatched, to isolate "did activation_id reach the search-service call"
from "does the full embedding+HNSW pipeline work" (already proven by
test_search_service_1458_activation_id.py and
test_filesystem_vector_store_1458_activation_cache_key.py).
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.models.api_models import SemanticSearchResponse
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager


@pytest.fixture
def manager():
    return SemanticQueryManager(
        activated_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )


class TestSearchSingleRepositoryDefaultPathThreadsActivationId:
    def test_activation_id_forwarded_to_search_repository_path(self, manager):
        captured = {}

        def fake_search_repository_path(self, **kwargs):
            captured.update(kwargs)
            return SemanticSearchResponse(query="q", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path",
            fake_search_repository_path,
        ):
            manager._search_single_repository(
                repo_path="/some/repo",
                repository_alias="my-repo",
                query_text="test query",
                limit=5,
                min_score=None,
                file_extensions=None,
                query_strategy="primary_only",
                activation_id="default-path-token",
            )

        assert captured.get("activation_id") == "default-path-token"


class TestSearchWithProviderThreadsActivationId:
    def test_activation_id_forwarded_to_search_repository_path_with_provider(
        self, manager
    ):
        captured = {}

        def fake_search_with_provider(self, **kwargs):
            captured.update(kwargs)
            return SemanticSearchResponse(query="q", results=[], total=0)

        with patch(
            "code_indexer.server.services.search_service.SemanticSearchService.search_repository_path_with_provider",
            fake_search_with_provider,
        ):
            manager._search_single_repository(
                repo_path="/some/repo",
                repository_alias="my-repo",
                query_text="test query",
                limit=5,
                min_score=None,
                file_extensions=None,
                query_strategy="specific",
                preferred_provider="voyage-ai",
                activation_id="specific-provider-token",
            )

        assert captured.get("activation_id") == "specific-provider-token"
