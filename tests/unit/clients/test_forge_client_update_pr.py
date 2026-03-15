"""
Unit tests for forge_client PR/MR update methods.

Story #450: update_pull_request - Update PR/MR metadata

Tests:
  - GitHubForgeClient.update_pull_request: title only
  - GitHubForgeClient.update_pull_request: multiple fields (title + body + labels)
  - GitHubForgeClient.update_pull_request: reviewers use separate endpoint
  - GitHubForgeClient.update_pull_request: assignees included in PATCH
  - GitHubForgeClient.update_pull_request: no fields raises ValueError
  - GitLabForgeClient.update_merge_request: title only
  - GitLabForgeClient.update_merge_request: labels as comma-separated string
  - GitLabForgeClient.update_merge_request: multiple fields
  - GitLabForgeClient.update_merge_request: no fields raises ValueError
  - Auth errors (401, 403)
  - Not found errors (404)
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.update_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientUpdatePR:
    """Tests for GitHubForgeClient.update_pull_request (sync, Story #450)."""

    def _make_pr_update_response(
        self,
        number=42,
        html_url="https://github.com/owner/repo/pull/42",
        title="Updated PR Title",
    ):
        """Build a minimal GitHub PR PATCH response."""
        return {
            "number": number,
            "html_url": html_url,
            "title": title,
        }

    def test_github_update_title_only_patches_pulls_endpoint(self):
        """Updating title only uses PATCH /repos/{owner}/{repo}/pulls/{number}."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="Updated PR Title",
            )

        mock_patch.assert_called_once()
        call_url = mock_patch.call_args[0][0]
        assert "pulls/42" in call_url
        assert "api.github.com" in call_url

    def test_github_update_title_only_returns_success(self):
        """update_pull_request returns success=True, url, and updated_fields."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        expected_url = "https://github.com/owner/repo/pull/42"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response(
            html_url=expected_url
        )

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            result = client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="New Title",
            )

        assert result["success"] is True
        assert result["url"] == expected_url
        assert "title" in result["updated_fields"]

    def test_github_update_only_sends_provided_fields_in_patch(self):
        """PATCH payload only contains the fields explicitly provided."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="Just title update",
                # body, labels, assignees, reviewers NOT provided
            )

        patch_kwargs = mock_patch.call_args[1]
        payload = patch_kwargs.get("json", {})
        assert "title" in payload
        assert "body" not in payload
        assert "labels" not in payload
        assert "assignees" not in payload

    def test_github_update_multiple_fields_includes_all_in_patch(self):
        """Updating title + body + labels includes all three in the PATCH payload."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            result = client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="New Title",
                description="Updated description",
                labels=["bug", "enhancement"],
            )

        patch_kwargs = mock_patch.call_args[1]
        payload = patch_kwargs.get("json", {})
        assert payload["title"] == "New Title"
        assert payload["body"] == "Updated description"
        assert payload["labels"] == ["bug", "enhancement"]
        assert result["updated_fields"] == sorted(["title", "description", "labels"])

    def test_github_update_reviewers_uses_separate_endpoint(self):
        """Providing reviewers triggers a second POST to /requested_reviewers."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_patch_response = MagicMock()
        mock_patch_response.status_code = 200
        mock_patch_response.json.return_value = self._make_pr_update_response()

        mock_post_response = MagicMock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {}

        client = GitHubForgeClient()
        with (
            patch("httpx.patch", return_value=mock_patch_response) as mock_patch,
            patch("httpx.post", return_value=mock_post_response) as mock_post,
        ):
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="PR with reviewers",
                reviewers=["alice", "bob"],
            )

        # PATCH for title
        mock_patch.assert_called_once()
        # POST for reviewers
        mock_post.assert_called_once()
        post_url = mock_post.call_args[0][0]
        assert "pulls/42/requested_reviewers" in post_url
        post_kwargs = mock_post.call_args[1]
        assert post_kwargs.get("json", {}).get("reviewers") == ["alice", "bob"]

    def test_github_update_reviewers_included_in_updated_fields(self):
        """reviewers appears in updated_fields when provided."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_patch_response = MagicMock()
        mock_patch_response.status_code = 200
        mock_patch_response.json.return_value = self._make_pr_update_response()

        mock_post_response = MagicMock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {}

        client = GitHubForgeClient()
        with (
            patch("httpx.patch", return_value=mock_patch_response),
            patch("httpx.post", return_value=mock_post_response),
        ):
            result = client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                reviewers=["alice"],
            )

        assert "reviewers" in result["updated_fields"]

    def test_github_update_assignees_included_in_patch_body(self):
        """assignees are included in the PATCH payload."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            result = client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                assignees=["charlie"],
            )

        patch_kwargs = mock_patch.call_args[1]
        payload = patch_kwargs.get("json", {})
        assert payload.get("assignees") == ["charlie"]
        assert "assignees" in result["updated_fields"]

    def test_github_update_no_fields_raises_value_error(self):
        """Calling update_pull_request with no fields to update raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()
        with pytest.raises(ValueError, match="(?i)at least one field"):
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                # No title, description, labels, assignees, or reviewers provided
            )

    def test_github_401_raises_forge_authentication_error(self):
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
                client.update_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                    title="test",
                )

    def test_github_404_raises_value_error(self):
        """HTTP 404 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response):
            with pytest.raises(ValueError, match="not found"):
                client.update_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=99999,
                    title="test",
                )

    def test_github_enterprise_uses_api_v3_base(self):
        """GitHub Enterprise Server uses {host}/api/v3 base URL."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                number=42,
                title="test",
            )

        call_url = mock_patch.call_args[0][0]
        assert "github.corp.com/api/v3" in call_url

    def test_github_no_reviewers_does_not_call_post(self):
        """When no reviewers provided, no POST to requested_reviewers endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_pr_update_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.patch", return_value=mock_response),
            patch("httpx.post") as mock_post,
        ):
            client.update_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                title="test",
            )

        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# GitLabForgeClient.update_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientUpdateMR:
    """Tests for GitLabForgeClient.update_merge_request (sync, Story #450)."""

    def _make_mr_update_response(
        self,
        iid=5,
        web_url="https://gitlab.com/owner/repo/-/merge_requests/5",
        title="Updated MR Title",
    ):
        """Build a minimal GitLab MR PUT response."""
        return {
            "iid": iid,
            "web_url": web_url,
            "title": title,
        }

    def test_gitlab_update_title_only_puts_to_merge_requests_endpoint(self):
        """Updating title only uses PUT /projects/{path}/merge_requests/{number}."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                title="Updated MR Title",
            )

        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "merge_requests/5" in call_url
        assert "gitlab.com" in call_url

    def test_gitlab_update_title_only_returns_success(self):
        """update_merge_request returns success=True, url, and updated_fields."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        expected_url = "https://gitlab.com/owner/repo/-/merge_requests/5"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response(
            web_url=expected_url
        )

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            result = client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                title="New MR Title",
            )

        assert result["success"] is True
        assert result["url"] == expected_url
        assert "title" in result["updated_fields"]

    def test_gitlab_labels_sent_as_comma_separated_string(self):
        """GitLab API requires labels as comma-separated string, not list."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                labels=["bug", "enhancement", "v2"],
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload["labels"] == "bug,enhancement,v2"

    def test_gitlab_update_only_sends_provided_fields(self):
        """PUT payload only contains the fields explicitly provided."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                title="Only title",
                # description, labels, assignees NOT provided
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert "title" in payload
        assert "description" not in payload
        assert "labels" not in payload

    def test_gitlab_update_description_uses_description_key(self):
        """GitLab uses 'description' key (not 'body') for MR description."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                description="Updated description text",
            )

        put_kwargs = mock_put.call_args[1]
        payload = put_kwargs.get("json", {})
        assert payload.get("description") == "Updated description text"
        # 'body' key should not be used
        assert "body" not in payload

    def test_gitlab_update_multiple_fields_returns_all_in_updated_fields(self):
        """Updating title + description + labels returns all three in updated_fields."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            result = client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                title="New Title",
                description="New desc",
                labels=["bug"],
            )

        assert "title" in result["updated_fields"]
        assert "description" in result["updated_fields"]
        assert "labels" in result["updated_fields"]

    def test_gitlab_update_no_fields_raises_value_error(self):
        """Calling update_merge_request with no fields to update raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()
        with pytest.raises(ValueError, match="(?i)at least one field"):
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                # No title, description, labels, or assignees provided
            )

    def test_gitlab_401_raises_forge_authentication_error(self):
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
                client.update_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                    title="test",
                )

    def test_gitlab_404_raises_value_error(self):
        """HTTP 404 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response):
            with pytest.raises(ValueError, match="not found"):
                client.update_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=99999,
                    title="test",
                )

    def test_gitlab_uses_url_encoded_project_path(self):
        """GitLab MR update API uses URL-encoded project path."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_mr_update_response()

        client = GitLabForgeClient()
        with patch("httpx.put", return_value=mock_response) as mock_put:
            client.update_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=5,
                title="test",
            )

        call_url = mock_put.call_args[0][0]
        assert "%2F" in call_url
