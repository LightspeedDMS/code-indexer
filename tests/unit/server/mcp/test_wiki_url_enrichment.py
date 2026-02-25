"""
Tests for MCP tool response wiki URL enrichment (Story #292).

Tests cover:
- Core helper functions: _get_wiki_enabled_repos, _enrich_with_wiki_url
- Integration with search_code, handle_regex_search, get_file_content handlers

AC1: Wiki URL in search results for .md files from wiki-enabled repos
AC2: Pre-built wiki-enabled repos set per request (no per-result DB queries)
AC3: Only golden repo wikis (not user activated)
AC4: Field completely omitted when wiki not enabled (no null/empty)
AC5: Works across search_code, regex_search, and get_file_content handlers
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def wiki_enabled_repos_set():
    """A set of wiki-enabled repo aliases (without -global suffix)."""
    return {"sf-kb-wiki", "docs-repo", "knowledge-base"}


@pytest.fixture
def mock_sqlite_backend_with_wiki():
    """Mock sqlite backend that returns repos with wiki_enabled flags."""
    backend = Mock()
    backend.list_repos.return_value = [
        {"alias": "sf-kb-wiki", "wiki_enabled": True, "repo_url": "git@github.com:org/sf-kb-wiki.git"},
        {"alias": "docs-repo", "wiki_enabled": True, "repo_url": "git@github.com:org/docs-repo.git"},
        {"alias": "code-indexer", "wiki_enabled": False, "repo_url": "git@github.com:org/ci.git"},
        {"alias": "another-repo", "wiki_enabled": False, "repo_url": "git@github.com:org/another.git"},
    ]
    return backend


@pytest.fixture
def mock_sqlite_backend_no_wiki():
    """Mock sqlite backend where no repos have wiki enabled."""
    backend = Mock()
    backend.list_repos.return_value = [
        {"alias": "code-indexer", "wiki_enabled": False, "repo_url": "git@github.com:org/ci.git"},
        {"alias": "another-repo", "wiki_enabled": False, "repo_url": "git@github.com:org/another.git"},
    ]
    return backend


# ============================================================================
# Tests for _enrich_with_wiki_url helper function
# ============================================================================

class TestEnrichWithWikiUrl:
    """Tests for the _enrich_with_wiki_url helper function (AC1, AC4)."""

    def test_enrich_adds_wiki_url_for_md_file_from_wiki_enabled_repo(self):
        """AC1: wiki_url is added for .md file from wiki-enabled repo."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        wiki_enabled_repos = {"sf-kb-wiki"}
        _enrich_with_wiki_url(result_dict, "Customer/getting-started.md",
                              "sf-kb-wiki-global", wiki_enabled_repos)

        assert "wiki_url" in result_dict
        assert result_dict["wiki_url"] == "/wiki/sf-kb-wiki/Customer/getting-started"

    def test_enrich_skips_non_md_file(self):
        """AC4: wiki_url is NOT added for non-.md files."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        wiki_enabled_repos = {"sf-kb-wiki"}
        _enrich_with_wiki_url(result_dict, "src/auth.py",
                              "sf-kb-wiki-global", wiki_enabled_repos)

        assert "wiki_url" not in result_dict

    def test_enrich_skips_non_wiki_enabled_repo(self):
        """AC3/AC4: wiki_url is NOT added for repos not in wiki_enabled_repos set."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        wiki_enabled_repos = {"sf-kb-wiki"}  # code-indexer is NOT in this set
        _enrich_with_wiki_url(result_dict, "docs/overview.md",
                              "code-indexer-global", wiki_enabled_repos)

        assert "wiki_url" not in result_dict

    def test_enrich_omits_field_completely_not_null(self):
        """AC4: Field is completely absent (not null, not empty string) when conditions not met."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        # Case 1: non-.md file
        result1 = {}
        _enrich_with_wiki_url(result1, "README.txt", "sf-kb-wiki-global", {"sf-kb-wiki"})
        assert "wiki_url" not in result1  # Key must not exist at all

        # Case 2: non-wiki repo
        result2 = {}
        _enrich_with_wiki_url(result2, "docs.md", "code-indexer-global", {"sf-kb-wiki"})
        assert "wiki_url" not in result2  # Key must not exist at all

        # Case 3: empty file_path
        result3 = {}
        _enrich_with_wiki_url(result3, "", "sf-kb-wiki-global", {"sf-kb-wiki"})
        assert "wiki_url" not in result3  # Key must not exist at all

    def test_enrich_strips_md_extension_from_url(self):
        """AC1: wiki_url strips .md extension from file path."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        _enrich_with_wiki_url(result_dict, "index.md",
                              "docs-repo-global", {"docs-repo"})

        assert result_dict["wiki_url"] == "/wiki/docs-repo/index"
        assert not result_dict["wiki_url"].endswith(".md")

    def test_enrich_strips_global_suffix_from_alias_in_url(self):
        """wiki_url uses alias WITHOUT -global suffix (matches wiki route pattern)."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        _enrich_with_wiki_url(result_dict, "article.md",
                              "sf-kb-wiki-global", {"sf-kb-wiki"})

        # URL should use 'sf-kb-wiki' not 'sf-kb-wiki-global'
        assert "/wiki/sf-kb-wiki/" in result_dict["wiki_url"]
        assert "-global" not in result_dict["wiki_url"]

    def test_enrich_handles_nested_paths(self):
        """wiki_url preserves nested directory structure."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = {}
        _enrich_with_wiki_url(result_dict, "Customer/Support/ticket-management.md",
                              "docs-repo-global", {"docs-repo"})

        assert result_dict["wiki_url"] == "/wiki/docs-repo/Customer/Support/ticket-management"

    def test_enrich_handles_empty_inputs(self):
        """_enrich_with_wiki_url handles None/empty inputs without error."""
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        # None file_path
        result1 = {}
        _enrich_with_wiki_url(result1, None, "sf-kb-wiki-global", {"sf-kb-wiki"})
        assert "wiki_url" not in result1

        # None repository_alias
        result2 = {}
        _enrich_with_wiki_url(result2, "article.md", None, {"sf-kb-wiki"})
        assert "wiki_url" not in result2

        # Empty set
        result3 = {}
        _enrich_with_wiki_url(result3, "article.md", "sf-kb-wiki-global", set())
        assert "wiki_url" not in result3


