"""issue-64616: a refresh of an EMPTY golden repository is a clean skip.

An empty repository -- no commit in the base clone (git's unborn HEAD) and no
branch upstream -- has nothing to check out, pull or index. A forced refresh
(the IndexStore backfill queues those) used to fail it at the registered-branch
checkout ("Cannot put the base clone ... git checkout failed"). Now:

* ``GitPullUpdater.is_empty_repository`` answers through the git seam, local
  git or git-mcp alike: one ``rev-parse HEAD`` for a clone with a commit; an
  unborn clone is fetched and asked for ``refs/remotes/origin``;
* a refresh (forced or not) of an empty repository returns success with
  ``skipped="empty_repository"`` and a reason, and publishes nothing;
* once upstream gets its first commit, the refresh proceeds as usual;
* any other git failure raises -- it never reads as "empty".

Real git against a real empty bare origin for the refresh; a recording fake
backend for the delegated (git-mcp) answers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pytest

from code_indexer.global_repos.git_pull_updater import GitPullUpdater
from code_indexer.global_repos.refresh_scheduler import (
    EMPTY_REPOSITORY_SKIP,
    RefreshScheduler,
)
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.services.git_head import EMPTY_REPOSITORY_REASON
from tests.unit.global_repos.test_refresh_git_cancel_2012 import (
    REAL_GIT,
    make_git_repo_scheduler,
)
from tests.unit.global_repos.test_refresh_registered_branch_2148 import (
    BASE_ALIAS,
    current_branch,
    git_out,
)
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import ALIAS

pytestmark = pytest.mark.slow

needs_git = pytest.mark.skipif(REAL_GIT is None, reason="git binary not installed")

UNBORN_STDERR = (
    "fatal: ambiguous argument 'HEAD': unknown revision or path not in the "
    "working tree.\n"
)


# ── the delegated answers (a recording git-mcp stand-in) ─────────────────────


class _GitMcp:
    """Answers like git-mcp: a git failure is a CalledProcessError."""

    owns_credentials = True

    def __init__(self, *, head: Optional[str], refs: bytes = b"") -> None:
        self.head = head
        self.refs = refs
        self.calls: List[Tuple[str, Any]] = []
        self.head_error: Optional[Exception] = None
        self.fetch_error: Optional[Exception] = None

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.calls.append(("rev_parse", args))
        if self.head_error is not None:
            raise self.head_error
        if self.head is None:
            raise subprocess.CalledProcessError(
                128, ["git", "rev-parse", "HEAD"], stderr=UNBORN_STDERR
            )
        return self.head

    def fetch(self, repo_path: str, **kw: Any) -> None:
        self.calls.append(("fetch", kw.get("remote")))
        if self.fetch_error is not None:
            raise self.fetch_error

    def for_each_ref(
        self, repo_path: str, fmt: str, patterns: Sequence[str] = (), **kw: Any
    ) -> bytes:
        self.calls.append(("for_each_ref", tuple(patterns)))
        return self.refs


def _updater(tmp_path: Path, backend: Any) -> GitPullUpdater:
    return GitPullUpdater(str(tmp_path), git_backend=backend)


def test_delegated_unborn_clone_with_no_upstream_branch_is_empty(
    tmp_path: Path,
) -> None:
    git = _GitMcp(head=None)
    assert _updater(tmp_path, git).is_empty_repository() is True
    assert git.calls == [
        ("rev_parse", ("HEAD",)),
        ("fetch", "origin"),
        ("for_each_ref", ("refs/remotes/origin",)),
    ]


def test_delegated_unborn_clone_whose_upstream_has_a_branch_is_not_empty(
    tmp_path: Path,
) -> None:
    git = _GitMcp(head=None, refs=b"refs/remotes/origin/main\n")
    assert _updater(tmp_path, git).is_empty_repository() is False


def test_a_clone_with_a_commit_is_not_empty_and_is_not_fetched(
    tmp_path: Path,
) -> None:
    git = _GitMcp(head="a" * 40)
    assert _updater(tmp_path, git).is_empty_repository() is False
    assert git.calls == [("rev_parse", ("HEAD",))]


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(128, ["git"], stderr="fatal: not a git repo"),
        ConnectionError("git-mcp unreachable"),
    ],
)
def test_other_head_failures_raise(tmp_path: Path, failure: Exception) -> None:
    git = _GitMcp(head=None)
    git.head_error = failure
    with pytest.raises(type(failure)):
        _updater(tmp_path, git).is_empty_repository()
    assert [c[0] for c in git.calls] == ["rev_parse"]


def test_a_failed_fetch_of_an_unborn_clone_is_a_classified_fetch_error(
    tmp_path: Path,
) -> None:
    """The same classified, redacted GitFetchError the change check raises."""
    from code_indexer.global_repos.git_error_classifier import GitFetchError

    git = _GitMcp(head=None)
    git.fetch_error = subprocess.CalledProcessError(
        128,
        ["git", "fetch"],
        stderr="fatal: unable to access 'https://user:s3cret@host/r.git/': "
        "Could not resolve host: host",
    )
    with pytest.raises(GitFetchError) as excinfo:
        _updater(tmp_path, git).is_empty_repository()
    assert excinfo.value.category == "transient"
    assert "s3cret" not in excinfo.value.stderr


def test_a_strategy_without_git_history_is_never_empty(tmp_path: Path) -> None:
    from code_indexer.global_repos.update_strategy import UpdateStrategy

    class _Directory(UpdateStrategy):
        def has_changes(self) -> bool:
            return True

        def update(self, force_reset: bool = False) -> None:
            return None

        def get_source_path(self) -> str:
            return str(tmp_path)

    assert _Directory().is_empty_repository() is False


# ── real refreshes over a real empty origin ──────────────────────────────────


def _empty_origin_and_clone(tmp_path: Path) -> Tuple[Path, Path]:
    """An empty bare origin (HEAD -> master, unborn, no refs) and its clone,
    as on tools-test (the registry says ``main``)."""
    origin = tmp_path / "origin.git"
    git_out("init", "--bare", "-b", "master", str(origin), cwd=tmp_path)
    golden = tmp_path / "golden_repos"
    golden.mkdir()
    master = golden / "example-repo"
    git_out("clone", str(origin), str(master), cwd=tmp_path)
    return origin, master


def _scheduler(tmp_path: Path, origin: Path, master: Path) -> Tuple[Any, Any]:
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    store = GoldenRepoMetadataSqliteBackend(str(tmp_path / "shared_store.db"))
    store.ensure_table_exists()
    store.add_repo(
        alias=BASE_ALIAS,
        repo_url=str(origin),
        default_branch="main",
        clone_path=str(master),
        created_at="2026-01-01T00:00:00+00:00",
    )
    scheduler.golden_repo_metadata = store
    return scheduler, recorder


@needs_git
@pytest.mark.parametrize("force_reset", [True, False])
def test_refresh_of_an_empty_repository_is_a_skip_not_a_failure(
    tmp_path: Path, force_reset: bool
) -> None:
    origin, master = _empty_origin_and_clone(tmp_path)
    scheduler, recorder = _scheduler(tmp_path, origin, master)

    result = scheduler._execute_refresh(ALIAS, force_reset=force_reset)

    assert result["success"] is True
    assert result["skipped"] == EMPTY_REPOSITORY_SKIP
    assert EMPTY_REPOSITORY_REASON in result["message"]
    assert recorder.calls == []


@needs_git
def test_scheduler_probe_answers_for_the_backfill(tmp_path: Path) -> None:
    origin, master = _empty_origin_and_clone(tmp_path)
    scheduler, _ = _scheduler(tmp_path, origin, master)
    assert scheduler.golden_repo_is_empty(ALIAS) is True


def test_scheduler_probe_is_false_for_local_and_orphaned_repos(
    tmp_path: Path,
) -> None:
    scheduler: Any = RefreshScheduler.__new__(RefreshScheduler)
    rows = {
        "loc-global": {"repo_url": "local://loc"},
        "gone-global": {"repo_url": "https://x/gone.git"},
    }
    scheduler.registry = type(
        "R", (), {"get_global_repo": staticmethod(lambda alias: rows.get(alias))}
    )()
    scheduler.golden_repos_dir = tmp_path
    scheduler._git_backend = _GitMcp(head=None)
    assert scheduler.golden_repo_is_empty("loc-global") is False
    assert scheduler.golden_repo_is_empty("missing-global") is False


@needs_git
def test_first_upstream_commit_ends_the_skip(tmp_path: Path) -> None:
    """Once ``main`` exists upstream the refresh checks it out and proceeds
    (here to indexing, which fails: the clone is not cidx-initialised)."""
    origin, master = _empty_origin_and_clone(tmp_path)
    scheduler, _ = _scheduler(tmp_path, origin, master)
    work = tmp_path / "work"
    git_out("init", "-b", "main", str(work), cwd=tmp_path)
    (work / "a.py").write_text("x = 1\n")
    git_out("add", "-A", cwd=work)
    git_out("commit", "-m", "first", cwd=work)
    git_out("push", str(origin), "main", cwd=work)

    with pytest.raises(RuntimeError, match="indexing on source failed"):
        scheduler._execute_refresh(ALIAS, force_reset=True)

    assert current_branch(master) == "main"


@needs_git
def test_a_failed_fetch_of_an_empty_clone_goes_through_fetch_error_handling(
    tmp_path: Path,
) -> None:
    import shutil

    origin, master = _empty_origin_and_clone(tmp_path)
    scheduler, recorder = _scheduler(tmp_path, origin, master)
    shutil.rmtree(origin)  # upstream unreachable: the fetch fails

    with pytest.raises(RuntimeError, match=r"Fetch failed for .*\(category="):
        scheduler._execute_refresh(ALIAS, force_reset=True)

    assert scheduler._fetch_failure_counts.get(ALIAS) == 1
    assert recorder.calls == []
