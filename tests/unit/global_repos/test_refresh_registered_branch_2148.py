"""Bug #2148: a refresh keeps the golden base clone on its REGISTERED branch.

The registered branch lives in golden_repos_metadata, in the store the
registration wrote: PostgreSQL in cluster mode, cidx_server.db in solo mode,
both reached through ``RefreshScheduler.golden_repo_metadata`` (the backend
registry). The refresh must never read a per-node SQLite file for this
decision in cluster mode, and must never guess the branch from
``origin/HEAD``: a guess switched clones registered on a non-default branch to
the remote default.

Cluster (or not-yet-determined) storage mode without the shared store wired,
and a known registered branch that cannot be checked out, FAIL the refresh
before it pulls or indexes. When the store is wired but the branch is simply
unknown (the read fails, no row -- Bug #469 orphan aliases --, a row without
a branch, a non-string value) the refresh switches nothing, logs a WARNING
naming the alias and the reason, and CONTINUES on the clone's current branch.

Real refreshes over a real bare origin and a real golden clone (no git
stand-ins). The origin's default branch is ``main``; ``feature-x`` is a
second branch.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable, Tuple
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.utils.registry_factory import (
    STORAGE_MODE_PENDING_SENTINEL,
)
from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    REAL_GIT,
    install_fake_git,
    make_git_repo_scheduler,
    make_origin_and_clone,
)
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import (
    ALIAS,
    RecordingSnapshotManager,
)

# golden_repos_metadata is keyed by the BARE alias; the global registry and
# the refresh use the "-global" alias (ALIAS == "example-repo-global").
BASE_ALIAS = ALIAS.removesuffix("-global")
READ_FAILURE_DETAIL = "unable to open database file"
SCHEDULER_LOGGER = "code_indexer.global_repos.refresh_scheduler"


def git_out(*args: str, cwd: Path) -> str:
    """Run the REAL git binary (never a PATH shim a test installed for the
    refresh) and return its stripped stdout."""
    assert REAL_GIT is not None
    result = subprocess.run(
        [REAL_GIT, *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def current_branch(clone: Path) -> str:
    return git_out("branch", "--show-current", cwd=clone)


def head_sha(clone: Path) -> str:
    return git_out("rev-parse", "HEAD", cwd=clone)


def make_feature_branch_clone(tmp_path: Path) -> Tuple[Path, Path]:
    """A bare origin whose default (origin/HEAD) is ``main`` plus a pushed
    ``feature-x`` branch, and a golden clone checked out on ``feature-x``."""
    origin, master = make_origin_and_clone(tmp_path)
    work = tmp_path / "work"
    git_out("checkout", "-b", "feature-x", cwd=work)
    (work / "feature_only.py").write_text("def feature_only():\n    pass\n")
    git_out("add", "-A", cwd=work)
    git_out("commit", "-m", "feature work", cwd=work)
    git_out("push", str(origin), "feature-x", cwd=work)
    git_out("fetch", "origin", cwd=master)
    git_out("checkout", "-b", "feature-x", "origin/feature-x", cwd=master)
    origin_head = git_out(
        "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=master
    )
    assert origin_head == "origin/main"
    assert current_branch(master) == "feature-x"
    return origin, master


def push_feature_commit(tmp_path: Path) -> None:
    """A new upstream commit on feature-x: a refresh that got past the branch
    check would pull it into the golden clone."""
    work = tmp_path / "work"
    (work / "feature_more.py").write_text("y = 2\n")
    git_out("add", "-A", cwd=work)
    git_out("commit", "-m", "more feature work", cwd=work)
    git_out("push", str(tmp_path / "origin.git"), "feature-x", cwd=work)


def per_node_sqlite(master: Path) -> GoldenRepoMetadataSqliteBackend:
    """The per-node solo store: cidx_server.db next to golden_repos/."""
    backend = GoldenRepoMetadataSqliteBackend(
        str(master.parent.parent / "cidx_server.db")
    )
    backend.ensure_table_exists()
    return backend


def register_branch(backend: Any, origin: Path, master: Path, branch: str) -> None:
    backend.add_repo(
        alias=BASE_ALIAS,
        repo_url=str(origin),
        default_branch=branch,
        clone_path=str(master),
        created_at="2026-01-01T00:00:00+00:00",
    )


class BranchReadFailingStore:
    """A real metadata store whose ``get_repo`` fails as an unreachable
    database does; every other call goes to the real store."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def get_repo(self, alias: str) -> Any:
        raise sqlite3.OperationalError(READ_FAILURE_DETAIL)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def server_storage_mode(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Run as a server process whose lifespan resolved *mode*. Without
    *registry_store* the lifespan has NOT yet wired
    ``app.state.backend_registry`` (the startup window); with it, the
    backend registry supplies *registry_store* as its shared
    ``golden_repo_metadata`` store. Only the ``app.state`` the storage-mode
    authority and the backend registry lookup read is provided."""

    # Any: the golden repo metadata stores (SQLite, PostgreSQL, the test
    # wrappers above) share no declared protocol type.
    def _set(mode: str, registry_store: Any = None) -> None:
        state = types.SimpleNamespace(storage_mode=mode)
        if registry_store is not None:
            state.backend_registry = types.SimpleNamespace(
                golden_repo_metadata=registry_store
            )
        module = types.ModuleType("code_indexer.server.app")
        setattr(module, "app", types.SimpleNamespace(state=state))
        monkeypatch.setitem(sys.modules, "code_indexer.server.app", module)

    return _set


def refresh(scheduler: RefreshScheduler) -> None:
    scheduler._execute_refresh(ALIAS)


def assert_refresh_fails_before_pull(
    tmp_path: Path,
    master: Path,
    scheduler: RefreshScheduler,
    recorder: RecordingSnapshotManager,
    reason: str,
) -> str:
    """The refresh raises naming the alias and *reason*; the clone keeps its
    branch and HEAD (nothing pulled), and nothing is published."""
    push_feature_commit(tmp_path)
    head_before = head_sha(master)

    with pytest.raises(RuntimeError) as excinfo:
        refresh(scheduler)

    message = str(excinfo.value)
    assert ALIAS in message, message
    assert reason in message, message
    assert current_branch(master) == "feature-x"
    assert head_sha(master) == head_before
    assert recorder.calls == []
    return message


def assert_refresh_continues_on_current_branch(
    tmp_path: Path,
    master: Path,
    scheduler: RefreshScheduler,
    caplog: pytest.LogCaptureFixture,
    reason: str,
) -> str:
    """The registered branch is unknown: nothing is switched (the clone
    stays on feature-x although origin/HEAD is main), exactly one WARNING
    names the alias and *reason* (never an exception message), and the
    refresh continues past the branch check -- it pulls the new upstream
    feature-x commit. Returns that WARNING's message."""
    push_feature_commit(tmp_path)
    upstream = git_out("rev-parse", "feature-x", cwd=tmp_path / "work")
    assert head_sha(master) != upstream

    # The golden clone is not cidx-initialised, so a refresh that continued
    # past the branch check and the pull fails later, at indexing -- never
    # with the registered-branch error.
    with (
        caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER),
        pytest.raises(RuntimeError, match="indexing on source failed") as excinfo,
    ):
        refresh(scheduler)

    assert "registered branch" not in str(excinfo.value)
    assert current_branch(master) == "feature-x"
    assert head_sha(master) == upstream, "the refresh must pull past the check"
    branch_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == SCHEDULER_LOGGER
        and "Registered branch of" in r.getMessage()
    ]
    assert len(branch_warnings) == 1, branch_warnings
    assert ALIAS in branch_warnings[0], branch_warnings
    assert reason in branch_warnings[0], branch_warnings
    assert READ_FAILURE_DETAIL not in branch_warnings[0], branch_warnings
    return branch_warnings[0]


