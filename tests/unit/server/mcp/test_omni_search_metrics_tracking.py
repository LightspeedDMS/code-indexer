"""
Tests for multi-repo search API metrics tracking.

Verifies that _omni_search_code() properly tracks API calls in the four buckets:
- semantic_searches: semantic search mode
- other_index_searches: FTS, temporal, hybrid searches
- regex_searches: regex search mode
- other_api_calls: (not applicable for search)

Bug: Multi-repo searches were not being tracked because they bypass
semantic_query_manager._perform_search() which has the tracking logic.
"""

import pytest
from unittest.mock import patch, MagicMock

from code_indexer.server.multi.multi_search_config import MultiSearchConfig


class TestOmniSearchMetricsTracking:
    """Tests that multi-repo searches are tracked in API metrics."""

    @pytest.fixture
    def mock_user(self):
        """Create mock user with permissions."""
        user = MagicMock()
        user.username = "testuser"
        user.permissions = {"query_repos": True}
        return user

    @pytest.fixture
    def mock_multi_search_response(self):
        """Create mock MultiSearchResponse."""
        response = MagicMock()
        response.results = {"repo1-global": [{"file_path": "test.py", "score": 0.9}]}
        response.errors = {}
        response.metadata = MagicMock()
        response.metadata.total_repos_searched = 1
        response.metadata.total_results = 1
        return response

    @pytest.fixture
    def valid_multi_search_config(self):
        """Create a valid MultiSearchConfig instance for mocking."""
        return MultiSearchConfig(
            max_workers=2,
            query_timeout_seconds=30,
            max_repos_per_query=50,
            max_results_per_repo=100,
        )

    def test_semantic_search_increments_semantic_metric(
        self, mock_user, mock_multi_search_response, valid_multi_search_config
    ):
        """Semantic multi-repo search should increment semantic_searches counter."""
        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch(
                "code_indexer.server.multi.multi_search_config.MultiSearchConfig.from_config"
            ) as mock_config_from_config,
            patch(
                "code_indexer.server.mcp.handlers.api_metrics_service"
            ) as mock_metrics,
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand,
        ):
            # Setup mocks - Story #51: handlers are now sync
            mock_service = MagicMock()
            mock_service.search = MagicMock(return_value=mock_multi_search_response)
            mock_service_class.return_value = mock_service

            # Return valid config directly
            mock_config_from_config.return_value = valid_multi_search_config

            # Return the repos as-is (no expansion needed)
            mock_expand.side_effect = lambda x: x

            # Import after patching
            from code_indexer.server.mcp.handlers import _omni_search_code

            params = {
                "repository_alias": ["repo1-global", "repo2-global"],
                "query_text": "test query",
                "search_mode": "semantic",
                "limit": 10,
            }

            _omni_search_code(params, mock_user)

            # Verify semantic search metric was incremented
            mock_metrics.increment_semantic_search.assert_called_once()
            mock_metrics.increment_other_index_search.assert_not_called()
            mock_metrics.increment_regex_search.assert_not_called()

    def test_fts_search_increments_other_index_metric(
        self, mock_user, mock_multi_search_response, valid_multi_search_config
    ):
        """FTS multi-repo search should increment other_index_searches counter."""
        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch(
                "code_indexer.server.multi.multi_search_config.MultiSearchConfig.from_config"
            ) as mock_config_from_config,
            patch(
                "code_indexer.server.mcp.handlers.api_metrics_service"
            ) as mock_metrics,
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand,
        ):
            # Setup mocks - Story #51: handlers are now sync
            mock_service = MagicMock()
            mock_service.search = MagicMock(return_value=mock_multi_search_response)
            mock_service_class.return_value = mock_service

            mock_config_from_config.return_value = valid_multi_search_config
            mock_expand.side_effect = lambda x: x

            from code_indexer.server.mcp.handlers import _omni_search_code

            params = {
                "repository_alias": ["repo1-global", "repo2-global"],
                "query_text": "test query",
                "search_mode": "fts",
                "limit": 10,
            }

            _omni_search_code(params, mock_user)

            # Verify other_index_search metric was incremented
            mock_metrics.increment_other_index_search.assert_called_once()
            mock_metrics.increment_semantic_search.assert_not_called()
            mock_metrics.increment_regex_search.assert_not_called()

    def test_regex_search_increments_regex_metric(
        self, mock_user, mock_multi_search_response, valid_multi_search_config
    ):
        """Regex multi-repo search should increment regex_searches counter."""
        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch(
                "code_indexer.server.multi.multi_search_config.MultiSearchConfig.from_config"
            ) as mock_config_from_config,
            patch(
                "code_indexer.server.mcp.handlers.api_metrics_service"
            ) as mock_metrics,
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand,
        ):
            # Setup mocks - Story #51: handlers are now sync
            mock_service = MagicMock()
            mock_service.search = MagicMock(return_value=mock_multi_search_response)
            mock_service_class.return_value = mock_service

            mock_config_from_config.return_value = valid_multi_search_config
            mock_expand.side_effect = lambda x: x

            from code_indexer.server.mcp.handlers import _omni_search_code

            params = {
                "repository_alias": ["repo1-global", "repo2-global"],
                "query_text": "test.*pattern",
                "search_mode": "regex",
                "limit": 10,
            }

            _omni_search_code(params, mock_user)

            # Verify regex search metric was incremented
            mock_metrics.increment_regex_search.assert_called_once()
            mock_metrics.increment_semantic_search.assert_not_called()
            mock_metrics.increment_other_index_search.assert_not_called()

    def test_temporal_search_increments_other_index_metric(
        self, mock_user, mock_multi_search_response, valid_multi_search_config
    ):
        """Temporal multi-repo search should increment other_index_searches counter."""
        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch(
                "code_indexer.server.multi.multi_search_config.MultiSearchConfig.from_config"
            ) as mock_config_from_config,
            patch(
                "code_indexer.server.mcp.handlers.api_metrics_service"
            ) as mock_metrics,
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand,
            patch(
                "code_indexer.server.mcp.handlers._is_temporal_query"
            ) as mock_is_temporal,
        ):
            # Setup mocks - Story #51: handlers are now sync
            mock_service = MagicMock()
            mock_service.search = MagicMock(return_value=mock_multi_search_response)
            mock_service_class.return_value = mock_service

            mock_config_from_config.return_value = valid_multi_search_config
            mock_expand.side_effect = lambda x: x
            mock_is_temporal.return_value = True

            from code_indexer.server.mcp.handlers import _omni_search_code

            params = {
                "repository_alias": ["repo1-global", "repo2-global"],
                "query_text": "test query",
                "search_mode": "semantic",  # Would be overridden to temporal
                "time_range_start": "2024-01-01",
                "time_range_end": "2024-12-31",
                "limit": 10,
            }

            _omni_search_code(params, mock_user)

            # Verify other_index_search metric was incremented (temporal is in this bucket)
            mock_metrics.increment_other_index_search.assert_called_once()
            mock_metrics.increment_semantic_search.assert_not_called()
            mock_metrics.increment_regex_search.assert_not_called()

    def test_empty_repos_does_not_track_metrics(self, mock_user):
        """Empty repository list should return early without tracking metrics."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.api_metrics_service"
            ) as mock_metrics,
            patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand,
        ):
            # Return empty list
            mock_expand.return_value = []

            from code_indexer.server.mcp.handlers import _omni_search_code

            params = {
                "repository_alias": [],
                "query_text": "test query",
                "search_mode": "semantic",
                "limit": 10,
            }

            result = _omni_search_code(params, mock_user)

            # Verify no metrics were incremented for empty repos
            mock_metrics.increment_semantic_search.assert_not_called()
            mock_metrics.increment_other_index_search.assert_not_called()
            mock_metrics.increment_regex_search.assert_not_called()

            # Verify early return with empty results
            assert result["content"][0]["text"]  # Should have response text
