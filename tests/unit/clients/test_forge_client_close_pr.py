"""
Unit tests for forge_client PR/MR close methods.

Story #452: close_pull_request - Close a GitHub PR or GitLab MR

Tests:
  - GitHubForgeClient.close_pull_request: uses PATCH with state=closed
  - GitHubForgeClient.close_pull_request: returns success message
  - GitHubForgeClient.close_pull_request: auth errors (401, 403)
  - GitHubForgeClient.close_pull_request: not found (404)
  - GitLabForgeClient.close_merge_request: uses PUT with state_event=close
  - GitLabForgeClient.close_merge_request: returns success message
  - GitLabForgeClient.close_merge_request: auth errors (401, 403)
  - GitLabForgeClient.close_merge_request: not found (404)
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.close_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientClosePR:
    """Tests for GitHubForgeClient.close_pull_request (sync, Story #452)."""

    def _make_close_response(
        self,
        number=42,
        state="closed",
        html_url="https://github.com/owner/repo/pull/42",
    ):
        """Build a minimal GitHub PR PATCH response for close."""
        return {
            "number": number,
            "state": state,
            "html_url": html_url,
        }

    def test_github_close_pr_uses_patch_with_state_closed(self):
        """close_pull_request uses PATCH /repos/{owner}/{repo}/pulls/{number} with state=closed."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            client.close_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        mock_patch.assert_called_once()
        call_url = mock_patch.call_args[0][0]
        assert "pulls/42" in call_url
        assert "api.github.com" in call_url
        patch_kwargs = mock_patch.call_args[1]
        payload = patch_kwargs.get("json", {})
        assert payload.get("state") == "closed"

    def test_github_close_pr_returns_success_dict(self):
        """close_pull_request returns success=True and message."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response(number=42)

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            result = client.close_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["success"] is True
        assert "PR #42 closed" in result["message"]

    def test_github_close_pr_401_raises_forge_authentication_error(self):
        """HTTP 401 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.close_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_close_pr_403_raises_forge_authentication_error(self):
        """HTTP 403 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.close_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_close_pr_404_raises_value_error(self):
        """HTTP 404 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            with pytest.raises(ValueError, match="(?i)not found|404"):
                client.close_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=99999,
                )

    def test_github_close_pr_enterprise_uses_api_v3_base(self):
        """GitHub Enterprise Server uses {host}/api/v3 base URL."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            client.close_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        call_url = mock_patch.call_args[0][0]
        assert "github.corp.com/api/v3" in call_url


# ---------------------------------------------------------------------------
# GitLabForgeClient.close_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientCloseMR:
    """Tests for GitLabForgeClient.close_merge_request (sync, Story #452)."""

    def _make_close_response(
        self,
        iid=5,
        state="closed",
        web_url="https://gitlab.com/owner/repo/-/merge_requests/5",
    ):
        """Build a minimal GitLab MR PUT response for close."""
        return {
            "iid": iid,
            "state": state,
            "web_url": web_url,
        }

    def test_gitlab_close_mr_uses_put_with_state_event_close(self):
        """close_merge_request uses PUT /projects/{path}/merge_requests/{number} with state_event=close."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.close_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "merge_requests/5" in call_url
        assert "gitlab.com" in call_url
        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("state_event") == "close"

    def test_gitlab_close_mr_returns_success_dict(self):
        """close_merge_request returns success=True and message."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response(iid=5)

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            result = client.close_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert result["success"] is True
        assert "MR #5 closed" in result["message"]

    def test_gitlab_close_mr_401_raises_forge_authentication_error(self):
        """HTTP 401 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.close_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_close_mr_403_raises_forge_authentication_error(self):
        """HTTP 403 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.close_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_close_mr_404_raises_value_error(self):
        """HTTP 404 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            with pytest.raises(ValueError, match="(?i)not found|404"):
                client.close_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=99999,
                )

    def test_gitlab_close_mr_uses_url_encoded_project_path(self):
        """GitLab close MR API uses URL-encoded project path."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_close_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.close_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=5,
            )

        call_url = mock_put.call_args[0][0]
        assert "%2F" in call_url
