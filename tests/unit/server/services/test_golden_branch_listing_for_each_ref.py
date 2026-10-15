"""Golden-repo branch listing in ONE ``for_each_ref`` round trip.

Under ``content_backend='delegated'`` the golden repo's git is with git-mcp.
The listing asks it once (``git_for_each_ref`` with the local format and
patterns) whatever the branch count, and parses the answer with the same
helper as the local ``git for-each-ref`` path -- so the two answers are
identical. A git-mcp failure raises; it never reads as "no branches".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GitOperationError
from code_indexer.server.services import golden_repo_branch_service as svc
from code_indexer.server.services.golden_repo_branch_service import (
    GoldenRepoBranchService,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

_BRANCH_COUNT = 25


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=T Tester", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone with many origin branches, a local-only one, origin/HEAD set."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "a.txt").write_text("a\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "first", cwd=upstream)
    for i in range(_BRANCH_COUNT):
        _git("branch", f"feature/f{i:02d}", cwd=upstream)
    _git("checkout", "-qb", "release/1.0", cwd=upstream)
    (upstream / "b.txt").write_text("b\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "second", cwd=upstream)
    _git("checkout", "-q", "main", cwd=upstream)
    target = tmp_path / "golden" / "repo1"
    target.parent.mkdir()
    _git("clone", "-q", str(upstream), str(target), cwd=tmp_path)
    _git("checkout", "-qb", "local-only", cwd=target)
    _git("checkout", "-q", "main", cwd=target)
    return target


class _CountingGitMcp(LocalGitBackend):
    """git-mcp stand-in: real git on the service's volume, every call counted,
    and NOT a LocalGitBackend to the seam checks."""

    def __init__(self, pod_path: str, volume_path: str) -> None:
        self.pod_path = pod_path
        self.volume_path = volume_path
        self.calls: Dict[str, int] = {}
        self.fail: Optional[Exception] = None
        self.for_each_ref_args: List[Any] = []

    def _at(self, op: str, repo_path: str) -> str:
        assert repo_path == self.pod_path
        self.calls[op] = self.calls.get(op, 0) + 1
        if self.fail is not None:
            raise self.fail
        return self.volume_path

    def for_each_ref(
        self, repo_path: str, fmt: str, patterns: Any = (), **kw: Any
    ) -> bytes:
        self.for_each_ref_args.append((fmt, tuple(patterns)))
        return super().for_each_ref(
            self._at("for_each_ref", repo_path), fmt, patterns, **kw
        )

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        return super().rev_parse(
            self._at("rev_parse", repo_path), *args, timeout=timeout
        )

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Any = None,
    ) -> List[str]:
        self._at("list_branches", repo_path)
        raise AssertionError("the listing must not list branch names separately")

    def log_raw(self, repo_path: str, fmt: str, revs: Any = (), **kw: Any) -> bytes:
        self._at("log_raw", repo_path)
        raise AssertionError("the listing must not ask for tips per branch")


def _service(clone_path: Path, git: Any, *, delegated: bool) -> GoldenRepoBranchService:
    manager = MagicMock()
    manager.get_golden_repo.return_value = MagicMock(
        repo_url="https://git.example/team/repo1.git", clone_path=str(clone_path)
    )
    manager.get_actual_repo_path.return_value = str(clone_path)
    manager.repositories_are_delegated.return_value = delegated
    manager.delegated_git_backend.return_value = git
    return GoldenRepoBranchService(manager)


def _delegated(clone: Path, tmp_path: Path) -> tuple:
    pod_path = tmp_path / "pod" / "golden-repos" / "repo1"  # never created
    git = _CountingGitMcp(str(pod_path), str(clone))
    return _service(pod_path, git, delegated=True), git


def test_delegated_listing_is_one_for_each_ref_whatever_the_branch_count(
    clone: Path, tmp_path: Path
) -> None:
    service, git = _delegated(clone, tmp_path)

    branches = service.get_golden_repo_branches("repo1")

    assert len(branches) == _BRANCH_COUNT + 3  # + main, release/1.0, local-only
    assert git.calls == {"for_each_ref": 1, "rev_parse": 1}
    assert git.for_each_ref_args == [
        (svc._BRANCH_LISTING_FMT, svc._BRANCH_LISTING_PATTERNS)
    ]


def test_delegated_listing_equals_the_local_parse(clone: Path, tmp_path: Path) -> None:
    local = _service(clone, None, delegated=False).get_golden_repo_branches("repo1")
    service, _ = _delegated(clone, tmp_path)

    delegated = service.get_golden_repo_branches("repo1")

    assert [b.model_dump() for b in delegated] == [b.model_dump() for b in local]
    assert local[0].name == "main" and local[0].is_default
    assert {"local-only", "release/1.0", "feature/f00"} <= {b.name for b in local}


def test_delegated_default_branch_falls_back_to_a_local_common_name(
    clone: Path, tmp_path: Path
) -> None:
    _git("remote", "set-head", "origin", "-d", cwd=clone)
    service, _ = _delegated(clone, tmp_path)

    branches = service.get_golden_repo_branches("repo1")

    assert [b.name for b in branches if b.is_default] == ["main"]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["git-mcp"], stderr="git-mcp down"),
        subprocess.CalledProcessError(128, ["git-mcp"], stderr="fatal: not a git"),
    ],
)
def test_delegated_listing_failure_raises_never_empty(
    clone: Path, tmp_path: Path, error: Exception
) -> None:
    service, git = _delegated(clone, tmp_path)
    git.fail = error

    with pytest.raises(GitOperationError, match="Git operation failed"):
        service.get_golden_repo_branches("repo1")


def test_local_listing_runs_the_same_argv_as_before(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[List[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(svc.subprocess, "run", recording_run)
    _service(clone, None, delegated=False).get_golden_repo_branches("repo1")

    assert seen[0] == [
        "git",
        "for-each-ref",
        "--format=%(refname:short)|%(objectname)|%(committerdate:iso8601)"
        "|%(authorname)|%(subject)",
        "refs/heads/",
        "refs/remotes/origin/",
    ]


def test_local_backend_for_each_ref_is_git_for_each_ref(clone: Path) -> None:
    expected = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "--sort=-refname", "--count=3"],
        cwd=clone,
        check=True,
        capture_output=True,
    ).stdout

    out = LocalGitBackend().for_each_ref(
        str(clone), "%(refname)", (), sort="-refname", count=3
    )

    assert out == expected and out.count(b"\n") == 3


def test_local_backend_for_each_ref_patterns_are_never_options(clone: Path) -> None:
    out = LocalGitBackend().for_each_ref(str(clone), "%(refname)", ["--contains=HEAD"])
    assert out == b""  # a pattern after ``--``: matches no ref, runs no option


def test_local_backend_for_each_ref_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        LocalGitBackend().for_each_ref(str(tmp_path), "%(refname)")