def test_registry_store_branch_wins_over_empty_per_node_sqlite(
    tmp_path: Path,
) -> None:
    """The registry-resolved store (here a separate SQLite file standing in
    for the shared cluster store; the live-PostgreSQL twin is in
    tests/unit/server/storage/postgres) holds ``feature-x`` while the
    per-node cidx_server.db is empty, as on a cluster node."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    per_node_sqlite(master)  # table exists, zero rows
    shared = GoldenRepoMetadataSqliteBackend(str(tmp_path / "shared_store.db"))
    shared.ensure_table_exists()
    register_branch(shared, origin, master, "feature-x")
    scheduler.golden_repo_metadata = shared

    refresh(scheduler)

    assert current_branch(master) == "feature-x"


def test_solo_registered_branch_is_kept(tmp_path: Path) -> None:
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "feature-x")

    refresh(scheduler)

    assert current_branch(master) == "feature-x"


def test_clone_on_wrong_branch_is_corrected_to_registered_branch(
    tmp_path: Path,
) -> None:
    """Bug #469 is preserved: registered ``main``, clone left on ``feature-x``."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "main")

    refresh(scheduler)

    assert current_branch(master) == "main"


def test_detached_clone_is_put_on_registered_branch(tmp_path: Path) -> None:
    """A detached HEAD is not the registered branch: it is checked out."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "feature-x")
    git_out("checkout", "--detach", "HEAD", cwd=master)
    assert current_branch(master) == ""

    refresh(scheduler)

    assert current_branch(master) == "feature-x"


def test_failed_checkout_fails_refresh_before_pull(tmp_path: Path) -> None:
    """The registered branch cannot be checked out: the clone would stay on
    an unverified branch, so the refresh stops."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "no-such-branch")

    assert_refresh_fails_before_pull(
        tmp_path, master, scheduler, recorder, "git checkout failed"
    )


