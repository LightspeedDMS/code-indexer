"""GitOperationsService.git_push_with_pat -- the MCP git_push front door --
error mapping and result shape, against real git.

Story #387: PAT-Authenticated Git Push.

The push itself is git_push, the single push implementation (see
test_git_push_single_path.py). A real repository and a real remote served
over http that requires the PAT (HTTP Basic auth). Hosts and secrets are
neutral placeholders.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)

PAT = "example-pat-789"
CREDENTIAL = {"token": PAT}


<<<<<<< HEAD
def _mock_git_remote_list(cmd, inner, **kwargs):
    """Let validate_remote_name's ``git remote`` through the mock."""
    if cmd == ["git", "remote"]:
        r = MagicMock()
        r.stdout = "origin\n"
        return r
    return inner(cmd, **kwargs)


# Credential fixture used across tests
MOCK_CREDENTIAL = {
    "token": "ghp_test_secret_token",
    "git_user_name": "Alice Smith",
    "git_user_email": "alice@example.com",
    "forge_username": "alice_gh",
    "forge_host": "github.com",
}


@pytest.fixture
def repo_dir(tmp_path):
    """Create a real git repository with a configured 'origin' remote.

    validate_remote_name() (git_argv_safety.py) confirms the remote is
    actually configured by running a real, unmocked `git remote` against
    `repo_path` before git_push_with_pat ever reaches the mocked
    run_git_command calls in these tests, so the fixture must be a real
    repository, not merely a directory containing a `.git` folder, with
    'origin' actually configured. The remote URL itself is never
    contacted (git_push_with_pat's own `git remote get-url`/push calls
    are mocked per test), so any well-formed placeholder URL is fine here.
    """
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://example.invalid/owner/repo.git",
        ],
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def git_ops_service():
    """Create GitOperationsService with mocked config."""
    from code_indexer.server.services.git_operations_service import GitOperationsService

    service = GitOperationsService.__new__(GitOperationsService)
    # Mock timeouts config
    timeouts = MagicMock()
    timeouts.git_remote_timeout = 60
    service._git_timeouts = timeouts
    return service


class TestGitPushWithPatEnvVars:
    """Tests for environment variable setup in git_push_with_pat."""

    def test_sets_git_askpass_env_var(self, repo_dir, git_ops_service):
        """GIT_ASKPASS is set to the temporary script path."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            # Capture env for the push command
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            result.returncode = 0
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert_env_present(captured_env, "GIT_ASKPASS")
        # Should be an absolute path to a script
        askpass_path = Path(captured_env["GIT_ASKPASS"])
        # File should be cleaned up (in finally), so just check it was a Path
        assert str(askpass_path).endswith(".sh")

    def test_sets_git_terminal_prompt_to_zero(self, repo_dir, git_ops_service):
        """GIT_TERMINAL_PROMPT=0 is set to prevent interactive prompts."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert captured_env.get("GIT_TERMINAL_PROMPT") == "0"

    def test_sets_git_author_name_from_credential(self, repo_dir, git_ops_service):
        """GIT_AUTHOR_NAME is set from credential's git_user_name."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert captured_env.get("GIT_AUTHOR_NAME") == "Alice Smith"

    def test_sets_git_author_email_from_credential(self, repo_dir, git_ops_service):
        """GIT_AUTHOR_EMAIL is set from credential's git_user_email."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert captured_env.get("GIT_AUTHOR_EMAIL") == "alice@example.com"

    def test_sets_git_committer_name_from_credential(self, repo_dir, git_ops_service):
        """GIT_COMMITTER_NAME is set from credential's git_user_name."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert captured_env.get("GIT_COMMITTER_NAME") == "Alice Smith"

    def test_sets_git_committer_email_from_credential(self, repo_dir, git_ops_service):
        """GIT_COMMITTER_EMAIL is set from credential's git_user_email."""
        captured_env = {}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert captured_env.get("GIT_COMMITTER_EMAIL") == "alice@example.com"

    def test_skips_name_email_when_not_in_credential(self, repo_dir, git_ops_service):
        """Does not set GIT_AUTHOR_NAME/EMAIL when credential lacks identity."""
        captured_env = {}
        credential_without_identity = {"token": "ghp_token_only"}

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            captured_env.update(kwargs.get("env", {}))
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", None, credential_without_identity
            )

        # Should not have been set since credential lacks these keys
        assert_env_absent(captured_env, "GIT_AUTHOR_NAME")
        assert_env_absent(captured_env, "GIT_AUTHOR_EMAIL")


