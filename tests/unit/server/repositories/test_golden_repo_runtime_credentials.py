"""Golden repositories: the clone stores no repository credentials, and the
clone / validation / branch-change git operations receive them at run time,
never on a command line.

Real git against a local HTTP git server that requires HTTP Basic
credentials. ``subprocess.run`` is wrapped by a pass-through recorder that
keeps every argv. Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerResourceConfig
from tests.unit.server.git.auth_http_git_server import served_bare_remote

USER = "example-user"
SECRET = "example-token-123"
USERINFO_PREFIX = "http://example-user:example-token-123@"


@pytest.fixture(autouse=True)
def _no_askpass_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[str]:
    with served_bare_remote(tmp_path, USER, SECRET) as clean:
        yield clean.replace("http://", USERINFO_PREFIX, 1)


@pytest.fixture
def argv_log(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    calls: List[List[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    return calls


def _manager() -> GoldenRepoManager:
    manager = object.__new__(GoldenRepoManager)
    manager.resource_config = ServerResourceConfig(
        git_pull_timeout=60, git_clone_timeout=60
    )
    return manager


def _assert_no_credentials(argv_log: List[List[str]], clone: Path) -> None:
    assert argv_log, "expected git subprocess calls"
    for argv in argv_log:
        assert not any(SECRET in part or USER in part for part in argv), argv
    config = (clone / ".git" / "config").read_text()
    assert SECRET not in config and USER not in config, config


def test_golden_clone_stores_no_credentials_and_keeps_them_off_argv(
    remote: str, tmp_path: Path, argv_log: List[List[str]]
) -> None:
    clone = tmp_path / "golden" / "repo"

    _manager()._clone_remote_repository(remote, str(clone))

    assert (clone / "README.md").read_text() == "example\n"
    _assert_no_credentials(argv_log, clone)


def test_repository_validation_keeps_credentials_off_argv(
    remote: str, argv_log: List[List[str]]
) -> None:
    assert _manager()._validate_git_repository(remote) is True
    for argv in argv_log:
        assert not any(SECRET in part for part in argv), argv


def test_branch_change_fetch_and_pull_use_runtime_credentials(
    remote: str, tmp_path: Path, argv_log: List[List[str]]
) -> None:
    manager = _manager()
    clone = tmp_path / "golden" / "repo"
    manager._clone_remote_repository(remote, str(clone))

    manager._cb_git_fetch_and_validate(str(clone), "main", 60, credentials_url=remote)
    manager._cb_checkout_and_pull(str(clone), "main", 60, credentials_url=remote)

    _assert_no_credentials(argv_log, clone)


def test_branch_change_stops_before_any_credentialed_git_call_when_sanitization_fails(
    remote: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential is handed to git for a clone whose stored remote URLs
    could not be made credential-free: the branch change fails first."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
    from code_indexer.server.services.git_operations_service import (
        GitCommandError,
    )

    clone = tmp_path / "golden" / "repo"
    _manager()._clone_remote_repository(remote, str(clone))
    manager = _manager()
    golden = GoldenRepo(
        alias="example-repo",
        repo_url=remote,
        default_branch="main",
        clone_path=str(clone),
        created_at="2025-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        manager, "_resolve_golden_repo_authoritative", lambda alias: golden
    )
    credentialed: List[List[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if "CIDX_GIT_REMOTE_PASSWORD" in (kwargs.get("env") or {}):
            credentialed.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    config = clone / ".git" / "config"
    config.chmod(0)
    try:
        with pytest.raises(GitCommandError, match="removing credentials"):
            manager.change_branch("example-repo", "other")
    finally:
        config.chmod(0o644)

    assert credentialed == []