# ============================================================================
# Tests for _get_wiki_enabled_repos helper function
# ============================================================================

class TestGetWikiEnabledRepos:
    """Tests for the _get_wiki_enabled_repos helper function (AC2)."""

    def test_get_wiki_enabled_repos_returns_correct_set(self, mock_sqlite_backend_with_wiki):
        """AC2: Returns set of wiki-enabled aliases (without -global suffix)."""
        from code_indexer.server.mcp.handlers import _get_wiki_enabled_repos

        with patch("code_indexer.server.app.golden_repo_manager") as mock_grm:
            mock_grm._sqlite_backend = mock_sqlite_backend_with_wiki
            result = _get_wiki_enabled_repos()

        assert isinstance(result, set)
        assert "sf-kb-wiki" in result
        assert "docs-repo" in result
        assert "code-indexer" not in result
        assert "another-repo" not in result

    def test_get_wiki_enabled_repos_returns_empty_when_no_wiki_repos(self, mock_sqlite_backend_no_wiki):
        """Returns empty set when no repos have wiki enabled."""
        from code_indexer.server.mcp.handlers import _get_wiki_enabled_repos

        with patch("code_indexer.server.app.golden_repo_manager") as mock_grm:
            mock_grm._sqlite_backend = mock_sqlite_backend_no_wiki
            result = _get_wiki_enabled_repos()

        assert isinstance(result, set)
        assert len(result) == 0

    def test_get_wiki_enabled_repos_handles_missing_manager(self):
        """AC2: Returns empty set gracefully when golden_repo_manager is None."""
        from code_indexer.server.mcp.handlers import _get_wiki_enabled_repos

        with patch("code_indexer.server.app.golden_repo_manager", None):
            result = _get_wiki_enabled_repos()

        assert isinstance(result, set)
        assert len(result) == 0

    def test_get_wiki_enabled_repos_handles_exception(self):
        """_get_wiki_enabled_repos degrades gracefully on exception."""
        from code_indexer.server.mcp.handlers import _get_wiki_enabled_repos

        with patch("code_indexer.server.app.golden_repo_manager") as mock_grm:
            mock_grm._sqlite_backend.list_repos.side_effect = RuntimeError("DB error")
            result = _get_wiki_enabled_repos()

        assert isinstance(result, set)
        assert len(result) == 0


# ============================================================================
# Integration tests: search_code handler wiki URL enrichment
# ============================================================================

