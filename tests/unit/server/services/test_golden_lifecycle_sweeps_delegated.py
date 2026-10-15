"""Golden-repo sweeps on a pod that does not hold the repositories.

With ``content_backend='delegated'`` a golden repo's directory on this pod is
index state; the repository is with workspace-mcp/git-mcp. A directory missing
HERE therefore never means "repository gone" -- the destructive bug class:

* the registry reconciler (Bug #1317) never submits a removal for a row whose
  index state is missing on this pod, and asks git-mcp for HEAD (GAP-4);
* the versioned-namespace reclaim (Bug #1570) never confirms a base clone
  absent from this pod's disk;
* the branch listing asks git-mcp -- byte-for-byte the local answer -- and
  fails loudly when it cannot.

Each sits next to a test pinning the local behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from code_indexer.server.services import golden_repo_reconciler as reconciler
from code_indexer.server.services import versioned_snapshot_reclaim as reclaim
from code_indexer.server.services.golden_repo_branch_service import (
    GoldenRepoBranchService,
)
from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import git_backend_wiring
from code_indexer.server.storage.shared.git_backend import LocalGitBackend


class _Delegated:
    is_local = False


def _park_delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: _Delegated())


# ── registry reconciler ─────────────────────────────────────────────────


def _sweep_manager(tmp_path: Path, *, delegated: bool) -> MagicMock:
    manager = MagicMock(spec=GoldenRepoManager)
    manager.golden_repos_dir = str(tmp_path)
    manager.job_tracker = None
    manager.list_golden_repos.return_value = [{"alias": "gone"}, {"alias": "a"}] + [
        {"alias": f"r{i}"} for i in range(4)
    ]

    def resolve(alias: str) -> str:
        if alias == "gone":
            raise GoldenRepoNotFoundError(alias)
        return str(tmp_path / alias)

    manager.get_actual_repo_path.side_effect = resolve
    manager.is_globally_active.return_value = False
    manager.repositories_are_delegated.return_value = delegated
    return manager


def test_local_sweep_removes_a_row_whose_clone_is_gone(tmp_path: Path) -> None:
    manager = _sweep_manager(tmp_path, delegated=False)

    result = reconciler.reconcile_golden_repo_registry(manager)

    manager.remove_orphaned_golden_repo.assert_called_once()
    assert result.orphans_removed == ["gone"]
    assert result.orphans_unconfirmed_delegated == []


def test_delegated_sweep_never_removes_on_missing_pod_index_state(
    tmp_path: Path,
) -> None:
    manager = _sweep_manager(tmp_path, delegated=True)

    result = reconciler.reconcile_golden_repo_registry(manager)

    manager.remove_orphaned_golden_repo.assert_not_called()
    assert result.orphans_found == []
    assert result.orphans_unconfirmed_delegated == ["gone"]


def test_delegated_sweep_is_also_gated_by_the_running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _sweep_manager(tmp_path, delegated=False)
    _park_delegated(monkeypatch)

    reconciler.reconcile_golden_repo_registry(manager)

    manager.remove_orphaned_golden_repo.assert_not_called()


class _GitMcp:
    owns_credentials = True

    def __init__(self, head: str = "abc1234def", fail: Optional[Exception] = None):
        self.head = head
        self.fail = fail
        self.asked: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        self.asked.append(repo_path)
        if self.fail is not None:
            raise self.fail
        return self.head


def test_delegated_head_comes_from_git_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _GitMcp()
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)

    head = reconciler._get_clone_git_head(str(tmp_path / "nowhere"), delegated=True)

    assert head == "abc1234def"
    assert git.asked == [str(tmp_path / "nowhere")]


def test_delegated_head_failure_raises_not_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _GitMcp(fail=subprocess.CalledProcessError(1, ["git"], stderr="down"))
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)

    with pytest.raises(subprocess.CalledProcessError):
        reconciler._get_clone_git_head(str(tmp_path), delegated=True)


def test_delegated_head_without_a_delegated_git_backend_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )

    with pytest.raises(RuntimeError, match="git_backend='delegated'"):
        reconciler._get_clone_git_head(str(tmp_path), delegated=True)


def test_local_head_of_a_non_repo_is_none(tmp_path: Path) -> None:
    assert reconciler._get_clone_git_head(str(tmp_path)) is None


def test_delegated_staleness_pass_triggers_refresh_from_git_mcp_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = _GitMcp(head="ffffffffffff")
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)
    monkeypatch.setattr(reconciler, "read_current_commit", lambda p: "aaaaaaaaaaaa")
    manager = _sweep_manager(tmp_path, delegated=True)
    manager.is_globally_active.return_value = True
    scheduler = MagicMock()
    result = reconciler.ReconcileResult()

    reconciler._reconcile_stale_indexes(
        scheduler, manager, [({"alias": "a"}, str(tmp_path / "a"))], "u", result
    )

    assert result.stale_indexes_refreshed == ["a"]
    assert git.asked == [str(tmp_path / "a")]


# ── versioned namespace reclaim ─────────────────────────────────────────


def test_local_reclaim_confirms_an_absent_base_clone(tmp_path: Path) -> None:
    assert reclaim.namespace_is_genuinely_orphaned("ns", tmp_path, set()) is True


def test_delegated_reclaim_never_confirms_absence_from_this_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _park_delegated(monkeypatch)

    assert reclaim.namespace_is_genuinely_orphaned("ns", tmp_path, set()) is False


# ── branch listing ──────────────────────────────────────────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T Tester", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A golden clone with local and origin branches, origin/HEAD set."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "a.txt").write_text("a\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "first", cwd=upstream)
    _git("checkout", "-qb", "feature/login", cwd=upstream)
    (upstream / "b.txt").write_text("b\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "second", cwd=upstream)
    _git("checkout", "-qb", "release/1.0", cwd=upstream)
    _git("checkout", "-q", "main", cwd=upstream)
    target = tmp_path / "golden" / "repo1"
    target.parent.mkdir()
    _git("clone", "-q", str(upstream), str(target), cwd=tmp_path)
    _git("checkout", "-qb", "local-only", cwd=target)
    _git("checkout", "-q", "main", cwd=target)
    return target


class _GitMcpOverLocal(LocalGitBackend):
    """git-mcp stand-in: the same git, but NOT a LocalGitBackend to the seam
    checks, answering for the pod path from the service's volume."""

    def __init__(self, pod_path: str, volume_path: str) -> None:
        self.pod_path = pod_path
        self.volume_path = volume_path
        self.fail: Optional[Exception] = None

    def _at(self, repo_path: str) -> str:
        assert repo_path == self.pod_path
        if self.fail is not None:
            raise self.fail
        return self.volume_path

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Any = None,
    ) -> List[str]:
        return super().list_branches(
            self._at(repo_path),
            remote=remote,
            all_refs=all_refs,
            fmt=fmt,
            timeout=timeout,
        )

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        return super().rev_parse(self._at(repo_path), *args, timeout=timeout)

    def log_raw(self, repo_path: str, fmt: str, revs: Any = (), **kw: Any) -> bytes:
        return super().log_raw(self._at(repo_path), fmt, revs, **kw)

    def for_each_ref(
        self, repo_path: str, fmt: str, patterns: Any = (), **kw: Any
    ) -> bytes:
        return super().for_each_ref(self._at(repo_path), fmt, patterns, **kw)


