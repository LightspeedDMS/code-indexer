"""
Unit tests for forge_client PR/MR creation.

Story #390: Pull/Merge Request Creation via MCP

Tests:
  - detect_forge_type: auto-detect GitHub or GitLab from remote URL
  - extract_owner_repo: parse owner and repo from remote URL
  - GitHubForgeClient.create_pull_request: create GitHub PR via REST API
  - GitLabForgeClient.create_merge_request: create GitLab MR via REST API
"""

import pytest
import httpx
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# detect_forge_type
# ---------------------------------------------------------------------------


class TestDetectForgeType:
    """Tests for detect_forge_type utility function."""

    def test_detect_github_https(self):
        """https://github.com URL returns 'github'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("https://github.com/owner/repo.git") == "github"

    def test_detect_github_ssh(self):
        """git@github.com SSH URL returns 'github'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("git@github.com:owner/repo.git") == "github"

    def test_detect_gitlab_https(self):
        """https://gitlab.com URL returns 'gitlab'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("https://gitlab.com/owner/repo.git") == "gitlab"

    def test_detect_gitlab_ssh(self):
        """git@gitlab.com SSH URL returns 'gitlab'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("git@gitlab.com:group/repo.git") == "gitlab"

    def test_detect_unknown_returns_none(self):
        """Unknown host returns None."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("https://bitbucket.org/owner/repo.git") is None

    def test_detect_github_enterprise(self):
        """GitHub Enterprise host containing 'github' returns 'github'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("https://github.corp.com/owner/repo.git") == "github"

    def test_detect_gitlab_self_hosted(self):
        """Self-hosted GitLab host containing 'gitlab' returns 'gitlab'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        assert detect_forge_type("https://gitlab.corp.com/owner/repo.git") == "gitlab"

    def test_detect_no_false_positive_on_url_path(self):
        """'github' in URL path (not hostname) must NOT return 'github'."""
        from code_indexer.server.clients.forge_client import detect_forge_type

        # The path contains 'github-tools' but the hostname is 'internal.corp'
        assert detect_forge_type("https://internal.corp/github-tools/repo.git") is None


# ---------------------------------------------------------------------------
# extract_owner_repo
# ---------------------------------------------------------------------------


class TestExtractOwnerRepo:
    """Tests for extract_owner_repo utility function."""

    def test_github_https_simple(self):
        """HTTPS GitHub URL extracts simple owner/repo."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        owner, repo = extract_owner_repo("https://github.com/myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_github_ssh_simple(self):
        """SSH GitHub URL extracts owner and repo."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        owner, repo = extract_owner_repo("git@github.com:myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_github_https_no_dot_git(self):
        """HTTPS URL without .git suffix still parsed correctly."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        owner, repo = extract_owner_repo("https://github.com/myorg/myrepo")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_gitlab_ssh_subgroup(self):
        """GitLab SSH URL with subgroup: owner is group/subgroup."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        owner, repo = extract_owner_repo("git@gitlab.com:group/subgroup/myrepo.git")
        assert owner == "group/subgroup"
        assert repo == "myrepo"

    def test_gitlab_https_subgroup(self):
        """GitLab HTTPS URL with subgroup: owner is group/subgroup."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        owner, repo = extract_owner_repo("https://gitlab.com/group/subgroup/myrepo.git")
        assert owner == "group/subgroup"
        assert repo == "myrepo"

    def test_invalid_url_raises_value_error(self):
        """Completely unparseable URL raises ValueError."""
        from code_indexer.server.clients.forge_client import extract_owner_repo

        with pytest.raises(ValueError):
            extract_owner_repo("not-a-url-at-all")


# ---------------------------------------------------------------------------
# GitHubForgeClient.create_pull_request
# ---------------------------------------------------------------------------


