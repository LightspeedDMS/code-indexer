"""
Tests for GitHubProvider.

Following TDD methodology - these tests are written FIRST before implementation.
Tests define the expected behavior for GitHub repository discovery.
"""

import pytest
from unittest.mock import MagicMock, patch
import httpx


class TestGitHubProviderConfiguration:
    """Tests for GitHubProvider configuration handling."""

    def test_provider_has_github_platform(self):
        """Test that GitHubProvider reports github as its platform."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        # Create provider with mock token manager
        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.platform == "github"

    @pytest.mark.asyncio
    async def test_is_configured_returns_true_when_token_exists(self):
        """Test is_configured returns True when GitHub token is configured."""
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

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_returns_false_when_no_token(self):
        """Test is_configured returns False when no GitHub token is configured."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        token_manager.get_token.return_value = None
        golden_repo_manager = MagicMock()

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider.is_configured() is False

    def test_default_base_url_is_github_api(self):
        """Test that default base URL is api.github.com."""
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

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        assert provider._get_base_url() == "https://api.github.com"


class TestGitHubProviderDiscovery:
    """Tests for GitHubProvider repository discovery."""

    @pytest.mark.asyncio
    async def test_discover_repositories_returns_result_model(self):
        """Test that discover_repositories returns a RepositoryDiscoveryResult."""
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData
        from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="dummy",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 0,
                        "nodes": [],
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert isinstance(result, RepositoryDiscoveryResult)
        assert result.platform == "github"

    @pytest.mark.asyncio
    async def test_discover_repositories_parses_github_response(self):
        """Test that discover_repositories correctly parses GitHub GraphQL response."""
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="dummy",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        # GraphQL node format (as returned by _parse_graphql_response)
        graphql_node = {
            "nameWithOwner": "owner/my-project",
            "description": "A test project",
            "isPrivate": True,
            "url": "https://github.com/owner/my-project",
            "sshUrl": "git@github.com:owner/my-project.git",
            "pushedAt": "2024-01-15T10:30:00Z",
            "defaultBranchRef": {"name": "main", "target": {"history": {"nodes": []}}},
        }

        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 1,
                        "nodes": [graphql_node],
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        assert len(result.repositories) == 1
        repo = result.repositories[0]
        assert repo.name == "owner/my-project"
        assert repo.description == "A test project"
        assert repo.default_branch == "main"
        assert repo.clone_url_https == "https://github.com/owner/my-project.git"
        assert repo.clone_url_ssh == "git@github.com:owner/my-project.git"
        assert repo.is_private is True

    @pytest.mark.asyncio
    async def test_discover_repositories_has_next_page_when_source_has_more(self):
        """Test cursor result: has_next_page=True and exact cursor when source has more pages."""
        import base64
        import json
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="dummy", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []
        provider = GitHubProvider(
            token_manager=token_manager, golden_repo_manager=golden_repo_manager
        )

        nodes = [
            {
                "nameWithOwner": f"owner/repo{i}",
                "name": f"repo{i}",
                "description": None,
                "isPrivate": False,
                "url": f"https://github.com/owner/repo{i}",
                "sshUrl": f"git@github.com:owner/repo{i}.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"history": {"nodes": []}},
                },
            }
            for i in range(2)
        ]
        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "abc123"},
                        "totalCount": 10,
                        "nodes": nodes,
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=2)

        expected_cursor = base64.b64encode(
            json.dumps(
                {
                    "v": 1,
                    "platform": "github",
                    "source": "abc123",
                    "skip": 0,
                    "mode": "graphql",
                }
            ).encode()
        ).decode()
        assert result.has_next_page is True
        assert result.next_cursor == expected_cursor
        assert result.page_size == 2


