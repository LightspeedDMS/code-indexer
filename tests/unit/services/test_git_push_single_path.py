"""Both push front doors -- REST (the registered repository credential) and
MCP (the user's PAT) -- push through ONE implementation,
``GitOperationsService.git_push``: argv ``git push [--set-upstream]
--end-of-options <remote> [refspec]``, credentials supplied only at run
time, upstream tracking recorded by git itself against the remote's name.

Real repositories and a real local bare remote. The remote is named by an
https URL (so a PAT applies to it); the repository's own
``url.<bare>.pushInsteadOf`` delivers the push to the local bare
repository. Hosts and secrets are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)

REMOTE_URL = "https://git.example.com/example/repo.git"
PAT = "example-pat-456"
CREDENTIAL = {
    "token": PAT,
    "git_user_name": "Example User",
    "git_user_email": "example@example.com",
}


def _git(args: List[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _upstream(repo: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{u}}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


@pytest.fixture
def remote_and_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[Path, Path]]:
    """(served bare repository, clone): the remote is served over http and
    requires the PAT (as username and password); the clone has one new
    commit."""
    from tests.unit.server.git.auth_http_git_server import served_bare_remote

    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    with served_bare_remote(tmp_path, PAT, PAT) as url:
        repo = tmp_path / "repo"
        subprocess.run(
            ["git", "clone", "-q", url, str(repo)],
            check=True,
            capture_output=True,
            env=_authed_env(url),
        )
        _git(["config", "user.email", "test@example.com"], repo)
        _git(["config", "user.name", "Test User"], repo)
        (repo / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-q", "-m", "c1"], repo)
        yield tmp_path / "served" / "remote.git", repo


@pytest.fixture
def push_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every call of the single push implementation (the real method runs)."""
    calls: List[Dict[str, Any]] = []
    real_git_push = git_operations_service.git_push

    def spy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        result: Dict[str, Any] = real_git_push(*args, **kwargs)
        return result

    monkeypatch.setattr(git_operations_service, "git_push", spy)
    return calls


def test_pat_push_runs_through_git_push_and_records_upstream_by_remote_name(
    remote_and_repo: Tuple[Path, Path], push_calls: List[Dict[str, Any]]
) -> None:
    bare, repo = remote_and_repo

    result = git_operations_service.git_push_with_pat(
        repo, "origin", None, CREDENTIAL, set_upstream=True
    )

    assert result["success"] is True
    assert len(push_calls) == 1
    assert _git(["rev-parse", "main"], bare) == _git(["rev-parse", "main"], repo)
    assert _upstream(repo, "main") == "origin/main"


SSH_URL = "git@git.example.com:example/repo.git"


@pytest.fixture
def git_runs(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[List[str], Dict[str, str]]]:
    """argv and env of every git subprocess. All run for real, except a
    push to the unreachable https form of SSH_URL, which reports success."""
    runs: List[Tuple[List[str], Dict[str, str]]] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if argv[:1] == ["git"]:
            env = dict(kwargs.get("env") or {})
            runs.append((argv, env))
            if argv[1:2] == ["push"] and SSH_URL in env.values():
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return runs


def _push_runs(
    runs: List[Tuple[List[str], Dict[str, str]]],
) -> List[Tuple[List[str], Dict[str, str]]]:
    return [(argv, env) for argv, env in runs if argv[1:2] == ["push"]]


def test_pat_reaches_git_only_through_the_push_environment(
    remote_and_repo: Tuple[Path, Path],
    git_runs: List[Tuple[List[str], Dict[str, str]]],
) -> None:
    _bare, repo = remote_and_repo

    result = git_operations_service.git_push_with_pat(
        repo, "origin", None, CREDENTIAL, set_upstream=True
    )

    pushes = _push_runs(git_runs)
    assert [argv for argv, _env in pushes] == [
        ["git", "push", "--set-upstream", "--end-of-options", "origin", "HEAD"]
    ]
    push_env = pushes[0][1]
    assert push_env["CIDX_GIT_REMOTE_PASSWORD"] == PAT
    # Scalar comparisons only: a failure must never print the environment.
    from code_indexer.server.git.git_subprocess_env import credential_scope

    origin = _git(["config", "--get", "remote.origin.url"], repo)
    helper_key = f"credential.{credential_scope(origin)}.helper"
    assert any(value == helper_key for value in push_env.values())
    assert push_env.get("GIT_ASKPASS") == ""
    for argv, _env in git_runs:
        assert not any(PAT in part for part in argv), argv
    assert PAT not in (repo / ".git" / "config").read_text()
    assert PAT not in str(result)


def test_pat_push_without_set_upstream_leaves_no_upstream(
    remote_and_repo: Tuple[Path, Path], push_calls: List[Dict[str, Any]]
) -> None:
    bare, repo = remote_and_repo
    # A clone's main already tracks origin/main; a new branch tracks nothing.
    _git(["checkout", "-q", "-b", "topic"], repo)

    git_operations_service.git_push_with_pat(
        repo, "origin", None, CREDENTIAL, set_upstream=False
    )

    assert len(push_calls) == 1
    assert _git(["rev-parse", "topic"], bare) == _git(["rev-parse", "HEAD"], repo)
    assert _upstream(repo, "topic") == ""


