"""GitPullUpdater on a mount-less pod: the working tree is workspace-mcp's.

Under ``content_backend='delegated'`` git runs in git-mcp (the injected git
backend) and the clone's working tree lives with workspace-mcp at the same
absolute path; this pod's ``golden-repos/<alias>`` holds index state only. So:

* untracked files git names (a pull or reset blocked by them, or cidx's own
  artifacts in ``status --porcelain``) are removed THROUGH the content writer,
  never unlinked from this pod's disk -- where a same-named file is cidx's own
  state, or nothing at all;
* a writer failure, or no writer parked, fails the update loudly;
* the clone's presence is git-mcp's answer: a missing pod directory is not an
  orphaned repository.

Under the local backend each is today's code path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.git_pull_updater import GitPullUpdater
from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld

UNTRACKED_STDERR = (
    "error: The following untracked working tree files would be overwritten "
    "by merge:\n\t{names}\nPlease move or remove them before you merge.\n"
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


def _blocked_once(*names: str) -> MagicMock:
    """A git backend whose first pull/reset is blocked by untracked *names*."""
    err = subprocess.CalledProcessError(
        1, ["git"], output="", stderr=UNTRACKED_STDERR.format(names="\n\t".join(names))
    )
    backend = MagicMock()
    backend.status_porcelain.return_value = ""
    backend.pull.side_effect = [err, None]
    backend.reset.side_effect = [err, None]
    backend.rev_parse.return_value = "main"
    return backend


def _seed_both(world: MetaWorld, rel: str, data: str) -> None:
    for root in (world.local_meta, world.remote_meta):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(data)


# ── local: today's behaviour ─────────────────────────────────────────────


def test_local_pull_unlinks_the_untracked_file_from_this_disk(world: MetaWorld) -> None:
    _seed_both(world, "blocker.txt", "x")
    backend = _blocked_once("blocker.txt")
    GitPullUpdater(str(world.local_meta), git_backend=backend).update()
    assert not (world.local_meta / "blocker.txt").exists()
    assert (world.remote_meta / "blocker.txt").exists()
    assert world.writer.calls == []


def test_local_missing_clone_is_an_orphan(world: MetaWorld) -> None:
    with pytest.raises(OrphanedRepoError):
        GitPullUpdater(str(world.local_golden / "gone"), git_backend=MagicMock())


# ── delegated: through the content writer ────────────────────────────────


def test_delegated_pull_removes_the_blocker_through_the_writer(
    world: MetaWorld,
) -> None:
    _seed_both(world, "blocker.txt", "x")
    (world.pod_meta / "blocker.txt").write_text("pod-local, not the repo")
    world.delegated()
    backend = _blocked_once("blocker.txt")
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io():
        updater.update()
    assert world.writer.calls == [("delete", str(world.pod_meta / "blocker.txt"), {})]
    assert not (world.remote_meta / "blocker.txt").exists()
    assert (world.pod_meta / "blocker.txt").read_text() == "pod-local, not the repo"
    assert backend.pull.call_count == 2


def test_delegated_force_reset_removes_the_blocker_through_the_writer(
    world: MetaWorld,
) -> None:
    _seed_both(world, "blocker.txt", "x")
    world.delegated()
    backend = _blocked_once("blocker.txt")
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io():
        updater.update(force_reset=True)
    assert [c[0] for c in world.writer.calls] == ["delete"]
    assert not (world.remote_meta / "blocker.txt").exists()


def test_delegated_escaping_name_is_never_deleted(world: MetaWorld) -> None:
    world.delegated()
    backend = _blocked_once("../other-repo/file.txt")
    blocked = subprocess.CalledProcessError(
        1,
        ["git"],
        output="",
        stderr=UNTRACKED_STDERR.format(names="../other-repo/file.txt"),
    )
    backend.pull.side_effect = [blocked, blocked]
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io(), pytest.raises(RuntimeError, match="after untracked"):
        updater.update()
    assert world.writer.calls == []


def test_delegated_writer_failure_fails_the_update(world: MetaWorld) -> None:
    world.delegated()
    world.writer.fail = RuntimeError("workspace-mcp down")
    backend = _blocked_once("blocker.txt")
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io(), pytest.raises(RuntimeError, match="workspace-mcp"):
        updater.update()
    assert backend.pull.call_count == 1  # never retried as if it were removed


def test_delegated_without_a_writer_fails_loud(world: MetaWorld) -> None:
    world.delegated(writer=False)
    backend = _blocked_once("blocker.txt")
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io(), pytest.raises(RuntimeError, match="content writer"):
        updater.update()


def test_delegated_cidx_artifact_is_removed_from_the_repository_not_the_pod(
    world: MetaWorld,
) -> None:
    _seed_both(world, ".code-indexer-override.yaml", "repo copy")
    (world.pod_meta / ".code-indexer-override.yaml").write_text("pod config")
    world.delegated()
    backend = MagicMock()
    backend.status_porcelain.return_value = "?? .code-indexer-override.yaml\n"
    updater = GitPullUpdater(str(world.pod_meta), git_backend=backend)
    with world.forbid_local_io():
        updater.update()
    deleted: List[Any] = [c[1] for c in world.writer.calls]
    assert deleted == [str(world.pod_meta / ".code-indexer-override.yaml")]
    assert not (world.remote_meta / ".code-indexer-override.yaml").exists()
    assert (world.pod_meta / ".code-indexer-override.yaml").read_text() == "pod config"


def test_local_cidx_artifact_is_unlinked_from_this_disk(world: MetaWorld) -> None:
    (world.local_meta / ".code-indexer-override.yaml").write_text("x")
    backend = MagicMock()
    backend.status_porcelain.return_value = "?? .code-indexer-override.yaml\n"
    GitPullUpdater(str(world.local_meta), git_backend=backend).update()
    assert not (world.local_meta / ".code-indexer-override.yaml").exists()
    assert world.writer.calls == []


def test_delegated_missing_pod_dir_is_not_an_orphan(world: MetaWorld) -> None:
    """The clone's presence is git-mcp's answer: a fetch of a missing clone
    raises, it is never skipped as an orphan from this pod's disk."""
    world.delegated()
    backend = MagicMock()
    backend.fetch.side_effect = subprocess.CalledProcessError(
        128, ["git", "fetch"], output="", stderr="fatal: not a git repository"
    )
    updater = GitPullUpdater(str(world.pod_golden / "absent"), git_backend=backend)
    with pytest.raises(Exception, match="not a git repository"):
        updater.has_changes()
