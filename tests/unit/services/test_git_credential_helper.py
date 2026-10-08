"""
Unit tests for GitCredentialHelper service.

Story #387: PAT-Authenticated Git Push with User Attribution & Security Hardening

Tests cover:
- convert_ssh_to_https: SSH formats, HTTPS passthrough, edge cases
- extract_host_from_remote_url: SSH, HTTPS, ssh:// format, invalid URL

(The PAT reaches git at run time through the push environment; see
tests/unit/services/test_git_push_single_path.py.)
"""

from code_indexer.server.services.git_credential_helper import GitCredentialHelper


class TestConvertSshToHttps:
    """Tests for GitCredentialHelper.convert_ssh_to_https."""

    def test_standard_github_ssh_url(self):
        """Converts git@github.com:owner/repo.git to https://github.com/owner/repo.git."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "git@github.com:owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_standard_gitlab_ssh_url(self):
        """Converts git@gitlab.com:owner/repo.git to https://gitlab.com/owner/repo.git."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "git@gitlab.com:owner/repo.git"
        )
        assert result == "https://gitlab.com/owner/repo.git"

    def test_gitlab_subgroup_ssh_url(self):
        """Handles GitLab subgroups in SSH URL path."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "git@gitlab.com:group/subgroup/repo.git"
        )
        assert result == "https://gitlab.com/group/subgroup/repo.git"

    def test_ssh_url_without_git_extension(self):
        """Handles SSH URLs without .git extension."""
        result = GitCredentialHelper.convert_ssh_to_https("git@github.com:owner/repo")
        assert result == "https://github.com/owner/repo"

    def test_https_url_passes_through(self):
        """HTTPS URLs are returned unchanged."""
        url = "https://github.com/owner/repo.git"
        assert GitCredentialHelper.convert_ssh_to_https(url) == url

    def test_https_with_credentials_passes_through(self):
        """HTTPS URLs with embedded credentials are returned unchanged."""
        url = "https://user:token@github.com/owner/repo.git"
        assert GitCredentialHelper.convert_ssh_to_https(url) == url

    def test_ssh_protocol_url(self):
        """Converts ssh://git@host/path format."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "ssh://git@github.com/owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_ssh_protocol_url_with_port(self):
        """Converts ssh://git@host:port/path format."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "ssh://git@github.com:22/owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_custom_self_hosted_host(self):
        """Handles custom self-hosted git hosts."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "git@git.mycompany.com:team/project.git"
        )
        assert result == "https://git.mycompany.com/team/project.git"

    def test_leading_whitespace_stripped(self):
        """Strips leading/trailing whitespace from URL before processing."""
        result = GitCredentialHelper.convert_ssh_to_https(
            "  git@github.com:owner/repo.git  "
        )
        assert result == "https://github.com/owner/repo.git"


class TestExtractHostFromRemoteUrl:
    """Tests for GitCredentialHelper.extract_host_from_remote_url."""

    def test_ssh_format_extracts_host(self):
        """Extracts host from git@host:path SSH URL."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "git@github.com:owner/repo.git"
        )
        assert host == "github.com"

    def test_https_format_extracts_host(self):
        """Extracts host from https://host/path URL."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "https://github.com/owner/repo.git"
        )
        assert host == "github.com"

    def test_http_format_extracts_host(self):
        """Extracts host from http://host/path URL."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "http://gitlab.com/owner/repo.git"
        )
        assert host == "gitlab.com"

    def test_ssh_protocol_format_extracts_host(self):
        """Extracts host from ssh://git@host/path URL."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "ssh://git@github.com/owner/repo.git"
        )
        assert host == "github.com"

    def test_ssh_protocol_with_port_extracts_host(self):
        """Extracts host from ssh://git@host:port/path URL (no port in result)."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "ssh://git@github.com:22/owner/repo.git"
        )
        assert host == "github.com"

    def test_self_hosted_host(self):
        """Extracts host from custom domain."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "git@git.mycompany.com:team/repo.git"
        )
        assert host == "git.mycompany.com"

    def test_invalid_url_returns_none(self):
        """Returns None for URLs that cannot be parsed."""
        assert GitCredentialHelper.extract_host_from_remote_url("not-a-url") is None

    def test_empty_string_returns_none(self):
        """Returns None for empty string."""
        assert GitCredentialHelper.extract_host_from_remote_url("") is None

    def test_gitlab_subgroup_https(self):
        """Extracts host from GitLab HTTPS URL with subgroups."""
        host = GitCredentialHelper.extract_host_from_remote_url(
            "https://gitlab.com/group/subgroup/repo.git"
        )
        assert host == "gitlab.com"
