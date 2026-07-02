"""
Unit tests for GitCredentialHelper service.

Story #387: PAT-Authenticated Git Push with User Attribution & Security Hardening

Tests cover:
- create_askpass_script: file creation, content, permissions
- cleanup_askpass_script: removes file, safe on non-existent file
- convert_ssh_to_https: SSH formats, HTTPS passthrough, edge cases
- extract_host_from_remote_url: SSH, HTTPS, ssh:// format, invalid URL
"""

import stat
from pathlib import Path
from unittest.mock import patch


class TestCreateAskpassScript:
    """Tests for GitCredentialHelper.create_askpass_script."""

    def test_creates_file_at_tmp_dir(self, tmp_path):
        """create_askpass_script creates a file in the configured tmp_dir."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        script_path = helper.create_askpass_script("mytoken")
        try:
            assert script_path.exists()
            assert script_path.parent == tmp_path
        finally:
            helper.cleanup_askpass_script(script_path)

    def test_script_filename_is_unique(self, tmp_path):
        """create_askpass_script generates unique filenames across calls."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        path1 = helper.create_askpass_script("token1")
        path2 = helper.create_askpass_script("token2")
        try:
            assert path1 != path2
        finally:
            helper.cleanup_askpass_script(path1)
            helper.cleanup_askpass_script(path2)

    def test_script_content_echoes_token(self, tmp_path):
        """Script contains the token and uses printf to output it."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        token = "ghp_secrettoken123"
        script_path = helper.create_askpass_script(token)
        try:
            content = script_path.read_text()
            assert "#!/bin/sh" in content
            assert token in content
            assert "printf" in content
        finally:
            helper.cleanup_askpass_script(script_path)

    def test_script_has_0700_permissions(self, tmp_path):
        """Script is created with 0700 permissions (owner-only rwx)."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        script_path = helper.create_askpass_script("token123")
        try:
            mode = script_path.stat().st_mode
            # Check only permission bits
            perms = stat.S_IMODE(mode)
            assert perms == 0o700, f"Expected 0700 but got {oct(perms)}"
        finally:
            helper.cleanup_askpass_script(script_path)

    def test_script_not_readable_by_group_or_others(self, tmp_path):
        """Script has no group or other read permissions."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        script_path = helper.create_askpass_script("secret")
        try:
            mode = script_path.stat().st_mode
            assert not (mode & stat.S_IRGRP), "Group read bit should not be set"
            assert not (mode & stat.S_IROTH), "Other read bit should not be set"
        finally:
            helper.cleanup_askpass_script(script_path)

    def test_default_tmp_dir_is_home_tmp(self):
        """Default tmp_dir is ~/.tmp."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper()
        assert helper.tmp_dir == Path.home() / ".tmp"

    def test_creates_tmp_dir_if_missing(self, tmp_path):
        """Creates tmp_dir if it does not exist."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        new_dir = tmp_path / "nested" / "subdir"
        assert not new_dir.exists()
        helper = GitCredentialHelper(tmp_dir=new_dir)
        assert new_dir.exists()
        # Clean up any script if created during init
        script = helper.create_askpass_script("token")
        helper.cleanup_askpass_script(script)

    def test_script_handles_token_with_shell_metacharacters(self, tmp_path):
        """Token with single quotes, backticks, and $(cmd) is echoed literally."""
        import subprocess

        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        dangerous_token = "abc'$(rm -rf ~)'`whoami`"
        script_path = helper.create_askpass_script(dangerous_token)

        # Execute the script and verify it outputs the exact token
        result = subprocess.run(
            ["sh", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == dangerous_token
        assert result.returncode == 0
        helper.cleanup_askpass_script(script_path)

    def test_script_handles_token_with_heredoc_delimiter(self, tmp_path):
        """Token containing heredoc delimiter strings is echoed literally."""
        import subprocess

        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        # Token that would break a heredoc-based approach
        delimiter_token = "before\n__CIDX_EOF__\nafter"
        script_path = helper.create_askpass_script(delimiter_token)

        result = subprocess.run(
            ["sh", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == delimiter_token
        assert result.returncode == 0
        helper.cleanup_askpass_script(script_path)


class TestCleanupAskpassScript:
    """Tests for GitCredentialHelper.cleanup_askpass_script."""

    def test_removes_existing_file(self, tmp_path):
        """cleanup_askpass_script removes an existing script file."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        script_path = helper.create_askpass_script("token")
        assert script_path.exists()

        helper.cleanup_askpass_script(script_path)
        assert not script_path.exists()

    def test_safe_on_nonexistent_file(self, tmp_path):
        """cleanup_askpass_script does not raise if file does not exist."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        nonexistent = tmp_path / "no_such_file.sh"

        # Should not raise
        helper.cleanup_askpass_script(nonexistent)

    def test_logs_warning_on_os_error(self, tmp_path):
        """cleanup_askpass_script logs a warning if unlink raises OSError."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper(tmp_dir=tmp_path)
        script_path = helper.create_askpass_script("token")

        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            with patch.object(Path, "exists", return_value=True):
                # Should not raise, just warn
                helper.cleanup_askpass_script(script_path)

        # Manually remove for cleanup
        try:
            script_path.unlink()
        except Exception:
            pass


