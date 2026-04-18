"""
Tests for enrich_repositories on GitLab and GitHub providers (Story #754).

RED phase: written before implementation to drive design.
Mocking strategy:
  - HTTP transport boundary: patch("httpx.post") for GraphQL calls
  - Constructor collaborators: MagicMock test doubles

GitLab: GraphQL batch of up to 10 per call; chunk larger inputs; per-repo soft-fail.
GitHub: passthrough/no-op — returns empty dict, makes zero HTTP calls.
"""

import inspect
import json
from unittest.mock import MagicMock, patch
import httpx


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_token_manager(platform):
    from code_indexer.server.services.ci_token_manager import TokenData

    tm = MagicMock()
    tm.get_token.return_value = TokenData(
        platform=platform,
        token="test-token",
        base_url=None,
    )
    return tm


def _make_golden_repo_manager():
    grm = MagicMock()
    grm.list_golden_repos.return_value = []
    return grm


def _make_gitlab_provider():
    from code_indexer.server.services.repository_providers.gitlab_provider import (
        GitLabProvider,
    )

    return GitLabProvider(
        token_manager=_make_token_manager("gitlab"),
        golden_repo_manager=_make_golden_repo_manager(),
    )


def _make_github_provider():
    from code_indexer.server.services.repository_providers.github_provider import (
        GitHubProvider,
    )

    return GitHubProvider(
        token_manager=_make_token_manager("github"),
        golden_repo_manager=_make_golden_repo_manager(),
    )


