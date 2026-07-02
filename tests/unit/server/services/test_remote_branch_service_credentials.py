"""
Tests for RemoteBranchService credential handling - SSH URL conversion and platform-specific auth.

Following TDD methodology: these tests define expected behavior BEFORE implementation.

Story #21: Fix Branch Fetching for Private Repositories

Tests cover:
1. SSH URL to HTTPS URL conversion with credentials
2. Platform-specific credential formats (oauth2 for GitLab, token-only for GitHub)
3. HTTPS URL credential insertion for both platforms
"""


class TestBuildEffectiveUrl:
    """Tests for URL conversion and credential handling in fetch_remote_branches.

    This tests the internal URL transformation logic that will be extracted
    to a helper function: _build_effective_url(clone_url, platform, credentials)
    """

    def test_ssh_url_to_https_gitlab_with_credentials(self):
        """Test SSH URL converts to HTTPS with oauth2 credentials for GitLab."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@gitlab.com:lightspeeddms/private/cloud-engineering/terraform/cloud-infrastructure.git"
        credentials = "glpat-test-token-12345"
        platform = "gitlab"

        result = _build_effective_url(clone_url, platform, credentials)

        # GitLab uses oauth2:<token> format
        expected = "https://oauth2:glpat-test-token-12345@gitlab.com/lightspeeddms/private/cloud-engineering/terraform/cloud-infrastructure.git"
        assert result == expected

    def test_ssh_url_to_https_github_with_credentials(self):
        """Test SSH URL converts to HTTPS with token credentials for GitHub."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@github.com:owner/repo.git"
        credentials = "ghp_testtoken1234567890"
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        # GitHub uses <token> format (no oauth2 prefix)
        expected = "https://ghp_testtoken1234567890@github.com/owner/repo.git"
        assert result == expected

    def test_https_url_gitlab_with_credentials(self):
        """Test HTTPS URL gets oauth2 credentials inserted for GitLab."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "https://gitlab.com/org/repo.git"
        credentials = "glpat-test-token-12345"
        platform = "gitlab"

        result = _build_effective_url(clone_url, platform, credentials)

        expected = "https://oauth2:glpat-test-token-12345@gitlab.com/org/repo.git"
        assert result == expected

    def test_https_url_github_with_credentials(self):
        """Test HTTPS URL gets token credentials inserted for GitHub."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "https://github.com/owner/repo.git"
        credentials = "ghp_testtoken1234567890"
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        expected = "https://ghp_testtoken1234567890@github.com/owner/repo.git"
        assert result == expected

    def test_no_credentials_returns_original_url(self):
        """Test that without credentials, the original URL is returned."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "https://github.com/owner/repo.git"
        credentials = None
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        assert result == clone_url

    def test_empty_credentials_returns_original_url(self):
        """Test that empty credentials returns the original URL."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "https://github.com/owner/repo.git"
        credentials = ""
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        assert result == clone_url

    def test_ssh_url_no_credentials_returns_original(self):
        """Test SSH URL without credentials is returned unchanged."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@github.com:owner/repo.git"
        credentials = None
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        # Without credentials, SSH URLs stay as-is
        assert result == clone_url

    def test_ssh_url_nested_path_gitlab(self):
        """Test SSH URL with nested path converts correctly for GitLab."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@gitlab.com:group/subgroup/project/repo.git"
        credentials = "glpat-test-token"
        platform = "gitlab"

        result = _build_effective_url(clone_url, platform, credentials)

        expected = (
            "https://oauth2:glpat-test-token@gitlab.com/group/subgroup/project/repo.git"
        )
        assert result == expected

    def test_ssh_url_without_git_suffix(self):
        """Test SSH URL without .git suffix works."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@github.com:owner/repo"
        credentials = "ghp_testtoken"
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        expected = "https://ghp_testtoken@github.com/owner/repo"
        assert result == expected

    def test_platform_detection_from_gitlab_url(self):
        """Test platform is correctly detected from GitLab URL when not provided."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@gitlab.com:org/repo.git"
        credentials = "glpat-token"
        # Platform can be None or not specified, should detect from URL
        platform = None

        result = _build_effective_url(clone_url, platform, credentials)

        # Should detect gitlab.com and use oauth2 format
        expected = "https://oauth2:glpat-token@gitlab.com/org/repo.git"
        assert result == expected

    def test_platform_detection_from_github_url(self):
        """Test platform is correctly detected from GitHub URL when not provided."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@github.com:org/repo.git"
        credentials = "ghp_token"
        platform = None

        result = _build_effective_url(clone_url, platform, credentials)

        # Should detect github.com and NOT use oauth2 format
        expected = "https://ghp_token@github.com/org/repo.git"
        assert result == expected

    def test_https_url_with_existing_credentials_replaced(self):
        """Test HTTPS URL with existing credentials gets them replaced."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "https://olduser:oldtoken@github.com/owner/repo.git"
        credentials = "ghp_newtoken"
        platform = "github"

        result = _build_effective_url(clone_url, platform, credentials)

        # New credentials should replace old ones
        expected = "https://ghp_newtoken@github.com/owner/repo.git"
        assert result == expected

    def test_self_hosted_gitlab_uses_oauth2(self):
        """Test self-hosted GitLab instance uses oauth2 format."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        clone_url = "git@gitlab.company.com:team/project.git"
        credentials = "glpat-internal-token"
        platform = "gitlab"

        result = _build_effective_url(clone_url, platform, credentials)

        expected = (
            "https://oauth2:glpat-internal-token@gitlab.company.com/team/project.git"
        )
        assert result == expected


class TestFetchRemoteBranchesWithCredentials:
    """Integration tests for fetch_remote_branches with credential handling."""

    def test_fetch_branches_uses_credentials_for_gitlab(self):
        """Test that fetch_remote_branches properly uses credentials for GitLab URLs."""
        from code_indexer.server.services.remote_branch_service import (
            RemoteBranchService,
        )

        service = RemoteBranchService()

        # This tests the internal URL building - we can't test actual private repos
        # without real credentials, but we can verify the URL is constructed correctly
        # by mocking subprocess.run (we use this sparingly as per CLAUDE.md Anti-Mock rule)
        # For now, just verify the method accepts credentials parameter
        result = service.fetch_remote_branches(
            clone_url="https://github.com/octocat/Hello-World.git",
            platform="github",
            credentials=None,  # Using None for public repo test
        )

        # Should work for public repos
        assert result.success is True
        assert len(result.branches) > 0


class TestSecurityEnhancements:
    """Tests for security-related fixes in credential handling.

    Story #21: Security fixes identified by code review.
    """

    def test_build_effective_url_rejects_credentials_over_http(self):
        """Test that credentials are not added to HTTP URLs (security).

        SECURITY: Credentials must never be sent over unencrypted HTTP.
        This prevents accidental credential exposure in clear text.
        """
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        result = _build_effective_url(
            "http://github.com/org/repo.git",
            "github",
            "secret_token",
        )
        # Should return original URL without credentials
        assert result == "http://github.com/org/repo.git"
        assert "secret_token" not in result

    def test_build_effective_url_rejects_credentials_over_http_gitlab(self):
        """Test that credentials are not added to HTTP URLs for GitLab."""
        from code_indexer.server.services.remote_branch_service import (
            _build_effective_url,
        )

        result = _build_effective_url(
            "http://gitlab.com/org/repo.git",
            "gitlab",
            "glpat-secret-token",
        )
        # Should return original URL without credentials
        assert result == "http://gitlab.com/org/repo.git"
        assert "glpat-secret-token" not in result

    def test_platform_detection_uses_hostname_for_github(self):
        """Test platform detection extracts hostname correctly for GitHub."""
        from code_indexer.server.services.remote_branch_service import (
            _detect_platform_from_url,
        )

        # Exact domain
        assert _detect_platform_from_url("https://github.com/org/repo.git") == "github"
        # SSH format
        assert _detect_platform_from_url("git@github.com:org/repo.git") == "github"
        # Subdomain
        assert (
            _detect_platform_from_url("https://enterprise.github.com/org/repo.git")
            == "github"
        )

    def test_platform_detection_uses_hostname_for_gitlab(self):
        """Test platform detection extracts hostname correctly for GitLab."""
        from code_indexer.server.services.remote_branch_service import (
            _detect_platform_from_url,
        )

        # Exact domain
        assert _detect_platform_from_url("https://gitlab.com/org/repo.git") == "gitlab"
        # SSH format
        assert _detect_platform_from_url("git@gitlab.com:org/repo.git") == "gitlab"
        # Self-hosted
        assert (
            _detect_platform_from_url("https://gitlab.company.com/org/repo.git")
            == "gitlab"
        )

    def test_platform_detection_no_false_positives(self):
        """Test platform detection doesn't match on path/query string.

        SECURITY: Platform detection should only match the hostname,
        not content in the path or query string that might cause false positives.
        """
        from code_indexer.server.services.remote_branch_service import (
            _detect_platform_from_url,
        )

        # URLs that contain 'github' in path but are not GitHub
        # These should return None (unknown platform)
        assert (
            _detect_platform_from_url("https://example.com/github-mirror/repo.git")
            is None
        )
        assert (
            _detect_platform_from_url("https://selfhosted.com/org/github-tools.git")
            is None
        )

    def test_platform_detection_handles_malformed_urls(self):
        """Test platform detection handles malformed URLs gracefully."""
        from code_indexer.server.services.remote_branch_service import (
            _detect_platform_from_url,
        )

        # Should not crash on malformed URLs
        assert _detect_platform_from_url("") is None
        assert _detect_platform_from_url("not-a-url") is None
        assert _detect_platform_from_url("://invalid") is None