class TestGitHubProviderLinkHeaderParsing:
    """Tests for parsing GitHub's Link header for pagination."""

    def test_parse_link_header_extracts_last_page(self):
        """Test that Link header parsing correctly extracts last page number."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        link_header = (
            '<https://api.github.com/user/repos?page=1&per_page=30>; rel="prev", '
            '<https://api.github.com/user/repos?page=5&per_page=30>; rel="last"'
        )

        total_pages = provider._parse_link_header_for_last_page(link_header)
        assert total_pages == 5

    def test_parse_link_header_returns_1_when_no_last(self):
        """Test that Link header returns 1 when no last page."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        # Only prev, no last - means we're on the last page
        link_header = (
            '<https://api.github.com/user/repos?page=1&per_page=30>; rel="prev"'
        )

        total_pages = provider._parse_link_header_for_last_page(link_header)
        assert total_pages == 1

    def test_parse_link_header_handles_empty(self):
        """Test that empty Link header returns 1."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        total_pages = provider._parse_link_header_for_last_page("")
        assert total_pages == 1

        total_pages = provider._parse_link_header_for_last_page(None)
        assert total_pages == 1


class TestGitHubProviderExclusion:
    """Tests for GitHubProvider excluding already-indexed repositories."""

    @pytest.mark.asyncio
    async def test_excludes_already_indexed_repos_by_https_url(self):
        """Test that already-indexed repos are excluded using HTTPS URL matching."""
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="dummy",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        # This repo is already indexed
        golden_repo_manager.list_golden_repos.return_value = [
            {"repo_url": "https://github.com/owner/already-indexed.git"}
        ]

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        nodes = [
            {
                "nameWithOwner": "owner/already-indexed",
                "description": "Already in golden repos",
                "isPrivate": True,
                "url": "https://github.com/owner/already-indexed",
                "sshUrl": "git@github.com:owner/already-indexed.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"history": {"nodes": []}},
                },
            },
            {
                "nameWithOwner": "owner/new-project",
                "description": "Not yet indexed",
                "isPrivate": False,
                "url": "https://github.com/owner/new-project",
                "sshUrl": "git@github.com:owner/new-project.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"history": {"nodes": []}},
                },
            },
        ]
        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 2,
                        "nodes": nodes,
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        # Should only return the new project
        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/new-project"

    @pytest.mark.asyncio
    async def test_excludes_already_indexed_repos_by_ssh_url(self):
        """Test that already-indexed repos are excluded using SSH URL matching."""
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="dummy",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        # This repo is indexed via SSH URL
        golden_repo_manager.list_golden_repos.return_value = [
            {"repo_url": "git@github.com:owner/already-indexed.git"}
        ]

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        nodes = [
            {
                "nameWithOwner": "owner/already-indexed",
                "description": "Already in golden repos",
                "isPrivate": True,
                "url": "https://github.com/owner/already-indexed",
                "sshUrl": "git@github.com:owner/already-indexed.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"history": {"nodes": []}},
                },
            },
        ]
        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 1,
                        "nodes": nodes,
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        # Should be filtered out
        assert len(result.repositories) == 0

    @pytest.mark.asyncio
    async def test_cross_platform_no_false_positives(self):
        """Test that GitLab repo doesn't exclude GitHub repo with same name."""
        import httpx as _httpx
        from unittest.mock import patch as _patch
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="dummy",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        # GitLab repo with same name as GitHub repo
        golden_repo_manager.list_golden_repos.return_value = [
            {"repo_url": "https://gitlab.com/owner/my-project.git"}
        ]

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        nodes = [
            {
                "nameWithOwner": "owner/my-project",
                "description": "GitHub version",
                "isPrivate": False,
                "url": "https://github.com/owner/my-project",
                "sshUrl": "git@github.com:owner/my-project.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"history": {"nodes": []}},
                },
            },
        ]
        graphql_resp = MagicMock(spec=_httpx.Response)
        graphql_resp.status_code = 200
        graphql_resp.raise_for_status = MagicMock()
        graphql_resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 1,
                        "nodes": nodes,
                    }
                }
            }
        }

        with _patch("httpx.post", return_value=graphql_resp):
            result = provider.discover_repositories(cursor=None, page_size=50)

        # GitHub repo should NOT be excluded - different host
        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/my-project"


class TestGitHubProviderSortingOrder:
    """Tests for GitHubProvider sorting by last push descending."""

    def test_api_request_uses_pushed_sort_descending(self):
        """Test that the GraphQL request contains the correct sort order clause.

        discover_repositories is synchronous. The primary code path uses GraphQL.
        We mock httpx.post (the external HTTP boundary) to capture all outgoing
        HTTP payloads without making a real network call, then find the GraphQL
        payload (identified by the presence of a 'query' key) and assert it
        contains orderBy: {field: PUSHED_AT, direction: DESC}.
        """
        import httpx
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="fake-github-token",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        # Capture all outgoing httpx.post calls at the external HTTP boundary
        captured_payloads: list = []

        def fake_httpx_post(url, **kwargs):
            captured_payloads.append(kwargs.get("json", {}))
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "data": {
                    "viewer": {
                        "repositories": {
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                            "totalCount": 0,
                            "nodes": [],
                        }
                    }
                }
            }
            return mock_response

        with patch("httpx.post", side_effect=fake_httpx_post):
            provider.discover_repositories(cursor=None, page_size=50)

        # Find the GraphQL payload (identified by the presence of a 'query' key)
        graphql_payloads = [p for p in captured_payloads if "query" in p]
        assert len(graphql_payloads) >= 1, (
            f"Expected at least one httpx.post call with a 'query' key; "
            f"captured payloads: {captured_payloads}"
        )

        graphql_query = graphql_payloads[0]["query"]
        assert "orderBy: {field: PUSHED_AT, direction: DESC}" in graphql_query, (
            f"Expected 'orderBy: {{field: PUSHED_AT, direction: DESC}}' in GraphQL query, "
            f"got:\n{graphql_query}"
        )