class BranchValueStore:
    """A real metadata store whose row for the alias carries *value* as its
    registered branch (e.g. a Mock, as a Mock store hands back)."""

    def __init__(self, real: Any, value: Any) -> None:
        self._real = real
        self._value = value

    def get_repo(self, alias: str) -> Any:
        row = self._real.get_repo(alias)
        return {**row, "default_branch": self._value}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _store_read_fails(origin: Path, master: Path) -> Any:
    return BranchReadFailingStore(per_node_sqlite(master))


def _store_without_row(origin: Path, master: Path) -> Any:
    return per_node_sqlite(master)  # readable, but no row for the alias


def _store_row_without_branch(origin: Path, master: Path) -> Any:
    store = per_node_sqlite(master)
    register_branch(store, origin, master, "")
    return store


def _store_row_with_non_str_branch(origin: Path, master: Path) -> Any:
    store = per_node_sqlite(master)
    register_branch(store, origin, master, "feature-x")
    return BranchValueStore(store, Mock())


@pytest.mark.parametrize(
    "make_store,reason",
    [
        (_store_read_fails, "read failed (OperationalError)"),
        (_store_without_row, "no golden repo metadata row"),
        (_store_row_without_branch, "no registered branch recorded"),
        (_store_row_with_non_str_branch, "no registered branch recorded"),
    ],
    ids=["read_fails", "no_row", "row_without_branch", "non_str_branch"],
)
def test_unknown_registered_branch_continues_without_switch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    make_store: Callable[[Path, Path], Any],
    reason: str,
) -> None:
    """The store is wired but the registered branch is unknown (Bug #469
    orphan aliases have no row): nothing is guessed (origin/HEAD is main, the
    clone is on feature-x), a WARNING names the alias and the reason (a read
    failure by exception TYPE only), and the refresh continues -- it pulls
    the new upstream feature-x commit."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    scheduler.golden_repo_metadata = make_store(origin, master)

    assert_refresh_continues_on_current_branch(
        tmp_path, master, scheduler, caplog, reason
    )


@pytest.mark.parametrize(
    "storage_mode", ["postgres", STORAGE_MODE_PENDING_SENTINEL], ids=["pg", "pending"]
)
def test_cluster_mode_without_registry_wiring_fails_closed(
    tmp_path: Path,
    server_storage_mode: Callable[[str], None],
    storage_mode: str,
) -> None:
    """Cluster (or not-yet-determined) storage mode before the backend
    registry is wired: the property would fall back to the per-node SQLite
    file, whose STALE row says ``main``. That row must never drive a
    checkout; the refresh fails before pulling."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "main")
    server_storage_mode(storage_mode)

    assert_refresh_fails_before_pull(
        tmp_path,
        master,
        scheduler,
        recorder,
        "shared golden repo metadata store is not wired",
    )


