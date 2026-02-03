"""
Tests for GitHub GraphQL Integration (Story #80).

Following TDD methodology - these tests are written FIRST before implementation.
Tests define expected behavior for GitHub GraphQL commit info retrieval.

Acceptance Criteria Covered:
- AC1: Non-Search Discovery Shows Commit Info via GraphQL
- AC2: Search Discovery Shows Commit Info via REST + GraphQL Enrichment
- AC3: Graceful Degradation for Missing Commit Info
- AC4: Existing Functionality Preserved
"""

import pytest
from unittest.mock import MagicMock, patch


class TestGitHubGraphQLNonSearchDiscovery:
    """Tests for AC1: Non-search discovery using GraphQL."""

    def _create_graphql_response(self, repos_data):
        """Helper to create mock GraphQL response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": len(repos_data),
                        "nodes": repos_data,
                    }
                }
            }
        }
        mock_response.raise_for_status = MagicMock()
        return mock_response

    @pytest.mark.asyncio
    async def test_non_search_uses_graphql_request(self):
        """Test that non-search mode makes GraphQL request instead of REST."""
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

        # Mock GraphQL response with commit info
        graphql_repos = [
            {
                "name": "test-repo",
                "nameWithOwner": "owner/test-repo",
                "description": "Test repository",
                "isPrivate": False,
                "url": "https://github.com/owner/test-repo",
                "sshUrl": "git@github.com:owner/test-repo.git",
                "pushedAt": "2024-01-15T10:30:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "abc123def456",
                                    "author": {"name": "John Doe"},
                                    "committedDate": "2024-01-15T10:30:00Z",
                                }
                            ]
                        }
                    },
                },
            }
        ]

        graphql_response = self._create_graphql_response(graphql_repos)

        with patch.object(
            provider, "_make_graphql_request", return_value=graphql_response
        ) as mock_graphql:
            result = provider.discover_repositories(page=1, page_size=50, search=None)

            # Verify GraphQL was called
            assert mock_graphql.called
            # Verify result contains commit info
            assert len(result.repositories) == 1
            repo = result.repositories[0]
            assert repo.last_commit_hash == "abc123d"  # 7 chars
            assert repo.last_commit_author == "John Doe"
            assert repo.last_commit_date is not None

    @pytest.mark.asyncio
    async def test_graphql_query_includes_commit_fields(self):
        """Test that GraphQL query includes commit history fields."""
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

        # Mock _build_graphql_query to capture query structure
        query_built = provider._build_graphql_query(first=50, after=None)

        # Verify query contains necessary fields
        assert "defaultBranchRef" in query_built
        assert "history(first: 1)" in query_built or "history(first:1)" in query_built
        assert "oid" in query_built
        assert "author" in query_built
        assert "committedDate" in query_built

    @pytest.mark.asyncio
    async def test_graphql_query_orders_by_pushed_at_desc(self):
        """Test that GraphQL query orders by PUSHED_AT descending."""
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

        query_built = provider._build_graphql_query(first=50, after=None)

        # Verify ordering
        assert "orderBy:" in query_built or "orderBy :" in query_built
        assert "PUSHED_AT" in query_built
        assert "DESC" in query_built

    @pytest.mark.asyncio
    async def test_graphql_query_includes_owner_affiliations(self):
        """Test that GraphQL query includes ownerAffiliations for org repos.

        GitHub GraphQL API requires BOTH affiliations AND ownerAffiliations
        to properly return repositories from private organizations.
        See: https://github.com/orgs/community/discussions/24860
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

        query_built = provider._build_graphql_query(first=50, after=None)

        # Verify both affiliations and ownerAffiliations are present
        assert "affiliations:" in query_built or "affiliations :" in query_built
        assert (
            "ownerAffiliations:" in query_built or "ownerAffiliations :" in query_built
        )
        # Verify ORGANIZATION_MEMBER is in both
        assert "ORGANIZATION_MEMBER" in query_built

    @pytest.mark.asyncio
    async def test_parse_graphql_response_extracts_commit_info(self):
        """Test _parse_graphql_response correctly extracts commit data."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(token_manager, golden_repo_manager)

        graphql_node = {
            "name": "my-repo",
            "nameWithOwner": "org/my-repo",
            "description": "Test",
            "isPrivate": True,
            "url": "https://github.com/org/my-repo",
            "sshUrl": "git@github.com:org/my-repo.git",
            "pushedAt": "2024-02-10T14:22:00Z",
            "defaultBranchRef": {
                "name": "main",
                "target": {
                    "history": {
                        "nodes": [
                            {
                                "oid": "1234567890abcdef",
                                "author": {"name": "Jane Smith"},
                                "committedDate": "2024-02-10T14:22:00Z",
                            }
                        ]
                    }
                },
            },
        }

        repo = provider._parse_graphql_response(graphql_node)

        assert repo.name == "org/my-repo"
        assert repo.last_commit_hash == "1234567"
        assert repo.last_commit_author == "Jane Smith"
        assert repo.last_commit_date is not None
        assert repo.last_commit_date.year == 2024
        assert repo.last_commit_date.month == 2
        assert repo.last_commit_date.day == 10


class TestGitHubGraphQLSearchModeEnrichment:
    """Tests for AC2: Search mode with REST + GraphQL enrichment."""

    def _create_rest_search_response(self, repos):
        """Helper to create mock REST search response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {
            "total_count": len(repos),
            "incomplete_results": False,
            "items": repos,
        }
        mock_response.raise_for_status = MagicMock()
        return mock_response

    def _create_graphql_enrichment_response(self, repos_data):
        """Helper to create GraphQL enrichment response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        data = {}
        for idx, repo_data in enumerate(repos_data):
            data[f"r{idx}"] = repo_data
        mock_response.json.return_value = {"data": data}
        mock_response.raise_for_status = MagicMock()
        return mock_response

    @pytest.mark.asyncio
    async def test_search_mode_uses_rest_then_enriches_with_graphql(self):
        """Test that search mode uses REST API then enriches with GraphQL."""
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

        # Mock REST search response
        rest_repos = [
            {
                "full_name": "owner/search-result",
                "description": "Search result",
                "clone_url": "https://github.com/owner/search-result.git",
                "ssh_url": "git@github.com:owner/search-result.git",
                "default_branch": "main",
                "pushed_at": "2024-01-20T12:00:00Z",
                "private": False,
            }
        ]

        # Mock GraphQL enrichment response
        graphql_enrichment = [
            {
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "fedcba987654321",
                                    "author": {"name": "Search Author"},
                                    "committedDate": "2024-01-20T12:00:00Z",
                                }
                            ]
                        }
                    }
                }
            }
        ]

        rest_response = self._create_rest_search_response(rest_repos)
        graphql_response = self._create_graphql_enrichment_response(graphql_enrichment)

        with (
            patch.object(
                provider, "_make_api_request", return_value=rest_response
            ) as mock_rest,
            patch.object(
                provider, "_make_graphql_request", return_value=graphql_response
            ) as mock_graphql,
        ):
            result = provider.discover_repositories(page=1, page_size=50, search="test")

            # Verify both REST and GraphQL were called
            assert mock_rest.called
            assert mock_graphql.called

            # Verify result has commit info
            assert len(result.repositories) == 1
            repo = result.repositories[0]
            assert repo.last_commit_hash == "fedcba9"
            assert repo.last_commit_author == "Search Author"

    @pytest.mark.asyncio
    async def test_enrich_with_commits_builds_batch_graphql_query(self):
        """Test _enrich_with_commits_graphql builds batch query for multiple repos."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )
        from code_indexer.server.models.auto_discovery import DiscoveredRepository

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(token_manager, golden_repo_manager)

        repos = [
            DiscoveredRepository(
                platform="github",
                name="owner/repo1",
                clone_url_https="https://github.com/owner/repo1.git",
                clone_url_ssh="git@github.com:owner/repo1.git",
                default_branch="main",
                is_private=False,
            ),
            DiscoveredRepository(
                platform="github",
                name="owner/repo2",
                clone_url_https="https://github.com/owner/repo2.git",
                clone_url_ssh="git@github.com:owner/repo2.git",
                default_branch="main",
                is_private=False,
            ),
        ]

        # Mock GraphQL response
        graphql_enrichment = [
            {
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "abc123",
                                    "author": {"name": "Author 1"},
                                    "committedDate": "2024-01-15T10:00:00Z",
                                }
                            ]
                        }
                    }
                }
            },
            {
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "def456",
                                    "author": {"name": "Author 2"},
                                    "committedDate": "2024-01-16T11:00:00Z",
                                }
                            ]
                        }
                    }
                }
            },
        ]

        graphql_response = self._create_graphql_enrichment_response(graphql_enrichment)

        with patch.object(
            provider, "_make_graphql_request", return_value=graphql_response
        ):
            enriched = provider._enrich_with_commits_graphql(repos)

            assert len(enriched) == 2
            assert enriched[0].last_commit_hash == "abc123"[:7]
            assert enriched[0].last_commit_author == "Author 1"
            assert enriched[1].last_commit_hash == "def456"[:7]
            assert enriched[1].last_commit_author == "Author 2"