def _make_graphql_ok_response(project_data=None):
    """Return a successful GraphQL response with given project data."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": project_data or {}}
    return resp


# ---------------------------------------------------------------------------
# GitLab: enrich_repositories signature
# ---------------------------------------------------------------------------


class TestGitLabEnrichSignature:
    """enrich_repositories must exist with correct parameter names on GitLabProvider."""

    def test_method_exists_with_correct_parameters(self):
        """enrich_repositories must accept clone_urls parameter."""
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        assert hasattr(GitLabProvider, "enrich_repositories"), (
            "enrich_repositories not found on GitLabProvider"
        )
        sig = inspect.signature(GitLabProvider.enrich_repositories)
        param_names = list(sig.parameters.keys())
        assert "clone_urls" in param_names, f"clone_urls not in {param_names}"


# ---------------------------------------------------------------------------
# GitLab: return shape — dict keyed by clone_url
# ---------------------------------------------------------------------------


class TestGitLabEnrichReturnShape:
    """enrich_repositories must return a dict with the input clone URL as a key."""

    def test_returns_dict_keyed_by_input_clone_url(self):
        """
        When enrichment for a repo succeeds, the returned dict must contain
        the input clone_url_https as a key.
        """
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"

        # GraphQL returns commit data for the project (alias 'project0')
        data = {
            "project0": {
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                }
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])

        assert isinstance(result, dict)
        assert clone_url in result, (
            f"Expected clone URL {clone_url!r} as a key in result, got keys: {list(result.keys())}"
        )

    def test_empty_input_returns_empty_dict(self):
        """enrich_repositories with empty list must return empty dict without HTTP calls."""
        provider = _make_gitlab_provider()
        with patch("httpx.post") as mock_post:
            result = provider.enrich_repositories([])
        assert result == {}
        mock_post.assert_not_called()

    def test_returns_last_activity_when_present(self):
        """
        When GraphQL returns lastActivityAt, the returned dict must contain
        last_activity as an ISO 8601 string alongside commit fields.
        """
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"

        data = {
            "project0": {
                "lastActivityAt": "2025-11-04T16:43:16+00:00",
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                },
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])

        assert clone_url in result
        assert "last_activity" in result[clone_url], (
            "last_activity must be present when GraphQL provides lastActivityAt"
        )
        assert result[clone_url]["last_activity"] == "2025-11-04T16:43:16+00:00"

    def test_absent_last_activity_at_does_not_crash(self):
        """
        When GraphQL response has no lastActivityAt field, enrichment must succeed
        without raising and last_activity key must be absent from the result.
        """
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"

        data = {
            "project0": {
                # No lastActivityAt
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                },
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])

        assert clone_url in result
        assert "last_activity" not in result[clone_url], (
            "last_activity must be absent when GraphQL does not provide lastActivityAt"
        )


# ---------------------------------------------------------------------------
# GitLab: GraphQL batching (max 10 per call)
# ---------------------------------------------------------------------------


class TestGitLabEnrichBatching:
    """GitLab must chunk clone_urls into batches of at most 10 for GraphQL."""

    def test_single_batch_for_10_or_fewer_urls(self):
        """10 clone URLs must trigger exactly one GraphQL POST."""
        provider = _make_gitlab_provider()
        clone_urls = [f"https://gitlab.com/group/project{i}.git" for i in range(10)]
        resp = _make_graphql_ok_response()
        with patch("httpx.post", return_value=resp) as mock_post:
            provider.enrich_repositories(clone_urls)
        assert mock_post.call_count == 1

    def test_two_batches_for_11_urls(self):
        """11 clone URLs must trigger exactly two GraphQL POSTs (10 + 1)."""
        provider = _make_gitlab_provider()
        clone_urls = [f"https://gitlab.com/group/project{i}.git" for i in range(11)]
        resp = _make_graphql_ok_response()
        with patch("httpx.post", return_value=resp) as mock_post:
            provider.enrich_repositories(clone_urls)
        assert mock_post.call_count == 2

    def test_two_batches_for_15_urls(self):
        """15 clone URLs must trigger exactly two GraphQL POSTs (10 + 5)."""
        provider = _make_gitlab_provider()
        clone_urls = [f"https://gitlab.com/group/project{i}.git" for i in range(15)]
        resp = _make_graphql_ok_response()
        with patch("httpx.post", return_value=resp) as mock_post:
            provider.enrich_repositories(clone_urls)
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# GitLab: per-repo soft-fail
# ---------------------------------------------------------------------------


class TestGitLabEnrichSoftFail:
    """A single-repo GraphQL null result must not abort the batch."""

    def test_null_project_in_response_does_not_raise(self):
        """A project returning null in GraphQL response must not abort enrichment."""
        provider = _make_gitlab_provider()
        clone_urls = [
            "https://gitlab.com/group/project0.git",
            "https://gitlab.com/group/project1.git",
        ]

        # project0 returns null (not-found), project1 returns valid data
        data = {
            "project0": None,
            "project1": {
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                }
            },
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            # Must NOT raise even though project0 returned null
            result = provider.enrich_repositories(clone_urls)
        assert isinstance(result, dict)

    def test_graphql_error_field_does_not_raise(self):
        """GraphQL errors field in response must not abort enrichment."""
        provider = _make_gitlab_provider()
        clone_urls = ["https://gitlab.com/group/project0.git"]

        error_body = {
            "data": {"project0": None},
            "errors": [{"message": "Field 'tree' doesn't exist on type 'Repository'"}],
        }
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = error_body
        with patch("httpx.post", return_value=resp):
            # Must NOT raise — per-repo soft-fail
            result = provider.enrich_repositories(clone_urls)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# GitLab: datetime serialization — commit_date must be JSON-safe string
# ---------------------------------------------------------------------------


class TestGitLabEnrichDatetimeSerialization:
    """commit_date in the returned dict must be a JSON-serializable string, not a datetime."""

    def test_commit_date_is_iso_string_not_datetime(self):
        """
        When GraphQL returns a committedDate, the returned dict must contain
        commit_date as an ISO 8601 string so JSONResponse(content=result) does not
        raise TypeError: Object of type datetime is not JSON serializable.
        """
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"

        data = {
            "project0": {
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                }
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])

        assert clone_url in result
        commit_date = result[clone_url].get("commit_date")
        assert commit_date is not None, (
            "commit_date must not be None when GraphQL provides a date"
        )
        assert isinstance(commit_date, str), (
            f"commit_date must be a str for JSON serialization, got {type(commit_date)!r}"
        )
        # Must not raise — this is what JSONResponse does internally
        serialized = json.dumps(result)
        assert "commit_date" in serialized

    def test_commit_date_none_is_json_safe(self):
        """
        When GraphQL returns no committedDate, commit_date must be None and still
        JSON-serializable.
        """
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"

        data = {
            "project0": {
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": {"name": "Alice"},
                            # No committedDate
                        }
                    }
                }
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])

        assert clone_url in result
        # Must not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# GitLab: None-safety — null at each GraphQL nesting level
# ---------------------------------------------------------------------------


class TestGitLabEnrichNoneSafety:
    """Null values at any GraphQL nesting level must not raise AttributeError."""

    def test_null_repository_does_not_raise(self):
        """project.repository = null must not raise."""
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"
        data = {"project0": {"repository": None}}
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])
        assert isinstance(result, dict)

    def test_null_tree_does_not_raise(self):
        """project.repository.tree = null must not raise."""
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"
        data = {"project0": {"repository": {"tree": None}}}
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])
        assert isinstance(result, dict)

    def test_null_last_commit_does_not_raise(self):
        """project.repository.tree.lastCommit = null must not raise."""
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"
        data = {"project0": {"repository": {"tree": {"lastCommit": None}}}}
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])
        assert isinstance(result, dict)

    def test_null_author_does_not_raise(self):
        """lastCommit.author = null must not raise."""
        provider = _make_gitlab_provider()
        clone_url = "https://gitlab.com/group/project0.git"
        data = {
            "project0": {
                "repository": {
                    "tree": {
                        "lastCommit": {
                            "sha": "abc1234567890",
                            "author": None,
                            "committedDate": "2024-01-15T10:30:00Z",
                        }
                    }
                }
            }
        }
        resp = _make_graphql_ok_response(data)
        with patch("httpx.post", return_value=resp):
            result = provider.enrich_repositories([clone_url])
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# GitHub: enrich_repositories signature
# ---------------------------------------------------------------------------


class TestGitHubEnrichSignature:
    """enrich_repositories must exist with correct parameter names on GitHubProvider."""

    def test_method_exists_with_correct_parameters(self):
        """enrich_repositories must accept clone_urls parameter."""
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        assert hasattr(GitHubProvider, "enrich_repositories"), (
            "enrich_repositories not found on GitHubProvider"
        )
        sig = inspect.signature(GitHubProvider.enrich_repositories)
        param_names = list(sig.parameters.keys())
        assert "clone_urls" in param_names, f"clone_urls not in {param_names}"


# ---------------------------------------------------------------------------
# GitHub: passthrough no-op
# ---------------------------------------------------------------------------


class TestGitHubEnrichPassthrough:
    """GitHub enrich_repositories is a no-op that makes no HTTP calls."""

    def test_returns_empty_dict(self):
        """enrich_repositories must return an empty dict."""
        provider = _make_github_provider()
        result = provider.enrich_repositories(["https://github.com/owner/repo.git"])
        assert result == {}

    def test_makes_no_http_calls(self):
        """enrich_repositories must not make any HTTP calls."""
        provider = _make_github_provider()
        with patch("httpx.post") as mock_post:
            with patch("httpx.get") as mock_get:
                provider.enrich_repositories(["https://github.com/owner/repo.git"])
        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_empty_input_returns_empty_dict(self):
        """Empty input must return empty dict without HTTP calls."""
        provider = _make_github_provider()
        result = provider.enrich_repositories([])
        assert result == {}