def test_postgres_mode_store_not_supplied_by_registry_fails_closed(
    tmp_path: Path,
    server_storage_mode: Callable[..., None],
) -> None:
    """Cluster mode with the backend registry wired: the scheduler's store
    is NOT the registry's own (and is not a per-node SQLite backend either),
    so its stale ``main`` must never drive a checkout; the refresh fails
    before pulling."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    shared = GoldenRepoMetadataSqliteBackend(str(tmp_path / "shared_store.db"))
    shared.ensure_table_exists()
    register_branch(shared, origin, master, "feature-x")
    stale = per_node_sqlite(master)
    register_branch(stale, origin, master, "feature-x")
    scheduler.golden_repo_metadata = BranchValueStore(stale, "main")
    server_storage_mode("postgres", shared)

    assert_refresh_fails_before_pull(
        tmp_path,
        master,
        scheduler,
        recorder,
        "shared golden repo metadata store is not wired",
    )


def test_postgres_mode_registry_supplied_store_is_used(
    tmp_path: Path,
    server_storage_mode: Callable[..., None],
) -> None:
    """Cluster mode: the scheduler's store IS the registry's own, so its
    registered branch (``main``) is trusted and the clone is put on it."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    shared_db = GoldenRepoMetadataSqliteBackend(str(tmp_path / "shared_store.db"))
    shared_db.ensure_table_exists()
    register_branch(shared_db, origin, master, "feature-x")
    shared = BranchValueStore(shared_db, "main")
    scheduler.golden_repo_metadata = shared
    server_storage_mode("postgres", shared)

    refresh(scheduler)

    assert current_branch(master) == "main"


BRANCH_READ_ARGV = ["git", "branch", "--show-current"]
BRANCH_READ_STDERR = "fatal: example branch read failure"
BRANCH_READ_FAILED = "current branch could not be read"


def fail_branch_read_with_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git branch`` exits 128 (a real process: a PATH shim that runs the
    real git for every other subcommand)."""
    install_fake_git(
        tmp_path,
        monkeypatch,
        tmp_path / "fake_git_pids.json",
        fail_on="branch",
        fail_stderr=BRANCH_READ_STDERR + "\n",
    )


def raise_on_git(monkeypatch: pytest.MonkeyPatch, argv_prefix: list) -> None:
    """The refresh's git call starting with *argv_prefix* raises
    TimeoutExpired, as a hung git does at its timeout; every other call
    runs the real git."""
    module = sys.modules[RefreshScheduler.__module__]
    real = module.run_with_cancel

    def wrapper(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if list(cmd[: len(argv_prefix)]) == argv_prefix:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(module, "run_with_cancel", wrapper)


def fail_branch_read_with_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raise_on_git(monkeypatch, BRANCH_READ_ARGV)


def reset_errors(caplog: pytest.LogCaptureFixture, branch: str) -> list:
    prefix = f"Failed to reset {ALIAS} to {branch}"
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR
        and r.name == SCHEDULER_LOGGER
        and r.getMessage().startswith(prefix)
    ]


FAIL_BRANCH_READ = pytest.mark.parametrize(
    "fail_branch_read",
    [fail_branch_read_with_exit_code, fail_branch_read_with_exception],
    ids=["exit_code", "raises"],
)


@FAIL_BRANCH_READ
def test_failed_branch_read_with_known_branch_fails_before_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_branch_read: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    """Registered ``main``, clone on feature-x: a checkout would succeed,
    but the clone's current branch was never read, so it is unverified --
    nothing is switched, one ERROR names the alias and branch, and the
    refresh fails before pulling."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "main")
    fail_branch_read(tmp_path, monkeypatch)

    with caplog.at_level(logging.ERROR, logger=SCHEDULER_LOGGER):
        assert_refresh_fails_before_pull(
            tmp_path, master, scheduler, recorder, BRANCH_READ_FAILED
        )

    errors = reset_errors(caplog, "main")
    assert len(errors) == 1, errors


def test_raising_checkout_logs_reset_error_and_fails_before_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The checkout to the registered branch raises (not a cancellation):
    the ERROR is logged before the refresh fails."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, "main")
    raise_on_git(monkeypatch, ["git", "checkout"])

    with caplog.at_level(logging.ERROR, logger=SCHEDULER_LOGGER):
        assert_refresh_fails_before_pull(
            tmp_path, master, scheduler, recorder, "git checkout failed"
        )

    errors = reset_errors(caplog, "main")
    assert len(errors) == 1, errors