class TestGitHubGraphQLGracefulDegradation:
    """Tests for AC3: Graceful degradation for missing commit info."""

    @pytest.mark.asyncio
    async def test_empty_repo_without_commits_shows_na(self):
        """Test that repos without commits display N/A for commit fields."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(token_manager, golden_repo_manager)

        # GraphQL node without commits (empty repo)
        graphql_node = {
            "name": "empty-repo",
            "nameWithOwner": "owner/empty-repo",
            "description": "Empty repository",
            "isPrivate": False,
            "url": "https://github.com/owner/empty-repo",
            "sshUrl": "git@github.com:owner/empty-repo.git",
            "pushedAt": None,
            "defaultBranchRef": None,  # No default branch yet
        }

        repo = provider._parse_graphql_response(graphql_node)

        # Should return None values that template renders as "N/A"
        assert repo.last_commit_hash is None
        assert repo.last_commit_author is None
        assert repo.last_commit_date is None

    @pytest.mark.asyncio
    async def test_graphql_error_returns_repos_without_commit_info(self):
        """Test that GraphQL errors log warning and fall back to REST API."""
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

        # Mock REST API fallback response
        rest_repos = [
            {
                "full_name": "owner/fallback-repo",
                "description": "Fallback repository",
                "clone_url": "https://github.com/owner/fallback-repo.git",
                "ssh_url": "git@github.com:owner/fallback-repo.git",
                "default_branch": "main",
                "pushed_at": "2024-01-15T10:00:00Z",
                "private": False,
            }
        ]

        mock_rest_response = MagicMock()
        mock_rest_response.status_code = 200
        mock_rest_response.headers = {}
        mock_rest_response.json.return_value = rest_repos
        mock_rest_response.raise_for_status = MagicMock()

        # Simulate GraphQL request failure, REST API success
        with (
            patch.object(
                provider,
                "_make_graphql_request",
                side_effect=Exception("GraphQL error"),
            ),
            patch.object(
                provider, "_make_api_request", return_value=mock_rest_response
            ),
        ):
            # Should not raise, should log warning and fall back to REST
            result = provider.discover_repositories(page=1, page_size=50, search=None)

            # Result should be valid with repositories but without commit info
            assert result is not None
            assert len(result.repositories) == 1
            assert result.repositories[0].name == "owner/fallback-repo"
            # Commit info should be None (REST API doesn't provide it)
            assert result.repositories[0].last_commit_hash is None
            assert result.repositories[0].last_commit_author is None
            assert result.repositories[0].last_commit_date is None

    @pytest.mark.asyncio
    async def test_partial_commit_data_handles_missing_author(self):
        """Test handling of commit with missing author field."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        token_manager = MagicMock()
        golden_repo_manager = MagicMock()
        provider = GitHubProvider(token_manager, golden_repo_manager)

        graphql_node = {
            "name": "partial-repo",
            "nameWithOwner": "owner/partial-repo",
            "description": "Partial data",
            "isPrivate": False,
            "url": "https://github.com/owner/partial-repo",
            "sshUrl": "git@github.com:owner/partial-repo.git",
            "pushedAt": "2024-01-15T10:00:00Z",
            "defaultBranchRef": {
                "name": "main",
                "target": {
                    "history": {
                        "nodes": [
                            {
                                "oid": "abc123",
                                "author": None,  # Missing author
                                "committedDate": "2024-01-15T10:00:00Z",
                            }
                        ]
                    }
                },
            },
        }

        repo = provider._parse_graphql_response(graphql_node)

        # Should have commit hash and date but no author
        assert repo.last_commit_hash == "abc123"[:7]
        assert repo.last_commit_author is None
        assert repo.last_commit_date is not None