class TestGitHubProviderErrorHandling:
    """Tests for GitHubProvider error handling."""

    # Named constant for the rate limit reset Unix epoch used in rate-limit tests.
    # Value represents 2024-01-01 00:00:00 UTC — a fixed, symbolic test timestamp.
    _RATE_LIMIT_RESET_EPOCH = "1704067200"

    def _make_provider(self):
        """Create a configured GitHubProvider with a fake token for error-path tests."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github",
            token="fake-github-token",
            base_url=None,
        )
        golden_repo_manager = MagicMock()
        return GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

    @pytest.mark.asyncio
    async def test_raises_error_when_not_configured(self):
        """Test that discover_repositories raises error when token not configured."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
            GitHubProviderError,
        )

        token_manager = MagicMock()
        token_manager.get_token.return_value = None
        golden_repo_manager = MagicMock()

        provider = GitHubProvider(
            token_manager=token_manager,
            golden_repo_manager=golden_repo_manager,
        )

        with pytest.raises(GitHubProviderError) as exc_info:
            provider.discover_repositories(cursor=None, page_size=50)

        assert "not configured" in str(exc_info.value).lower()

    def test_handles_api_error(self):
        """Test that provider handles GitHub API errors gracefully.

        The primary code path makes a GraphQL request via httpx.post.
        We mock httpx.post at the external HTTP boundary to simulate a 401.
        """
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProviderError,
        )

        provider = self._make_provider()

        mock_request = MagicMock()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=mock_request, response=mock_response
        )

        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(GitHubProviderError) as exc_info:
                provider.discover_repositories(cursor=None, page_size=50)

        error_msg = str(exc_info.value).lower()
        assert "api" in error_msg or "error" in error_msg or "unauthorized" in error_msg

    def test_handles_timeout(self):
        """Test that provider handles request timeout.

        The primary code path makes a GraphQL request via httpx.post.
        We mock httpx.post at the external HTTP boundary to raise a timeout.
        """
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProviderError,
        )

        provider = self._make_provider()

        with patch(
            "httpx.post", side_effect=httpx.TimeoutException("Connection timed out")
        ):
            with pytest.raises(GitHubProviderError) as exc_info:
                provider.discover_repositories(cursor=None, page_size=50)

        assert "timed out" in str(exc_info.value).lower()

    def test_handles_rate_limit(self):
        """Test that provider handles GitHub rate limit response.

        The primary code path makes a GraphQL request via httpx.post.
        We mock httpx.post at the external HTTP boundary to simulate a 403 rate limit.
        """
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProviderError,
        )

        provider = self._make_provider()

        mock_request = MagicMock()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.headers = {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": self._RATE_LIMIT_RESET_EPOCH,
        }
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limit exceeded", request=mock_request, response=mock_response
        )

        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(GitHubProviderError) as exc_info:
                provider.discover_repositories(cursor=None, page_size=50)

        error_msg = str(exc_info.value).lower()
        assert "rate limit" in error_msg or "api" in error_msg or "error" in error_msg


