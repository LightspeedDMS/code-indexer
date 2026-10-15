"""Golden-repo lifecycle on a pod that does not hold the repositories.

With ``content_backend='delegated'`` the repository is with workspace-mcp (the
files) and git-mcp (the history), at the same absolute path as
``golden-repos/<alias>`` on this pod, which holds only cidx's index state.
workspace-mcp's ``/clone-path/delete`` and ``/clone-path/stat`` (reached through
the server's DelegatedCloneBackend, as activated repositories reach theirs)
are how cidx deletes and asks about a clone there. On such a pod:

* REMOVE deletes the clone on workspace-mcp BEFORE the registry row goes (a
  failure aborts with the row intact; a re-run completes it), then this pod's
  index state -- never anything here cidx cannot prove is its own, never a
  quarantine rename. It never deletes a ``local://`` repository there, a path
  outside golden-repos, or an in-place registration, and the result says
  "deleted" only when workspace-mcp deleted something;
* a failed ADD and an add RETRY remove the partial clone on workspace-mcp
  when no registry row names the alias;
* the registry reconcile prunes a row only when workspace-mcp says its clone
  is gone (never on a missing directory HERE, never on an error), and
  backfills an identity from git-mcp -- never a made-up ``file://`` URL;
* branch lookups ask git-mcp and fail loudly instead of answering ``["main"]``.

Local IO on repository paths is forbidden in the delegated tests (only the
index state under ``.code-indexer`` and an EMPTY directory may go), and each
delegated behaviour sits next to a parity test pinning the local one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import DelegatedCloneBackend

ALIAS = "repo1"
REMOTE_URL = "https://git.example/team/repo1.git"
DELETE = "/clone-path/delete"
STAT = "/clone-path/stat"


class _WorkspaceMcp:
    """A delegated content backend. The lifecycle never needs to read files."""

    is_local = False

    def manifest(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("lifecycle must not list repository content")

    def fetch(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("lifecycle must not read repository content")


class _Response:
    def __init__(self, status_code: int, body: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Dict[str, Any]:
        return self._body


class _WorkspaceMcpClones(DelegatedCloneBackend):
    """The real DelegatedCloneBackend over a fake workspace-mcp transport.

    ``present`` is workspace-mcp's storage: the clone paths it holds. Only the
    HTTP POST is faked, so the real stat/delete request and answer handling
    run.
    """

    def __init__(self) -> None:
        super().__init__(SimpleNamespace(endpoint="http://wsm:3000", api_key="k"))
        self.present: Set[str] = set()
        self.calls: List[Tuple[str, str]] = []
        self.fail_status: Optional[int] = None

    def _post(self, route: str, body: dict, timeout: Any, what: str) -> Any:
        path = body["path"]
        self.calls.append((route, path))
        if self.fail_status is not None:
            return _Response(self.fail_status, {"message": "workspace-mcp down"})
        if route == STAT:
            held = path in self.present
            return _Response(200, {"path": path, "exists": held, "is_dir": held})
        if route == DELETE:
            held = path in self.present
            self.present.discard(path)
            return _Response(200, {"path": path, "deleted": held})
        raise AssertionError(f"unexpected workspace-mcp route {route}")


class _GitMcp:
    """A delegated git backend answering identity and branch questions."""

    owns_credentials = True

    def __init__(self) -> None:
        self.fail: Optional[Exception] = None
        self.calls: List[str] = []
        self.origin = REMOTE_URL
        self.head = "develop"
        self.remote_branches = ["origin/HEAD", "origin/main", "origin/feature/x"]

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> str:
        self._check("remote_get_url")
        return self.origin

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self._check("rev_parse")
        return self.head

    def list_branches(self, repo_path: str, **kw: Any) -> List[str]:
        self._check("list_branches")
        return list(self.remote_branches)


def _manager(
    data: Path,
    *,
    delegated: bool,
    git: Any = None,
    clones: Optional[_WorkspaceMcpClones] = None,
) -> GoldenRepoManager:
    kwargs: Dict[str, Any] = {}
    if delegated:
        kwargs = {"content_backend": _WorkspaceMcp(), "git_backend": git or _GitMcp()}
    mgr = GoldenRepoManager(str(data), **kwargs)
    bjm = MagicMock(spec=BackgroundJobManager)
    bjm.submit_job.return_value = "job-1"
    mgr.background_job_manager = bjm
    DatabaseSchema(mgr.db_path).initialize_database()
    if clones is not None:
        # How lifespan wires it: the snapshot manager's clone backend, the one
        # ActivatedRepoManager also gets.
        mgr._snapshot_manager = SimpleNamespace(_clone_backend=clones)
    return mgr


def _register(
    mgr: GoldenRepoManager,
    alias: str = ALIAS,
    url: str = REMOTE_URL,
    clone_path: Optional[str] = None,
) -> str:
    clone_path = clone_path or os.path.join(mgr.golden_repos_dir, alias)
    repo = GoldenRepo(
        alias=alias,
        repo_url=url,
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    mgr.golden_repos[alias] = repo
    mgr._sqlite_backend.add_repo(
        alias=alias,
        repo_url=url,
        default_branch="main",
        clone_path=clone_path,
        created_at=repo.created_at,
    )
    return clone_path


def _index_state(clone_path: str) -> Path:
    state = Path(clone_path) / ".code-indexer"
    (state / "index" / "coll").mkdir(parents=True)
    (state / "index" / "coll" / "hnsw_index.bin").write_bytes(b"x")
    (state / "config.json").write_text("{}")
    return state


def _repository_files(clone_path: str) -> None:
    """What a still-attached mount would show: the repository itself."""
    (Path(clone_path) / ".git").mkdir(parents=True, exist_ok=True)
    (Path(clone_path) / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (Path(clone_path) / "README.md").write_text("payload\n")


class LocalRepositoryIO(AssertionError):
    """A delegated lifecycle step deleted or moved repository files here."""


@contextmanager
def _forbid_local_repository_io(golden_repos_dir: str) -> Iterator[None]:
    """Fail on any local delete/move under golden-repos that is not index state.

    Allowed: anything under ``<alias>/.code-indexer`` (cidx's index state) and
    ``os.rmdir`` (which only ever removes an EMPTY directory).
    """
    root = os.path.abspath(golden_repos_dir)

    def _repository_path(path: Any) -> bool:
        full = os.path.abspath(os.fspath(path))
        if not full.startswith(root + os.sep):
            return False
        return ".code-indexer" not in Path(full).relative_to(root).parts

    def _guard(owner: Any, name: str, patcher: pytest.MonkeyPatch) -> None:
        original = getattr(owner, name)

        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            if _repository_path(path):
                raise LocalRepositoryIO(f"{name}({path!r})")
            return original(path, *args, **kwargs)

        patcher.setattr(owner, name, wrapper)

    with pytest.MonkeyPatch.context() as patcher:
        _guard(shutil, "rmtree", patcher)
        _guard(shutil, "move", patcher)
        _guard(os, "rename", patcher)
        _guard(os, "remove", patcher)
        _guard(os, "unlink", patcher)
        yield


def _remove(mgr: GoldenRepoManager, alias: str = ALIAS) -> Dict[str, Any]:
    mgr.remove_golden_repo(alias, submitter_username="admin")
    worker = mgr.background_job_manager.submit_job.call_args[1]["func"]
    return dict(worker())


def _delegated(tmp_path: Path) -> Tuple[GoldenRepoManager, _WorkspaceMcpClones]:
    clones = _WorkspaceMcpClones()
    return _manager(tmp_path, delegated=True, clones=clones), clones


# ── remove ──────────────────────────────────────────────────────────────


def test_local_remove_deletes_the_clone_and_says_nothing_more(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, delegated=False)
    clone = _register(mgr)
    _index_state(clone)
    _repository_files(clone)

    result = _remove(mgr)

    assert not os.path.exists(clone)
    assert result["message"] == f"Golden repository '{ALIAS}' removed successfully"
    assert not [k for k in result["cascade_results"] if k.startswith("repository_")]
    assert mgr._sqlite_backend.get_repo(ALIAS) is None


def test_delegated_remove_deletes_the_clone_on_workspace_mcp(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    _index_state(clone)
    clones.present.add(clone)

    with _forbid_local_repository_io(mgr.golden_repos_dir):
        result = _remove(mgr)

    assert clones.calls == [(DELETE, clone)]
    assert clone not in clones.present
    assert mgr._sqlite_backend.get_repo(ALIAS) is None
    assert not os.path.exists(clone)  # the pod's index state went too
    assert result["success"] is True
    assert f"deleted the repository at {clone} on workspace-mcp" in result["message"]
    assert result["cascade_results"]["repository_deleted_at"] == clone


def test_delegated_remove_failure_keeps_the_row_and_a_rerun_completes_it(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    _index_state(clone)
    clones.present.add(clone)
    clones.fail_status = 500

    with pytest.raises(GitOperationError) as err:
        _remove(mgr)

    message = str(err.value)
    assert "NOT removed" in message and "NOT deleted on workspace-mcp" in message
    assert "re-run the removal" in message
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None  # nothing destroyed
    assert (Path(clone) / ".code-indexer").is_dir()
    assert clone in clones.present

    clones.fail_status = None
    result = _remove(mgr)

    assert result["cascade_results"]["repository_deleted_at"] == clone
    assert mgr._sqlite_backend.get_repo(ALIAS) is None


def test_delegated_remove_of_an_absent_clone_never_claims_a_deletion(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)  # workspace-mcp holds nothing (a re-run, say)

    result = _remove(mgr)

    assert clones.calls == [(DELETE, clone)]
    assert "already absent on workspace-mcp" in result["message"]
    assert "deleted the repository" not in result["message"]
    assert result["cascade_results"]["repository_already_absent_at"] == clone
    assert "repository_deleted_at" not in result["cascade_results"]


def test_delegated_remove_retains_an_in_place_registration(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    in_place = os.path.join(mgr.golden_repos_dir, ALIAS)
    clone = _register(mgr, url=f"file://{in_place}")
    clones.present.add(clone)

    result = _remove(mgr)

    assert clones.calls == []
    assert clone in clones.present
    assert mgr._sqlite_backend.get_repo(ALIAS) is None
    assert f"the repository at {clone} was NOT deleted" in result["message"]
    assert "registered in place" in result["message"]
    assert result["cascade_results"]["repository_retained_at"] == clone


def test_delegated_remove_retains_a_clone_outside_golden_repos(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    elsewhere = str(tmp_path / "elsewhere" / ALIAS)
    _register(mgr, clone_path=elsewhere)
    clones.present.add(elsewhere)

    result = _remove(mgr)

    assert clones.calls == []
    assert result["cascade_results"]["repository_retained_at"] == elsewhere
    assert "NOT deleted" in result["message"]


def test_delegated_remove_fails_loud_without_a_workspace_mcp_clone_backend(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path, delegated=True)  # no DelegatedCloneBackend wired
    _register(mgr)

    with pytest.raises(GitOperationError, match="clone_backend='delegated'"):
        _remove(mgr)

    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_delegated_remove_waits_for_a_running_refresh(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    clones.present.add(clone)
    scheduler = MagicMock()
    scheduler.check_refresh_not_in_progress.side_effect = DuplicateJobError(
        "global_repo_refresh", ALIAS, "job-9"
    )
    mgr._refresh_scheduler = scheduler

    with pytest.raises(GitOperationError, match="a refresh is in progress"):
        _remove(mgr)

    assert clones.calls == []
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_delegated_remove_never_deletes_or_moves_local_entries_that_are_not_index_state(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    _index_state(clone)
    _repository_files(clone)  # entries here cidx cannot prove are its own
    clones.present.add(clone)

    with _forbid_local_repository_io(mgr.golden_repos_dir):
        with pytest.raises(GitOperationError) as err:
            _remove(mgr)

    # Index state gone; the other local entries untouched, not renamed.
    assert not (Path(clone) / ".code-indexer").exists()
    assert (Path(clone) / "README.md").read_text() == "payload\n"
    assert (Path(clone) / ".git" / "HEAD").exists()
    assert not [p for p in os.listdir(mgr.golden_repos_dir) if ".corrupt-" in p]
    assert f"deleted the repository at {clone} on workspace-mcp" in str(err.value)


def test_delegated_remove_of_a_pod_local_repo_keeps_the_local_behaviour(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr, alias="langfuse_x", url="local://langfuse_x")
    (Path(clone)).mkdir(parents=True)
    (Path(clone) / "trace.json").write_text("{}")

    result = _remove(mgr, alias="langfuse_x")

    assert not os.path.exists(clone)
    assert clones.calls == []
    assert not [k for k in result["cascade_results"] if k.startswith("repository_")]


# ── the index-state removal itself ──────────────────────────────────────


def test_pod_index_state_removal_leaves_a_symlink_target_alone(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, delegated=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = Path(mgr.golden_repos_dir) / "linked"
    link.symlink_to(target)

    assert mgr._remove_pod_index_state(str(link)) is True

    assert not os.path.lexists(link)
    assert (target / "keep.txt").read_text() == "keep"


# ── add: failure cleanup and retry ──────────────────────────────────────


def _failing_add(
    mgr: GoldenRepoManager,
    clone_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    repo_url: str = REMOTE_URL,
) -> None:
    clone_path = os.path.join(mgr.golden_repos_dir, ALIAS)

    def fake_clone(repo_url: str, alias: str, branch: Optional[str]) -> str:
        clone_setup(clone_path)
        return clone_path

    def fail(*a: Any, **kw: Any) -> None:
        raise GitOperationError("indexing failed")

    monkeypatch.setattr(mgr, "_clone_repository", fake_clone)
    monkeypatch.setattr(mgr, "_execute_post_clone_workflow", fail)
    with pytest.raises(GitOperationError, match="indexing failed"):
        mgr.execute_add_golden_repo_work(
            repo_url=repo_url,
            alias=ALIAS,
            default_branch="main",
            submitter_username="admin",
        )


def test_local_failed_add_removes_the_whole_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _manager(tmp_path, delegated=False)

    def setup(clone: str) -> None:
        _repository_files(clone)
        _index_state(clone)

    _failing_add(mgr, setup, monkeypatch)

    assert not os.path.exists(os.path.join(mgr.golden_repos_dir, ALIAS))


def test_delegated_failed_add_deletes_the_clone_on_workspace_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)

    def setup(path: str) -> None:
        clones.present.add(path)  # git-mcp cloned it there
        _repository_files(path)  # as if the mount were still attached
        _index_state(path)

    with _forbid_local_repository_io(mgr.golden_repos_dir):
        _failing_add(mgr, setup, monkeypatch)

    assert (DELETE, clone) in clones.calls
    assert clone not in clones.present
    assert not (Path(clone) / ".code-indexer").exists()
    assert (Path(clone) / "README.md").exists()  # never deleted through this pod


def test_delegated_clone_that_fails_partway_is_deleted_in_the_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git-mcp leaves a partial clone and fails (or the visibility wait times
    out): the job itself deletes it, so the next add does not have to."""
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)

    def partial_clone(repo_url: str, alias: str, branch: Optional[str]) -> str:
        clones.present.add(clone)
        raise GitOperationError("git-mcp clone failed halfway")

    monkeypatch.setattr(mgr, "_clone_repository", partial_clone)
    with _forbid_local_repository_io(mgr.golden_repos_dir):
        with pytest.raises(GitOperationError, match="halfway"):
            mgr.execute_add_golden_repo_work(
                repo_url=REMOTE_URL,
                alias=ALIAS,
                default_branch="main",
                submitter_username="admin",
            )

    assert clone not in clones.present
    assert clones.calls.count((DELETE, clone)) == 2  # retry pre-step + cleanup


