"""
Unit tests for GitLab Provider GraphQL enrichment functionality.

Tests the enrichment of repository listings with commit information
via GraphQL batch queries.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock
import httpx

from src.code_indexer.server.services.repository_providers.gitlab_provider import (
    GitLabProvider,
)
from src.code_indexer.server.models.auto_discovery import DiscoveredRepository


@pytest.fixture
def mock_token_manager():
    """Create a mock CI token manager."""
    manager = Mock()
    manager.get_token.return_value = Mock(
        token="test-token",
        base_url="https://gitlab.com",
    )
    return manager


@pytest.fixture
def mock_golden_repo_manager():
    """Create a mock golden repo manager."""
    manager = Mock()
    manager.list_golden_repos.return_value = []
    return manager


@pytest.fixture
def gitlab_provider(mock_token_manager, mock_golden_repo_manager):
    """Create a GitLab provider instance for testing."""
    return GitLabProvider(mock_token_manager, mock_golden_repo_manager)


class TestEnrichRepositoriesWithCommits:
    """Tests for _enrich_repositories_with_commits method."""

    def test_enriches_single_repository(self, gitlab_provider, monkeypatch):
        """Should enrich repository with commit info from GraphQL."""
        repos = [
            DiscoveredRepository(
                platform="gitlab",
                name="group/repo1",
                description="Test repo",
                clone_url_https="https://gitlab.com/group/repo1.git",
                clone_url_ssh="git@gitlab.com:group/repo1.git",
                default_branch="main",
                last_activity=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                is_private=False,
            )
        ]

        # Mock GraphQL request/response
        def mock_make_graphql_request(query, variables=None):
            response = Mock()
            response.status_code = 200
            response.raise_for_status = Mock()
            response.json.return_value = {
                "data": {
                    "project0": {
                        "repository": {
                            "tree": {
                                "lastCommit": {
                                    "sha": "abc123def",
                                    "author": {"name": "Alice"},
                                    "committedDate": "2024-01-15T10:00:00Z",
                                }
                            }
                        }
                    }
                }
            }
            return response

        monkeypatch.setattr(
            gitlab_provider, "_make_graphql_request", mock_make_graphql_request
        )

        enriched = gitlab_provider._enrich_repositories_with_commits(repos)

        assert len(enriched) == 1
        assert enriched[0].last_commit_hash == "abc123d"
        assert enriched[0].last_commit_author == "Alice"
        assert enriched[0].last_commit_date == datetime(
            2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc
        )

    def test_enriches_multiple_repositories(self, gitlab_provider, monkeypatch):
        """Should enrich multiple repositories with commit info."""
        repos = [
            DiscoveredRepository(
                platform="gitlab",
                name="group/repo1",
                description="Repo 1",
                clone_url_https="https://gitlab.com/group/repo1.git",
                clone_url_ssh="git@gitlab.com:group/repo1.git",
                default_branch="main",
                last_activity=datetime(2024, 1, 15, tzinfo=timezone.utc),
                is_private=False,
            ),
            DiscoveredRepository(
                platform="gitlab",
                name="group/repo2",
                description="Repo 2",
                clone_url_https="https://gitlab.com/group/repo2.git",
                clone_url_ssh="git@gitlab.com:group/repo2.git",
                default_branch="main",
                last_activity=datetime(2024, 1, 16, tzinfo=timezone.utc),
                is_private=False,
            ),
        ]

        def mock_make_graphql_request(query, variables=None):
            response = Mock()
            response.status_code = 200
            response.raise_for_status = Mock()
            response.json.return_value = {
                "data": {
                    "project0": {
                        "repository": {
                            "tree": {
                                "lastCommit": {
                                    "sha": "abc123",
                                    "author": {"name": "Alice"},
                                    "committedDate": "2024-01-15T10:00:00Z",
                                }
                            }
                        }
                    },
                    "project1": {
                        "repository": {
                            "tree": {
                                "lastCommit": {
                                    "sha": "def456",
                                    "author": {"name": "Bob"},
                                    "committedDate": "2024-01-16T11:00:00Z",
                                }
                            }
                        }
                    },
                }
            }
            return response

        monkeypatch.setattr(
            gitlab_provider, "_make_graphql_request", mock_make_graphql_request
        )

        enriched = gitlab_provider._enrich_repositories_with_commits(repos)

        assert len(enriched) == 2
        assert enriched[0].last_commit_hash == "abc123"[:7]
        assert enriched[1].last_commit_hash == "def456"[:7]

    def test_handles_batching_for_large_repo_lists(self, gitlab_provider, monkeypatch):
        """Should batch GraphQL requests for >10 repositories (GitLab query limit)."""
        # Create 25 repositories (should require 3 batches with batch size 10)
        repos = [
            DiscoveredRepository(
                platform="gitlab",
                name=f"group/repo{i}",
                description=f"Repo {i}",
                clone_url_https=f"https://gitlab.com/group/repo{i}.git",
                clone_url_ssh=f"git@gitlab.com:group/repo{i}.git",
                default_branch="main",
                last_activity=datetime(2024, 1, 15, tzinfo=timezone.utc),
                is_private=False,
            )
            for i in range(25)
        ]

        graphql_call_count = 0
        batch_size = 10  # Match production BATCH_SIZE

        def mock_make_graphql_request(query, variables=None):
            nonlocal graphql_call_count
            graphql_call_count += 1

            response = Mock()
            response.raise_for_status = Mock()
            response.status_code = 200

            # Return data for batch
            data = {}
            start_idx = (graphql_call_count - 1) * batch_size
            end_idx = min(start_idx + batch_size, 25)

            for i in range(start_idx, end_idx):
                alias_idx = i - start_idx
                data[f"project{alias_idx}"] = {
                    "repository": {
                        "tree": {
                            "lastCommit": {
                                "sha": f"commit{i}",
                                "author": {"name": f"Author{i}"},
                                "committedDate": "2024-01-15T10:00:00Z",
                            }
                        }
                    }
                }

            response.json.return_value = {"data": data}
            return response

        monkeypatch.setattr(
            gitlab_provider, "_make_graphql_request", mock_make_graphql_request
        )

        enriched = gitlab_provider._enrich_repositories_with_commits(repos)

        # Verify 3 batches were made (10 + 10 + 5)
        assert graphql_call_count == 3
        assert len(enriched) == 25
        # Verify all repos enriched
        assert all(repo.last_commit_hash is not None for repo in enriched)

    def test_graceful_degradation_on_graphql_error(
        self, gitlab_provider, monkeypatch, caplog
    ):
        """Should continue with N/A values on GraphQL failure."""
        repos = [
            DiscoveredRepository(
                platform="gitlab",
                name="group/repo1",
                description="Test repo",
                clone_url_https="https://gitlab.com/group/repo1.git",
                clone_url_ssh="git@gitlab.com:group/repo1.git",
                default_branch="main",
                last_activity=datetime(2024, 1, 15, tzinfo=timezone.utc),
                is_private=False,
            )
        ]

        def mock_make_graphql_request(query, variables=None):
            raise httpx.RequestError("Network error")

        monkeypatch.setattr(
            gitlab_provider, "_make_graphql_request", mock_make_graphql_request
        )

        # Should not raise, but log warning
        enriched = gitlab_provider._enrich_repositories_with_commits(repos)

        assert len(enriched) == 1
        # Commit info should remain None (not set)
        assert enriched[0].last_commit_hash is None
        assert enriched[0].last_commit_author is None
        assert enriched[0].last_commit_date is None

        # Verify warning was logged
        assert any(
            "Failed to enrich repositories with commit info" in record.message
            for record in caplog.records
        )

    def test_returns_empty_list_for_empty_input(self, gitlab_provider):
        """Should return empty list when given empty repository list."""
        enriched = gitlab_provider._enrich_repositories_with_commits([])
        assert enriched == []


class TestDiscoverRepositoriesIntegration:
    """Integration tests for discover_repositories with GraphQL enrichment."""

    def test_discover_enriches_repositories_with_commits(
        self, gitlab_provider, monkeypatch
    ):
        """Should enrich discovered repositories with commit info via GraphQL."""
        # Mock REST API response
        def mock_make_api_request(endpoint, params=None):
            response = Mock()
            response.raise_for_status = Mock()
            response.headers = {
                "x-total": "1",
                "x-total-pages": "1",
            }
            response.json.return_value = [
                {
                    "path_with_namespace": "group/repo1",
                    "description": "Test repo",
                    "http_url_to_repo": "https://gitlab.com/group/repo1.git",
                    "ssh_url_to_repo": "git@gitlab.com:group/repo1.git",
                    "default_branch": "main",
                    "last_activity_at": "2024-01-15T10:00:00Z",
                    "visibility": "public",
                }
            ]
            return response

        # Mock GraphQL enrichment
        def mock_make_graphql_request(query, variables=None):
            response = Mock()
            response.status_code = 200
            response.raise_for_status = Mock()
            response.json.return_value = {
                "data": {
                    "project0": {
                        "repository": {
                            "tree": {
                                "lastCommit": {
                                    "sha": "abc123def",
                                    "author": {"name": "Alice"},
                                    "committedDate": "2024-01-15T10:00:00Z",
                                }
                            }
                        }
                    }
                }
            }
            return response

        monkeypatch.setattr(gitlab_provider, "_make_api_request", mock_make_api_request)
        monkeypatch.setattr(
            gitlab_provider, "_make_graphql_request", mock_make_graphql_request
        )

        result = gitlab_provider.discover_repositories(page=1, page_size=50)

        assert len(result.repositories) == 1
        repo = result.repositories[0]
        assert repo.name == "group/repo1"
        assert repo.last_commit_hash == "abc123d"
        assert repo.last_commit_author == "Alice"
        assert isinstance(repo.last_commit_date, datetime)
