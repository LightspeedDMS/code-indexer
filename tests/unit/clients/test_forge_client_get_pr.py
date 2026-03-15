"""
Unit tests for forge_client get_pull_request / get_merge_request methods.

Story #447: get_pull_request - Get full PR/MR details

Tests:
  - GitHubForgeClient.get_pull_request: full GitHub PR response mapped correctly
  - GitLabForgeClient.get_merge_request: full GitLab MR response mapped correctly
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.get_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientGetPR:
    """Tests for GitHubForgeClient.get_pull_request (sync, Story #447)."""

    def _make_github_pr(
        self,
        number=42,
        title="My PR",
        state="open",
        login="octocat",
        body="PR description",
        head_ref="feature/x",
        base_ref="main",
        html_url=None,
        created_at="2026-03-10T14:30:00Z",
        updated_at="2026-03-12T09:15:00Z",
        labels=None,
        requested_reviewers=None,
        mergeable=True,
        mergeable_state="clean",
        additions=150,
        deletions=30,
        changed_files=5,
    ):
        """Build a minimal GitHub PR API response dict."""
        return {
            "number": number,
            "title": title,
            "state": state,
            "body": body,
            "user": {"login": login},
            "head": {"ref": head_ref},
            "base": {"ref": base_ref},
            "html_url": html_url or f"https://github.com/owner/repo/pull/{number}",
            "created_at": created_at,
            "updated_at": updated_at,
            "labels": labels if labels is not None else [],
            "requested_reviewers": requested_reviewers
            if requested_reviewers is not None
            else [],
            "mergeable": mergeable,
            "mergeable_state": mergeable_state,
            "additions": additions,
            "deletions": deletions,
            "changed_files": changed_files,
        }

    def test_github_get_pr(self):
        """get_pull_request returns fully normalized PR dict from GitHub API."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr(
            number=42,
            title="My PR",
            state="open",
            login="octocat",
            body="PR description",
            head_ref="feature/x",
            base_ref="main",
            html_url="https://github.com/owner/repo/pull/42",
            created_at="2026-03-10T14:30:00Z",
            updated_at="2026-03-12T09:15:00Z",
            labels=[{"name": "bug"}, {"name": "urgent"}],
            requested_reviewers=[{"login": "reviewer1"}],
            mergeable=True,
            mergeable_state="clean",
            additions=150,
            deletions=30,
            changed_files=5,
        )

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["number"] == 42
        assert result["title"] == "My PR"
        assert result["description"] == "PR description"
        assert result["state"] == "open"
        assert result["author"] == "octocat"
        assert result["source_branch"] == "feature/x"
        assert result["target_branch"] == "main"
        assert result["url"] == "https://github.com/owner/repo/pull/42"
        assert result["labels"] == ["bug", "urgent"]
        assert result["reviewers"] == ["reviewer1"]
        assert result["mergeable"] is True
        assert result["ci_status"] == "clean"
        assert result["diff_stats"]["additions"] == 150
        assert result["diff_stats"]["deletions"] == 30
        assert result["diff_stats"]["changed_files"] == 5
        assert result["created_at"] == "2026-03-10T14:30:00Z"
        assert result["updated_at"] == "2026-03-12T09:15:00Z"

    def test_github_get_pr_labels_extracted(self):
        """Labels are extracted from GitHub [{name: 'x'}] format to flat list."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr(
            labels=[
                {"name": "enhancement"},
                {"name": "help wanted"},
                {"name": "good first issue"},
            ]
        )

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["labels"] == ["enhancement", "help wanted", "good first issue"]

    def test_github_get_pr_reviewers_extracted(self):
        """Reviewers extracted from requested_reviewers[].login list."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr(
            requested_reviewers=[
                {"login": "alice"},
                {"login": "bob"},
            ]
        )

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["reviewers"] == ["alice", "bob"]

    def test_github_get_pr_null_mergeable(self):
        """mergeable=None (GitHub pending check) is handled gracefully."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr(mergeable=None)

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        # None should be preserved (not coerced to True/False)
        assert result["mergeable"] is None

    def test_github_get_pr_empty_labels_and_reviewers(self):
        """Empty labels and reviewers return empty lists."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr(
            labels=[], requested_reviewers=[]
        )

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert result["labels"] == []
        assert result["reviewers"] == []

    def test_github_get_pr_uses_correct_api_url(self):
        """github.com uses api.github.com/repos/.../pulls/{number} endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr()

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.get_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )
            call_url = mock_get.call_args[0][0]
            assert "api.github.com" in call_url
            assert "/repos/owner/repo/pulls/42" in call_url

    def test_github_get_pr_enterprise_uses_correct_api_url(self):
        """GitHub Enterprise uses {host}/api/v3/repos/.../pulls/{number} endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_github_pr()

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.get_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                number=42,
            )
            call_url = mock_get.call_args[0][0]
            assert "github.corp.com/api/v3" in call_url
            assert "/repos/owner/repo/pulls/42" in call_url

    def test_github_get_pr_404(self):
        """HTTP 404 from GitHub raises ValueError with 'not found' message."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="not found"):
                client.get_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=9999,
                )

    def test_github_get_pr_401(self):
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
                client.get_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_get_pr_403(self):
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
                client.get_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_get_pr_other_error(self):
        """Non-2xx non-401/403/404 from GitHub raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="500"):
                client.get_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )


# ---------------------------------------------------------------------------
# GitLabForgeClient.get_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientGetMR:
    """Tests for GitLabForgeClient.get_merge_request (sync, Story #447)."""

    def _make_gitlab_mr(
        self,
        iid=7,
        title="My MR",
        state="opened",
        username="john_doe",
        description="MR description",
        source_branch="feature/x",
        target_branch="main",
        web_url=None,
        created_at="2026-03-10T14:30:00.000Z",
        updated_at="2026-03-12T09:15:00.000Z",
        labels=None,
        reviewers=None,
        merge_status="can_be_merged",
        head_pipeline=None,
        additions=80,
        deletions=20,
        changed_files=3,
    ):
        """Build a minimal GitLab MR API response dict."""
        return {
            "iid": iid,
            "title": title,
            "state": state,
            "description": description,
            "author": {"username": username},
            "source_branch": source_branch,
            "target_branch": target_branch,
            "web_url": web_url
            or f"https://gitlab.com/owner/repo/-/merge_requests/{iid}",
            "created_at": created_at,
            "updated_at": updated_at,
            "labels": labels if labels is not None else [],
            "reviewers": reviewers if reviewers is not None else [],
            "merge_status": merge_status,
            "head_pipeline": head_pipeline,
            "changes_count": str(changed_files),
            "diff_stats": {
                "additions": additions,
                "deletions": deletions,
                "changes": changed_files,
            },
        }

    def test_gitlab_get_mr(self):
        """get_merge_request returns fully normalized MR dict from GitLab API."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(
            iid=7,
            title="My MR",
            state="opened",
            username="john_doe",
            description="MR description",
            source_branch="feature/x",
            target_branch="main",
            web_url="https://gitlab.com/owner/repo/-/merge_requests/7",
            labels=["bug", "urgent"],
            reviewers=[{"username": "reviewer1"}],
            merge_status="can_be_merged",
            head_pipeline={"status": "success"},
            additions=80,
            deletions=20,
            changed_files=3,
        )

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["number"] == 7
        assert result["title"] == "My MR"
        assert result["description"] == "MR description"
        assert result["state"] == "open"  # normalized from "opened"
        assert result["author"] == "john_doe"
        assert result["source_branch"] == "feature/x"
        assert result["target_branch"] == "main"
        assert result["url"] == "https://gitlab.com/owner/repo/-/merge_requests/7"
        assert result["labels"] == ["bug", "urgent"]
        assert result["reviewers"] == ["reviewer1"]
        assert result["mergeable"] is True
        assert result["ci_status"] == "success"
        assert result["diff_stats"]["additions"] == 80
        assert result["diff_stats"]["deletions"] == 20
        assert result["created_at"] == "2026-03-10T14:30:00.000Z"
        assert result["updated_at"] == "2026-03-12T09:15:00.000Z"

    def test_gitlab_get_mr_state_normalized(self):
        """GitLab 'opened' state in response is normalized to 'open' in output."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(state="opened")

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["state"] == "open"

    def test_gitlab_get_mr_state_merged_not_changed(self):
        """GitLab 'merged' state is passed through unchanged."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(state="merged")

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["state"] == "merged"

    def test_gitlab_get_mr_ci_status(self):
        """head_pipeline.status is extracted as ci_status."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(
            head_pipeline={"status": "failed"}
        )

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["ci_status"] == "failed"

    def test_gitlab_get_mr_no_pipeline(self):
        """head_pipeline=None results in ci_status=None."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(head_pipeline=None)

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["ci_status"] is None

    def test_gitlab_get_mr_mergeable_false_when_cannot_merge(self):
        """merge_status != 'can_be_merged' results in mergeable=False."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(
            merge_status="cannot_be_merged"
        )

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["mergeable"] is False

    def test_gitlab_get_mr_reviewers_extracted(self):
        """Reviewers extracted from reviewers[].username list."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr(
            reviewers=[
                {"username": "alice"},
                {"username": "bob"},
            ]
        )

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=7,
            )

        assert result["reviewers"] == ["alice", "bob"]

    def test_gitlab_get_mr_uses_url_encoded_project_path(self):
        """GitLab get MR uses URL-encoded project path in API URL."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = self._make_gitlab_mr()

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.get_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=7,
            )
            call_url = mock_get.call_args[0][0]
            # URL-encoded slash: %2F
            assert "%2F" in call_url
            assert "/merge_requests/7" in call_url

    def test_gitlab_get_mr_404(self):
        """HTTP 404 from GitLab raises ValueError with 'not found' message."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="not found"):
                client.get_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=9999,
                )

    def test_gitlab_get_mr_401(self):
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
                client.get_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=7,
                )

    def test_gitlab_get_mr_other_error(self):
        """Non-2xx non-401/403/404 from GitLab raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ValueError, match="500"):
                client.get_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=7,
                )