class TestGitHubGraphQLExistingFunctionality:
    """Tests for AC4: Existing functionality preserved."""

    @pytest.mark.asyncio
    async def test_pagination_still_works(self):
        """Test that pagination is preserved in GraphQL mode."""
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

        # Mock GraphQL response with pagination
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor123",
                        },
                        "totalCount": 150,
                        "nodes": [],
                    }
                }
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            provider, "_make_graphql_request", return_value=mock_response
        ):
            result = provider.discover_repositories(page=2, page_size=50)

            assert result.page == 2
            assert result.page_size == 50
            # Total pages should be calculated from totalCount
            assert result.total_pages == 3  # ceil(150/50)

    @pytest.mark.asyncio
    async def test_indexed_repo_exclusion_still_works(self):
        """Test that already-indexed repos are still excluded."""
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
            {"repo_url": "https://github.com/owner/indexed-repo.git"}
        ]

        provider = GitHubProvider(token_manager, golden_repo_manager)

        graphql_repos = [
            {
                "name": "indexed-repo",
                "nameWithOwner": "owner/indexed-repo",
                "description": "Already indexed",
                "isPrivate": False,
                "url": "https://github.com/owner/indexed-repo",
                "sshUrl": "git@github.com:owner/indexed-repo.git",
                "pushedAt": "2024-01-15T10:00:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "abc123",
                                    "author": {"name": "Author"},
                                    "committedDate": "2024-01-15T10:00:00Z",
                                }
                            ]
                        }
                    },
                },
            },
            {
                "name": "new-repo",
                "nameWithOwner": "owner/new-repo",
                "description": "Not indexed",
                "isPrivate": False,
                "url": "https://github.com/owner/new-repo",
                "sshUrl": "git@github.com:owner/new-repo.git",
                "pushedAt": "2024-01-16T10:00:00Z",
                "defaultBranchRef": {
                    "name": "main",
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "oid": "def456",
                                    "author": {"name": "Author 2"},
                                    "committedDate": "2024-01-16T10:00:00Z",
                                }
                            ]
                        }
                    },
                },
            },
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 2,
                        "nodes": graphql_repos,
                    }
                }
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            provider, "_make_graphql_request", return_value=mock_response
        ):
            result = provider.discover_repositories(page=1, page_size=50)

            # Should only return new-repo
            assert len(result.repositories) == 1
            assert result.repositories[0].name == "owner/new-repo"