class TestGitPushWithPatUrlConversion:
    """Tests for SSH-to-HTTPS URL conversion in git_push_with_pat."""

    def test_ssh_url_converted_to_https_in_push_command(
        self, repo_dir, git_ops_service
    ):
        """SSH remote URL is converted to HTTPS for the push command."""
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            push_cmd.extend(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        # The push command should use HTTPS URL, not SSH
        assert "https://github.com/owner/repo.git" in push_cmd
        # Should NOT contain SSH URL or remote name
        assert "git@github.com:owner/repo.git" not in push_cmd
        assert "origin" not in push_cmd[2:]  # origin should not appear after "git push"

    def test_https_url_passed_through_unchanged(self, repo_dir, git_ops_service):
        """HTTPS remote URL is passed through unchanged."""
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "https://github.com/owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            push_cmd.extend(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        assert "https://github.com/owner/repo.git" in push_cmd

    def test_push_without_branch_auto_detects_and_uses_refspec(
        self, repo_dir, git_ops_service
    ):
        """Push without branch auto-detects current branch and uses explicit refspec.

        Story #445: branch=None triggers rev-parse and HEAD:refs/heads/<branch> refspec.
        """
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            result = MagicMock()
            result.stderr = ""
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "rev-parse" in cmd and "--abbrev-ref" in cmd:
                result.stdout = "main"
            elif "push" in cmd:
                push_cmd.extend(cmd)
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(repo_dir, "origin", None, MOCK_CREDENTIAL)

        # Story #445: push uses explicit refspec, not bare branch name
        assert "HEAD:refs/heads/main" in push_cmd
        assert len(push_cmd) == 4  # ["git", "push", url, "HEAD:refs/heads/main"]

    def test_push_with_branch_uses_explicit_refspec(self, repo_dir, git_ops_service):
        """Push with branch uses explicit HEAD:refs/heads/<branch> refspec.

        Story #445: refspec form works even without upstream tracking.
        """
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            result = MagicMock()
            result.stderr = ""
            if "get-url" in cmd:
                result.stdout = "git@github.com:owner/repo.git"
            elif "push" in cmd:
                push_cmd.extend(cmd)
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL
            )

        # Story #445: refspec form used, not bare branch name
        assert "HEAD:refs/heads/main" in push_cmd
        assert len(push_cmd) == 4  # ["git", "push", url, "HEAD:refs/heads/main"]

    def test_uses_provided_remote_url_without_subprocess_call(
        self, repo_dir, git_ops_service
    ):
        """When remote_url is pre-provided, no 'get-url' subprocess call is made."""
        push_cmd = []

        def mock_run_git(cmd, **kwargs):
            assert "get-url" not in cmd, (
                "Should not call git remote get-url when URL pre-provided"
            )
            if "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            push_cmd.extend(cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            git_ops_service.git_push_with_pat(
                repo_dir,
                "origin",
                None,
                MOCK_CREDENTIAL,
                remote_url="git@github.com:owner/repo.git",
            )

        # SSH URL should be converted to HTTPS in the push command
        assert "https://github.com/owner/repo.git" in push_cmd


class TestGitPushWithPatErrorHandling:
    """Tests for error handling and cleanup in git_push_with_pat."""

    def test_cleanup_happens_on_success(self, repo_dir, git_ops_service):
        """Askpass script is cleaned up after successful push."""
        created_scripts = []

        def mock_create(token):
            script = MagicMock()
            script.__str__ = lambda s: "/tmp/fake_askpass.sh"  # type: ignore[assignment, method-assign, misc]
            created_scripts.append(script)
            return script

        cleanup_called = []

        def mock_cleanup(path):
            cleanup_called.append(path)

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            elif "rev-parse" in cmd:
                result = MagicMock()
                result.stdout = "main"
                return result
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            with patch(
                "code_indexer.server.services.git_credential_helper.GitCredentialHelper.create_askpass_script",
                side_effect=mock_create,
            ):
                with patch(
                    "code_indexer.server.services.git_credential_helper.GitCredentialHelper.cleanup_askpass_script",
                    side_effect=mock_cleanup,
                ):
                    git_ops_service.git_push_with_pat(
                        repo_dir, "origin", None, MOCK_CREDENTIAL
                    )

        assert len(cleanup_called) == 1

    def test_cleanup_happens_on_git_command_error(self, repo_dir, git_ops_service):
        """Askpass script is cleaned up even when push fails with CalledProcessError."""
        cleanup_called = []

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            # Simulate push failure
            error = subprocess.CalledProcessError(1, cmd)
            error.stderr = "remote: Permission denied"
            raise error

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            with patch(
                "code_indexer.server.services.git_credential_helper.GitCredentialHelper.cleanup_askpass_script",
                side_effect=lambda p: cleanup_called.append(p),
            ):
                from code_indexer.server.services.git_operations_service import (
                    GitCommandError,
                )

                with pytest.raises(GitCommandError):
                    git_ops_service.git_push_with_pat(
                        repo_dir, "origin", None, MOCK_CREDENTIAL
                    )

        assert len(cleanup_called) == 1

    def test_called_process_error_raises_git_command_error(
        self, repo_dir, git_ops_service
    ):
        """CalledProcessError from git push is wrapped in GitCommandError."""
        from code_indexer.server.services.git_operations_service import GitCommandError

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            error = subprocess.CalledProcessError(128, cmd)
            error.stderr = "remote: Repository not found"
            raise error

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            with pytest.raises(GitCommandError) as exc_info:
                git_ops_service.git_push_with_pat(
                    repo_dir, "origin", None, MOCK_CREDENTIAL
                )

        assert "git push failed" in str(exc_info.value)

    def test_timeout_raises_git_command_error(self, repo_dir, git_ops_service):
        """TimeoutExpired from git push is wrapped in GitCommandError."""
        from code_indexer.server.services.git_operations_service import GitCommandError

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            raise subprocess.TimeoutExpired(cmd, timeout=60)

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            with pytest.raises(GitCommandError) as exc_info:
                git_ops_service.git_push_with_pat(
                    repo_dir, "origin", None, MOCK_CREDENTIAL
                )

        assert "timed out" in str(exc_info.value)

    def test_get_remote_url_failure_raises_git_command_error(
        self, repo_dir, git_ops_service
    ):
        """Failed git remote get-url raises GitCommandError."""
        from code_indexer.server.services.git_operations_service import GitCommandError

        def mock_run_git(cmd, **kwargs):
            error = subprocess.CalledProcessError(128, cmd)
            error.stderr = "fatal: No such remote 'origin'"
            raise error

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            with pytest.raises(GitCommandError) as exc_info:
                git_ops_service.git_push_with_pat(
                    repo_dir, "origin", None, MOCK_CREDENTIAL
                )

        assert "Failed to get remote URL" in str(exc_info.value)


