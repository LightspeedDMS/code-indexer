"""
Unit tests for forge_client list PRs/MRs methods.

Story #446: list_pull_requests - List PRs/MRs for a repository

Tests:
  - GitHubForgeClient.list_pull_requests: list GitHub PRs via REST API
  - GitLabForgeClient.list_merge_requests: list GitLab MRs via REST API
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.list_pull_requests
# ---------------------------------------------------------------------------


class TestGitHubForgeClientListPRs:
    """Tests for GitHubForgeClient.list_pull_requests (sync, Story #446)."""

    def _make_github_pr(
        self,
        number=1,
        title="Test PR",
        state="open",
        login="octocat",
        head_ref="feature",
        base_ref="main",
        html_url=None,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        merged_at=None,
    ):
        """Build a minimal GitHub PR API response dict."""
        return {
            "number": number,
            "title": title,
            "state": state,
            "user": {"login": login},
            "head": {"ref": head_ref},
            "base": {"ref": base_ref},
            "html_url": html_url or f"https://github.com/owner/repo/pull/{number}",
            "created_at": created_at,
            "updated_at": updated_at,
            "merged_at": merged_at,
        }

    def test_github_list_open_prs(self):
        """list_pull_requests returns normalized open PRs from GitHub API."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_github_pr(number=1, title="First PR", state="open"),
            self._make_github_pr(number=2, title="Second PR", state="open"),
        ]

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=10,
            )

        assert len(result) == 2
        assert result[0]["number"] == 1
        assert result[0]["title"] == "First PR"
        assert result[0]["state"] == "open"
        assert result[0]["author"] == "octocat"
        assert result[0]["source_branch"] == "feature"
        assert result[0]["target_branch"] == "main"
        assert "github.com/owner/repo/pull/1" in result[0]["url"]
        assert result[0]["created_at"] == "2024-01-01T00:00:00Z"
        assert result[0]["updated_at"] == "2024-01-02T00:00:00Z"

    def test_github_list_merged_prs(self):
        """list_pull_requests with state='merged' filters closed PRs by merged_at."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        # Two closed PRs: one merged, one unmerged (rejected)
        mock_response.json.return_value = [
            self._make_github_pr(
                number=10, state="closed", merged_at="2024-01-05T00:00:00Z"
            ),
            self._make_github_pr(
                number=11,
                state="closed",
                merged_at=None,  # rejected/closed without merge
            ),
        ]

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                state="merged",
                limit=10,
            )

        # Only the merged PR should be returned
        assert len(result) == 1
        assert result[0]["number"] == 10
        assert result[0]["state"] == "merged"

    def test_github_list_with_author(self):
        """list_pull_requests passes creator param to GitHub API when author given."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_github_pr(number=5, login="alice"),
        ]

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=10,
                author="alice",
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("creator") == "alice"

        assert len(result) == 1
        assert result[0]["author"] == "alice"

    def test_github_list_with_limit(self):
        """list_pull_requests passes per_page param to GitHub API."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=25,
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("per_page") == 25

    def test_github_list_uses_correct_api_url(self):
        """github.com uses api.github.com endpoint for listing PRs."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
            )
            call_url = mock_get.call_args[0][0]
            assert "api.github.com" in call_url
            assert "/repos/owner/repo/pulls" in call_url

    def test_github_list_enterprise_uses_correct_api_url(self):
        """GitHub Enterprise uses {host}/api/v3 endpoint for listing PRs."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_pull_requests(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
            )
            call_url = mock_get.call_args[0][0]
            assert "github.corp.com/api/v3" in call_url

    def test_github_401_raises_forge_auth_error(self):
        """HTTP 401 from GitHub raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.list_pull_requests(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                )

    def test_github_403_raises_forge_auth_error(self):
        """HTTP 403 from GitHub raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError, match="403"):
                client.list_pull_requests(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                )

    def test_github_other_error_raises_value_error(self):
        """Non-2xx non-401/403 from GitHub raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="500"):
                client.list_pull_requests(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                )

    def test_github_list_state_all_does_not_filter(self):
        """state='all' passes 'all' to API and returns mixed states without filtering."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_github_pr(number=1, state="open"),
            self._make_github_pr(
                number=2, state="closed", merged_at="2024-01-05T00:00:00Z"
            ),
            self._make_github_pr(number=3, state="closed", merged_at=None),
        ]

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = client.list_pull_requests(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                state="all",
                limit=10,
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            # state='all' should pass 'all' to the API
            assert params.get("state") == "all"

        # all 3 should be returned - state is preserved as-is for closed (not filtered)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# GitLabForgeClient.list_merge_requests
# ---------------------------------------------------------------------------


class TestGitLabForgeClientListMRs:
    """Tests for GitLabForgeClient.list_merge_requests (sync, Story #446)."""

    def _make_gitlab_mr(
        self,
        iid=1,
        title="Test MR",
        state="opened",
        username="john_doe",
        source_branch="feature",
        target_branch="main",
        web_url=None,
        created_at="2024-01-01T00:00:00.000Z",
        updated_at="2024-01-02T00:00:00.000Z",
    ):
        """Build a minimal GitLab MR API response dict."""
        return {
            "iid": iid,
            "title": title,
            "state": state,
            "author": {"username": username},
            "source_branch": source_branch,
            "target_branch": target_branch,
            "web_url": web_url
            or f"https://gitlab.com/owner/repo/-/merge_requests/{iid}",
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def test_gitlab_list_open_mrs(self):
        """list_merge_requests returns normalized open MRs from GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_gitlab_mr(iid=1, title="First MR", state="opened"),
            self._make_gitlab_mr(iid=2, title="Second MR", state="opened"),
        ]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=10,
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            # 'open' should be mapped to 'opened' for GitLab API
            assert params.get("state") == "opened"

        assert len(result) == 2
        assert result[0]["number"] == 1
        assert result[0]["title"] == "First MR"
        assert result[0]["state"] == "open"
        assert result[0]["author"] == "john_doe"
        assert result[0]["source_branch"] == "feature"
        assert result[0]["target_branch"] == "main"
        assert "merge_requests/1" in result[0]["url"]
        assert result[0]["created_at"] == "2024-01-01T00:00:00.000Z"
        assert result[0]["updated_at"] == "2024-01-02T00:00:00.000Z"

    def test_gitlab_list_merged_mrs(self):
        """list_merge_requests with state='merged' passes 'merged' to GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_gitlab_mr(iid=5, state="merged"),
        ]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="merged",
                limit=10,
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            # 'merged' should pass through as-is to GitLab
            assert params.get("state") == "merged"

        assert len(result) == 1
        assert result[0]["number"] == 5
        assert result[0]["state"] == "merged"

    def test_gitlab_list_with_author(self):
        """list_merge_requests passes author_username param when author given."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_gitlab_mr(iid=3, username="alice"),
        ]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            result = client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=10,
                author="alice",
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("author_username") == "alice"

        assert len(result) == 1
        assert result[0]["author"] == "alice"

    def test_gitlab_list_with_limit(self):
        """list_merge_requests passes per_page param to GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="open",
                limit=50,
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("per_page") == 50

    def test_gitlab_list_uses_url_encoded_project_path(self):
        """GitLab list MRs uses URL-encoded project path in API URL."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                state="open",
            )
            call_url = mock_get.call_args[0][0]
            # URL-encoded slash: %2F
            assert "%2F" in call_url

    def test_gitlab_list_closed_state_mapping(self):
        """list_merge_requests with state='closed' passes 'closed' through to GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="closed",
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("state") == "closed"

    def test_gitlab_list_all_state_mapping(self):
        """list_merge_requests with state='all' passes 'all' through to GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="all",
            )
            call_kwargs = mock_get.call_args[1]
            params = call_kwargs.get("params", {})
            assert params.get("state") == "all"

    def test_gitlab_401_raises_forge_auth_error(self):
        """HTTP 401 from GitLab raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.list_merge_requests(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                )

    def test_gitlab_403_raises_forge_auth_error(self):
        """HTTP 403 from GitLab raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError, match="403"):
                client.list_merge_requests(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                )

    def test_gitlab_other_error_raises_value_error(self):
        """Non-2xx non-401/403 from GitLab raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="500"):
                client.list_merge_requests(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                )

    def test_gitlab_normalized_state_opened_to_open(self):
        """GitLab 'opened' state in response is normalized to 'open' in output."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            self._make_gitlab_mr(iid=1, state="opened"),
        ]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_requests(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                state="open",
            )

        # GitLab returns 'opened' but we should normalize to 'open'
        assert result[0]["state"] == "open"
