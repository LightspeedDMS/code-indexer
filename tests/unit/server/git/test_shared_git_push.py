"""Every server push -- REST, MCP (PAT) and the SCIP self-heal PR flow --
runs the ONE shared push implementation: argv ``git push [--set-upstream]
--end-of-options <remote> [refspec]`` after git_argv_safety validation,
credentials supplied at run time only.

Real repositories and a real local bare remote. Hosts and secrets are
neutral placeholders.
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import Mock

import pytest

GitRun = Tuple[List[str], Dict[str, str]]


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
def remote_and_repo(tmp_path: Path) -> Tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "repo"
    _git(["init", "-q", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    return bare, repo


@pytest.fixture
def git_runs(monkeypatch: pytest.MonkeyPatch) -> List[GitRun]:
    """argv and env of every git subprocess; every one runs for real."""
    runs: List[GitRun] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else []
        if argv[:1] == ["git"]:
            runs.append((argv, dict(kwargs.get("env") or {})))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return runs


def _pushes(runs: List[GitRun]) -> List[GitRun]:
    return [(argv, env) for argv, env in runs if argv[1:2] == ["push"]]


def _state_manager() -> Any:
    from code_indexer.server.services.git_state_manager import GitStateManager

    return GitStateManager(config=Mock())


def test_scip_self_heal_push_uses_the_shared_push_argv(
    remote_and_repo: Tuple[Path, Path], git_runs: List[GitRun]
) -> None:
    bare, repo = remote_and_repo
    _git(["checkout", "-q", "-b", "scip-fix-example"], repo)

    _state_manager()._push_branch_to_remote(repo, "scip-fix-example")

    assert [argv for argv, _env in _pushes(git_runs)] == [
        [
            "git",
            "push",
            "--set-upstream",
            "--end-of-options",
            "origin",
            "scip-fix-example",
        ]
    ]
    assert _git(["rev-parse", "scip-fix-example"], bare) == _git(
        ["rev-parse", "HEAD"], repo
    )
    assert _upstream(repo, "scip-fix-example") == "origin/scip-fix-example"


@pytest.fixture
def shared_push_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every call of the shared push (the real function runs)."""
    from code_indexer.server.git import git_push

    calls: List[Dict[str, Any]] = []
    real_push = git_push.push

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, "kwargs": kwargs})
        return real_push(*args, **kwargs)

    monkeypatch.setattr(git_push, "push", spy)
    return calls


def test_every_push_caller_runs_the_shared_push(
    remote_and_repo: Tuple[Path, Path],
    git_runs: List[GitRun],
    shared_push_calls: List[Dict[str, Any]],
) -> None:
    from code_indexer.server.services.git_operations_service import (
        git_operations_service,
    )

    bare, repo = remote_and_repo
    _git(["checkout", "-q", "-b", "scip-fix-example"], repo)

    _state_manager()._push_branch_to_remote(repo, "scip-fix-example")
    git_operations_service.git_push(repo, "origin", "main", set_upstream=False)
    git_operations_service.git_push_with_pat(
        repo, "origin", "pat-dest", {"token": "example-pat"}, set_upstream=False
    )

    assert [call["args"][2] for call in shared_push_calls] == [
        "scip-fix-example",
        "main",
        "HEAD:refs/heads/pat-dest",
    ]
    scip = shared_push_calls[0]["kwargs"]
    assert scip["credentials_url"] is None and scip["set_upstream"] is True
    scip_env = _pushes(git_runs)[0][1]
    assert scip_env.get("CIDX_GIT_REMOTE_PASSWORD") is None
    assert _git(["rev-parse", "pat-dest"], bare) == _git(["rev-parse", "HEAD"], repo)


def test_scip_push_refuses_an_option_like_branch_before_any_push(
    remote_and_repo: Tuple[Path, Path], git_runs: List[GitRun]
) -> None:
    from code_indexer.server.services.git_state_manager import GitStateError

    _bare, repo = remote_and_repo

    with pytest.raises(GitStateError) as exc_info:
        _state_manager()._push_branch_to_remote(repo, "--upload-pack=touch x")

    assert "git push refused" in str(exc_info.value)
    assert _pushes(git_runs) == []


