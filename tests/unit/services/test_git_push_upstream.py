"""
Unit tests for Story #445: Fix git_push to handle branches with no upstream tracking.

Tests cover:
- Branch auto-detection when branch=None via git rev-parse --abbrev-ref HEAD
- Explicit refspec format: HEAD:refs/heads/<branch>
- Upstream tracking set after successful push via git branch --set-upstream-to
- set_upstream=False skips tracking setup
- Handler extracts set_upstream param and passes to git_push_with_pat
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


MOCK_CREDENTIAL = {
    "token": "ghp_test_secret_token",
    "git_user_name": "Alice Smith",
    "git_user_email": "alice@example.com",
    "forge_username": "alice_gh",
    "forge_host": "github.com",
}


@pytest.fixture
def repo_dir(tmp_path):
    """Create a minimal git repo directory for testing."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return tmp_path


@pytest.fixture
def git_ops_service():
    """Create GitOperationsService with mocked config."""
    from code_indexer.server.services.git_operations_service import GitOperationsService

    service = GitOperationsService.__new__(GitOperationsService)
    timeouts = MagicMock()
    timeouts.git_remote_timeout = 60
    service._git_timeouts = timeouts
    return service


class TestGitPushAutoDetectBranch:
    """Branch auto-detection when branch=None."""

    def test_push_auto_detects_branch_when_none(self, repo_dir, git_ops_service):
        """When branch=None, calls git rev-parse --abbrev-ref HEAD to detect current branch."""
        calls_made = []

        def mock_run_git(cmd, **kwargs):
            calls_made.append(list(cmd))
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd and "--abbrev-ref" in cmd:
                result.stdout = "feature/my-branch"
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        rev_parse_calls = [
            c for c in calls_made if "rev-parse" in c and "--abbrev-ref" in c
        ]
        assert len(rev_parse_calls) == 1, f"Expected rev-parse call, got: {calls_made}"
        assert rev_parse_calls[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]

    def test_push_uses_detected_branch_in_refspec(self, repo_dir, git_ops_service):
        """When branch=None and auto-detected as 'feature/my-branch', refspec uses detected name."""
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd and "--abbrev-ref" in cmd:
                result.stdout = "feature/my-branch"
            elif "push" in cmd:
                push_cmd.extend(cmd)
                result.stdout = ""
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert "HEAD:refs/heads/feature/my-branch" in push_cmd, (
            f"Expected refspec in push cmd: {push_cmd}"
        )

    def test_push_does_not_call_rev_parse_when_branch_provided(
        self, repo_dir, git_ops_service
    ):
        """When branch is explicitly provided, no rev-parse call is made."""
        calls_made = []

        def mock_run_git(cmd, **kwargs):
            calls_made.append(list(cmd))
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL
            )

        rev_parse_calls = [c for c in calls_made if "rev-parse" in c]
        assert len(rev_parse_calls) == 0, (
            f"Should not call rev-parse when branch provided: {calls_made}"
        )


class TestGitPushExplicitRefspecAndUpstream:
    """Explicit refspec format and upstream tracking behavior."""

    def test_push_uses_explicit_refspec_with_provided_branch(
        self, repo_dir, git_ops_service
    ):
        """When branch='main', push command includes HEAD:refs/heads/main refspec."""
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "push" in cmd:
                push_cmd.extend(cmd)
                result.stdout = ""
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL
            )

        assert "HEAD:refs/heads/main" in push_cmd, (
            f"Expected explicit refspec in: {push_cmd}"
        )

    def test_push_sets_upstream_after_success(self, repo_dir, git_ops_service):
        """After successful push, calls git branch --set-upstream-to=origin/main main."""
        calls_made = []

        def mock_run_git(cmd, **kwargs):
            calls_made.append(list(cmd))
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd:
                result.stdout = "main"
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL
            )

        upstream_calls = [
            c
            for c in calls_made
            if "branch" in c and "--set-upstream-to" in " ".join(c)
        ]
        assert len(upstream_calls) == 1, (
            f"Expected upstream tracking call, got: {calls_made}"
        )
        upstream_cmd = upstream_calls[0]
        assert any("origin/main" in arg for arg in upstream_cmd), (
            f"Expected origin/main in: {upstream_cmd}"
        )
        assert "main" in upstream_cmd, f"Expected branch name in: {upstream_cmd}"

    def test_push_skips_upstream_when_set_upstream_false(
        self, repo_dir, git_ops_service
    ):
        """When set_upstream=False, no git branch --set-upstream-to call is made."""
        calls_made = []

        def mock_run_git(cmd, **kwargs):
            calls_made.append(list(cmd))
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd:
                result.stdout = "main"
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL, set_upstream=False
            )

        upstream_calls = [
            c
            for c in calls_made
            if "branch" in c and "--set-upstream-to" in " ".join(c)
        ]
        assert len(upstream_calls) == 0, f"Expected no upstream call, got: {calls_made}"

    def test_upstream_not_set_when_push_fails(self, repo_dir, git_ops_service):
        """When push fails, upstream tracking is NOT set."""
        from code_indexer.server.services.git_operations_service import GitCommandError

        calls_made = []

        def mock_run_git(cmd, **kwargs):
            calls_made.append(list(cmd))
            result = MagicMock()
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd:
                result.stdout = "main"
            elif "push" in cmd:
                error = subprocess.CalledProcessError(1, cmd)
                error.stderr = "remote: Permission denied"
                raise error
            else:
                result.stdout = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=mock_run_git,
        ):
            with pytest.raises(GitCommandError):
                git_ops_service.git_push_with_pat(
                    repo_dir, "origin", "main", MOCK_CREDENTIAL
                )

        upstream_calls = [
            c
            for c in calls_made
            if "branch" in c and "--set-upstream-to" in " ".join(c)
        ]
        assert len(upstream_calls) == 0, (
            f"Upstream should not be set after failed push: {calls_made}"
        )


