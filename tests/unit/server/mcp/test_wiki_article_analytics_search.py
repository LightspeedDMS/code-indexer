"""
Tests for wiki_article_analytics MCP tool - Search Query Filter (Story #293).

Tests cover:
- AC4: Optional search_query filters articles via CIDX semantic/FTS index.
        Results are still sorted by view count, not relevance.
"""

import json
import pytest
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_grm_wiki_enabled():
    """Mock golden_repo_manager where sf-kb-wiki has wiki_enabled=True."""
    grm = Mock()
    grm.db_path = "/mock/path/cidx_server.db"
    grm._sqlite_backend = Mock()
    grm._sqlite_backend.list_repos.return_value = [
        {"alias": "sf-kb-wiki", "wiki_enabled": True},
    ]
    return grm


@pytest.fixture
def multi_article_cache():
    """Mock wiki cache with multiple articles at varying view counts."""
    views = [
        {
            "article_path": "Customer/getting-started.md",
            "real_views": 150,
            "first_viewed_at": "2024-01-01T10:00:00",
            "last_viewed_at": "2024-03-01T15:30:00",
        },
        {
            "article_path": "Admin/configuration.md",
            "real_views": 75,
            "first_viewed_at": "2024-01-05T09:00:00",
            "last_viewed_at": "2024-02-28T11:00:00",
        },
        {
            "article_path": "API/reference.md",
            "real_views": 200,
            "first_viewed_at": "2023-12-01T08:00:00",
            "last_viewed_at": "2024-03-02T16:00:00",
        },
        {
            "article_path": "FAQ/common-issues.md",
            "real_views": 42,
            "first_viewed_at": "2024-02-01T12:00:00",
            "last_viewed_at": "2024-03-01T09:00:00",
        },
        {
            "article_path": "index.md",
            "real_views": 500,
            "first_viewed_at": "2023-11-01T07:00:00",
            "last_viewed_at": "2024-03-02T18:00:00",
        },
    ]
    cache = Mock()
    cache.get_all_view_counts.return_value = views
    return cache


# ============================================================================
# AC4: search_query filters articles via CIDX
# ============================================================================


class TestSearchQueryFilter:
    """AC4: Optional search_query filters articles via CIDX semantic/FTS."""

    def test_search_query_filters_to_matching_paths(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: search_query must filter articles to only those matching search results."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()
        mock_sqm.query_user_repositories.return_value = {
            "results": [{"file_path": "Customer/getting-started.md", "similarity_score": 0.9}]
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {
                    "repo_alias": "sf-kb-wiki-global",
                    "search_query": "getting started guide",
                },
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["articles"]) == 1
        assert data["articles"][0]["path"] == "Customer/getting-started.md"

    def test_search_query_results_sorted_by_views_not_relevance(
        self, mock_grm_wiki_enabled, mock_user
    ):
        """AC4: After search filter, results must be sorted by view count, not relevance."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        two_views = [
            {
                "article_path": "Admin/configuration.md",
                "real_views": 75,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
            {
                "article_path": "Customer/getting-started.md",
                "real_views": 150,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = two_views

        # Search returns both but Admin first (higher relevance in search)
        mock_sqm = Mock()
        mock_sqm.query_user_repositories.return_value = {
            "results": [
                {"file_path": "Admin/configuration.md", "similarity_score": 0.95},
                {"file_path": "Customer/getting-started.md", "similarity_score": 0.85},
            ]
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {
                    "repo_alias": "sf-kb-wiki-global",
                    "search_query": "configuration guide",
                },
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["articles"]) == 2
        # Must be sorted by views DESC (150 before 75), not by relevance (Admin was first)
        assert data["articles"][0]["real_views"] == 150
        assert data["articles"][0]["path"] == "Customer/getting-started.md"
        assert data["articles"][1]["real_views"] == 75

    def test_search_query_empty_results_returns_empty_list(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: search_query with no matches returns empty articles list."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()
        mock_sqm.query_user_repositories.return_value = {"results": []}

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {
                    "repo_alias": "sf-kb-wiki-global",
                    "search_query": "nonexistent topic xyz",
                },
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["articles"] == []
        assert data["total_count"] == 0

    def test_short_search_query_one_char_is_skipped(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: search_query shorter than 2 chars must be ignored (no search call)."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global", "search_query": "a"},
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        # All 5 articles returned (no filter applied)
        assert len(data["articles"]) == 5
        # semantic_query_manager was NOT called
        mock_sqm.query_user_repositories.assert_not_called()

    def test_empty_string_search_query_is_skipped(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: Empty string search_query must be ignored (no search call)."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global", "search_query": ""},
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["articles"]) == 5
        mock_sqm.query_user_repositories.assert_not_called()

    def test_no_search_query_returns_all_articles(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: No search_query must return all articles (no CIDX call)."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global"},
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["articles"]) == 5
        mock_sqm.query_user_repositories.assert_not_called()

    def test_search_mode_semantic_used_by_default(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: Default search_mode=semantic is passed to query_user_repositories."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()
        mock_sqm.query_user_repositories.return_value = {
            "results": [{"file_path": "Customer/getting-started.md", "similarity_score": 0.9}]
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            handle_wiki_article_analytics(
                {
                    "repo_alias": "sf-kb-wiki-global",
                    "search_query": "customer guide",
                    # No search_mode - should default to semantic
                },
                mock_user,
            )

        call_kwargs = mock_sqm.query_user_repositories.call_args
        # Verify search_mode="semantic" was used
        assert call_kwargs is not None
        # Use .kwargs for keyword arguments (the handler passes all params as kwargs)
        all_params = call_kwargs.kwargs
        assert (
            all_params.get("search_mode") == "semantic"
        ), f"Expected search_mode=semantic, got: {all_params}"

    def test_search_mode_fts_passed_through(
        self, mock_grm_wiki_enabled, multi_article_cache, mock_user
    ):
        """AC4: Explicit search_mode=fts is passed to query_user_repositories."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        mock_sqm = Mock()
        mock_sqm.query_user_repositories.return_value = {
            "results": [{"file_path": "Customer/getting-started.md", "similarity_score": 0.9}]
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=multi_article_cache,
            ),
            patch(
                "code_indexer.server.app.semantic_query_manager",
                mock_sqm,
            ),
        ):
            handle_wiki_article_analytics(
                {
                    "repo_alias": "sf-kb-wiki-global",
                    "search_query": "getting started",
                    "search_mode": "fts",
                },
                mock_user,
            )

        call_kwargs = mock_sqm.query_user_repositories.call_args
        assert call_kwargs is not None
        # Use .kwargs for keyword arguments (the handler passes all params as kwargs)
        all_params = call_kwargs.kwargs
        assert (
            all_params.get("search_mode") == "fts"
        ), f"Expected search_mode=fts, got: {all_params}"