class TestGitHubForgeClientCreatePR:
    """Tests for GitHubForgeClient.create_pull_request (sync, Story #390 AC1)."""

    def test_create_pr_github_com_success(self):
        """Successful GitHub PR creation returns url and number."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.com/owner/repo/pull/42",
            "number": 42,
        }

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            result = client.create_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                title="My PR",
                body="Description",
                head="feature-branch",
                base="main",
            )

        assert result["url"] == "https://github.com/owner/repo/pull/42"
        assert result["number"] == 42

    def test_create_pr_uses_correct_api_url_for_github_com(self):
        """github.com uses api.github.com endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.com/owner/repo/pull/1",
            "number": 1,
        }

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_pull_request(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                title="Test",
                body="",
                head="feature",
                base="main",
            )
            call_args = mock_post.call_args
            assert "api.github.com" in call_args[0][0]

    def test_create_pr_uses_correct_api_url_for_ghe(self):
        """GitHub Enterprise uses {host}/api/v3 endpoint."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.corp.com/owner/repo/pull/5",
            "number": 5,
        }

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_pull_request(
                token="ghp_testtoken",
                host="github.corp.com",
                owner="owner",
                repo="repo",
                title="Test",
                body="",
                head="feature",
                base="main",
            )
            call_args = mock_post.call_args
            assert "github.corp.com/api/v3" in call_args[0][0]

    def test_create_pr_401_raises_forge_auth_error(self):
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
                client.create_pull_request(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    head="feature",
                    base="main",
                )

    def test_create_pr_403_raises_forge_auth_error(self):
        """HTTP 403 raises ForgeAuthenticationError with permissions message."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError, match="403"):
                client.create_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    head="feature",
                    base="main",
                )

    def test_create_pr_422_raises_value_error(self):
        """HTTP 422 (e.g. PR already exists) raises ValueError."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = '{"message":"Validation Failed","errors":[{"message":"A pull request already exists"}]}'

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ValueError, match="422"):
                client.create_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    head="feature",
                    base="main",
                )

    def test_create_pr_network_error_raises(self):
        """Network errors propagate as exceptions."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        client = GitHubForgeClient()
        with patch("httpx.post", side_effect=httpx.ConnectError("Connection refused")):
            with pytest.raises(Exception):
                client.create_pull_request(
                    token="ghp_testtoken",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    head="feature",
                    base="main",
                )

    def test_create_pr_sends_correct_headers_and_payload(self):
        """Correct Authorization header and payload fields are sent."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.com/owner/repo/pull/7",
            "number": 7,
        }

        client = GitHubForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_pull_request(
                token="ghp_mytoken",
                host="github.com",
                owner="owner",
                repo="repo",
                title="Fix bug",
                body="Detailed description",
                head="fix/bug-123",
                base="main",
            )
            call_kwargs = mock_post.call_args[1]
            headers = call_kwargs["headers"]
            payload = call_kwargs["json"]

            assert "ghp_mytoken" in headers.get("Authorization", "")
            assert payload["title"] == "Fix bug"
            assert payload["body"] == "Detailed description"
            assert payload["head"] == "fix/bug-123"
            assert payload["base"] == "main"


# ---------------------------------------------------------------------------
# GitLabForgeClient.create_merge_request
# ---------------------------------------------------------------------------


class TestGitLabForgeClientCreateMR:
    """Tests for GitLabForgeClient.create_merge_request (sync, Story #390 AC2)."""

    def test_create_mr_gitlab_com_success(self):
        """Successful GitLab MR creation returns url and number."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "web_url": "https://gitlab.com/owner/repo/-/merge_requests/7",
            "iid": 7,
        }

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response):
            result = client.create_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                title="My MR",
                body="Description",
                source_branch="feature-branch",
                target_branch="main",
            )

        assert result["url"] == "https://gitlab.com/owner/repo/-/merge_requests/7"
        assert result["number"] == 7

    def test_create_mr_uses_url_encoded_project_path(self):
        """Project path is URL-encoded in the API URL."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/1",
            "iid": 1,
        }

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_merge_request(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/sub",
                repo="repo",
                title="Test",
                body="",
                source_branch="feature",
                target_branch="main",
            )
            call_url = mock_post.call_args[0][0]
            # URL-encoded slash: %2F
            assert "%2F" in call_url

    def test_create_mr_401_raises_forge_auth_error(self):
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
                client.create_merge_request(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    source_branch="feature",
                    target_branch="main",
                )

    def test_create_mr_403_raises_forge_auth_error(self):
        """HTTP 403 raises ForgeAuthenticationError with permissions message."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError, match="403"):
                client.create_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    source_branch="feature",
                    target_branch="main",
                )

    def test_create_mr_409_conflict_raises_value_error(self):
        """HTTP 409 (MR already exists) raises ValueError."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.text = "Another open merge request already exists"

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response):
            with pytest.raises(ValueError, match="409"):
                client.create_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    source_branch="feature",
                    target_branch="main",
                )

    def test_create_mr_network_error_raises(self):
        """Network errors propagate as exceptions."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        client = GitLabForgeClient()
        with patch("httpx.post", side_effect=httpx.ConnectError("Connection refused")):
            with pytest.raises(Exception):
                client.create_merge_request(
                    token="glpat-testtoken",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    title="Test",
                    body="",
                    source_branch="feature",
                    target_branch="main",
                )

    def test_create_mr_sends_correct_headers_and_payload(self):
        """Correct PRIVATE-TOKEN header and payload fields are sent."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "web_url": "https://gitlab.com/owner/repo/-/merge_requests/3",
            "iid": 3,
        }

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_merge_request(
                token="glpat-mytoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                title="Add feature",
                body="Implements AC1",
                source_branch="feat/new-thing",
                target_branch="develop",
            )
            call_kwargs = mock_post.call_args[1]
            headers = call_kwargs["headers"]
            payload = call_kwargs["json"]

            assert headers.get("PRIVATE-TOKEN") == "glpat-mytoken"
            assert payload["title"] == "Add feature"
            assert payload["description"] == "Implements AC1"
            assert payload["source_branch"] == "feat/new-thing"
            assert payload["target_branch"] == "develop"

    def test_create_mr_self_hosted_uses_correct_host(self):
        """Self-hosted GitLab uses the provided host in the API URL."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "web_url": "https://gitlab.corp.com/owner/repo/-/merge_requests/2",
            "iid": 2,
        }

        client = GitLabForgeClient()
        with patch("httpx.post", return_value=mock_response) as mock_post:
            client.create_merge_request(
                token="glpat-testtoken",
                host="gitlab.corp.com",
                owner="owner",
                repo="repo",
                title="Test",
                body="",
                source_branch="feature",
                target_branch="main",
            )
            call_url = mock_post.call_args[0][0]
            assert "gitlab.corp.com" in call_url
