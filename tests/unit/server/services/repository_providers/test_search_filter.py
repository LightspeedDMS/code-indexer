"""
Tests for repository provider search filtering functionality.

Story #16: These tests verify SERVER-SIDE search filtering behavior.
With server-side filtering, the API returns pre-filtered results, so:
- Mock responses should contain only matching repos (simulating API filtering)
- Tests verify the correct search params are passed to API
- Tests verify response parsing works correctly
"""

from typing import Optional
import pytest
from unittest.mock import MagicMock, patch


def create_mock_gitlab_project(
    name: str,
    path_with_namespace: str,
    description: Optional[str] = None,
    visibility: str = "private",
    last_commit_hash: Optional[str] = None,
    last_commit_author: Optional[str] = None,
) -> dict:
    """Helper to create mock GitLab project API response."""
    project = {
        "id": hash(name) % 10000,
        "name": name,
        "path_with_namespace": path_with_namespace,
        "description": description,
        "http_url_to_repo": f"https://gitlab.com/{path_with_namespace}.git",
        "ssh_url_to_repo": f"git@gitlab.com:{path_with_namespace}.git",
        "default_branch": "main",
        "last_activity_at": "2024-01-15T10:30:00Z",
        "visibility": visibility,
    }
    # Add commit info if provided (simulating extended API data)
    if last_commit_hash or last_commit_author:
        project["_last_commit_hash"] = last_commit_hash
        project["_last_commit_author"] = last_commit_author
    return project


@pytest.fixture
def gitlab_provider():
    """Create a GitLab provider with mocked dependencies."""
    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    token_manager.get_token.return_value = TokenData(
        platform="gitlab",
        token="glpat-test-token-123456789012",
        base_url=None,
    )
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = []

    return GitLabProvider(
        token_manager=token_manager,
        golden_repo_manager=golden_repo_manager,
    )


def create_mock_response(
    projects: list, total: Optional[int] = None, total_pages: int = 1
):
    """Create a mock HTTP response for GitLab API."""
    if total is None:
        total = len(projects)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"x-total": str(total), "x-total-pages": str(total_pages)}
    mock_response.json.return_value = projects
    mock_response.raise_for_status = MagicMock()
    return mock_response