PAT = "example-pat-654"


@contextmanager
def _served_clone(tmp_path: Path) -> Iterator[Tuple[str, Path]]:
    """(url, clone): a clone, plus one new commit, of a remote served over
    http that accepts only the PAT (as username and password)."""
    from code_indexer.server.git.git_subprocess_env import (
        build_non_interactive_git_env,
        http_credentials_url,
    )
    from tests.unit.server.git.auth_http_git_server import served_bare_remote

    with served_bare_remote(tmp_path, PAT, PAT) as url:
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", url, str(clone)],
            check=True,
            capture_output=True,
            env=build_non_interactive_git_env(http_credentials_url(url, PAT, PAT)),
        )
        _git(["config", "user.email", "test@example.com"], clone)
        _git(["config", "user.name", "Test User"], clone)
        (clone / "f.txt").write_text("pushed\n")
        _git(["add", "f.txt"], clone)
        _git(["commit", "-q", "-m", "c2"], clone)
        yield url, clone


@contextmanager
def _spy_server() -> Iterator[Tuple[str, List[str]]]:
    """(url, Authorization headers seen) of an http server that asks for
    credentials on every request."""
    seen: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _ask(self) -> None:
            if self.headers.get("Authorization"):
                seen.append(self.headers["Authorization"])
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="spy"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _ask
        do_POST = _ask

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/remote.git", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_push_through_ssh_to_https_rewrite_authenticates_and_keeps_ssh_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.git.git_push import push
    from code_indexer.server.git.git_subprocess_env import http_credentials_url

    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    with _served_clone(tmp_path) as (url, clone):
        ssh_url = "git@127.0.0.1:remote.git"
        _git(["remote", "set-url", "origin", ssh_url], clone)

        push(
            clone,
            "origin",
            "main",
            set_upstream=True,
            credentials_url=http_credentials_url(url, PAT, PAT),
            push_url=url,
        )

        served = tmp_path / "served" / "remote.git"
        assert _git(["rev-parse", "main"], served) == _git(["rev-parse", "HEAD"], clone)
        assert _git(["config", "--get", "remote.origin.url"], clone) == ssh_url
        assert _upstream(clone, "main") == "origin/main"


def test_second_url_on_the_same_remote_receives_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.git.git_push import (
        PushCredentialsNotApplicableError,
        push,
    )
    from code_indexer.server.git.git_subprocess_env import http_credentials_url

    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    with _served_clone(tmp_path) as (url, clone), _spy_server() as (spy, seen):
        _git(["config", "--add", "remote.origin.url", spy], clone)

        # A remote only partly at the credential's scope is refused before
        # any push: no URL of it receives anything.
        with pytest.raises(PushCredentialsNotApplicableError):
            push(
                clone,
                "origin",
                "main",
                set_upstream=False,
                credentials_url=http_credentials_url(url, PAT, PAT),
            )

        served = tmp_path / "served" / "remote.git"
        assert _git(["rev-parse", "main"], served) != _git(["rev-parse", "HEAD"], clone)
        assert seen == [], "the spy URL received a credential"


HTTPS_REMOTE = "https://git.example.com/example/repo.git"


def _recording_transport(tmp_path: Path) -> Tuple[Path, Path]:
    """(script, record): a transport program recording the credential it
    finds in its environment."""
    record = tmp_path / "transport-saw-credential"
    script = tmp_path / "transport.sh"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' \"$CIDX_GIT_REMOTE_PASSWORD\" > '{record}'\nexit 1\n"
    )
    script.chmod(0o700)
    return script, record


