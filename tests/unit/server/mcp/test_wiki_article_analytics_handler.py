"""
Tests for wiki_article_analytics MCP tool - Handler Behavior (Story #293).

Tests cover:
- AC2: Returns article list with required fields (title, path, real_views,
        first_viewed_at, last_viewed_at, wiki_url), sorted DESC by default.
- AC3: sort_by most_viewed=DESC, least_viewed=ASC, tie-breaking by article_path.
- AC5: Returns explicit error for non-wiki-enabled repos.
- Empty results when no views exist.
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
def sample_view_records():
    """Sample view count records as returned by wiki_cache.get_all_view_counts()."""
    return [
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


@pytest.fixture
def mock_wiki_cache(sample_view_records):
    """Mock WikiCache with get_all_view_counts returning sample data."""
    cache = Mock()
    cache.get_all_view_counts.return_value = sample_view_records
    return cache


@pytest.fixture
def mock_grm_wiki_enabled():
    """Mock golden_repo_manager where sf-kb-wiki has wiki_enabled=True."""
    grm = Mock()
    grm.db_path = "/mock/path/cidx_server.db"
    grm._sqlite_backend = Mock()
    grm._sqlite_backend.list_repos.return_value = [
        {"alias": "sf-kb-wiki", "wiki_enabled": True},
        {"alias": "code-indexer", "wiki_enabled": False},
    ]
    return grm


@pytest.fixture
def mock_grm_no_wiki():
    """Mock golden_repo_manager where no repos have wiki enabled."""
    grm = Mock()
    grm.db_path = "/mock/path/cidx_server.db"
    grm._sqlite_backend = Mock()
    grm._sqlite_backend.list_repos.return_value = [
        {"alias": "code-indexer", "wiki_enabled": False},
    ]
    return grm


def _call_handler(params, mock_grm, mock_cache, mock_user):
    """Helper: call handle_wiki_article_analytics with mocked dependencies."""
    from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

    with (
        patch("code_indexer.server.app.golden_repo_manager", mock_grm),
        patch(
            "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
            return_value=mock_cache,
        ),
    ):
        return handle_wiki_article_analytics(params, mock_user)


# ============================================================================
# AC2: Response shape and required fields
# ============================================================================


class TestRequiredFields:
    """AC2: Handler returns article list with all required fields."""

    def test_returns_mcp_compliant_response(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Response must be MCP-compliant content array."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        assert "content" in result
        assert len(result["content"]) > 0
        assert result["content"][0]["type"] == "text"
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

    def test_articles_list_present(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Response must have 'articles' list."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert "articles" in data
        assert isinstance(data["articles"], list)

    def test_articles_have_title_field(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have a non-empty title field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "title" in article, f"Missing 'title': {article}"
            assert len(article["title"]) > 0

    def test_articles_have_path_field(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have a path field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "path" in article, f"Missing 'path': {article}"

    def test_articles_have_real_views_integer(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have real_views as integer."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "real_views" in article, f"Missing 'real_views': {article}"
            assert isinstance(article["real_views"], int)

    def test_articles_have_first_viewed_at(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have first_viewed_at field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "first_viewed_at" in article, f"Missing 'first_viewed_at': {article}"

    def test_articles_have_last_viewed_at(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have last_viewed_at field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "last_viewed_at" in article, f"Missing 'last_viewed_at': {article}"

    def test_articles_have_wiki_url(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Each article must have wiki_url field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            assert "wiki_url" in article, f"Missing 'wiki_url': {article}"

    def test_wiki_url_format(self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user):
        """AC2: wiki_url must be /wiki/{alias_no_global}/{path_no_md}."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        for article in data["articles"]:
            wiki_url = article["wiki_url"]
            assert wiki_url.startswith("/wiki/"), f"Must start with /wiki/: {wiki_url}"
            assert "-global" not in wiki_url, f"Must not contain -global: {wiki_url}"
            assert not wiki_url.endswith(".md"), f"Must not end with .md: {wiki_url}"
            assert (
                "/wiki/sf-kb-wiki/" in wiki_url
            ), f"Must contain /wiki/sf-kb-wiki/: {wiki_url}"

    def test_response_includes_total_count(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Response must include integer total_count field."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert "total_count" in data
        assert isinstance(data["total_count"], int)

    def test_response_includes_repo_alias(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: Response must echo back the repo_alias."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert "repo_alias" in data
        assert data["repo_alias"] == "sf-kb-wiki-global"


# ============================================================================
# AC3: Sorting - most_viewed DESC, least_viewed ASC, tie-breaking by path
# ============================================================================


class TestSorting:
    """AC3: Correct sort order for most_viewed and least_viewed."""

    def test_default_sort_is_most_viewed_descending(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2/AC3: Default sort (no sort_by) must be most_viewed (DESC)."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        view_counts = [a["real_views"] for a in data["articles"]]
        assert view_counts == sorted(
            view_counts, reverse=True
        ), f"Default sort must be DESC by real_views, got: {view_counts}"

    def test_most_viewed_returns_descending(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC3: sort_by=most_viewed must return articles in DESC order."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global", "sort_by": "most_viewed"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        view_counts = [a["real_views"] for a in data["articles"]]
        assert view_counts == sorted(
            view_counts, reverse=True
        ), f"most_viewed must be DESC: {view_counts}"

    def test_least_viewed_returns_ascending(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC3: sort_by=least_viewed must return articles in ASC order."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global", "sort_by": "least_viewed"},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        view_counts = [a["real_views"] for a in data["articles"]]
        assert view_counts == sorted(
            view_counts
        ), f"least_viewed must be ASC: {view_counts}"

    def test_tie_breaking_most_viewed_alphabetical(self, mock_user):
        """AC3: Ties in most_viewed sort must break alphabetically by path."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        tie_views = [
            {
                "article_path": "zebra.md",
                "real_views": 100,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
            {
                "article_path": "alpha.md",
                "real_views": 100,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
            {
                "article_path": "middle.md",
                "real_views": 100,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = tie_views

        grm = Mock()
        grm._sqlite_backend = Mock()
        grm._sqlite_backend.list_repos.return_value = [
            {"alias": "sf-kb-wiki", "wiki_enabled": True}
        ]

        with (
            patch("code_indexer.server.app.golden_repo_manager", grm),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global", "sort_by": "most_viewed"},
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        paths = [a["path"] for a in data["articles"]]
        assert paths == sorted(paths), f"Ties must break alphabetically: {paths}"

    def test_tie_breaking_least_viewed_alphabetical(self, mock_user):
        """AC3: Ties in least_viewed sort must break alphabetically by path."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        tie_views = [
            {
                "article_path": "zebra.md",
                "real_views": 10,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
            {
                "article_path": "alpha.md",
                "real_views": 10,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
            {
                "article_path": "middle.md",
                "real_views": 10,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            },
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = tie_views

        grm = Mock()
        grm._sqlite_backend = Mock()
        grm._sqlite_backend.list_repos.return_value = [
            {"alias": "sf-kb-wiki", "wiki_enabled": True}
        ]

        with (
            patch("code_indexer.server.app.golden_repo_manager", grm),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global", "sort_by": "least_viewed"},
                mock_user,
            )

        data = json.loads(result["content"][0]["text"])
        paths = [a["path"] for a in data["articles"]]
        assert paths == sorted(paths), f"Ties must break alphabetically: {paths}"


# ============================================================================
# AC2: limit parameter caps results
# ============================================================================


class TestLimitParameter:
    """AC2: limit parameter caps results."""

    def test_limit_caps_results(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: limit=2 must return at most 2 articles."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global", "limit": 2},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert (
            len(data["articles"]) <= 2
        ), f"limit=2 must cap at 2, got {len(data['articles'])}"

    def test_limit_default_is_20(self, mock_grm_wiki_enabled, mock_user):
        """AC1: Default limit is 20 - verified with 25 article records."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        many_views = [
            {
                "article_path": f"article{i:02d}.md",
                "real_views": 100 - i,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            }
            for i in range(25)
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = many_views

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global"}, mock_user
            )

        data = json.loads(result["content"][0]["text"])
        assert (
            len(data["articles"]) == 20
        ), f"Default limit must be 20, got {len(data['articles'])}"

    def test_limit_larger_than_available_returns_all(
        self, mock_grm_wiki_enabled, mock_wiki_cache, mock_user
    ):
        """AC2: limit=100 with 5 articles returns all 5."""
        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global", "limit": 100},
            mock_grm_wiki_enabled,
            mock_wiki_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert len(data["articles"]) == 5


# ============================================================================
# AC5: Error response for non-wiki-enabled repos
# ============================================================================


class TestNonWikiEnabledRepo:
    """AC5: Returns explicit error for non-wiki-enabled repos."""

    def test_returns_error_for_non_wiki_repo(self, mock_grm_no_wiki, mock_user):
        """AC5: Non-wiki repo must return success=False with explicit error."""
        mock_cache = Mock()
        result = _call_handler(
            {"repo_alias": "code-indexer-global"},
            mock_grm_no_wiki,
            mock_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "error" in data

    def test_error_message_exact_ac5_text(self, mock_grm_no_wiki, mock_user):
        """AC5: Error message must be exact AC5 text."""
        mock_cache = Mock()
        result = _call_handler(
            {"repo_alias": "code-indexer-global"},
            mock_grm_no_wiki,
            mock_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert data["error"] == "Wiki is not enabled for this repository"

    def test_global_suffix_stripped_for_wiki_check(self, mock_user):
        """AC5: -global suffix must be stripped when checking wiki_enabled."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        grm = Mock()
        grm._sqlite_backend = Mock()
        grm._sqlite_backend.list_repos.return_value = [
            {"alias": "sf-kb-wiki", "wiki_enabled": True}
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = []

        with (
            patch("code_indexer.server.app.golden_repo_manager", grm),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global"}, mock_user
            )

        data = json.loads(result["content"][0]["text"])
        assert (
            data["success"] is True
        ), "sf-kb-wiki-global must be recognized as wiki-enabled"

    def test_alias_without_global_suffix_also_works(self, mock_user):
        """AC5: Alias without -global suffix also works for wiki check."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        grm = Mock()
        grm._sqlite_backend = Mock()
        grm._sqlite_backend.list_repos.return_value = [
            {"alias": "sf-kb-wiki", "wiki_enabled": True}
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = []

        with (
            patch("code_indexer.server.app.golden_repo_manager", grm),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki"}, mock_user
            )

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True


# ============================================================================
# Empty results behavior
# ============================================================================


class TestEmptyResults:
    """Handler returns empty list gracefully when no views exist."""

    def test_empty_results_when_no_views(self, mock_grm_wiki_enabled, mock_user):
        """AC2: Returns success=True with empty articles when no views recorded."""
        empty_cache = Mock()
        empty_cache.get_all_view_counts.return_value = []

        result = _call_handler(
            {"repo_alias": "sf-kb-wiki-global"},
            mock_grm_wiki_enabled,
            empty_cache,
            mock_user,
        )
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert data["articles"] == []
        assert data["total_count"] == 0


# ============================================================================
# Title generation from path
# ============================================================================


class TestTitleGeneration:
    """AC2: Title derived from last path segment, humanized."""

    def test_hyphenated_filename_becomes_title_case(
        self, mock_grm_wiki_enabled, mock_user
    ):
        """AC2: 'getting-started.md' title becomes 'Getting Started'."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        views = [
            {
                "article_path": "Customer/getting-started.md",
                "real_views": 100,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            }
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = views

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global"}, mock_user
            )

        data = json.loads(result["content"][0]["text"])
        assert data["articles"][0]["title"] == "Getting Started"

    def test_underscore_in_filename_becomes_space(
        self, mock_grm_wiki_enabled, mock_user
    ):
        """AC2: 'system_configuration.md' title becomes 'System Configuration'."""
        from code_indexer.server.mcp.handlers import handle_wiki_article_analytics

        views = [
            {
                "article_path": "Admin/system_configuration.md",
                "real_views": 50,
                "first_viewed_at": "2024-01-01",
                "last_viewed_at": "2024-01-02",
            }
        ]
        mock_cache = Mock()
        mock_cache.get_all_view_counts.return_value = views

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm_wiki_enabled),
            patch(
                "code_indexer.server.mcp.handlers._get_wiki_cache_for_handler",
                return_value=mock_cache,
            ),
        ):
            result = handle_wiki_article_analytics(
                {"repo_alias": "sf-kb-wiki-global"}, mock_user
            )

        data = json.loads(result["content"][0]["text"])
        assert data["articles"][0]["title"] == "System Configuration"
