"""issue-64616: a golden refresh heals a base clone whose origin was repointed,
when its content is still the registered repository's -- and keeps the stop
otherwise.

The workspace-mcp seeder ran ``git remote set-url origin <other project>``
on clones of another project with the same name. Heal only when the clone
was cloned FROM the registered repository (HEAD's oldest reflog entry) AND
its content did not follow the repointed origin since (after fetching HEAD's
branch from it, HEAD is not on its history). Stop, naming the case and both
credential-free URLs, when the clone source is unknown, is a third project,
or the content followed the other project (or that cannot be determined).

Delegated: a git-mcp-shaped backend (owns its credentials, runs local
subcommands through ``run`` = ``git_exec``) -- every read and the repair go
through it, no local git. A git-mcp with !26 answers ``log --walk-reflogs``
(the heal tests); an older one refuses it, which reads as "source unknown":
stop (the refusal cases). Local: real git on real clones.
"""

from __future__ import annotations

import functools
import http.server
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

from code_indexer.global_repos import refresh_scheduler as scheduler_module
from code_indexer.global_repos.refresh_scheduler import (
    ORIGINS_RESTORED_KEY,
    RefreshScheduler,
)
from code_indexer.global_repos.repointed_origin import REFLOG_ARGS
from code_indexer.server.storage.shared.git_backend import local_git_backend
from code_indexer.services.progress_subprocess_runner import IndexingCancelledError
from tests.unit.global_repos.test_refresh_git_cancel_2012 import REAL_GIT
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import ALIAS

SCHEDULER_LOGGER = "code_indexer.global_repos.refresh_scheduler"
SECRET = "s3cr3t-token"
REGISTERED = "https://example.com/team/example-repo.git"
OTHER = "https://example.com/other-team/example-repo.git"
THIRD = "https://example.com/third-team/example-repo.git"
MASTER = "/golden/example-repo"


def _scheduler(backend: Any) -> RefreshScheduler:
    scheduler = object.__new__(RefreshScheduler)
    scheduler._git_backend = backend
    return scheduler


def _check(
    backend: Any, registered: str = REGISTERED, cancel_check: Any = None
) -> None:
    _scheduler(backend)._ensure_origin_is_registered(
        ALIAS, MASTER, registered, cancel_check
    )


# -- delegated: a git-mcp-shaped backend ---------------------------------------


class DelegatedBackend:
    """git-mcp as the scheduler sees it: owns its credentials; ``run`` is
    ``git_exec``. Records every call; nothing runs locally."""

    owns_credentials = True

    def __init__(
        self,
        origin: str = OTHER,
        reflog: Optional[List[str]] = None,
        reflog_error: Optional[Exception] = None,
        reflog_exit: int = 0,
        branch: Optional[str] = "main",
        fetch_error: Optional[Exception] = None,
        only_here: int = 3,
        set_url_error: Optional[Exception] = None,
    ) -> None:
        self.origin = origin
        self.reflog = (
            reflog
            if reflog is not None
            else ["pull: Fast-forward", f"clone: from {REGISTERED}"]
        )
        self.reflog_error = reflog_error
        self.reflog_exit = reflog_exit
        self.branch = branch
        self.fetch_error = fetch_error
        self.only_here = only_here
        self.set_url_error = set_url_error
        self.calls: List[Tuple[Any, ...]] = []

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> str:
        self.calls.append(("remote_get_url", repo_path, name))
        return self.origin

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Any = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        self.calls.append(("run", repo_path, tuple(args)))
        if self.reflog_error is not None:
            raise self.reflog_error
        return subprocess.CompletedProcess(
            ["git", *args],
            self.reflog_exit,
            stdout="\n".join(self.reflog) + "\n",
            stderr="fatal: refused" if self.reflog_exit else "",
        )

    def push(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        raise AssertionError("never pushes")

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Any = None
    ) -> str:
        self.calls.append(("symbolic_ref", repo_path))
        if self.branch is None:
            raise subprocess.CalledProcessError(
                128, ["git"], stderr="fatal: ref HEAD is not a symbolic ref"
            )
        return self.branch

    def fetch(self, repo_path: str, remote: str = "origin", **kwargs: Any) -> None:
        self.calls.append(("fetch", repo_path, remote, kwargs.get("ref")))
        if self.fetch_error is not None:
            raise self.fetch_error

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Any = None
    ) -> int:
        self.calls.append(("rev_list_count", repo_path, range_expr))
        return self.only_here

    def remote_set_url(
        self, repo_path: str, name: str, url: str, timeout: Any = None
    ) -> None:
        self.calls.append(("remote_set_url", repo_path, name, url))
        if self.set_url_error is not None:
            raise self.set_url_error