@pytest.mark.parametrize("transport", ["ext", "file"])
def test_pat_push_redirected_to_another_transport_is_refused(
    remote_and_repo: Tuple[Path, Path], tmp_path: Path, transport: str
) -> None:
    from code_indexer.server.services.git_operations_service import (
        GitCommandError,
        git_operations_service,
    )

    bare, repo = remote_and_repo
    script, record = _recording_transport(tmp_path)
    redirect = f"ext::{script}" if transport == "ext" else f"file://{bare}"
    _git(["remote", "set-url", "origin", HTTPS_REMOTE], repo)
    _git(["config", f"url.{redirect}.pushInsteadOf", HTTPS_REMOTE], repo)
    _git(["config", "protocol.ext.allow", "always"], repo)
    _git(["config", "protocol.file.allow", "always"], repo)

    with pytest.raises(GitCommandError):
        git_operations_service.git_push_with_pat(
            repo, "origin", "main", {"token": PAT}, set_upstream=False
        )

    assert not record.exists()
    refs = _git(["for-each-ref", "refs/heads/main"], bare)
    assert refs == ""


def test_credential_bearing_git_refuses_non_http_transports(
    remote_and_repo: Tuple[Path, Path], tmp_path: Path
) -> None:
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.utils.git_runner import run_git_command

    _bare, repo = remote_and_repo
    script, record = _recording_transport(tmp_path)
    _git(["config", "protocol.ext.allow", "always"], repo)

    result = run_git_command(
        ["git", "ls-remote", f"ext::{script}"],
        cwd=repo,
        check=False,
        credentials_url=http_credentials_url(HTTPS_REMOTE, PAT, PAT),
    )

    assert result.returncode != 0
    assert not record.exists()


def test_git_output_never_carries_the_supplied_credential(
    remote_and_repo: Tuple[Path, Path],
) -> None:
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.utils.git_runner import run_git_command

    _bare, repo = remote_and_repo
    echo = 'echo "token=$CIDX_GIT_REMOTE_PASSWORD"'
    _git(["config", "alias.leak", f"!{echo}; {echo} >&2; exit 1"], repo)
    credentials_url = http_credentials_url(HTTPS_REMOTE, PAT, PAT)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_git_command(["git", "leak"], cwd=repo, credentials_url=credentials_url)
    result = run_git_command(
        ["git", "leak"], cwd=repo, check=False, credentials_url=credentials_url
    )

    assert (PAT in str(exc_info.value.stderr)) is False
    assert (PAT in str(exc_info.value.output)) is False
    assert (PAT in str(exc_info.value)) is False
    assert (PAT in result.stderr) is False
    assert (PAT in result.stdout) is False
    assert "token=" in result.stderr


_ECHO_EVERY_ENCODING = """
import os, re, sys
from urllib.parse import quote, quote_plus
s = os.environ["CIDX_GIT_REMOTE_PASSWORD"]
forms = [s]
for encode in (quote, quote_plus):
    for safe in ("", "/"):
        forms.append(encode(s, safe=safe))
lower = [re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), f) for f in forms]
for form in forms + lower:
    print("echo:" + form)
    print("echo:" + form, file=sys.stderr)
sys.exit(1)
"""


def test_git_output_never_carries_any_encoding_of_the_supplied_credential(
    remote_and_repo: Tuple[Path, Path], tmp_path: Path
) -> None:
    """A supplied credential is masked in its raw form and in every
    percent/plus encoding, whatever the case of its escapes."""
    import re
    import sys
    from urllib.parse import quote, quote_plus

    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.utils.git_runner import run_git_command

    _bare, repo = remote_and_repo
    script = tmp_path / "echo_every_encoding.py"
    script.write_text(_ECHO_EVERY_ENCODING)
    _git(["config", "alias.leak", f"!{sys.executable} {script}"], repo)
    password = "abc def/@:ü€xyz"
    credentials_url = http_credentials_url(HTTPS_REMOTE, "example-user", password)
    forms = {password}
    for encode in (quote, quote_plus):
        for safe in ("", "/"):
            forms.add(encode(password, safe=safe))

    result = run_git_command(
        ["git", "leak"], cwd=repo, check=False, credentials_url=credentials_url
    )

    # Each echoed line is masked as a whole token (the secret holds ':', so
    # the "echo:" prefix may be part of the masked token).
    lines = result.stdout.splitlines()
    assert len(lines) == 10 and all("***" in line for line in lines), lines
    for output in (result.stdout, result.stderr):
        for form in forms:
            assert (form.lower() in output.lower()) is False, form
        assert re.search(r"abc[ +]def|abc%20def", output, re.I) is None