class TestConvertSshToHttps:
    """Tests for GitCredentialHelper.convert_ssh_to_https."""

    def test_standard_github_ssh_url(self):
        """Converts git@github.com:owner/repo.git to https://github.com/owner/repo.git."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "git@github.com:owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_standard_gitlab_ssh_url(self):
        """Converts git@gitlab.com:owner/repo.git to https://gitlab.com/owner/repo.git."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "git@gitlab.com:owner/repo.git"
        )
        assert result == "https://gitlab.com/owner/repo.git"

    def test_gitlab_subgroup_ssh_url(self):
        """Handles GitLab subgroups in SSH URL path."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "git@gitlab.com:group/subgroup/repo.git"
        )
        assert result == "https://gitlab.com/group/subgroup/repo.git"

    def test_ssh_url_without_git_extension(self):
        """Handles SSH URLs without .git extension."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https("git@github.com:owner/repo")
        assert result == "https://github.com/owner/repo"

    def test_https_url_passes_through(self):
        """HTTPS URLs are returned unchanged."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        url = "https://github.com/owner/repo.git"
        result = GitCredentialHelper.convert_ssh_to_https(url)
        assert result == url

    def test_https_with_credentials_passes_through(self):
        """HTTPS URLs with embedded credentials are returned unchanged."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        url = "https://user:token@github.com/owner/repo.git"
        result = GitCredentialHelper.convert_ssh_to_https(url)
        assert result == url

    def test_ssh_protocol_url(self):
        """Converts ssh://git@host/path format."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "ssh://git@github.com/owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_ssh_protocol_url_with_port(self):
        """Converts ssh://git@host:port/path format."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "ssh://git@github.com:22/owner/repo.git"
        )
        assert result == "https://github.com/owner/repo.git"

    def test_custom_self_hosted_host(self):
        """Handles custom self-hosted git hosts."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "git@git.mycompany.com:team/project.git"
        )
        assert result == "https://git.mycompany.com/team/project.git"

    def test_leading_whitespace_stripped(self):
        """Strips leading/trailing whitespace from URL before processing."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        result = GitCredentialHelper.convert_ssh_to_https(
            "  git@github.com:owner/repo.git  "
        )
        assert result == "https://github.com/owner/repo.git"


class TestExtractHostFromRemoteUrl:
    """Tests for GitCredentialHelper.extract_host_from_remote_url."""

    def test_ssh_format_extracts_host(self):
        """Extracts host from git@host:path SSH URL."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "git@github.com:owner/repo.git"
        )
        assert host == "github.com"

    def test_https_format_extracts_host(self):
        """Extracts host from https://host/path URL."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "https://github.com/owner/repo.git"
        )
        assert host == "github.com"

    def test_http_format_extracts_host(self):
        """Extracts host from http://host/path URL."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "http://gitlab.com/owner/repo.git"
        )
        assert host == "gitlab.com"

    def test_ssh_protocol_format_extracts_host(self):
        """Extracts host from ssh://git@host/path URL."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "ssh://git@github.com/owner/repo.git"
        )
        assert host == "github.com"

    def test_ssh_protocol_with_port_extracts_host(self):
        """Extracts host from ssh://git@host:port/path URL (no port in result)."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "ssh://git@github.com:22/owner/repo.git"
        )
        assert host == "github.com"

    def test_self_hosted_host(self):
        """Extracts host from custom domain."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "git@git.mycompany.com:team/repo.git"
        )
        assert host == "git.mycompany.com"

    def test_invalid_url_returns_none(self):
        """Returns None for URLs that cannot be parsed."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url("not-a-url")
        assert host is None

    def test_empty_string_returns_none(self):
        """Returns None for empty string."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url("")
        assert host is None

    def test_gitlab_subgroup_https(self):
        """Extracts host from GitLab HTTPS URL with subgroups."""
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        host = GitCredentialHelper.extract_host_from_remote_url(
            "https://gitlab.com/group/subgroup/repo.git"
        )
        assert host == "gitlab.com"