@FAIL_BRANCH_READ
def test_failed_branch_read_with_unknown_branch_warns_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_branch_read: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    """Unknown registered branch AND a failed branch read: ONE combined
    WARNING names both (the read failure without git's output), no second
    WARNING, and the refresh continues."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    scheduler.golden_repo_metadata = per_node_sqlite(master)  # no row
    fail_branch_read(tmp_path, monkeypatch)

    warning = assert_refresh_continues_on_current_branch(
        tmp_path, master, scheduler, caplog, "no golden repo metadata row"
    )

    assert BRANCH_READ_FAILED in warning, warning
    assert BRANCH_READ_STDERR not in warning, warning
    other_branch_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == SCHEDULER_LOGGER
        and "current branch" in r.getMessage()
        and r.getMessage() != warning
    ]
    assert other_branch_warnings == [], other_branch_warnings


# A repository registered from a detached source (e.g. a local seed clone
# checked out at a tag) records the literal ``HEAD`` as its branch
# (``git rev-parse --abbrev-ref HEAD`` on a detached tree): there is no branch
# to enforce, so the branch check must do nothing at all.
DETACHED_REGISTRATION = "HEAD"


def record_git_commands(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every git argv the refresh scheduler runs; the real git still
    runs for each one."""
    module = sys.modules[RefreshScheduler.__module__]
    real = module.run_with_cancel
    commands: list = []

    def wrapper(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        commands.append(list(cmd))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(module, "run_with_cancel", wrapper)
    return commands


def branch_check_records(caplog: pytest.LogCaptureFixture) -> list:
    """WARNING/ERROR records the registered-branch check itself can emit."""
    markers = ("Base clone for", "Failed to reset", "Registered branch of")
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name == SCHEDULER_LOGGER
        and any(marker in r.getMessage() for marker in markers)
    ]


def refresh_past_branch_check(
    scheduler: RefreshScheduler, caplog: pytest.LogCaptureFixture
) -> None:
    """Run a refresh that gets past the branch check: the golden clone is
    not cidx-initialised, so it fails later, at indexing -- never with a
    registered-branch error."""
    with (
        caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER),
        pytest.raises(RuntimeError, match="indexing on source failed") as excinfo,
    ):
        refresh(scheduler)
    assert "registered branch" not in str(excinfo.value)


def test_detached_registration_on_detached_clone_switches_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registered ``HEAD``, clone detached: no checkout, no branch WARNING
    or ERROR, and the refresh continues past the branch check."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, DETACHED_REGISTRATION)
    git_out("checkout", "--detach", "HEAD", cwd=master)
    detached_at = head_sha(master)
    commands = record_git_commands(monkeypatch)

    # A detached clone has no upstream branch to compare, so the refresh may
    # finish (nothing to pull) instead of reaching indexing; either way it
    # must not stop at the branch check.
    with caplog.at_level(logging.WARNING, logger=SCHEDULER_LOGGER):
        try:
            refresh(scheduler)
        except RuntimeError as e:
            assert "branch" not in str(e), str(e)
    assert [c[:2] for c in commands].count(["git", "branch"]) == 1

    assert [c for c in commands if c[:2] == ["git", "checkout"]] == []
    assert branch_check_records(caplog) == []
    assert current_branch(master) == ""
    assert head_sha(master) == detached_at


def test_detached_registration_on_named_branch_keeps_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registered ``HEAD``, clone on feature-x: no checkout, no branch
    WARNING or ERROR; the clone stays on feature-x and the refresh pulls the
    new upstream feature-x commit."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, DETACHED_REGISTRATION)
    push_feature_commit(tmp_path)
    upstream = git_out("rev-parse", "feature-x", cwd=tmp_path / "work")
    commands = record_git_commands(monkeypatch)

    refresh_past_branch_check(scheduler, caplog)

    assert [c for c in commands if c[:2] == ["git", "checkout"]] == []
    assert branch_check_records(caplog) == []
    assert current_branch(master) == "feature-x"
    assert head_sha(master) == upstream, "the refresh must pull past the check"


@FAIL_BRANCH_READ
def test_detached_registration_with_failed_branch_read_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_branch_read: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    """Registered ``HEAD`` and the clone's branch cannot be read: there is
    no branch to verify, so nothing is raised, logged or checked out, and the
    refresh pulls the new upstream feature-x commit."""
    origin, master = make_feature_branch_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    register_branch(per_node_sqlite(master), origin, master, DETACHED_REGISTRATION)
    push_feature_commit(tmp_path)
    upstream = git_out("rev-parse", "feature-x", cwd=tmp_path / "work")
    fail_branch_read(tmp_path, monkeypatch)
    commands = record_git_commands(monkeypatch)

    refresh_past_branch_check(scheduler, caplog)

    assert [c for c in commands if c[:2] == ["git", "checkout"]] == []
    assert branch_check_records(caplog) == []
    assert current_branch(master) == "feature-x"
    assert head_sha(master) == upstream, "the refresh must pull past the check"