def test_fetch_and_pull_from_a_local_remote_carry_no_credential(
    remote_and_repo: Tuple[Path, Path], tmp_path: Path, git_runs: List[GitRun]
) -> None:
    """A credential applies only to a remote resolving to its scope: a
    local-path remote is fetched and pulled exactly as without one."""
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.server.services.git_operations_service import (
        git_operations_service,
    )

    bare, repo = remote_and_repo
    _git(["push", "-q", "origin", "main"], repo)
    other = tmp_path / "other"
    _git(["clone", "-q", "-b", "main", str(bare), str(other)], tmp_path)
    _git(["config", "user.email", "test@example.com"], other)
    _git(["config", "user.name", "Test User"], other)
    (other / "g.txt").write_text("two\n")
    _git(["add", "g.txt"], other)
    _git(["commit", "-q", "-m", "c2"], other)
    _git(["push", "-q", "origin", "HEAD:main"], other)
    git_runs.clear()
    credentials_url = http_credentials_url(HTTPS_REMOTE, PAT, PAT)

    fetched = git_operations_service.git_fetch(
        repo, "origin", credentials_url=credentials_url
    )
    pulled = git_operations_service.git_pull(
        repo, "origin", "main", credentials_url=credentials_url
    )

    assert fetched["success"] is True
    assert pulled["success"] is True
    assert (repo / "g.txt").exists()
    network = [env for argv, env in git_runs if argv[1:2] in (["fetch"], ["pull"])]
    assert len(network) == 2
    for env in network:
        assert env.get("CIDX_GIT_REMOTE_PASSWORD") is None
        assert env.get("GIT_ALLOW_PROTOCOL") is None


@pytest.mark.timeout(30)
def test_unresolvable_remote_fails_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination lookup is a bounded metadata call: when git cannot
    resolve the remote, the operation fails -- it never proceeds to send
    the credential."""
    import os
    import time

    from code_indexer.server.git import remote_credentials
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.utils import git_runner

    # ONE definition, in git_runner; remote_credentials keeps no copy.
    assert not hasattr(remote_credentials, "REMOTE_RESOLVE_TIMEOUT_SECONDS")
    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1)
    repo = tmp_path / "repo"
    _git(["init", "-q", str(repo)], tmp_path)
    fifo = tmp_path / "blocking-config"
    os.mkfifo(fifo)
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text()
        + f'[remote "origin"]\n\turl = {HTTPS_REMOTE}\n[include]\n\tpath = {fifo}\n'
    )

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        remote_credentials.credentials_for_remote(
            repo,
            "origin",
            http_credentials_url(HTTPS_REMOTE, PAT, PAT),
            push=True,
        )
    # The patched 1 s bound governs (plus git start-up), not the 30 s default.
    assert time.monotonic() - started < 5


def _blocking_config_repo(tmp_path: Path) -> Path:
    """A repository whose configuration includes a FIFO: every git command
    reading it blocks."""
    import os

    repo = tmp_path / "blocked"
    _git(["init", "-q", str(repo)], tmp_path)
    fifo = tmp_path / "blocking-include"
    os.mkfifo(fifo)
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text()
        + f'[remote "origin"]\n\turl = {HTTPS_REMOTE}\n[include]\n\tpath = {fifo}\n'
    )
    return repo


@pytest.mark.timeout(30)
@pytest.mark.parametrize("operation", ["push", "pat_push", "pull", "fetch"])
def test_every_remote_operation_fails_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Every local metadata git call on the push, pull and fetch paths is
    bounded; a timeout fails the operation, saying what timed out."""
    from code_indexer.server.git.git_subprocess_env import http_credentials_url
    from code_indexer.server.services.git_operations_service import (
        GitCommandError,
        git_operations_service,
    )
    from code_indexer.utils import git_runner

    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1, raising=False)
    repo = _blocking_config_repo(tmp_path)
    cred = http_credentials_url(HTTPS_REMOTE, PAT, PAT)

    with pytest.raises(GitCommandError) as exc_info:
        if operation == "push":
            git_operations_service.git_push(
                repo, "origin", "main", credentials_url=cred, set_upstream=False
            )
        elif operation == "pat_push":
            git_operations_service.git_push_with_pat(
                repo, "origin", "main", {"token": PAT}, set_upstream=False
            )
        elif operation == "pull":
            git_operations_service.git_pull(
                repo, "origin", "main", credentials_url=cred
            )
        else:
            git_operations_service.git_fetch(repo, "origin", credentials_url=cred)

    assert "resolving remote 'origin' timed out" in str(exc_info.value)


