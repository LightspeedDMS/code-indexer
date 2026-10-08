"""Repository credentials are supplied to git at run time and never stored
in a clone's configuration nor placed on a command line.

Real git against a local HTTP git server that requires HTTP Basic
credentials (auth_http_git_server). Hosts, usernames and secrets are
neutral placeholders.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Iterator, List
from urllib.parse import unquote

import pytest

from code_indexer.server.git.git_subprocess_env import (
    append_run_time_git_config,
    build_non_interactive_git_env,
    ensure_remote_url_without_credentials,
    http_credentials_url,
    remote_url_without_credentials,
    supply_remote_credentials,
)
from tests.unit.server.git.auth_http_git_server import served_bare_remote

USER = "example-user"
SECRET = "example-token-123"
USERINFO_PREFIX = "http://example-user:example-token-123@"


@pytest.fixture(autouse=True)
def _no_askpass_program(monkeypatch: pytest.MonkeyPatch) -> None:
    """A desktop session's askpass program would otherwise be launched by
    git for the unauthenticated request instead of failing at once."""
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[str]:
    """The URL, with userinfo, of a bare repository with one commit on
    ``main`` that accepts only USER/SECRET."""
    with served_bare_remote(tmp_path, USER, SECRET) as clean:
        yield clean.replace("http://", USERINFO_PREFIX, 1)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://example-user:example-token-123@git.example.com:8443/a/b.git?x=1#f",
            "https://git.example.com:8443/a/b.git?x=1#f",
        ),
        (
            "https://example-pat-456@git.example.com/a.git",
            "https://git.example.com/a.git",
        ),
        ("http://u:p@ss@git.example.com/a.git", "http://git.example.com/a.git"),
        ("https://git.example.com/a.git", "https://git.example.com/a.git"),
        ("ssh://git@git.example.com/a.git", "ssh://git@git.example.com/a.git"),
        ("git@git.example.com:a.git", "git@git.example.com:a.git"),
        ("/srv/golden/repo", "/srv/golden/repo"),
    ],
)
def test_remote_url_without_credentials(url: str, expected: str) -> None:
    assert remote_url_without_credentials(url) == expected


@pytest.mark.parametrize("encoded", ["%0D", "%0A", "%00"])
@pytest.mark.parametrize("field", ["username", "password"])
def test_credentials_with_line_breaks_or_nul_are_rejected(
    encoded: str, field: str
) -> None:
    """A decoded username/password holding CR, LF or NUL could inject extra
    credential-protocol lines; such a URL is refused, and the error never
    names the value."""
    user = f"exa{encoded}mple" if field == "username" else "example-user"
    secret = f"tok{encoded}en-123" if field == "password" else "token-123"
    url = f"https://{user}:{secret}@git.example.com/a.git"

    for build in (build_non_interactive_git_env, remote_url_without_credentials):
        with pytest.raises(ValueError) as raised:
            build(url)
        message = str(raised.value)
        assert "credential" in message
        for part in (user, secret, unquote(user), unquote(secret), "tok", "exa"):
            assert part not in message, message


def test_ls_remote_succeeds_only_with_runtime_credentials(
    remote: str, tmp_path: Path
) -> None:
    clean = remote_url_without_credentials(remote)
    assert SECRET not in clean and USER not in clean

    with_credentials = subprocess.run(
        ["git", "ls-remote", clean],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=build_non_interactive_git_env(remote),
    )
    without = subprocess.run(
        ["git", "ls-remote", clean],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=build_non_interactive_git_env(),
    )

    assert with_credentials.returncode == 0, with_credentials.stderr
    assert "refs/heads/main" in with_credentials.stdout
    assert without.returncode != 0


def test_runtime_credentials_never_reach_git_configuration_values(remote: str) -> None:
    env = build_non_interactive_git_env(remote)
    count = int(env["GIT_CONFIG_COUNT"])
    entries = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)
    ]
    assert entries, env
    for key, value in entries:
        assert SECRET not in key and SECRET not in value, (key, value)
        assert USER not in key and USER not in value, (key, value)


def _clone_with_stored_userinfo(remote: str, tmp_path: Path, name: str) -> Path:
    """A clone whose origin URL still carries userinfo (an existing clone
    made before credentials were supplied at run time)."""
    clone = tmp_path / name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", remote_url_without_credentials(remote), str(clone)],
        capture_output=True,
        text=True,
        check=True,
        env=build_non_interactive_git_env(remote),
    )
    _git("remote", "set-url", "origin", remote, cwd=clone)
    assert SECRET in (clone / ".git" / "config").read_text()
    return clone


def test_existing_clone_origin_is_rewritten_once_and_still_fetches(
    remote: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clone = _clone_with_stored_userinfo(remote, tmp_path, "golden/repo")

    with caplog.at_level(logging.INFO):
        first = ensure_remote_url_without_credentials(str(clone))
        second = ensure_remote_url_without_credentials(str(clone))

    assert (first, second) == (1, 0)
    config = (clone / ".git" / "config").read_text()
    assert SECRET not in config and USER not in config, config
    assert _git("remote", "get-url", "origin", cwd=clone).strip() == (
        remote_url_without_credentials(remote)
    )
    rewritten = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(rewritten) == 1, [r.getMessage() for r in caplog.records]
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    fetched = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=clone,
        capture_output=True,
        text=True,
        env=build_non_interactive_git_env(remote),
    )
    assert fetched.returncode == 0, fetched.stderr


def test_versioned_snapshot_is_never_rewritten(remote: str, tmp_path: Path) -> None:
    snapshot = _clone_with_stored_userinfo(
        remote, tmp_path, ".versioned/repo/v_1700000000"
    )
    before = (snapshot / ".git" / "config").read_text()

    assert ensure_remote_url_without_credentials(str(snapshot)) == 0
    assert (snapshot / ".git" / "config").read_text() == before


def test_every_stored_remote_url_and_pushurl_is_sanitized(
    remote: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _clone_with_stored_userinfo(remote, tmp_path, "golden/repo")
    _git("config", "remote.origin.pushurl", remote, cwd=clone)
    _git("remote", "add", "mirror", remote, cwd=clone)
    argvs: List[List[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argvs.append([str(part) for part in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)

    assert ensure_remote_url_without_credentials(str(clone)) == 3
    config = (clone / ".git" / "config").read_text()
    assert SECRET not in config and USER not in config, config
    clean = remote_url_without_credentials(remote)
    for key in ("remote.origin.url", "remote.origin.pushurl", "remote.mirror.url"):
        assert _git("config", "--get", key, cwd=clone).strip() == clean, key
    for argv in argvs:
        assert not any(SECRET in part for part in argv), argv


def test_unreadable_configuration_is_raised_and_logged_without_the_url(
    remote: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.services.git_operations_service import (
        GitCommandError,
    )

    clone = _clone_with_stored_userinfo(remote, tmp_path, "golden/repo")
    config = clone / ".git" / "config"
    config.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(GitCommandError) as raised:
                ensure_remote_url_without_credentials(str(clone))
    finally:
        config.chmod(0o644)

    assert "removing credentials" in str(raised.value)
    assert (SECRET in str(raised.value)) is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, caplog.records
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_http_credentials_url_round_trips_through_run_time_supply() -> None:
    username, password = "ex ample:user@x/y", "p@ss:w/rd%2F tok"
    url = http_credentials_url(
        "https://git.example.com:8443/owner/repo.git", username, password
    )
    assert url is not None
    env: dict = {}

    supply_remote_credentials(env, url)

    assert env["CIDX_GIT_REMOTE_USERNAME"] == username
    assert env["CIDX_GIT_REMOTE_PASSWORD"] == password
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://git.example.com:8443.helper"


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@git.example.com/owner/repo.git",
        "git@git.example.com:owner/repo.git",
        "/srv/example/repo.git",
    ],
)
def test_http_credentials_url_is_none_for_other_transports(url: str) -> None:
    assert http_credentials_url(url, USER, SECRET) is None


def test_http_credentials_url_refuses_a_url_that_already_carries_credentials() -> None:
    with pytest.raises(ValueError) as exc_info:
        http_credentials_url(
            f"https://{USER}:{SECRET}@git.example.com/owner/repo.git", USER, "other"
        )

    assert SECRET not in str(exc_info.value)


@pytest.mark.parametrize("variable", ["GIT_ASKPASS", "SSH_ASKPASS"])
def test_inherited_askpass_program_is_never_invoked(
    remote: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    record = tmp_path / "askpass-invoked"
    script = tmp_path / "askpass.sh"
    script.write_text(f"#!/bin/sh\ntouch '{record}'\necho wrong-password\n")
    script.chmod(0o700)
    monkeypatch.setenv(variable, str(script))

    result = subprocess.run(
        ["git", "ls-remote", remote_url_without_credentials(remote)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=build_non_interactive_git_env(),
        timeout=60,
    )

    assert result.returncode != 0
    assert not record.exists(), f"{variable} program was invoked"


def test_append_run_time_git_config_appends_after_existing_entries() -> None:
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/srv/example",
    }

    append_run_time_git_config(env, [("a.b", "1"), ("c.d", "2")])

    assert env == {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/srv/example",
        "GIT_CONFIG_KEY_1": "a.b",
        "GIT_CONFIG_VALUE_1": "1",
        "GIT_CONFIG_KEY_2": "c.d",
        "GIT_CONFIG_VALUE_2": "2",
    }
