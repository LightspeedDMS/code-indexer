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