class TestGitPushWithPatReturn:
    """Tests for return value of git_push_with_pat."""

    def test_returns_success_true_on_successful_push(self, repo_dir, git_ops_service):
        """Returns {'success': True, 'pushed_commits': ...} on success."""

        def mock_run_git(cmd, **kwargs):
            if "get-url" in cmd:
                result = MagicMock()
                result.stdout = "git@github.com:owner/repo.git"
                return result
            result = MagicMock()
            result.stdout = "abc123..def456  main -> main"
            result.stderr = ""
            return result

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=lambda cmd, **kw: _mock_git_remote_list(cmd, mock_run_git, **kw),
        ):
            result = git_ops_service.git_push_with_pat(
                repo_dir, "origin", "main", MOCK_CREDENTIAL
            )

        assert result["success"] is True
        assert "pushed_commits" in result
=======
def _git(args: List[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, text: str) -> None:
    (repo / "f.txt").write_text(text)
    _git(["commit", "-q", "-am", text], repo)


@pytest.fixture
def remote_and_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[Path, Path]]:
    """(served bare repository, clone): the remote is served over http and
    requires the PAT (as username and password); the clone's main, with
    one more commit, is already pushed."""
    from code_indexer.server.git.git_subprocess_env import (
        build_non_interactive_git_env,
        http_credentials_url,
    )
    from tests.unit.server.git.auth_http_git_server import served_bare_remote

    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    with served_bare_remote(tmp_path, PAT, PAT) as url:
        env = build_non_interactive_git_env(http_credentials_url(url, PAT, PAT))
        repo = tmp_path / "repo"
        subprocess.run(
            ["git", "clone", "-q", url, str(repo)],
            check=True,
            capture_output=True,
            env=env,
        )
        _git(["config", "user.email", "test@example.com"], repo)
        _git(["config", "user.name", "Test User"], repo)
        (repo / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-q", "-m", "c1"], repo)
        subprocess.run(
            ["git", "push", "-q", "origin", "main"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            env=env,
        )
        yield tmp_path / "served" / "remote.git", repo


def test_rejected_push_raises_git_command_error_without_the_pat(
    remote_and_repo: Tuple[Path, Path],
) -> None:
    _bare, repo = remote_and_repo
    _git(["commit", "-q", "--amend", "-m", "c1 (amended)"], repo)

    with pytest.raises(GitCommandError) as exc_info:
        git_operations_service.git_push_with_pat(
            repo, "origin", "main", CREDENTIAL, set_upstream=False
        )

    assert "git push failed" in str(exc_info.value)
    assert PAT not in str(exc_info.value)
    assert PAT not in (exc_info.value.stderr or "")


def test_result_reports_pushed_commit_count(
    remote_and_repo: Tuple[Path, Path],
) -> None:
    bare, repo = remote_and_repo
    _commit(repo, "two\n")
    _commit(repo, "three\n")

    result = git_operations_service.git_push_with_pat(
        repo, "origin", "main", CREDENTIAL, set_upstream=False
    )

    assert result == {"success": True, "pushed_commits": 2}
    assert _git(["rev-parse", "main"], bare) == _git(["rev-parse", "main"], repo)
>>>>>>> upstream/master