class TestGitHubProviderServerSideSearch:
    """Tests for GitHub server-side search functionality (Story #16)."""

    def _create_mock_response(self, json_data, status_code=200, headers=None):
        """Helper to create mock HTTP response."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = headers or {}
        mock_response.json.return_value = json_data
        mock_response.raise_for_status = MagicMock()
        return mock_response

    @pytest.mark.asyncio
    async def test_search_uses_search_repositories_endpoint(self):
        """Test that search uses /search/repositories endpoint."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="ghp_test123", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(token_manager, golden_repo_manager)
        captured_endpoint = None
        captured_params = {}  # type: ignore[var-annotated]

        def capture_request(endpoint, params=None):
            nonlocal captured_endpoint, captured_params
            captured_endpoint = endpoint
            captured_params = params or {}
            return self._create_mock_response(
                {"total_count": 0, "incomplete_results": False, "items": []}
            )

        with patch.object(provider, "_make_api_request", side_effect=capture_request):
            provider.discover_repositories(
                cursor=None, page_size=50, search="myproject"
            )

        assert captured_endpoint == "search/repositories"
        assert "q" in captured_params
        assert "myproject" in captured_params["q"]

    @pytest.mark.asyncio
    async def test_search_response_parsing_from_items_array(self):
        """Test that search response parses repos from 'items' array."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="ghp_test123", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(token_manager, golden_repo_manager)
        search_response = {
            "total_count": 1,
            "items": [
                {
                    "full_name": "owner/myproject",
                    "description": "Test",
                    "clone_url": "https://github.com/owner/myproject.git",
                    "ssh_url": "git@github.com:owner/myproject.git",
                    "default_branch": "main",
                    "pushed_at": "2024-01-15T10:30:00Z",
                    "private": False,
                }
            ],
        }

        with patch.object(
            provider,
            "_make_api_request",
            return_value=self._create_mock_response(search_response),
        ):
            result = provider.discover_repositories(
                cursor=None, page_size=50, search="myproject"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/myproject"

    @pytest.mark.asyncio
    async def test_search_pagination_from_response_body(self):
        """Test that search propagates total_count from API response as source_total."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="ghp_test123", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(token_manager, golden_repo_manager)
        search_response = {"total_count": 150, "incomplete_results": False, "items": []}

        with patch.object(
            provider,
            "_make_api_request",
            return_value=self._create_mock_response(search_response),
        ):
            result = provider.discover_repositories(
                cursor=None, page_size=50, search="myproject"
            )

        assert result.source_total == 150
        assert result.has_next_page is False  # no items returned, source exhausted

    @pytest.mark.asyncio
    async def test_no_search_uses_user_repos_endpoint(self):
        """Test that without search, /user/repos endpoint is used (via REST fallback).

        The provider tries GraphQL first. A network-level failure (ConnectError)
        triggers the GraphQL-to-REST fallback, so user/repos gets called.
        """
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="ghp_test123", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(token_manager, golden_repo_manager)
        captured_endpoint = None

        def capture_request(endpoint, params=None):
            nonlocal captured_endpoint
            captured_endpoint = endpoint
            return self._create_mock_response([])

        with patch(
            "httpx.post", side_effect=httpx.ConnectError("simulated graphql failure")
        ):
            with patch.object(
                provider, "_make_api_request", side_effect=capture_request
            ):
                provider.discover_repositories(cursor=None, page_size=50, search=None)

        assert captured_endpoint == "user/repos"

    @pytest.mark.asyncio
    async def test_search_with_indexed_repo_exclusion(self):
        """Test that indexed repos are excluded when search is provided."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token="ghp_test123", base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = [
            {"repo_url": "https://github.com/owner/data-indexed.git"}
        ]

        provider = GitHubProvider(token_manager, golden_repo_manager)
        search_response = {
            "total_count": 2,
            "items": [
                {
                    "full_name": "owner/data-indexed",
                    "clone_url": "https://github.com/owner/data-indexed.git",
                    "ssh_url": "git@github.com:owner/data-indexed.git",
                    "default_branch": "main",
                    "private": True,
                },
                {
                    "full_name": "owner/data-services",
                    "clone_url": "https://github.com/owner/data-services.git",
                    "ssh_url": "git@github.com:owner/data-services.git",
                    "default_branch": "main",
                    "private": False,
                },
            ],
        }

        with patch.object(
            provider,
            "_make_api_request",
            return_value=self._create_mock_response(search_response),
        ):
            result = provider.discover_repositories(
                cursor=None, page_size=50, search="data"
            )

        assert len(result.repositories) == 1
        assert result.repositories[0].name == "owner/data-services"

    @pytest.mark.asyncio
    async def test_empty_search_string_uses_regular_endpoint(self):
        """Test that empty search string uses /user/repos endpoint (via REST fallback)."""
        import httpx
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.services.ci_token_manager import TokenData

        FAKE_TOKEN_FOR_TESTS = "test-token-not-real"
        token_manager = MagicMock()
        token_manager.get_token.return_value = TokenData(
            platform="github", token=FAKE_TOKEN_FOR_TESTS, base_url=None
        )
        golden_repo_manager = MagicMock()
        golden_repo_manager.list_golden_repos.return_value = []

        provider = GitHubProvider(token_manager, golden_repo_manager)
        captured_url = None

        def capture_get(url, **kwargs):
            nonlocal captured_url
            captured_url = url
            mock_resp = MagicMock(spec=httpx.Response)
            mock_resp.status_code = 200
            mock_resp.json.return_value = []
            mock_resp.headers = {}
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        # httpx.post → GraphQL fails; httpx.get → REST captures URL
        with (
            patch("httpx.post", side_effect=httpx.ConnectError("forced graphql fail")),
            patch("httpx.get", side_effect=capture_get),
        ):
            provider.discover_repositories(cursor=None, page_size=50, search="")

        assert captured_url is not None, "REST endpoint was never called"
        assert "/user/repos" in captured_url, f"Expected /user/repos in {captured_url}"
