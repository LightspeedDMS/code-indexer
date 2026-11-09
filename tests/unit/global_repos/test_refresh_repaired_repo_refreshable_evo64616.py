"""issue-64616: a refresh stops before touching a clone that fetches from the
wrong repository, and initializes a source left with no configuration.

The refresh fetches ``origin``, never the registered URL. A writer sharing the
clone directory can repoint ``origin`` at another repository of the same name;
the refresh then failed as "registered branch gone upstream" / "git checkout
failed" -- or, when that repository had the branch, silently replaced the
alias's content with it. ``_ensure_origin_is_registered`` stops it first, by
repository identity:

* same repository (credentials, transport, ``.git`` and case aside): passes;
* a different repository: RuntimeError naming the alias and both
  credential-free URLs, logged at ERROR, before any checkout or fetch -- pinned
  on a real refresh over a real clone;
* a registered URL or origin that is not a remote git URL, or an origin that
  cannot be read: not compared, the refresh goes on as before;
* a cancelled job stops before the read.

``_initialize_unconfigured_source``: a source whose ``.code-indexer/`` holds no
valid configuration (an evicted base the backfill sweeps wrote into again,
nothing published to seed from) is initialized with the add's ``cidx init``
before indexing; a source without ``.code-indexer/`` or with a valid
configuration is left alone; a failed init fails the refresh naming the alias.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple, cast

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.services.progress_subprocess_runner import IndexingCancelledError
from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    REAL_GIT,
    make_git_repo_scheduler,
    make_origin_and_clone,
)
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import ALIAS

SCHEDULER_LOGGER = "code_indexer.global_repos.refresh_scheduler"
REGISTERED = "https://example.com/team/example-repo.git"
OTHER = "https://example.com/other-team/example-repo.git"
SECRET = "s3cr3t-token"


class StubGitBackend:
    """Answers ``git remote get-url origin`` with *origin* (or raises)."""

    def __init__(self, origin: Any = None, error: Optional[Exception] = None):
        self.origin = origin
        self.error = error
        self.calls: List[Tuple[str, str]] = []

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> Any:
        self.calls.append((repo_path, name))
        if self.error is not None:
            raise self.error
        return self.origin


def _check(
    backend: StubGitBackend,
    registered: str = REGISTERED,
    cancel_check: Any = None,
) -> None:
    scheduler = object.__new__(RefreshScheduler)
    scheduler._git_backend = cast(Any, backend)
    scheduler._ensure_origin_is_registered(
        ALIAS, "/golden/example-repo", registered, cancel_check
    )


# -- same repository: passes ----------------------------------------------------


@pytest.mark.parametrize(
    "registered, origin",
    [
        (REGISTERED, REGISTERED),
        (REGISTERED, "https://example.com/team/example-repo"),
        (f"https://oauth2:{SECRET}@example.com/team/example-repo.git", REGISTERED),
        (REGISTERED, "git@example.com:team/example-repo.git"),
        (REGISTERED, "https://EXAMPLE.com/Team/Example-Repo.git"),
    ],
)
def test_the_registered_repository_passes(registered: str, origin: str) -> None:
    backend = StubGitBackend(origin)
    _check(backend, registered=registered)
    assert backend.calls == [("/golden/example-repo", "origin")]


# -- another repository: stopped -----------------------------------------------


def test_another_repository_stops_the_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = StubGitBackend(
        f"https://oauth2:{SECRET}@example.com/other-team/example-repo"
    )
    with (
        caplog.at_level(logging.ERROR, logger=SCHEDULER_LOGGER),
        pytest.raises(RuntimeError) as excinfo,
    ):
        _check(
            backend,
            registered=f"https://user:{SECRET}@example.com/team/example-repo.git",
        )

    message = str(excinfo.value)
    assert ALIAS in message
    assert "example.com/other-team/example-repo" in message
    assert "example.com/team/example-repo.git" in message
    assert "not from its registered repository" in message
    assert SECRET not in message
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [message]


# -- not compared ---------------------------------------------------------------


@pytest.mark.parametrize(
    "registered",
    ["file:///golden/example-repo", "local://example-repo", "/golden/example-repo", ""],
)
def test_a_registered_url_that_is_not_remote_is_not_compared(registered: str) -> None:
    backend = StubGitBackend(OTHER)
    _check(backend, registered=registered)
    assert backend.calls == [], "origin not even read"


@pytest.mark.parametrize("origin", ["/srv/elsewhere/example-repo", "", None, 42])
def test_an_origin_that_is_not_a_remote_url_is_not_compared(origin: Any) -> None:
    _check(StubGitBackend(origin))


def test_an_unreadable_origin_is_left_to_the_fetch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = subprocess.CalledProcessError(2, ["git", "remote"], stderr="no remote")
    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        _check(StubGitBackend(error=error))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and ALIAS in warnings[0]
    assert "CalledProcessError" in warnings[0]


def test_a_cancelled_refresh_stops_before_the_read() -> None:
    backend = StubGitBackend(OTHER)
    with pytest.raises(IndexingCancelledError):
        _check(backend, cancel_check=lambda: True)
    assert backend.calls == []


def test_a_cancellation_during_the_read_propagates() -> None:
    with pytest.raises(IndexingCancelledError):
        _check(StubGitBackend(error=IndexingCancelledError("cancelled")))


# -- wired into a real refresh -----------------------------------------------------


@pytest.mark.skipif(REAL_GIT is None, reason="git is required")
def test_a_real_refresh_stops_before_checkout_fetch_or_index(tmp_path: Path) -> None:
    """The base clone's origin names another repository: the refresh raises
    before the branch check, any fetch or any indexing -- the clone is
    untouched and nothing is published. (Both URLs are unreachable: a refresh
    that got past the check would fail differently, on the network.)"""
    origin, master = make_origin_and_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    scheduler.registry.register_global_repo(
        "example-repo", ALIAS, REGISTERED, str(master)
    )
    _git("remote", "set-url", "origin", OTHER, cwd=master)
    head_before = _git("rev-parse", "HEAD", cwd=master)

    with pytest.raises(RuntimeError, match="not from its registered repository"):
        scheduler._execute_refresh(ALIAS)

    assert _git("rev-parse", "HEAD", cwd=master) == head_before
    assert _git("remote", "get-url", "origin", cwd=master) == OTHER
    assert recorder.calls == []


def _git(*args: str, cwd: Path) -> str:
    assert REAL_GIT is not None
    return subprocess.run(
        [REAL_GIT, *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


# -- a source left with no configuration -------------------------------------------


class InitRecorder:
    """Stands in for ``_repair_uninitialized_local_repo``."""

    def __init__(self, ok: bool = True, detail: Optional[str] = None) -> None:
        self.ok, self.detail = ok, detail
        self.calls: List[Tuple[str, str, Any]] = []

    def __call__(
        self, source_path: str, alias_name: str, cancel_check: Any = None
    ) -> Tuple[bool, Optional[str]]:
        self.calls.append((source_path, alias_name, cancel_check))
        return self.ok, self.detail


def _configure(source: Path, init: InitRecorder, cancel_check: Any = None) -> None:
    scheduler = SimpleNamespace(_repair_uninitialized_local_repo=init)
    scheduler._is_local_config_valid = lambda path: (
        RefreshScheduler._is_local_config_valid(cast(Any, scheduler), path)
    )
    RefreshScheduler._initialize_unconfigured_source(
        cast(Any, scheduler), ALIAS, str(source), cancel_check
    )


def test_index_state_without_configuration_is_initialized(tmp_path: Path) -> None:
    (tmp_path / ".code-indexer").mkdir()
    (tmp_path / ".code-indexer" / ".scip_reconcile_attempt").touch()
    init = InitRecorder()

    def cancel_check() -> bool:
        return False

    _configure(tmp_path, init, cancel_check)
    assert init.calls == [(str(tmp_path), ALIAS, cancel_check)]


def test_an_unparseable_configuration_is_initialized(tmp_path: Path) -> None:
    (tmp_path / ".code-indexer").mkdir()
    (tmp_path / ".code-indexer" / "config.json").write_text("{not json")
    init = InitRecorder()
    _configure(tmp_path, init)
    assert len(init.calls) == 1


def test_a_valid_configuration_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / ".code-indexer").mkdir()
    (tmp_path / ".code-indexer" / "config.json").write_text("{}")
    init = InitRecorder()
    _configure(tmp_path, init)
    assert init.calls == []


def test_a_source_without_index_state_is_left_as_before(tmp_path: Path) -> None:
    init = InitRecorder()
    _configure(tmp_path, init)
    assert init.calls == []


def test_a_failed_init_fails_the_refresh_naming_the_alias(tmp_path: Path) -> None:
    (tmp_path / ".code-indexer").mkdir()
    init = InitRecorder(ok=False, detail="init exploded")
    with pytest.raises(RuntimeError) as excinfo:
        _configure(tmp_path, init)
    assert ALIAS in str(excinfo.value)
    assert "init exploded" in str(excinfo.value)