@pytest.mark.timeout(30)
def test_mcp_push_remote_preflight_fails_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP git_push handler's own remote URL lookup (before the PAT is
    chosen) is bounded and reports what timed out."""
    import time

    from code_indexer.server.mcp.handlers.git_write import (
        _get_pat_credential_for_remote,
    )
    from code_indexer.utils import git_runner

    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1)
    repo = _blocking_config_repo(tmp_path)

    started = time.monotonic()
    credential, remote_url, error = _get_pat_credential_for_remote(
        str(repo), "origin", "example-user"
    )

    assert time.monotonic() - started < 5
    assert credential is None and remote_url is None
    assert error is not None
    assert "resolving remote 'origin' timed out after 1s" in error


@pytest.mark.timeout(30)
def test_stored_url_sanitization_is_bounded_by_the_one_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored-URL sanitization run before every push, pull and fetch
    reads the clone's configuration under the same one bound."""
    import time

    from code_indexer.server.git import git_subprocess_env
    from code_indexer.utils import git_runner

    assert not hasattr(git_subprocess_env, "_LOCAL_GIT_TIMEOUT_SECONDS")
    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1)
    repo = _blocking_config_repo(tmp_path)

    from code_indexer.server.services.git_operations_service import GitCommandError

    started = time.monotonic()
    with pytest.raises(GitCommandError, match="timed out"):
        git_subprocess_env.ensure_remote_url_without_credentials(str(repo))

    assert time.monotonic() - started < 5


@pytest.mark.timeout(30)
def test_upstream_check_timeout_says_what_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current-branch/upstream check before a set_upstream push is
    bounded, and its timeout names that check -- not the push, not the
    remote lookup. (An empty remote skips the remote listing, so the
    upstream check is the first git call that reads the configuration.)"""
    import time

    from code_indexer.server.services.git_operations_service import (
        GitCommandError,
        git_operations_service,
    )
    from code_indexer.utils import git_runner

    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1)
    repo = _blocking_config_repo(tmp_path)

    started = time.monotonic()
    with pytest.raises(GitCommandError) as exc_info:
        git_operations_service.git_push(repo, "", None, set_upstream=True)

    assert time.monotonic() - started < 5
    message = str(exc_info.value)
    assert "checking the current branch's upstream timed out after 1s" in message
    assert "git push timed out" not in message
    assert "resolving remote" not in message


@pytest.mark.timeout(30)
def test_pushed_commit_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting the pushed commits after a completed push is a bounded
    local lookup; when it cannot finish, the push still reports success
    with the existing at-least-one count."""
    import time

    from code_indexer.server.services.git_operations_service import (
        git_operations_service,
    )
    from code_indexer.utils import git_runner

    monkeypatch.setattr(git_runner, "REMOTE_RESOLVE_TIMEOUT_SECONDS", 1)
    repo = _blocking_config_repo(tmp_path)
    pushed = subprocess.CompletedProcess(
        args=["git", "push"], returncode=0, stdout="", stderr="abc1234..def5678"
    )

    started = time.monotonic()
    count = git_operations_service._count_pushed_commits(pushed, repo)

    assert time.monotonic() - started < 5
    assert count == 1
