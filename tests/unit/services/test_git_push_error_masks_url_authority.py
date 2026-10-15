"""git_push_with_pat must not surface a URL authority in its failure output.

Drives the REAL method: the push target is an authenticated URL, so the authority
appears in git's own stderr and in CalledProcessError's rendering of the command,
and both reach the caller through GitCommandError.

These assert on what git_push_with_pat actually raises -- asserting on a locally
recomputed `mask_url_credentials(...)` would pass even if the production code
stopped masking, which is the whole failure mode being guarded.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)

_SECRET = "ghp-secret-token-value"
_AUTHED_URL = f"https://oauth2:{_SECRET}@git.example.com/org/repo.git"
_CREDENTIAL = {
    "token": _SECRET,
    "git_user_name": "Example User",
    "git_user_email": "user@example.com",
    "forge_username": "example",
    "forge_host": "git.example.com",
}


@pytest.fixture
def repo_dir(tmp_path: Any) -> Any:
    """A real git repo with an `origin` remote.

    git_argv_safety validates the remote name against `git remote` before the push,
    and that runs against the real directory, so a bare tmp dir is rejected first.
    """
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "remote", "add", "origin", _AUTHED_URL], cwd=d, check=True)
    return d


def _push_raising(stderr: str) -> Any:
    """Fail the `git push` call the way git does, leaving `get-url` working."""

    def _mock_run_git(cmd: Any, **kwargs: Any) -> Any:
        if cmd == ["git", "remote"]:
            result = MagicMock()
            result.stdout = "origin\n"
            return result
        if "get-url" in cmd:
            result = MagicMock()
            result.stdout = _AUTHED_URL
            return result
        if "rev-parse" in cmd:
            result = MagicMock()
            result.stdout = "main"
            return result
        error = subprocess.CalledProcessError(128, cmd)
        error.stderr = stderr
        raise error

    return _mock_run_git


def _run_and_capture(repo_dir: Any, stderr: str) -> GitCommandError:
    service = GitOperationsService()
    with patch(
        "code_indexer.server.services.git_operations_service.run_git_command",
        side_effect=_push_raising(stderr),
    ):
        with patch(
            "code_indexer.server.services.git_credential_helper."
            "GitCredentialHelper.cleanup_askpass_script"
        ):
            with pytest.raises(GitCommandError) as excinfo:
                service.git_push_with_pat(repo_dir, "origin", "main", _CREDENTIAL)
    return excinfo.value


def test_stderr_echoing_the_url_is_masked(repo_dir: Any) -> None:
    exc = _run_and_capture(
        repo_dir,
        f"remote: Invalid credentials\nfatal: could not read from '{_AUTHED_URL}'\n",
    )
    assert _SECRET not in str(exc)
    assert _SECRET not in (exc.stderr or "")
    # still diagnostic: host and path survive
    assert "git.example.com" in str(exc)


def test_empty_stderr_falls_back_to_the_rendered_command_and_is_masked(
    repo_dir: Any,
) -> None:
    """The subtle half: with empty stderr the detail comes from str(e), which
    renders the whole command line -- including the authenticated push URL."""
    exc = _run_and_capture(repo_dir, "")
    assert _SECRET not in str(exc)
    assert _SECRET not in (exc.stderr or "")


def test_credential_free_stderr_is_passed_through_unchanged(repo_dir: Any) -> None:
    exc = _run_and_capture(repo_dir, "fatal: repository not found")
    assert "fatal: repository not found" in str(exc)