def _stop_message(backend: Any, **kwargs: Any) -> str:
    with pytest.raises(RuntimeError) as excinfo:
        _check(backend, **kwargs)
    message = str(excinfo.value)
    assert ALIAS in message and "not from its registered repository" in message
    assert SECRET not in message
    return message


def _set_urls(backend: DelegatedBackend) -> List[Tuple[Any, ...]]:
    return [c for c in backend.calls if c[0] == "remote_set_url"]


def test_delegated_heal_restores_the_credential_free_registered_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = DelegatedBackend(
        origin=f"https://oauth2:{SECRET}@example.com/other-team/example-repo.git"
    )
    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        _check(
            backend,
            registered=f"https://oauth2:{SECRET}@example.com/team/example-repo.git",
        )

    assert backend.calls == [
        ("remote_get_url", MASTER, "origin"),
        ("run", MASTER, REFLOG_ARGS),
        ("symbolic_ref", MASTER),
        ("fetch", MASTER, "origin", "main"),
        ("rev_list_count", MASTER, "refs/remotes/origin/main..HEAD"),
        ("remote_set_url", MASTER, "origin", REGISTERED),
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ALIAS in warnings[0]
    assert "example.com/other-team/example-repo.git" in warnings[0]
    assert f"to {REGISTERED}" in warnings[0]
    assert SECRET not in warnings[0]


@pytest.mark.parametrize(
    "backend, case",
    [
        (DelegatedBackend(reflog=[]), "the clone has no reflog"),
        (
            DelegatedBackend(reflog=["reset: moving to HEAD", "commit: x"]),
            "the oldest reflog entry is not the clone's",
        ),
        (DelegatedBackend(reflog_exit=128), "reading HEAD's reflog failed (exit 128"),
        (
            # git-mcp's git_exec refuses --walk-reflogs today
            DelegatedBackend(reflog_error=RuntimeError("git_exec refuses option")),
            "reading HEAD's reflog raised RuntimeError",
        ),
    ],
)
def test_delegated_unknown_source_keeps_the_stop(
    backend: DelegatedBackend, case: str
) -> None:
    message = _stop_message(backend)
    assert "where it was cloned from is unknown" in message
    assert case in message
    assert "example.com/other-team/example-repo.git" in message
    assert REGISTERED in message
    assert _set_urls(backend) == []
    assert not [c for c in backend.calls if c[0] == "fetch"]


class GitMcpRefusal(subprocess.CalledProcessError):
    """What the cidx_git_backend plugin raises for a git-mcp tool error."""


def test_git_mcp_before_26_refuses_the_reflog_read_and_the_stop_is_kept() -> None:
    refusal = GitMcpRefusal(
        1,
        "git-mcp",
        stderr="git-mcp git_exec tool error: git_exec refuses option "
        "--walk-reflogs for log",
    )
    backend = DelegatedBackend(reflog_error=refusal)
    message = _stop_message(backend)
    assert "where it was cloned from is unknown" in message
    assert _set_urls(backend) == []
    assert [c for c in backend.calls if c[0] == "run"] == [
        ("run", MASTER, ("log", "--walk-reflogs", "--format=%gs", "HEAD"))
    ]


def test_git_mcp_with_26_reads_the_reflog_and_heals() -> None:
    backend = DelegatedBackend(
        reflog=[
            "reset: moving to HEAD",
            "pull: Fast-forward",
            f"clone: from {REGISTERED}",
        ]
    )
    _check(backend)
    assert _set_urls(backend) == [("remote_set_url", MASTER, "origin", REGISTERED)]


def test_a_backend_that_cannot_read_the_reflog_keeps_the_stop() -> None:
    class ReadOnlyOrigin:
        def remote_get_url(self, *args: Any, **kwargs: Any) -> str:
            return OTHER

    message = _stop_message(ReadOnlyOrigin())
    assert "the git backend cannot read the reflog" in message


@pytest.mark.parametrize(
    "source",
    [
        THIRD,
        OTHER,
        "/srv/elsewhere/example-repo",
        f"https://u:{SECRET}@example.com/third-team/example-repo",
    ],
)
def test_delegated_third_project_keeps_the_stop(source: str) -> None:
    backend = DelegatedBackend(reflog=["pull: Fast-forward", f"clone: from {source}"])
    message = _stop_message(backend)
    assert "it holds another repository" in message
    assert "it was cloned from" in message
    assert REGISTERED in message
    assert _set_urls(backend) == []
    assert not [c for c in backend.calls if c[0] == "fetch"]


def test_delegated_content_that_followed_the_origin_keeps_the_stop() -> None:
    backend = DelegatedBackend(only_here=0)
    message = _stop_message(backend)
    assert (
        "the clone now holds https://example.com/other-team/example-repo.git" in message
    )
    assert _set_urls(backend) == []


@pytest.mark.parametrize(
    "backend, detail",
    [
        (DelegatedBackend(branch=None), "HEAD is detached"),
        (DelegatedBackend(fetch_error=RuntimeError("no such branch")), "RuntimeError"),
    ],
)
def test_delegated_undetermined_content_keeps_the_stop(
    backend: DelegatedBackend, detail: str
) -> None:
    message = _stop_message(backend)
    assert "could not be determined" in message and detail in message
    assert _set_urls(backend) == []


def test_a_failed_repair_keeps_the_stop() -> None:
    backend = DelegatedBackend(set_url_error=RuntimeError("git_set_remote failed"))
    message = _stop_message(backend)
    assert "restoring its origin to that URL failed (RuntimeError)" in message


def test_a_registered_url_with_a_bad_credential_is_never_written() -> None:
    backend = DelegatedBackend()
    message = _stop_message(
        backend, registered="https://user:a%0Ab@example.com/team/example-repo.git"
    )
    assert "restoring its origin to that URL failed (ValueError)" in message
    assert _set_urls(backend) == []


@pytest.mark.parametrize("stage_calls", [1, 2, 3])
def test_a_cancellation_between_steps_propagates(stage_calls: int) -> None:
    """The first armed check passes; cancelled before the source read, the
    content check, or the repair."""
    seen: List[int] = []

    def cancel_check() -> bool:
        seen.append(1)
        return len(seen) > stage_calls

    backend = DelegatedBackend()
    with pytest.raises(IndexingCancelledError):
        _check(backend, cancel_check=cancel_check)
    assert _set_urls(backend) == []


def test_a_cancellation_during_the_reflog_read_propagates() -> None:
    backend = DelegatedBackend(reflog_error=IndexingCancelledError("cancelled"))
    with pytest.raises(IndexingCancelledError):
        _check(backend)


def test_a_seeder_file_url_is_neither_stopped_nor_healed() -> None:
    backend = DelegatedBackend()
    _check(backend, registered="file:///golden/example-repo")
    assert backend.calls == []


# -- the heal is counted in the refresh result ---------------------------------


def test_the_heal_is_counted_in_the_refresh_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DelegatedBackend()
    scheduler = _scheduler(backend)
    monkeypatch.setattr(RefreshScheduler, "golden_repo_metadata", None)

    def impl(self: RefreshScheduler, alias_name: str, **kwargs: Any) -> Dict[str, Any]:
        self._ensure_origin_is_registered(alias_name, MASTER, REGISTERED, None)
        return {"success": True, "alias": alias_name}

    monkeypatch.setattr(RefreshScheduler, "_execute_refresh_impl", impl)
    monkeypatch.setattr(
        scheduler_module.failure_recovery, "current_trigger_generation", lambda *a: 0
    )
    monkeypatch.setattr(
        scheduler_module.failure_recovery, "settle_skip", lambda *a: None
    )

    result = scheduler._execute_refresh(ALIAS)
    assert result[ORIGINS_RESTORED_KEY] == 1
    assert scheduler_module._ORIGINS_RESTORED.get() is None

    backend.origin = REGISTERED
    assert ORIGINS_RESTORED_KEY not in scheduler._execute_refresh(ALIAS)


# -- local: real git on real clones, served over (dumb) http -------------------


def _git(*args: str, cwd: Path) -> str:
    assert REAL_GIT is not None
    return subprocess.run(
        [REAL_GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def forge(tmp_path: Path) -> Iterator[Tuple[Path, str]]:
    """A local http forge serving bare repositories under ``srv/`` (git's
    dumb protocol): real remote URLs, no network."""
    if REAL_GIT is None:
        pytest.skip("git is required")
    srv = tmp_path / "srv"
    srv.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(srv))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _project(tmp_path: Path, srv: Path, path: str) -> None:
    """A served bare repository *path* with one commit of its own on main."""
    work = tmp_path / path.replace("/", "-")
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "name.py").write_text(f"NAME = {path!r}\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", path, cwd=work)
    bare = srv / path
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("update-server-info", cwd=bare)


@pytest.fixture
def urls(tmp_path: Path, forge: Tuple[Path, str]) -> Dict[str, str]:
    srv, base = forge
    names = {
        "registered": "team/example-repo.git",
        "other": "other-team/example-repo.git",
        "third": "third-team/example-repo.git",
    }
    for path in names.values():
        _project(tmp_path, srv, path)
    return {key: f"{base}/{path}" for key, path in names.items()}


def _clone(tmp_path: Path, url: str) -> Path:
    master = tmp_path / "golden" / "example-repo"
    master.parent.mkdir(exist_ok=True)
    _git("clone", "-q", url, str(master), cwd=tmp_path)
    return master


def _local_check(master: Path, registered: str) -> None:
    _scheduler(local_git_backend())._ensure_origin_is_registered(
        ALIAS, str(master), registered, None
    )


def test_local_heal(tmp_path: Path, urls: Dict[str, str]) -> None:
    master = _clone(tmp_path, urls["registered"])
    _git("remote", "set-url", "origin", urls["other"], cwd=master)
    head = _git("rev-parse", "HEAD", cwd=master)

    _local_check(master, urls["registered"])

    assert _git("remote", "get-url", "origin", cwd=master) == urls["registered"]
    assert _git("rev-parse", "HEAD", cwd=master) == head


def test_local_content_that_followed_the_origin_keeps_the_stop(
    tmp_path: Path, urls: Dict[str, str]
) -> None:
    """The tools-test shape after the repoint: reset to the other project."""
    master = _clone(tmp_path, urls["registered"])
    _git("remote", "set-url", "origin", urls["other"], cwd=master)
    _git("fetch", "-q", "origin", cwd=master)
    _git("reset", "-q", "--hard", "origin/main", cwd=master)

    with pytest.raises(RuntimeError, match="the clone now holds"):
        _local_check(master, urls["registered"])
    assert _git("remote", "get-url", "origin", cwd=master) == urls["other"]


def test_local_third_project_keeps_the_stop(
    tmp_path: Path, urls: Dict[str, str]
) -> None:
    master = _clone(tmp_path, urls["third"])
    _git("remote", "set-url", "origin", urls["other"], cwd=master)

    with pytest.raises(RuntimeError, match="it holds another repository"):
        _local_check(master, urls["registered"])
    assert _git("remote", "get-url", "origin", cwd=master) == urls["other"]


def test_local_unknown_source_keeps_the_stop(
    tmp_path: Path, urls: Dict[str, str]
) -> None:
    master = _clone(tmp_path, urls["registered"])
    _git("remote", "set-url", "origin", urls["other"], cwd=master)
    _git("reflog", "expire", "--expire=now", "--all", cwd=master)

    with pytest.raises(RuntimeError, match="where it was cloned from is unknown"):
        _local_check(master, urls["registered"])
    assert _git("remote", "get-url", "origin", cwd=master) == urls["other"]


def test_local_origin_already_registered_is_untouched(
    tmp_path: Path, urls: Dict[str, str]
) -> None:
    """The acm / general-5 shape: cloned from A, origin repointed to B, which
    IS the registered repository, content reset to B -- consistent; passes
    without a reflog read."""
    master = _clone(tmp_path, urls["other"])
    _git("remote", "set-url", "origin", urls["registered"], cwd=master)
    _git("fetch", "-q", "origin", cwd=master)
    _git("reset", "-q", "--hard", "origin/main", cwd=master)

    _local_check(master, urls["registered"])
    assert _git("remote", "get-url", "origin", cwd=master) == urls["registered"]