def test_pat_push_named_branch_pushes_head_to_it_and_tracks_it(
    remote_and_repo: Tuple[Path, Path], push_calls: List[Dict[str, Any]]
) -> None:
    """MCP contract: ``branch`` names the destination for HEAD."""
    bare, repo = remote_and_repo

    git_operations_service.git_push_with_pat(
        repo, "origin", "feature-x", CREDENTIAL, set_upstream=True
    )

    assert push_calls[0]["kwargs"]["branch"] == "HEAD:refs/heads/feature-x"
    assert _git(["rev-parse", "feature-x"], bare) == _git(["rev-parse", "HEAD"], repo)
    assert _upstream(repo, "main") == "origin/feature-x"


def test_ssh_remote_is_pushed_over_https_keeping_its_name(
    tmp_path: Path, git_runs: List[Tuple[List[str], Dict[str, str]]]
) -> None:
    repo = tmp_path / "repo"
    _git(["init", "-q", "-b", "main", str(repo)], tmp_path)
    _git(["remote", "add", "origin", SSH_URL], repo)

    git_operations_service.git_push_with_pat(
        repo, "origin", "main", CREDENTIAL, set_upstream=False
    )

    pushes = _push_runs(git_runs)
    assert [argv for argv, _env in pushes] == [
        ["git", "push", "--end-of-options", "origin", "HEAD:refs/heads/main"]
    ]
    push_env = pushes[0][1]
    rewrite = [
        (push_env[key], push_env[key.replace("KEY", "VALUE")])
        for key in push_env
        if key.startswith("GIT_CONFIG_KEY_")
        and push_env[key].endswith(".pushInsteadOf")
    ]
    assert rewrite == [
        ("url.https://git.example.com/example/repo.git.pushInsteadOf", SSH_URL)
    ]
    assert push_env["CIDX_GIT_REMOTE_PASSWORD"] == PAT
    assert _git(["config", "remote.origin.url"], repo) == SSH_URL
    assert "pushInsteadOf" not in (repo / ".git" / "config").read_text()


def _authed_env(url: str) -> Dict[str, str]:
    """Non-interactive git env carrying the PAT for ``url`` at run time."""
    from code_indexer.server.git.git_subprocess_env import (
        build_non_interactive_git_env,
        http_credentials_url,
    )

    return build_non_interactive_git_env(http_credentials_url(url, PAT, PAT))


@pytest.fixture
def http_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Tuple[str, Path]]:
    """A clone of a remote served over http that requires the PAT (as both
    username and password) plus one new commit; yields (URL, clone)."""
    from tests.unit.server.git.auth_http_git_server import served_bare_remote

    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    with served_bare_remote(tmp_path, PAT, PAT) as url:
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", url, str(clone)],
            check=True,
            capture_output=True,
            env=_authed_env(url),
        )
        _git(["config", "user.email", "test@example.com"], clone)
        _git(["config", "user.name", "Test User"], clone)
        (clone / "f.txt").write_text("pushed over http\n")
        _git(["add", "f.txt"], clone)
        _git(["commit", "-q", "-m", "c2"], clone)
        yield url, clone


def _served_main(url: str) -> str:
    listed = subprocess.run(
        ["git", "ls-remote", url, "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
        env=_authed_env(url),
    )
    return listed.stdout.split()[0]


def test_pat_push_authenticates_over_http_with_the_run_time_credential(
    http_clone: Tuple[str, Path],
) -> None:
    url, clone = http_clone

    result = git_operations_service.git_push_with_pat(
        clone, "origin", None, CREDENTIAL, set_upstream=True
    )

    assert result["success"] is True
    assert _served_main(url) == _git(["rev-parse", "HEAD"], clone)
    assert _upstream(clone, "main") == "origin/main"
    assert PAT not in (clone / ".git" / "config").read_text()


def test_pat_push_with_a_wrong_pat_is_refused(http_clone: Tuple[str, Path]) -> None:
    from code_indexer.server.services.git_operations_service import GitCommandError

    url, clone = http_clone
    before = _served_main(url)

    with pytest.raises(GitCommandError) as exc_info:
        git_operations_service.git_push_with_pat(
            clone, "origin", None, {"token": "example-wrong-pat"}, set_upstream=True
        )

    assert "example-wrong-pat" not in str(exc_info.value)
    assert _served_main(url) == before


def test_explicit_pushurl_that_the_credential_cannot_reach_is_refused(
    tmp_path: Path, git_runs: List[Tuple[List[str], Dict[str, str]]]
) -> None:
    """git sends a push to an explicit pushurl as configured, so the run-time
    rewrite over https cannot apply: the push is refused, never sent without
    the credential."""
    from code_indexer.server.services.git_operations_service import GitCommandError

    repo = tmp_path / "repo"
    _git(["init", "-q", "-b", "main", str(repo)], tmp_path)
    _git(["remote", "add", "origin", SSH_URL], repo)
    _git(
        ["config", "remote.origin.pushurl", "git@git.example.com:example/other.git"],
        repo,
    )

    with pytest.raises(GitCommandError) as exc_info:
        git_operations_service.git_push_with_pat(
            repo, "origin", "main", CREDENTIAL, set_upstream=False
        )

    assert "explicit pushurl" in str(exc_info.value)
    assert _push_runs(git_runs) == []