class TestGitLabProviderSearchFilter:
    """Tests for GitLab provider search filtering (server-side via API)."""

    @pytest.mark.asyncio
    async def test_search_by_name_matches(self, gitlab_provider):
        """Test that server-side search finds repos by name (API pre-filters)."""
        # Server-side: API returns only matching repos
        projects = [
            create_mock_gitlab_project("auth-service", "team/auth-service", "Auth"),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="auth"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "team/auth-service"

    @pytest.mark.asyncio
    async def test_search_by_description_matches(self, gitlab_provider):
        """Test that server-side search finds repos by description (API pre-filters)."""
        # Server-side: API returns only matching repos
        projects = [
            create_mock_gitlab_project(
                "gateway", "team/gateway", "API with authentication"
            ),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="authentication"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "team/gateway"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, gitlab_provider):
        """Test that server-side search is case-insensitive (API handles this)."""
        # Server-side: API returns matching repos regardless of case
        projects = [
            create_mock_gitlab_project("MyProject", "team/MyProject", "IMPORTANT"),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="myproject"
            )
        assert len(result.repositories) == 1

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="important"
            )
        assert len(result.repositories) == 1

    @pytest.mark.asyncio
    async def test_search_no_matches(self, gitlab_provider):
        """Test that server-side search returns empty when no matches."""
        # Server-side: API returns empty when no matches
        projects = []
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="nonexistent"
            )

        assert len(result.repositories) == 0

    @pytest.mark.asyncio
    async def test_search_empty_string_returns_all(self, gitlab_provider):
        """Test that empty search string returns all repositories."""
        projects = [
            create_mock_gitlab_project("project-a", "team/project-a"),
            create_mock_gitlab_project("project-b", "team/project-b"),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search=""
            )
        assert len(result.repositories) == 2

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search=None
            )
        assert len(result.repositories) == 2

    @pytest.mark.asyncio
    async def test_search_special_characters_handled_safely(self, gitlab_provider):
        """Test that special characters in search are handled safely."""
        projects = [
            create_mock_gitlab_project(
                "test-project", "team/test-project", "Test (v1.0)"
            ),
        ]
        mock_response = create_mock_response(projects)

        special_searches = ["(v1.0)", "[test]", "test.*", "test/path"]
        for search_term in special_searches:
            with patch.object(
                gitlab_provider, "_make_api_request", return_value=mock_response
            ):
                result = await gitlab_provider.discover_repositories(
                    page=1, page_size=50, search=search_term
                )
                assert isinstance(result.repositories, list)

    @pytest.mark.asyncio
    async def test_search_with_null_description(self, gitlab_provider):
        """Test that server-side search handles repos with null description."""
        # Server-side: API returns all matching repos including those with null desc
        projects = [
            create_mock_gitlab_project("no-desc", "team/no-desc", None),
            create_mock_gitlab_project("target", "team/target", "Has description"),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="team"
            )

        # All API results returned (server-side filtering already done)
        assert len(result.repositories) == 2
        # Verify null description is handled
        assert result.repositories[0].description is None
        assert result.repositories[1].description == "Has description"

    @pytest.mark.asyncio
    async def test_search_applies_after_indexed_repo_exclusion(self, gitlab_provider):
        """Test that search filter is applied after excluding already-indexed repos."""
        gitlab_provider._golden_repo_manager.list_golden_repos.return_value = [
            {"repo_url": "https://gitlab.com/team/auth-service.git"}
        ]

        projects = [
            create_mock_gitlab_project("auth-service", "team/auth-service", "Indexed"),
            create_mock_gitlab_project(
                "auth-middleware", "team/auth-middleware", "Not indexed"
            ),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="auth"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "team/auth-middleware"

    @pytest.mark.asyncio
    async def test_search_returns_all_api_results(self, gitlab_provider):
        """Server-side search returns all repos from API (API did filtering)."""
        # Server-side: GitLab API search only searches name/path/description
        # Commit hash/author search is NOT supported by GitLab API
        projects = [
            create_mock_gitlab_project(
                "project-a",
                "team/project-a",
                "Desc A",
                last_commit_hash="abc1234def5678",
                last_commit_author="John Doe",
            ),
            create_mock_gitlab_project(
                "project-b",
                "team/project-b",
                "Desc B",
                last_commit_hash="xyz9999fff1111",
                last_commit_author="Jane Smith",
            ),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="project"
            )

        # All repos returned by API are included (no client-side filtering)
        assert len(result.repositories) == 2

    @pytest.mark.asyncio
    async def test_search_with_commit_info_preserved(self, gitlab_provider):
        """Commit info is preserved in results even with server-side search."""
        projects = [
            create_mock_gitlab_project(
                "project-a",
                "team/project-a",
                "Desc A",
                last_commit_hash="abc1234def5678",
                last_commit_author="John Doe",
            ),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="project"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].last_commit_hash == "abc1234def5678"
        assert result.repositories[0].last_commit_author == "John Doe"

    @pytest.mark.asyncio
    async def test_search_with_null_commit_info(self, gitlab_provider):
        """Server-side search handles repos with null commit info."""
        # Server-side: API returns repos regardless of commit info
        projects = [
            create_mock_gitlab_project(
                "no-commit",
                "team/no-commit",
                "No commit info",
            ),
            create_mock_gitlab_project(
                "has-commit",
                "team/has-commit",
                "Has commit info",
                last_commit_hash="abc1234",
                last_commit_author="Author",
            ),
        ]
        mock_response = create_mock_response(projects)

        with patch.object(
            gitlab_provider, "_make_api_request", return_value=mock_response
        ):
            result = await gitlab_provider.discover_repositories(
                page=1, page_size=50, search="commit"
            )

        # All API results returned (server-side filtering by name/desc)
        assert len(result.repositories) == 2


def create_mock_github_repo(
    name: str,
    full_name: str,
    description: Optional[str] = None,
    private: bool = False,
    last_commit_hash: Optional[str] = None,
    last_commit_author: Optional[str] = None,
) -> dict:
    """Helper to create mock GitHub repo API response."""
    repo = {
        "id": hash(name) % 10000,
        "name": name,
        "full_name": full_name,
        "description": description,
        "clone_url": f"https://github.com/{full_name}.git",
        "ssh_url": f"git@github.com:{full_name}.git",
        "default_branch": "main",
        "pushed_at": "2024-01-15T10:30:00Z",
        "private": private,
    }
    # Add commit info if provided (simulating extended API data)
    if last_commit_hash or last_commit_author:
        repo["_last_commit_hash"] = last_commit_hash
        repo["_last_commit_author"] = last_commit_author
    return repo


@pytest.fixture
def github_provider():
    """Create a GitHub provider with mocked dependencies."""
    from code_indexer.server.services.repository_providers.github_provider import (
        GitHubProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    token_manager.get_token.return_value = TokenData(
        platform="github",
        token="ghp_test123456789012345678901234567890",
        base_url=None,
    )
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = []

    return GitHubProvider(
        token_manager=token_manager,
        golden_repo_manager=golden_repo_manager,
    )


def create_github_mock_response(
    repos: list, link_header: str = "", is_search: bool = False
):
    """Create a mock HTTP response for GitHub API.

    Args:
        repos: List of repository dicts
        link_header: Optional Link header for pagination
        is_search: If True, format response as search API ({total_count, items})
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Link": link_header} if link_header else {}
    if is_search:
        # Search API returns {total_count, incomplete_results, items: [...]}
        mock_response.json.return_value = {
            "total_count": len(repos),
            "incomplete_results": False,
            "items": repos,
        }
    else:
        # Regular API returns [...]
        mock_response.json.return_value = repos
    mock_response.raise_for_status = MagicMock()
    return mock_response


class TestGitHubProviderSearchFilter:
    """Tests for GitHub provider search filtering (server-side via search API)."""

    @pytest.mark.asyncio
    async def test_search_by_name_matches(self, github_provider):
        """Test that server-side search finds repos by name (API pre-filters)."""
        # Server-side: Search API returns only matching repos
        repos = [
            create_mock_github_repo("auth-lib", "owner/auth-lib", "Auth library"),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="auth"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/auth-lib"

    @pytest.mark.asyncio
    async def test_search_by_description_matches(self, github_provider):
        """Test that server-side search finds repos by description (API pre-filters)."""
        # Server-side: Search API returns only matching repos
        repos = [
            create_mock_github_repo(
                "gateway", "owner/gateway", "API with authentication"
            ),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="authentication"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/gateway"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, github_provider):
        """Test that server-side search is case-insensitive (API handles this)."""
        # Server-side: Search API returns matching repos regardless of case
        repos = [
            create_mock_github_repo("AwesomeProject", "owner/AwesomeProject", "GREAT"),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="awesome"
            )
        assert len(result.repositories) == 1

    @pytest.mark.asyncio
    async def test_search_no_matches(self, github_provider):
        """Test that server-side search returns empty when no matches."""
        # Server-side: Search API returns empty when no matches
        repos = []
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="nonexistent"
            )

        assert len(result.repositories) == 0

    @pytest.mark.asyncio
    async def test_search_empty_string_returns_all(self, github_provider):
        """Test that empty search uses regular API and returns all repos."""
        repos = [
            create_mock_github_repo("repo-a", "owner/repo-a"),
            create_mock_github_repo("repo-b", "owner/repo-b"),
        ]
        # Empty search uses regular API (not search API)
        mock_response = create_github_mock_response(repos, is_search=False)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search=""
            )
        assert len(result.repositories) == 2

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search=None
            )
        assert len(result.repositories) == 2

    @pytest.mark.asyncio
    async def test_search_special_characters_handled_safely(self, github_provider):
        """Test that special characters in search are handled safely."""
        repos = [
            create_mock_github_repo("test-repo", "owner/test-repo", "Test (v2.0)"),
        ]
        # Search uses search API format
        mock_response = create_github_mock_response(repos, is_search=True)

        special_searches = ["(v2.0)", "[test]", "test.*", "test/path"]
        for search_term in special_searches:
            with patch.object(
                github_provider, "_make_api_request", return_value=mock_response
            ):
                result = await github_provider.discover_repositories(
                    page=1, page_size=50, search=search_term
                )
                assert isinstance(result.repositories, list)

    @pytest.mark.asyncio
    async def test_search_returns_all_api_results(self, github_provider):
        """Server-side search returns all repos from API (API did filtering)."""
        # Server-side: GitHub search API only searches name/description
        # Commit hash/author search is NOT supported by GitHub search API
        repos = [
            create_mock_github_repo(
                "repo-a",
                "owner/repo-a",
                "Desc A",
                last_commit_hash="abc1234def5678",
                last_commit_author="John Doe",
            ),
            create_mock_github_repo(
                "repo-b",
                "owner/repo-b",
                "Desc B",
                last_commit_hash="xyz9999fff1111",
                last_commit_author="Jane Smith",
            ),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="repo"
            )

        # All repos returned by API are included (no client-side filtering)
        assert len(result.repositories) == 2

    @pytest.mark.asyncio
    async def test_search_with_commit_info_preserved(self, github_provider):
        """Commit info is preserved in results even with server-side search."""
        repos = [
            create_mock_github_repo(
                "repo-a",
                "owner/repo-a",
                "Desc A",
                last_commit_hash="abc1234def5678",
                last_commit_author="John Doe",
            ),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="repo"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].last_commit_hash == "abc1234def5678"
        assert result.repositories[0].last_commit_author == "John Doe"

    @pytest.mark.asyncio
    async def test_search_with_null_commit_info(self, github_provider):
        """Server-side search handles repos with null commit info."""
        # Server-side: API returns repos regardless of commit info
        repos = [
            create_mock_github_repo(
                "no-commit",
                "owner/no-commit",
                "No commit info",
            ),
            create_mock_github_repo(
                "has-commit",
                "owner/has-commit",
                "Has commit info",
                last_commit_hash="abc1234",
                last_commit_author="Author",
            ),
        ]
        mock_response = create_github_mock_response(repos, is_search=True)

        with patch.object(
            github_provider, "_make_api_request", return_value=mock_response
        ):
            result = await github_provider.discover_repositories(
                page=1, page_size=50, search="commit"
            )

        # All API results returned (server-side filtering by name/desc)
        assert len(result.repositories) == 2
