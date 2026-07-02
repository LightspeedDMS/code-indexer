"""
Unit tests for forge_client PR/MR comment creation methods.

Story #449: comment_on_pull_request - Add comments to PR/MR

Tests:
  - GitHubForgeClient.comment_on_pull_request: general comment (no file_path)
  - GitHubForgeClient.comment_on_pull_request: inline comment (file_path+line_number)
  - GitLabForgeClient.comment_on_merge_request: general comment (no file_path)
  - GitLabForgeClient.comment_on_merge_request: inline comment (file_path+line_number)
  - Auth errors
  - Validation errors
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.comment_on_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientCommentOnPR:
    """Tests for GitHubForgeClient.comment_on_pull_request (sync, Story #449)."""

    def _make_issue_comment_response(
        self,
        comment_id=9001,
        html_url="https://github.com/owner/repo/pull/42#issuecomment-9001",
    ):
        """Build a GitHub issue comment creation response."""
        return {
            "id": comment_id,
            "html_url": html_url,
        }

    def _make_review_comment_response(
        self,
        comment_id=9002,
        html_url="https://github.com/owner/repo/pull/42#discussion_r9002",
    ):
        """Build a GitHub pull request review comment creation response."""
        return {
            "id": comment_id,
            "html_url": html_url,
        }

    def _make_pr_response(self, head_sha="abc123def456"):
        """Build a minimal GitHub PR GET response."""
        return {
            "number": 42,
            "head": {"sha": head_sha},
        }

    def test_github_general_comment_posts_to_issues_endpoint(self):
        """General comment (no file_path) POSTs to /issues/{number}/comments."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_issue_comment_response(
            comment_id=9001
        )

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="This looks good!",
            )

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "issues/42/comments" in call_url
        assert "api.github.com" in call_url

    def test_github_general_comment_returns_comment_id_and_url(self):
        """General comment returns dict with comment_id and url."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        expected_url = "https://github.com/owner/repo/pull/42#issuecomment-9001"
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_issue_comment_response(
            comment_id=9001, html_url=expected_url
        )

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            result = client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="LGTM",
            )

        assert result["comment_id"] == 9001
        assert result["url"] == expected_url

    def test_github_inline_comment_fetches_head_sha_first(self):
        """Inline comment (file_path+line_number) GETs PR to fetch head.sha before POSTing."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_pr_response = MagicMock()
        mock_pr_response.status_code = 200
        mock_pr_response.json.return_value = self._make_pr_response(head_sha="abc123")

        mock_comment_response = MagicMock()
        mock_comment_response.status_code = 201
        mock_comment_response.json.return_value = self._make_review_comment_response(
            comment_id=9002
        )

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_pr_response) as mock_get,
            patch("httpx.post", return_value=mock_comment_response),
        ):
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="Consider extracting this function",
                file_path="src/auth.py",
                line_number=55,
            )

        # Should have called GET to fetch PR first
        mock_get.assert_called_once()
        get_url = mock_get.call_args[0][0]
        assert "pulls/42" in get_url
        assert "api.github.com" in get_url

    def test_github_inline_comment_posts_to_pulls_comments_endpoint(self):
        """Inline comment POSTs to /pulls/{number}/comments (not issues endpoint)."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_pr_response = MagicMock()
        mock_pr_response.status_code = 200
        mock_pr_response.json.return_value = self._make_pr_response(head_sha="abc123")

        mock_comment_response = MagicMock()
        mock_comment_response.status_code = 201
        mock_comment_response.json.return_value = self._make_review_comment_response(
            comment_id=9002
        )

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_pr_response),
            patch("httpx.post", return_value=mock_comment_response) as mock_post,
        ):
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="Consider extracting this function",
                file_path="src/auth.py",
                line_number=55,
            )

        mock_post.assert_called_once()
        post_url = mock_post.call_args[0][0]
        assert "pulls/42/comments" in post_url

    def test_github_inline_comment_payload_contains_commit_id_path_line(self):
        """Inline comment payload includes commit_id (head sha), path, line, side=RIGHT."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        head_sha = "deadbeef12345678"
        mock_pr_response = MagicMock()
        mock_pr_response.status_code = 200
        mock_pr_response.json.return_value = self._make_pr_response(head_sha=head_sha)

        mock_comment_response = MagicMock()
        mock_comment_response.status_code = 201
        mock_comment_response.json.return_value = self._make_review_comment_response(
            comment_id=9002
        )

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_pr_response),
            patch("httpx.post", return_value=mock_comment_response) as mock_post,
        ):
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="Nit: rename this variable",
                file_path="src/utils.py",
                line_number=77,
            )

        post_kwargs = mock_post.call_args[1]
        payload = post_kwargs.get("json", {})
        assert payload["commit_id"] == head_sha
        assert payload["path"] == "src/utils.py"
        assert payload["line"] == 77
        assert payload["side"] == "RIGHT"
        assert payload["body"] == "Nit: rename this variable"

    def test_github_inline_comment_returns_comment_id_and_url(self):
        """Inline comment returns dict with comment_id and url from pull review comment."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        expected_url = "https://github.com/owner/repo/pull/42#discussion_r9002"
        mock_pr_response = MagicMock()
        mock_pr_response.status_code = 200
        mock_pr_response.json.return_value = self._make_pr_response(head_sha="abc123")

        mock_comment_response = MagicMock()
        mock_comment_response.status_code = 201
        mock_comment_response.json.return_value = self._make_review_comment_response(
            comment_id=9002, html_url=expected_url
        )

        client = GitHubForgeClient()
        with (
            patch("httpx.get", return_value=mock_pr_response),
            patch("httpx.post", return_value=mock_comment_response),
        ):
            result = client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="Check this",
                file_path="src/auth.py",
                line_number=10,
            )

        assert result["comment_id"] == 9002
        assert result["url"] == expected_url

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
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.comment_on_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                    body="test comment",
                )

    def test_github_403_raises_forge_authentication_error(self):
        """HTTP 403 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.comment_on_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                    body="test comment",
                )

    def test_github_422_raises_value_error(self):
        """HTTP 422 raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = "Unprocessable Entity"

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ValueError):
                client.comment_on_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                    body="test comment",
                )

    def test_github_enterprise_uses_api_v3_base(self):
        """GitHub Enterprise Server uses {host}/api/v3 base URL."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_issue_comment_response()

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                number=42,
                body="test comment",
            )

        call_url = mock_post.call_args[0][0]
        assert "github.corp.com/api/v3" in call_url

    def test_github_general_comment_does_not_call_get(self):
        """General comment (no file_path) does not make a GET request to fetch head sha."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_issue_comment_response()

        client = GitHubForgeClient()
        with (
            patch("httpx.get") as mock_get,
            patch("httpx.post", return_value=mock_response),
        ):
            client.comment_on_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                body="general comment",
            )

        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# GitLabForgeClient.comment_on_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientCommentOnMR:
    """Tests for GitLabForgeClient.comment_on_merge_request (sync, Story #449)."""

    def _make_note_response(self, note_id=5001, web_url=None):
        """Build a GitLab note creation response."""
        return {
            "id": note_id,
            "web_url": web_url
            or f"https://gitlab.com/owner/repo/-/merge_requests/5#note_{note_id}",
        }

    def _make_mr_response(
        self, base_sha="base000", head_sha="head111", start_sha="start222"
    ):
        """Build a minimal GitLab MR GET response with diff_refs."""
        return {
            "iid": 5,
            "diff_refs": {
                "base_sha": base_sha,
                "head_sha": head_sha,
                "start_sha": start_sha,
            },
        }

    def test_gitlab_general_comment_posts_to_notes_endpoint(self):
        """General comment (no file_path) POSTs to .../merge_requests/{number}/notes."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_note_response(note_id=5001)

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="Looks good!",
            )

        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "merge_requests/5/notes" in call_url

    def test_gitlab_general_comment_returns_comment_id_and_url(self):
        """General comment returns dict with comment_id and url."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        expected_url = "https://gitlab.com/owner/repo/-/merge_requests/5#note_5001"
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_note_response(
            note_id=5001, web_url=expected_url
        )

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response):
            result = client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="LGTM",
            )

        assert result["comment_id"] == 5001
        assert result["url"] == expected_url

    def test_gitlab_inline_comment_fetches_diff_refs_first(self):
        """Inline comment GETs MR to fetch diff_refs before POSTing."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_mr_response = MagicMock()
        mock_mr_response.status_code = 200
        mock_mr_response.json.return_value = self._make_mr_response(
            base_sha="base000", head_sha="head111", start_sha="start222"
        )

        mock_note_response = MagicMock()
        mock_note_response.status_code = 201
        mock_note_response.json.return_value = self._make_note_response(note_id=5002)

        client = GitLabForgeClient()
        with (
            patch("httpx.get", return_value=mock_mr_response) as mock_get,
            patch("httpx.post", return_value=mock_note_response),
        ):
            client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="Check this line",
                file_path="src/auth.py",
                line_number=30,
            )

        # Should have called GET to fetch MR diff_refs first
        mock_get.assert_called_once()
        get_url = mock_get.call_args[0][0]
        assert "merge_requests/5" in get_url

    def test_gitlab_inline_comment_payload_contains_position(self):
        """Inline comment payload includes full position object with diff_refs and file info."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_mr_response = MagicMock()
        mock_mr_response.status_code = 200
        mock_mr_response.json.return_value = self._make_mr_response(
            base_sha="base000", head_sha="head111", start_sha="start222"
        )

        mock_note_response = MagicMock()
        mock_note_response.status_code = 201
        mock_note_response.json.return_value = self._make_note_response(note_id=5002)

        client = GitLabForgeClient()
        with (
            patch("httpx.get", return_value=mock_mr_response),
            patch("httpx.post", return_value=mock_note_response) as mock_post,
        ):
            client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="Wrong indentation",
                file_path="src/models.py",
                line_number=88,
            )

        post_kwargs = mock_post.call_args[1]
        payload = post_kwargs.get("json", {})
        assert payload["body"] == "Wrong indentation"
        position = payload["position"]
        assert position["base_sha"] == "base000"
        assert position["head_sha"] == "head111"
        assert position["start_sha"] == "start222"
        assert position["new_path"] == "src/models.py"
        assert position["new_line"] == 88
        assert position["position_type"] == "text"

    def test_gitlab_inline_comment_returns_comment_id_and_url(self):
        """Inline comment returns dict with comment_id and url."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        expected_url = "https://gitlab.com/owner/repo/-/merge_requests/5#note_5002"
        mock_mr_response = MagicMock()
        mock_mr_response.status_code = 200
        mock_mr_response.json.return_value = self._make_mr_response()

        mock_note_response = MagicMock()
        mock_note_response.status_code = 201
        mock_note_response.json.return_value = self._make_note_response(
            note_id=5002, web_url=expected_url
        )

        client = GitLabForgeClient()
        with (
            patch("httpx.get", return_value=mock_mr_response),
            patch("httpx.post", return_value=mock_note_response),
        ):
            result = client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="Check this",
                file_path="src/auth.py",
                line_number=10,
            )

        assert result["comment_id"] == 5002
        assert result["url"] == expected_url

    def test_gitlab_general_comment_does_not_call_get(self):
        """General comment (no file_path) does not GET the MR to fetch diff_refs."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_note_response()

        client = GitLabForgeClient()
        with (
            patch("httpx.get") as mock_get,
            patch("httpx.post", return_value=mock_response),
        ):
            client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                body="general comment",
            )

        mock_get.assert_not_called()

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
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.comment_on_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                    body="test comment",
                )

    def test_gitlab_403_raises_forge_authentication_error(self):
        """HTTP 403 raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.comment_on_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                    body="test comment",
                )

    def test_gitlab_uses_url_encoded_project_path(self):
        """GitLab notes API uses URL-encoded project path."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = self._make_note_response()

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.comment_on_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=5,
                body="test comment",
            )

        call_url = mock_post.call_args[0][0]
        assert "%2F" in call_url
