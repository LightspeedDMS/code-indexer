"""
Unit tests for forge_client PR/MR merge methods.

Story #451: merge_pull_request - Merge a GitHub PR or GitLab MR

Tests:
  - GitHubForgeClient.merge_pull_request: merge method (merge/squash/rebase)
  - GitHubForgeClient.merge_pull_request: deletes branch when delete_branch=True
  - GitHubForgeClient.merge_pull_request: 405 -> ValueError (not mergeable)
  - GitHubForgeClient.merge_pull_request: 409 -> ValueError (conflict)
  - GitLabForgeClient.merge_merge_request: basic merge
  - GitLabForgeClient.merge_merge_request: squash merge
  - GitLabForgeClient.merge_merge_request: delete_branch=True
  - GitLabForgeClient.merge_merge_request: 405/406 -> ValueError
  - Auth errors (401, 403)
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.merge_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientMergePR:
    """Tests for GitHubForgeClient.merge_pull_request (sync, Story #451)."""

    def _make_pr_get_response(
        self, number=42, head_sha="abc123def456", head_ref="feature/branch"
    ):
        """Build a minimal GitHub PR GET response."""
        return {
            "number": number,
            "head": {
                "sha": head_sha,
                "ref": head_ref,
            },
            "state": "open",
        }

    def _make_merge_response(
        self, sha="merged_sha123", message="Pull Request successfully merged"
    ):
        """Build a minimal GitHub merge response."""
        return {
            "sha": sha,
            "merged": True,
            "message": message,
        }

    def test_github_merge_pr_uses_put_to_merge_endpoint(self):
        """merge_pull_request uses PUT /repos/{owner}/{repo}/pulls/{number}/merge."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "pulls/42/merge" in call_url
        assert "api.github.com" in call_url

    def test_github_merge_pr_gets_head_sha_first(self):
        """merge_pull_request first GETs the PR to get head.sha."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response(
            head_sha="deadbeef1234"
        )

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("sha") == "deadbeef1234"

    def test_github_merge_pr_sends_merge_method_in_payload(self):
        """merge_pull_request sends merge_method in PUT payload."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                merge_method="squash",
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("merge_method") == "squash"

    def test_github_merge_pr_default_merge_method_is_merge(self):
        """Default merge_method is 'merge'."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("merge_method") == "merge"

    def test_github_merge_pr_returns_success_dict(self):
        """merge_pull_request returns success=True, merged=True, sha, message."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response(
            sha="sha789", message="Merged"
        )

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response),
        ):
            result = client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["success"] is True
        assert result["merged"] is True
        assert result["sha"] == "sha789"
        assert "PR #42 merged" in result["message"]

    def test_github_merge_pr_with_commit_message(self):
        """Custom commit_message is included in PUT payload."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                commit_message="Custom merge message",
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("commit_message") == "Custom merge message"

    def test_github_merge_pr_no_commit_message_omits_field(self):
        """When commit_message is None, it is not included in PUT payload."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert "commit_message" not in payload

    def test_github_merge_pr_delete_branch_calls_delete(self):
        """When delete_branch=True, DELETE /git/refs/heads/{branch} is called."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response(
            head_ref="feature/to-delete"
        )

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 204

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response),
            patch("httpx.delete", return_value=mock_delete_response) as mock_delete,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                delete_branch=True,
            )

        mock_delete.assert_called_once()
        delete_url = mock_delete.call_args[0][0]
        assert "git/refs/heads/feature/to-delete" in delete_url

    def test_github_merge_pr_no_delete_branch_does_not_call_delete(self):
        """When delete_branch=False (default), no DELETE is called."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response),
            patch("httpx.delete") as mock_delete,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                delete_branch=False,
            )

        mock_delete.assert_not_called()

    def test_github_merge_pr_405_raises_value_error(self):
        """HTTP 405 (Method Not Allowed / not mergeable) raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 405
        mock_put_response.text = "Pull Request is not mergeable"

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response),
        ):
            with pytest.raises(ValueError, match="(?i)not mergeable|405"):
                client.merge_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_merge_pr_409_raises_value_error(self):
        """HTTP 409 (merge conflict) raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 409
        mock_put_response.text = "Merge conflict"

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response),
            patch("httpx.put", return_value=mock_put_response),
        ):
            with pytest.raises(ValueError, match="(?i)conflict|409"):
                client.merge_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_merge_pr_401_raises_forge_authentication_error(self):
        """HTTP 401 on GET raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_get_response = MagicMock()
        mock_get_response.status_code = 401
        mock_get_response.text = "Unauthorized"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_get_response):
            with pytest.raises(ForgeAuthenticationError):
                client.merge_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_merge_pr_enterprise_uses_api_v3_base(self):
        """GitHub Enterprise Server uses {host}/api/v3 base URL."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = self._make_pr_get_response()

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_get_response) as mock_get,
            patch("httpx.put", return_value=mock_put_response) as mock_put,
        ):
            client.merge_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        get_url = mock_get.call_args[0][0]
        assert "github.corp.com/api/v3" in get_url
        put_url = mock_put.call_args[0][0]
        assert "github.corp.com/api/v3" in put_url


# ---------------------------------------------------------------------------
# GitLabForgeClient.merge_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientMergeMR:
    """Tests for GitLabForgeClient.merge_merge_request (sync, Story #451)."""

    def _make_merge_response(
        self,
        iid=5,
        state="merged",
        web_url="https://gitlab.com/owner/repo/-/merge_requests/5",
    ):
        """Build a minimal GitLab MR merge response."""
        return {
            "iid": iid,
            "state": state,
            "web_url": web_url,
            "merge_commit_sha": "gitlab_merge_sha123",
        }

    def test_gitlab_merge_mr_uses_put_to_merge_endpoint(self):
        """merge_merge_request uses PUT /projects/{path}/merge_requests/{number}/merge."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response) as mock_put:
            client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "merge_requests/5/merge" in call_url
        assert "gitlab.com" in call_url

    def test_gitlab_merge_mr_returns_success_dict(self):
        """merge_merge_request returns success=True, merged=True, sha, message."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response(iid=5)

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response):
            result = client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert result["success"] is True
        assert result["merged"] is True
        assert "MR #5 merged" in result["message"]

    def test_gitlab_merge_mr_squash_method_sets_squash_true(self):
        """When merge_method='squash', squash=true is sent in payload."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response) as mock_put:
            client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                merge_method="squash",
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("squash") is True

    def test_gitlab_merge_mr_default_method_does_not_set_squash(self):
        """Default merge method does not set squash=true."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response) as mock_put:
            client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("squash") is not True

    def test_gitlab_merge_mr_delete_branch_sets_should_remove_source_branch(self):
        """When delete_branch=True, should_remove_source_branch=true is in payload."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response) as mock_put:
            client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                delete_branch=True,
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("should_remove_source_branch") is True

    def test_gitlab_merge_mr_405_raises_value_error(self):
        """HTTP 405 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 405
        mock_put_response.text = "Method Not Allowed"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response):
            with pytest.raises(ValueError, match="(?i)405|method not allowed"):
                client.merge_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_merge_mr_406_raises_value_error(self):
        """HTTP 406 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 406
        mock_put_response.text = "Branch cannot be merged"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response):
            with pytest.raises(ValueError, match="(?i)406|cannot be merged"):
                client.merge_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_merge_mr_401_raises_forge_authentication_error(self):
        """HTTP 401 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_put_response = MagicMock()
        mock_put_response.status_code = 401
        mock_put_response.text = "Unauthorized"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response):
            with pytest.raises(ForgeAuthenticationError):
                client.merge_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_merge_mr_uses_url_encoded_project_path(self):
        """GitLab merge API uses URL-encoded project path."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_put_response = MagicMock()
        mock_put_response.status_code = 200
        mock_put_response.json.return_value = self._make_merge_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_put_response) as mock_put:
            client.merge_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=5,
            )

        call_url = mock_put.call_args[0][0]
        assert "%2F" in call_url