class TestSearchCodeWikiUrlEnrichment:
    """Integration tests for wiki URL enrichment in search_code handler (AC5).

    Tests use the same result dict structure that search_code produces:
    {"file_path": ..., "similarity_score": ..., "code_snippet": ..., "repository_alias": ...}
    and call _enrich_with_wiki_url directly to verify enrichment logic without
    needing to mock the entire complex search_code handler stack.
    """

    def _build_search_result_dict(self, file_path, repository_alias="sf-kb-wiki-global", score=0.9):
        """Build a result dict matching the structure search_code produces."""
        return {
            "file_path": file_path,
            "similarity_score": score,
            "code_snippet": "some content",
            "repository_alias": repository_alias,
        }

    def test_search_code_result_includes_wiki_url_for_md(self, mock_sqlite_backend_with_wiki):
        """AC1/AC5: search_code result includes wiki_url for .md files from wiki-enabled repos.

        Verifies that _enrich_with_wiki_url (called by search_code) adds wiki_url
        to result dicts with the structure search_code produces.
        """
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = self._build_search_result_dict("Customer/getting-started.md")
        wiki_enabled_repos = {"sf-kb-wiki", "docs-repo"}

        _enrich_with_wiki_url(
            result_dict,
            result_dict.get("file_path", ""),
            result_dict.get("repository_alias", ""),
            wiki_enabled_repos,
        )

        assert "wiki_url" in result_dict
        assert "/wiki/sf-kb-wiki/" in result_dict["wiki_url"]
        assert "getting-started" in result_dict["wiki_url"]
        assert not result_dict["wiki_url"].endswith(".md")

    def test_search_code_result_excludes_wiki_url_for_non_md(self, mock_sqlite_backend_with_wiki):
        """AC4: search_code result does NOT include wiki_url for non-.md files.

        Verifies that _enrich_with_wiki_url (called by search_code) omits wiki_url
        for non-.md files using the same dict structure search_code produces.
        """
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        result_dict = self._build_search_result_dict("src/auth.py")
        wiki_enabled_repos = {"sf-kb-wiki", "docs-repo"}

        _enrich_with_wiki_url(
            result_dict,
            result_dict.get("file_path", ""),
            result_dict.get("repository_alias", ""),
            wiki_enabled_repos,
        )

        assert "wiki_url" not in result_dict


# ============================================================================
# Integration tests: handle_regex_search handler wiki URL enrichment
# ============================================================================

class TestRegexSearchWikiUrlEnrichment:
    """Integration tests for wiki URL enrichment in handle_regex_search (AC5).

    Tests use the same match dict structure that handle_regex_search produces:
    {"file_path": ..., "line_number": ..., "column": ..., "line_content": ...,
     "context_before": ..., "context_after": ...}
    and call _enrich_with_wiki_url directly to verify enrichment logic without
    needing to mock the entire complex handle_regex_search handler stack.
    """

    def test_regex_search_result_includes_wiki_url(self, mock_sqlite_backend_with_wiki):
        """AC1/AC5: handle_regex_search includes wiki_url for .md file matches.

        Verifies that _enrich_with_wiki_url (called by handle_regex_search) adds
        wiki_url to match dicts with the structure handle_regex_search produces.
        """
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        # This is the exact structure handle_regex_search builds for each match
        match_dict = {
            "file_path": "Customer/faq.md",
            "line_number": 5,
            "column": 1,
            "line_content": "## Frequently Asked Questions",
            "context_before": [],
            "context_after": [],
        }
        wiki_enabled_repos = {"sf-kb-wiki", "docs-repo"}
        repository_alias = "sf-kb-wiki-global"

        _enrich_with_wiki_url(
            match_dict,
            match_dict.get("file_path", ""),
            repository_alias,
            wiki_enabled_repos,
        )

        assert "wiki_url" in match_dict
        assert "/wiki/sf-kb-wiki/" in match_dict["wiki_url"]
        assert "Customer/faq" in match_dict["wiki_url"]
        assert not match_dict["wiki_url"].endswith(".md")


# ============================================================================
# Integration tests: get_file_content handler wiki URL enrichment
# ============================================================================

