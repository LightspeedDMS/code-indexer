"""
Unit tests for GitLab Provider GraphQL multiplex query functionality.

Tests the GraphQL multiplex pattern for building queries and parsing responses.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock

from src.code_indexer.server.services.repository_providers.gitlab_provider import (
    GitLabProvider,
)


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


class TestBuildMultiplexQuery:
    """Tests for _build_multiplex_query method."""

    def test_builds_single_project_query(self, gitlab_provider):
        """Should build query with single aliased project."""
        full_paths = ["group/repo1"]

        query = gitlab_provider._build_multiplex_query(full_paths)

        assert "query {" in query
        assert 'project0: project(fullPath: "group/repo1")' in query
        assert "repository {" in query
        assert "tree {" in query
        assert "lastCommit {" in query
        assert "sha" in query
        assert "author { name }" in query
        assert "committedDate" in query

    def test_builds_multiple_projects_query(self, gitlab_provider):
        """Should build query with multiple aliased projects."""
        full_paths = ["group/repo1", "group/repo2", "group/repo3"]

        query = gitlab_provider._build_multiplex_query(full_paths)

        assert 'project0: project(fullPath: "group/repo1")' in query
        assert 'project1: project(fullPath: "group/repo2")' in query
        assert 'project2: project(fullPath: "group/repo3")' in query

    def test_handles_special_characters_in_paths(self, gitlab_provider):
        """Should properly escape special characters in project paths."""
        full_paths = ["group/repo-with-dash", "group/repo_underscore"]

        query = gitlab_provider._build_multiplex_query(full_paths)

        assert "group/repo-with-dash" in query
        assert "group/repo_underscore" in query

    def test_handles_max_batch_size(self, gitlab_provider):
        """Should handle maximum batch size of 50 projects."""
        full_paths = [f"group/repo{i}" for i in range(50)]

        query = gitlab_provider._build_multiplex_query(full_paths)

        # Verify all 50 projects included
        for i in range(50):
            assert f'project{i}: project(fullPath: "group/repo{i}")' in query


class TestParseMultiplexResponse:
    """Tests for _parse_multiplex_response method."""

    def test_parses_single_project_response(self, gitlab_provider):
        """Should parse commit info from single project response."""
        full_paths = ["group/repo1"]
        graphql_response = {
            "data": {
                "project0": {
                    "repository": {
                        "tree": {
                            "lastCommit": {
                                "sha": "abc123def456",
                                "author": {"name": "John Doe"},
                                "committedDate": "2024-01-15T10:30:00Z",
                            }
                        }
                    }
                }
            }
        }

        result = gitlab_provider._parse_multiplex_response(graphql_response, full_paths)

        assert len(result) == 1
        assert result["group/repo1"]["commit_hash"] == "abc123d"  # 7 chars
        assert result["group/repo1"]["commit_author"] == "John Doe"
        assert isinstance(result["group/repo1"]["commit_date"], datetime)

    def test_parses_multiple_projects_response(self, gitlab_provider):
        """Should parse commit info from multiple projects."""
        full_paths = ["group/repo1", "group/repo2"]
        graphql_response = {
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

        result = gitlab_provider._parse_multiplex_response(graphql_response, full_paths)

        assert len(result) == 2
        assert result["group/repo1"]["commit_hash"] == "abc123"[:7]
        assert result["group/repo1"]["commit_author"] == "Alice"
        assert result["group/repo2"]["commit_hash"] == "def456"[:7]
        assert result["group/repo2"]["commit_author"] == "Bob"

    def test_handles_missing_commit_gracefully(self, gitlab_provider):
        """Should return None values when commit data is missing."""
        full_paths = ["group/repo1"]
        graphql_response = {
            "data": {"project0": {"repository": {"tree": None}}}  # No commits
        }

        result = gitlab_provider._parse_multiplex_response(graphql_response, full_paths)

        assert result["group/repo1"]["commit_hash"] is None
        assert result["group/repo1"]["commit_author"] is None
        assert result["group/repo1"]["commit_date"] is None

    def test_handles_missing_project_in_response(self, gitlab_provider):
        """Should handle when project is not in GraphQL response."""
        full_paths = ["group/repo1", "group/repo2"]
        graphql_response = {
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
                # project1 missing (e.g., access denied)
            }
        }

        result = gitlab_provider._parse_multiplex_response(graphql_response, full_paths)

        assert len(result) == 2
        assert result["group/repo1"]["commit_hash"] == "abc123"[:7]
        assert result["group/repo2"]["commit_hash"] is None

    def test_handles_invalid_date_format(self, gitlab_provider):
        """Should handle invalid committedDate gracefully."""
        full_paths = ["group/repo1"]
        graphql_response = {
            "data": {
                "project0": {
                    "repository": {
                        "tree": {
                            "lastCommit": {
                                "sha": "abc123",
                                "author": {"name": "Alice"},
                                "committedDate": "invalid-date",
                            }
                        }
                    }
                }
            }
        }

        result = gitlab_provider._parse_multiplex_response(graphql_response, full_paths)

        assert result["group/repo1"]["commit_hash"] == "abc123"[:7]
        assert result["group/repo1"]["commit_author"] == "Alice"
        assert result["group/repo1"]["commit_date"] is None