def _service(clone_path: Path, git: Any, *, delegated: bool) -> GoldenRepoBranchService:
    manager = MagicMock()
    manager.get_golden_repo.return_value = MagicMock(
        repo_url="https://git.example/team/repo1.git", clone_path=str(clone_path)
    )
    manager.get_actual_repo_path.return_value = str(clone_path)
    manager.repositories_are_delegated.return_value = delegated
    manager.delegated_git_backend.return_value = git
    return GoldenRepoBranchService(manager)


def test_delegated_branch_listing_matches_the_local_one(
    clone: Path, tmp_path: Path
) -> None:
    local = _service(clone, None, delegated=False).get_golden_repo_branches("repo1")

    pod_path = tmp_path / "pod" / "golden-repos" / "repo1"  # never created
    git = _GitMcpOverLocal(str(pod_path), str(clone))
    delegated = _service(pod_path, git, delegated=True).get_golden_repo_branches(
        "repo1"
    )

    assert [b.model_dump() for b in delegated] == [b.model_dump() for b in local]
    names = [b.name for b in local]
    assert names[0] == "main" and local[0].is_default
    assert set(names) == {"main", "feature/login", "release/1.0", "local-only"}


def test_delegated_branch_listing_fails_loud(clone: Path, tmp_path: Path) -> None:
    pod_path = tmp_path / "pod" / "repo1"
    git = _GitMcpOverLocal(str(pod_path), str(clone))
    git.fail = subprocess.CalledProcessError(1, ["git"], stderr="git-mcp down")

    with pytest.raises(GitOperationError, match="git-mcp down"):
        _service(pod_path, git, delegated=True).get_golden_repo_branches("repo1")


def test_pod_local_repo_keeps_the_local_listing(tmp_path: Path) -> None:
    folder = tmp_path / "langfuse_x"
    folder.mkdir()
    service = _service(folder, None, delegated=True)
    manager = cast(MagicMock, service.golden_repo_manager)
    manager.get_golden_repo.return_value = MagicMock(
        repo_url="local://langfuse_x", clone_path=str(folder)
    )

    assert service.get_golden_repo_branches("langfuse_x") == []
    manager.delegated_git_backend.assert_not_called()