class TestGetFileContentWikiUrlEnrichment:
    """Integration tests for wiki URL enrichment in get_file_content handler (AC5)."""

    def test_get_file_content_metadata_includes_wiki_url(self, mock_user, mock_sqlite_backend_with_wiki):
        """AC1/AC5: get_file_content metadata includes wiki_url for .md files."""
        from code_indexer.server.mcp.handlers import get_file_content

        mock_file_result = {
            "content": "# Getting Started\n\nWelcome to the wiki!",
            "metadata": {
                "file_path": "Customer/getting-started.md",
                "repository_alias": "sf-kb-wiki-global",
                "language": "markdown",
                "size_bytes": 42,
            }
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager") as mock_grm,
            patch("code_indexer.server.app.file_service") as mock_fs,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/mock/golden-repos"),
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_registry_factory,
            patch("code_indexer.server.app.app") as mock_app,
        ):
            mock_grm._sqlite_backend = mock_sqlite_backend_with_wiki
            mock_fs.get_file_content_by_path.return_value = mock_file_result
            mock_registry = Mock()
            mock_registry.list_global_repos.return_value = [
                {"alias_name": "sf-kb-wiki-global", "repo_name": "sf-kb-wiki", "index_path": "/mock/path"}
            ]
            mock_registry_factory.return_value = mock_registry

            # Mock alias manager
            with patch("code_indexer.global_repos.alias_manager.AliasManager") as mock_am_class:
                mock_am = Mock()
                mock_am.read_alias.return_value = "/mock/golden-repos/sf-kb-wiki"
                mock_am_class.return_value = mock_am

                # Mock app.state.payload_cache as None to skip truncation
                mock_app.state.payload_cache = None

                params = {
                    "repository_alias": "sf-kb-wiki-global",
                    "file_path": "Customer/getting-started.md",
                }
                result = get_file_content(params, mock_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert "metadata" in data
        assert "wiki_url" in data["metadata"]
        assert "/wiki/sf-kb-wiki/" in data["metadata"]["wiki_url"]
        assert "Customer/getting-started" in data["metadata"]["wiki_url"]
        assert not data["metadata"]["wiki_url"].endswith(".md")

    def test_get_file_content_metadata_excludes_wiki_url_for_non_md(self, mock_user, mock_sqlite_backend_with_wiki):
        """AC4: get_file_content metadata does NOT include wiki_url for non-.md files."""
        from code_indexer.server.mcp.handlers import get_file_content

        mock_file_result = {
            "content": "def main(): pass",
            "metadata": {
                "file_path": "src/main.py",
                "repository_alias": "sf-kb-wiki-global",
                "language": "python",
                "size_bytes": 18,
            }
        }

        with (
            patch("code_indexer.server.app.golden_repo_manager") as mock_grm,
            patch("code_indexer.server.app.file_service") as mock_fs,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/mock/golden-repos"),
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_registry_factory,
            patch("code_indexer.server.app.app") as mock_app,
        ):
            mock_grm._sqlite_backend = mock_sqlite_backend_with_wiki
            mock_fs.get_file_content_by_path.return_value = mock_file_result
            mock_registry = Mock()
            mock_registry.list_global_repos.return_value = [
                {"alias_name": "sf-kb-wiki-global", "repo_name": "sf-kb-wiki", "index_path": "/mock/path"}
            ]
            mock_registry_factory.return_value = mock_registry

            with patch("code_indexer.global_repos.alias_manager.AliasManager") as mock_am_class:
                mock_am = Mock()
                mock_am.read_alias.return_value = "/mock/golden-repos/sf-kb-wiki"
                mock_am_class.return_value = mock_am

                mock_app.state.payload_cache = None

                params = {
                    "repository_alias": "sf-kb-wiki-global",
                    "file_path": "src/main.py",
                }
                result = get_file_content(params, mock_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert "metadata" in data
        assert "wiki_url" not in data["metadata"]


# ============================================================================
# AC3: Only golden repos get wiki_url (not user-activated repos)
# ============================================================================

class TestOnlyGoldenReposGetWikiUrl:
    """AC3: Verify wiki URL enrichment only applies to golden repos, not activated repos."""

    def test_only_golden_repos_get_wiki_url_not_activated(self, wiki_enabled_repos_set):
        """AC3: _enrich_with_wiki_url requires repo to be in wiki_enabled_repos set.

        Activated repos are NOT in the wiki_enabled_repos set (which only contains
        golden repo aliases). So they naturally get no wiki_url enrichment.
        """
        from code_indexer.server.mcp.handlers import _enrich_with_wiki_url

        # Simulate a user-activated repo (not in wiki_enabled_repos)
        activated_alias = "my-personal-docs"  # Not a golden repo
        result_dict = {}
        _enrich_with_wiki_url(result_dict, "README.md",
                              activated_alias, wiki_enabled_repos_set)

        # No wiki_url for user-activated repos
        assert "wiki_url" not in result_dict

        # Golden repo with wiki enabled gets wiki_url
        golden_result = {}
        _enrich_with_wiki_url(golden_result, "README.md",
                              "sf-kb-wiki-global", wiki_enabled_repos_set)
        assert "wiki_url" in golden_result