def test_local_clone_failure_is_not_cleaned_by_path_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally the clone path is tracked only once cidx made it, as before."""
    mgr = _manager(tmp_path, delegated=False)
    clone = Path(mgr.golden_repos_dir) / ALIAS

    def failing_clone(repo_url: str, alias: str, branch: Optional[str]) -> str:
        _repository_files(str(clone))
        raise GitOperationError("clone failed")

    monkeypatch.setattr(mgr, "_clone_repository", failing_clone)
    with pytest.raises(GitOperationError, match="clone failed"):
        mgr.execute_add_golden_repo_work(
            repo_url=REMOTE_URL,
            alias=ALIAS,
            default_branch="main",
            submitter_username="admin",
        )

    assert (clone / "README.md").exists()


def test_delegated_failed_add_keeps_a_clone_a_registry_row_still_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, clones = _delegated(tmp_path)

    def setup(path: str) -> None:
        clones.present.add(path)
        clones.calls.clear()  # the add's up-front orphan check ran before this
        _register(mgr)  # e.g. the activation rollback could not remove it

    _failing_add(mgr, setup, monkeypatch)

    assert clones.calls == []
    assert os.path.join(mgr.golden_repos_dir, ALIAS) in clones.present
    assert "a registry row still names it" in caplog.text


def test_delegated_failed_add_keeps_an_in_place_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)

    def setup(path: str) -> None:
        clones.present.add(path)
        clones.calls.clear()
        _index_state(path)

    _failing_add(mgr, setup, monkeypatch, repo_url=f"file://{clone}")

    assert clones.calls == []
    assert clone in clones.present


def test_delegated_failed_add_reports_its_own_error_when_workspace_mcp_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, clones = _delegated(tmp_path)

    def setup(path: str) -> None:
        clones.present.add(path)
        clones.fail_status = 503  # after the add's up-front orphan check

    _failing_add(mgr, setup, monkeypatch)  # "indexing failed" is still raised

    assert "RESOURCE LEAK WARNING" in caplog.text


def test_delegated_retry_deletes_the_orphan_partial_clone_on_workspace_mcp(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    clones.present.add(clone)  # nothing on this pod at all

    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is True

    assert clones.calls == [(DELETE, clone)]
    assert clone not in clones.present


def test_delegated_retry_removes_orphan_index_state_never_local_repository_files(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    _index_state(clone)
    _repository_files(clone)

    with _forbid_local_repository_io(mgr.golden_repos_dir):
        assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is True

    assert not (Path(clone) / ".code-indexer").exists()
    assert (Path(clone) / "README.md").exists()
    assert clones.calls == [(DELETE, clone)]


def test_delegated_retry_with_nothing_anywhere_removes_nothing(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)

    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is False


def test_delegated_retry_never_touches_a_registered_repo(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    _index_state(clone)
    clones.present.add(clone)

    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is False
    assert (Path(clone) / ".code-indexer").exists()
    assert clones.calls == []


def test_delegated_retry_never_deletes_an_in_place_source(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    clones.present.add(clone)

    mgr._remove_orphan_clone_for_retry(f"file://{clone}", ALIAS)

    assert clones.calls == []
    assert clone in clones.present


def test_delegated_retry_fails_loud_when_workspace_mcp_cannot_delete(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    clones.present.add(os.path.join(mgr.golden_repos_dir, ALIAS))
    clones.fail_status = 500

    with pytest.raises(GitOperationError, match="destination exists"):
        mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS)


def test_local_retry_removes_the_orphan_clone(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, delegated=False)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    _repository_files(clone)

    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is True
    assert not os.path.exists(clone)


# ── registry reconcile from the filesystem ──────────────────────────────


def _no_activations(mgr: GoldenRepoManager) -> None:
    mgr.activated_repo_manager = MagicMock()
    mgr.activated_repo_manager.find_repos_by_golden_alias.return_value = []


def test_local_reconcile_prunes_a_row_whose_clone_is_gone(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, delegated=False)
    _no_activations(mgr)
    _register(mgr)  # no directory on disk

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 1
    assert mgr._sqlite_backend.get_repo(ALIAS) is None


def test_delegated_reconcile_never_prunes_on_a_missing_pod_directory(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    _no_activations(mgr)
    clone = _register(mgr)  # this pod has no index state for it
    clones.present.add(clone)

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 0
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None
    assert clones.calls == [(STAT, clone)]


def test_delegated_reconcile_prunes_a_row_whose_clone_workspace_mcp_lacks(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    _no_activations(mgr)
    clone = _register(mgr)
    _index_state(clone)  # index state HERE is no evidence either way

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 1
    assert mgr._sqlite_backend.get_repo(ALIAS) is None
    assert clones.calls == [(STAT, clone)]


def test_delegated_reconcile_never_prunes_on_a_workspace_mcp_error(
    tmp_path: Path,
) -> None:
    mgr, clones = _delegated(tmp_path)
    _no_activations(mgr)
    _register(mgr)
    clones.fail_status = 500

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 0
    assert counts["errors"] == 1
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_delegated_reconcile_without_a_clone_backend_prunes_nothing(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path, delegated=True)
    _no_activations(mgr)
    _register(mgr)

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 0
    assert counts["errors"] == 1
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_delegated_reconcile_skips_pod_local_rows(tmp_path: Path) -> None:
    mgr, clones = _delegated(tmp_path)
    _no_activations(mgr)
    _register(mgr, alias="langfuse_x", url="local://langfuse_x")  # not on this pod

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["pruned_orphan_rows"] == 0
    assert mgr._sqlite_backend.get_repo("langfuse_x") is not None
    assert clones.calls == []


def test_delegated_backfill_takes_the_identity_from_git_mcp(tmp_path: Path) -> None:
    git = _GitMcp()
    mgr = _manager(tmp_path, delegated=True, git=git)
    _index_state(os.path.join(mgr.golden_repos_dir, "orphan"))

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["backfilled"] == 1
    row = mgr._sqlite_backend.get_repo("orphan")
    assert row["repo_url"] == REMOTE_URL
    assert row["default_branch"] == "develop"


def test_delegated_backfill_fails_loud_instead_of_inventing_an_identity(
    tmp_path: Path,
) -> None:
    git = _GitMcp()
    git.fail = subprocess.CalledProcessError(1, ["git"], stderr="unreachable")
    mgr = _manager(tmp_path, delegated=True, git=git)
    _index_state(os.path.join(mgr.golden_repos_dir, "orphan"))

    counts = mgr.reconcile_registry_from_filesystem()

    assert counts["backfilled"] == 0
    assert counts["errors"] == 1
    assert mgr._sqlite_backend.get_repo("orphan") is None


def test_local_identity_falls_back_to_file_url_for_a_non_git_dir(
    tmp_path: Path,
) -> None:
    mgr = _manager(tmp_path, delegated=False)
    repo_dir = tmp_path / "plain"
    repo_dir.mkdir()

    assert mgr._reconstruct_repo_identity(repo_dir) == (f"file://{repo_dir}", "main")


# ── branch lookups ──────────────────────────────────────────────────────


def test_delegated_branch_lookup_asks_git_mcp(tmp_path: Path) -> None:
    git = _GitMcp()
    mgr = _manager(tmp_path, delegated=True, git=git)
    clone = _register(mgr)  # nothing on this pod at all

    assert mgr._get_repository_branches(clone) == ["main", "feature/x"]
    assert git.calls == ["list_branches"]


def test_delegated_branch_lookup_fails_loud_not_main(tmp_path: Path) -> None:
    git = _GitMcp()
    git.fail = subprocess.CalledProcessError(1, ["git"], stderr="git-mcp down")
    mgr = _manager(tmp_path, delegated=True, git=git)
    clone = _register(mgr)

    with pytest.raises(GitOperationError, match="git-mcp down"):
        mgr._get_repository_branches(clone)
    with pytest.raises(GitOperationError):
        mgr.find_by_canonical_url("git.example/team/repo1")


def test_local_branch_lookup_keeps_its_main_fallback(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, delegated=False)

    assert mgr._get_repository_branches(str(tmp_path / "missing")) == ["main"]


def test_delegated_branch_lookup_refuses_local_git(tmp_path: Path) -> None:
    mgr = GoldenRepoManager(str(tmp_path), content_backend=_WorkspaceMcp())

    with pytest.raises(GitOperationError, match="git_backend='delegated'"):
        mgr._get_repository_branches(str(tmp_path / "x"))


    def clone_into_existing(repo_url: str, alias: str, branch: Optional[str]) -> str:
        raise GitOperationError(f"destination path '{clone}' already exists")


    def lookup(alias: str) -> Any:
        if registry_down["now"]:
            raise RuntimeError("database is locked")
        return real_lookup(alias)


def test_delegated_clean_add_needs_no_workspace_mcp_clone_backend(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """content and git delegated, clone_backend not: a clean add reaches its
    clone instead of failing on a cleanup step that has nothing to clean."""
    mgr = _manager(tmp_path, delegated=True)
    cloned: List[str] = []

    _failing_add(mgr, cloned.append, monkeypatch)

    assert cloned == [os.path.join(mgr.golden_repos_dir, ALIAS)]
    assert "no workspace-mcp clone backend" in caplog.text


def test_delegated_failed_add_keeps_everything_when_the_registry_is_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, clones = _delegated(tmp_path)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    registry_down = {"now": False}
    real_lookup = mgr.get_golden_repo

    def lookup(alias: str) -> Any:
        if registry_down["now"]:
            raise RuntimeError("database is locked")
        return real_lookup(alias)

    def setup(path: str) -> None:
        clones.present.add(path)
        clones.calls.clear()
        _index_state(path)
        registry_down["now"] = True

    monkeypatch.setattr(mgr, "get_golden_repo", lookup)
    _failing_add(mgr, setup, monkeypatch)

    assert clones.calls == []
    assert (Path(clone) / ".code-indexer" / "config.json").exists()
    assert "database is locked" in caplog.text


def test_delegated_rerun_of_a_registered_add_keeps_its_index_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod-pull reclaim re-runs an add whose first executor had committed
    the row: its clone fails ("destination exists"), and the live repository's
    index state and clone must survive that failure."""
    mgr, clones = _delegated(tmp_path)
    clone = _register(mgr)
    state = _index_state(clone)
    clones.present.add(clone)

    def clone_into_existing(repo_url: str, alias: str, branch: Optional[str]) -> str:
        raise GitOperationError(f"destination path '{clone}' already exists")

    monkeypatch.setattr(mgr, "_clone_repository", clone_into_existing)
    with _forbid_local_repository_io(mgr.golden_repos_dir):
        with pytest.raises(GitOperationError, match="already exists"):
            mgr.execute_add_golden_repo_work(
                repo_url=REMOTE_URL,
                alias=ALIAS,
                default_branch="main",
                submitter_username="admin",
            )

    assert (state / "config.json").exists()
    assert (state / "index" / "coll" / "hnsw_index.bin").exists()
    assert clone in clones.present
    assert (DELETE, clone) not in clones.calls


def test_delegated_retry_without_a_clone_backend_removes_only_index_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mgr = _manager(tmp_path, delegated=True)
    clone = os.path.join(mgr.golden_repos_dir, ALIAS)
    _index_state(clone)

    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is True
    assert not (Path(clone) / ".code-indexer").exists()
    assert mgr._remove_orphan_clone_for_retry(REMOTE_URL, ALIAS) is False
    assert "destination exists" in caplog.text
