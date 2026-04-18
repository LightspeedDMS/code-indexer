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

from unittest.mock import MagicMock

_FAKE_GITHUB_TOKEN = "fake-github-token"


def _make_provider():
    """Return a GitHubProvider with a fake token and no indexed repos."""
    from code_indexer.server.services.repository_providers.github_provider import (
        GitHubProvider,
    )
    from code_indexer.server.services.ci_token_manager import TokenData

    token_manager = MagicMock()
    token_manager.get_token.return_value = TokenData(
        platform="github", token=_FAKE_GITHUB_TOKEN, base_url=None
    )
    golden_repo_manager = MagicMock()
    golden_repo_manager.list_golden_repos.return_value = []
    return GitHubProvider(token_manager, golden_repo_manager)


class TestGitHubGraphQLNonSearchDiscovery:
    """Tests for AC1: Non-search discovery using GraphQL."""

    def test_graphql_query_includes_commit_fields(self):
        """Test that GraphQL query includes commit history fields."""
        provider = _make_provider()
        query_built = provider._build_graphql_query(first=50, after=None)

        assert "defaultBranchRef" in query_built
        assert "history(first: 1)" in query_built or "history(first:1)" in query_built
        assert "oid" in query_built
        assert "author" in query_built
        assert "committedDate" in query_built

    def test_graphql_query_orders_by_pushed_at_desc(self):
        """Test that GraphQL query orders by PUSHED_AT descending."""
        provider = _make_provider()
        query_built = provider._build_graphql_query(first=50, after=None)

        assert "orderBy:" in query_built or "orderBy :" in query_built
        assert "PUSHED_AT" in query_built
        assert "DESC" in query_built

    def test_graphql_query_includes_owner_affiliations(self):
        """Test that GraphQL query includes ownerAffiliations for org repos.

        GitHub GraphQL API requires BOTH affiliations AND ownerAffiliations
        to properly return repositories from private organizations.
        See: https://github.com/orgs/community/discussions/24860
        """
        provider = _make_provider()
        query_built = provider._build_graphql_query(first=50, after=None)

        assert "affiliations:" in query_built or "affiliations :" in query_built
        assert (
            "ownerAffiliations:" in query_built or "ownerAffiliations :" in query_built
        )
        assert "ORGANIZATION_MEMBER" in query_built

    def test_parse_graphql_response_extracts_commit_info(self):
        """Test _parse_graphql_response correctly extracts commit data."""
        provider = _make_provider()

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


class TestGitHubGraphQLGracefulDegradation:
    """Tests for AC3: Graceful degradation for missing commit info."""

    def test_empty_repo_without_commits_shows_na(self):
        """Test that repos without commits display N/A for commit fields."""
        provider = _make_provider()

        graphql_node = {
            "name": "empty-repo",
            "nameWithOwner": "owner/empty-repo",
            "description": "Empty repository",
            "isPrivate": False,
            "url": "https://github.com/owner/empty-repo",
            "sshUrl": "git@github.com:owner/empty-repo.git",
            "pushedAt": None,
            "defaultBranchRef": None,
        }

        repo = provider._parse_graphql_response(graphql_node)

        assert repo.last_commit_hash is None
        assert repo.last_commit_author is None
        assert repo.last_commit_date is None

    def test_partial_commit_data_handles_missing_author(self):
        """Test handling of commit with missing author field."""
        provider = _make_provider()

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
                                "author": None,
                                "committedDate": "2024-01-15T10:00:00Z",
                            }
                        ]
                    }
                },
            },
        }

        repo = provider._parse_graphql_response(graphql_node)

        assert repo.last_commit_hash == "abc123"[:7]
        assert repo.last_commit_author is None
        assert repo.last_commit_date is not None
