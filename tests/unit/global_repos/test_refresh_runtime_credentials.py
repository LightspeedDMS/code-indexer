"""Golden refresh: an existing base clone whose origin URL still carries
userinfo converges to the credential-free URL at the start of the refresh,
and the refresh fetch/pull and the re-clone receive repository credentials
at run time, never on a command line.

Real git against a local HTTP git server that requires HTTP Basic
credentials. ``subprocess.run`` is wrapped by a pass-through recorder.
Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from code_indexer.global_repos import refresh_scheduler
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.git.git_subprocess_env import (
    build_non_interactive_git_env,
    remote_url_without_credentials,
)
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


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _assert_no_credentials(argv_log: List[List[str]], clone: Path) -> None:
    assert argv_log, "expected git subprocess calls"
    for argv in argv_log:
        assert not any(SECRET in part for part in argv), argv
    config = (clone / ".git" / "config").read_text()
    assert SECRET not in config and USER not in config, config


def _push_new_remote_commit(tmp_path: Path) -> str:
    """Commit to the served bare repository directly; return the commit."""
    work = tmp_path / "upstream-work"
    _git("clone", str(tmp_path / "served" / "remote.git"), str(work), cwd=tmp_path)
    (work / "NEW.md").write_text("new\n")
    _git("add", "NEW.md", cwd=work)
    _git(
        "-c",
        "user.name=Example",
        "-c",
        "user.email=e@example.com",
        "commit",
        "-m",
        "second",
        cwd=work,
    )
    _git("push", "origin", "main", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).strip()


def _assert_no_credentials_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "expected log records from the flow"
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.getMessage()


def test_refresh_rewrites_stored_userinfo_and_pulls_with_runtime_credentials(
    remote: str,
    tmp_path: Path,
    argv_log: List[List[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    master = tmp_path / "golden-repos" / "repo"
    master.parent.mkdir()
    subprocess.run(
        ["git", "clone", remote_url_without_credentials(remote), str(master)],
        capture_output=True,
        check=True,
        env=build_non_interactive_git_env(remote),
    )
    # An existing clone made before credentials were supplied at run time.
    _git("config", "remote.origin.url", remote, cwd=master)
    new_commit = _push_new_remote_commit(tmp_path)
    argv_log.clear()

    updater = refresh_scheduler._git_pull_updater_for(str(master), remote, None)

    assert updater.has_changes() is True
    updater.update()
    assert _git("rev-parse", "HEAD", cwd=master).strip() == new_commit
    _assert_no_credentials(argv_log, master)
    _assert_no_credentials_logged(caplog)


def test_refresh_continues_with_a_warning_when_stored_urls_cannot_be_sanitized(
    remote: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The stored URL still authenticates, so the refresh is not aborted."""
    master = tmp_path / "golden-repos" / "repo"
    master.parent.mkdir()
    subprocess.run(
        ["git", "clone", remote_url_without_credentials(remote), str(master)],
        capture_output=True,
        check=True,
        env=build_non_interactive_git_env(remote),
    )
    _git("config", "remote.origin.url", remote, cwd=master)
    config = master / ".git" / "config"
    config.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            updater = refresh_scheduler._git_pull_updater_for(str(master), remote, None)
    finally:
        config.chmod(0o644)

    assert isinstance(updater, refresh_scheduler.GitPullUpdater)
    caller_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == refresh_scheduler.__name__
    ]
    assert caller_warnings, [(r.name, r.getMessage()) for r in caplog.records]
    assert str(master) in caller_warnings[0].getMessage()
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_reclone_stores_no_credentials(
    remote: str,
    tmp_path: Path,
    argv_log: List[List[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    master = tmp_path / "golden-repos" / "repo"
    master.mkdir(parents=True)
    scheduler = object.__new__(RefreshScheduler)

    assert scheduler._attempt_reclone("repo-global", remote, str(master)) is True

    assert (master / "README.md").read_text() == "example\n"
    _assert_no_credentials(argv_log, master)
    _assert_no_credentials_logged(caplog)