class TestGitPushHandlerSetUpstream:
    """Handler extracts set_upstream parameter and passes to service."""

    def _make_handler_patches(self, captured_kwargs):
        """Return mock push function and mock user for handler tests."""

        def mock_push_with_pat(repo_path, remote, branch, credential, **kwargs):
            captured_kwargs.update(kwargs)
            return {"success": True, "pushed_commits": 0}

        mock_user = MagicMock()
        mock_user.username = "testuser"
        return mock_push_with_pat, mock_user

    def test_handler_passes_set_upstream_true_to_service(self):
        """git_push handler extracts set_upstream=True from args and passes to service."""
        captured_kwargs = {}  # type: ignore[var-annotated]
        mock_push_with_pat, mock_user = self._make_handler_patches(captured_kwargs)

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_service:
            mock_service.git_push_with_pat.side_effect = mock_push_with_pat
            mock_service._trigger_migration_if_needed.return_value = None
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve:
                mock_resolve.return_value = ("/fake/repo", None)
                with patch(
                    "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
                ) as mock_cred:
                    mock_cred.return_value = (
                        MOCK_CREDENTIAL,
                        "https://github.com/owner/repo.git",
                        None,
                    )
                    from code_indexer.server.mcp.handlers import git_push

                    _ = git_push(
                        {
                            "repository_alias": "test-repo",
                            "remote": "origin",
                            "branch": "main",
                            "set_upstream": True,
                        },
                        mock_user,
                    )

        assert captured_kwargs.get("set_upstream") is True

    def test_handler_passes_set_upstream_false_to_service(self):
        """git_push handler extracts set_upstream=False from args and passes to service."""
        captured_kwargs = {}  # type: ignore[var-annotated]
        mock_push_with_pat, mock_user = self._make_handler_patches(captured_kwargs)

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_service:
            mock_service.git_push_with_pat.side_effect = mock_push_with_pat
            mock_service._trigger_migration_if_needed.return_value = None
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve:
                mock_resolve.return_value = ("/fake/repo", None)
                with patch(
                    "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
                ) as mock_cred:
                    mock_cred.return_value = (
                        MOCK_CREDENTIAL,
                        "https://github.com/owner/repo.git",
                        None,
                    )
                    from code_indexer.server.mcp.handlers import git_push

                    _ = git_push(
                        {"repository_alias": "test-repo", "set_upstream": False},
                        mock_user,
                    )

        assert captured_kwargs.get("set_upstream") is False

    def test_handler_defaults_set_upstream_true_when_not_provided(self):
        """git_push handler passes set_upstream=True when not in args (default)."""
        captured_kwargs = {}  # type: ignore[var-annotated]
        mock_push_with_pat, mock_user = self._make_handler_patches(captured_kwargs)

        with patch(
            "code_indexer.server.mcp.handlers.git_operations_service"
        ) as mock_service:
            mock_service.git_push_with_pat.side_effect = mock_push_with_pat
            mock_service._trigger_migration_if_needed.return_value = None
            with patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve:
                mock_resolve.return_value = ("/fake/repo", None)
                with patch(
                    "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
                ) as mock_cred:
                    mock_cred.return_value = (
                        MOCK_CREDENTIAL,
                        "https://github.com/owner/repo.git",
                        None,
                    )
                    from code_indexer.server.mcp.handlers import git_push

                    _ = git_push({"repository_alias": "test-repo"}, mock_user)

        assert captured_kwargs.get("set_upstream") is True
