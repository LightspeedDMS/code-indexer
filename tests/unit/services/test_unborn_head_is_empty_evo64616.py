"""EVO-64616: an unborn HEAD (a repository with no commit) is a state, not an error.

A golden repo registered from an empty remote has no commit, so every HEAD
probe through git-mcp failed with git's ``ambiguous argument 'HEAD'``. The
index-freshness sweep logged that as "HEAD unavailable" at WARNING every poll,
for every empty repo, and gather_repo_metrics / the GAP-4 reconcile / default
branch detection did the same on their schedules. git's answer is definitive:
the repository exists and holds no commit. Each prober now treats it as
"no commit" -- exactly what its local path already answers -- while every
other failure stays loud.

The local half runs real git on a real empty repository, so the parity claim is
measured, not assumed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, List, cast

import pytest

import code_indexer.server.startup.content_backend_wiring as content_wiring
import code_indexer.server.startup.git_backend_wiring as git_wiring
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services import golden_repo_reconciler, index_freshness
from code_indexer.services import progress_subprocess_runner
from code_indexer.services.git_head import (
    UNBORN_HEAD_MARKER,
    head_is_unborn,
    is_unborn_head,
    is_unborn_head_stderr,
)

ALIAS = "example-repo"
#: What git-mcp relays for ``rev-parse HEAD`` in an empty repository.
UNBORN_STDERR = (
    "HEAD\nfatal: ambiguous argument 'HEAD': unknown revision or path not in "
    "the working tree."
)


def _unborn() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(128, "git-mcp", stderr=UNBORN_STDERR)


def _other_failure() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        128, "git-mcp", stderr="fatal: unable to read tree (deadbeef)"
    )


class _Seam:
    """A git backend whose HEAD probes all answer *outcome*."""

    def __init__(self, outcome: BaseException) -> None:
        self.outcome = outcome
        self.calls: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append(repo_path)
        raise self.outcome

    def rev_list_count(self, repo_path: str, *args: str, **kw: Any) -> int:
        raise self.outcome

    def ls_files(self, repo_path: str, *args: str, **kw: Any) -> List[str]:
        return []


def _warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


# -- the classifier -----------------------------------------------------------


class TestClassifier:
    def test_real_git_says_it_this_way(self, empty_repo: Path) -> None:
        for args in (
            ["rev-parse", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
            ["rev-list", "--count", "HEAD"],
        ):
            done = subprocess.run(
                ["git", "-C", str(empty_repo), *args], capture_output=True, text=True
            )
            assert done.returncode != 0
            assert UNBORN_HEAD_MARKER in done.stderr, args

    def test_unborn_head_is_recognised(self) -> None:
        assert is_unborn_head(_unborn())
        assert is_unborn_head_stderr(UNBORN_STDERR.encode())

    def test_other_git_failures_are_not(self) -> None:
        assert not is_unborn_head(_other_failure())
        assert not is_unborn_head(
            subprocess.CalledProcessError(
                128, "git", stderr="fatal: not a git repository"
            )
        )
        # Another revision being unknown is not an empty repository.
        assert not is_unborn_head_stderr("fatal: ambiguous argument 'HEAD~1'")

    def test_a_transport_failure_is_never_read_as_an_answer(self) -> None:
        assert not is_unborn_head(ConnectionError(UNBORN_STDERR))


# -- index freshness ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_head_cache() -> Any:
    index_freshness._reset_head_cache()
    yield
    index_freshness._reset_head_cache()


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos"
    (root / ALIAS / ".code-indexer").mkdir(parents=True)
    AliasManager(root / "aliases").create_alias(
        f"{ALIAS}-global", str(root / ".versioned" / ALIAS / "v_1"), repo_name=ALIAS
    )
    return root


def _delegate(monkeypatch: pytest.MonkeyPatch, seam: _Seam) -> None:
    monkeypatch.setattr(content_wiring, "is_content_seam_delegated", lambda: True)
    monkeypatch.setattr(git_wiring, "server_delegated_git_backend", lambda: seam)


class TestIndexFreshness:
    def test_empty_repo_is_no_commit_quietly_and_cached(
        self, golden: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        seam = _Seam(_unborn())
        _delegate(monkeypatch, seam)

        with caplog.at_level(logging.DEBUG):
            assert index_freshness.current_commit(golden / ALIAS) is None
            assert index_freshness.current_commit(golden / ALIAS) is None

        assert _warnings(caplog) == []
        assert len(seam.calls) == 1, "git's definitive answer is asked once"

    def test_other_failures_still_warn_and_are_not_cached(
        self, golden: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        seam = _Seam(_other_failure())
        _delegate(monkeypatch, seam)

        with caplog.at_level(logging.WARNING):
            assert index_freshness.current_commit(golden / ALIAS) is None
            assert index_freshness.current_commit(golden / ALIAS) is None

        assert any("unavailable" in m for m in _warnings(caplog))
        assert len(seam.calls) == 2


# -- gather_repo_metrics ----------------------------------------------------------


class TestGatherRepoMetrics:
    def test_through_backend_empty_repo_counts_zero_quietly(
        self, tmp_path: Path, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            counts = progress_subprocess_runner._gather_repo_metrics_through_backend(
                tmp_path, _Seam(_unborn())
            )

        assert counts == (0, 0)
        assert _warnings(caplog) == []

    def test_through_backend_other_failure_still_warns(
        self, tmp_path: Path, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            counts = progress_subprocess_runner._gather_repo_metrics_through_backend(
                tmp_path, _Seam(_other_failure())
            )

        assert counts == (0, 0)
        assert any("counting commits" in m for m in _warnings(caplog))

    def test_local_empty_repo_counts_zero_quietly(
        self, empty_repo: Path, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert progress_subprocess_runner.gather_repo_metrics(empty_repo) == (0, 0)

        assert _warnings(caplog) == []


# -- GAP-4 reconcile ------------------------------------------------------------


class TestReconcileHead:
    def test_delegated_empty_repo_has_no_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _delegate(monkeypatch, _Seam(_unborn()))

        assert (
            golden_repo_reconciler._get_clone_git_head(str(tmp_path), delegated=True)
            is None
        )

    def test_delegated_other_failure_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _delegate(monkeypatch, _Seam(_other_failure()))

        with pytest.raises(subprocess.CalledProcessError):
            golden_repo_reconciler._get_clone_git_head(str(tmp_path), delegated=True)

    def test_local_empty_repo_has_no_head(self, empty_repo: Path) -> None:
        assert golden_repo_reconciler._get_clone_git_head(str(empty_repo)) is None


# -- default branch of a fresh clone ---------------------------------------------------


class TestResolveClonedBranch:
    def test_empty_clone_uses_main_without_a_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
        manager._git_backend = cast(Any, _Seam(_unborn()))

        with caplog.at_level(logging.INFO):
            assert manager._resolve_cloned_branch(str(tmp_path)) == "main"

        assert _warnings(caplog) == []
        assert any("no commit" in r.getMessage() for r in caplog.records)

    def test_other_failure_still_warns(self, tmp_path: Path, caplog) -> None:
        manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
        manager._git_backend = cast(Any, _Seam(_other_failure()))

        with caplog.at_level(logging.WARNING):
            assert manager._resolve_cloned_branch(str(tmp_path)) == "main"

        assert any("Failed to detect default branch" in m for m in _warnings(caplog))


class _RevParse:
    def __init__(self, exc: Any = None) -> None:
        self.exc = exc

    def rev_parse(self, repo_path: str, *args: str, timeout: Any = None) -> str:
        if self.exc is not None:
            raise self.exc
        return "a" * 40


def test_head_is_unborn_reads_only_gits_unborn_answer_as_empty() -> None:
    assert head_is_unborn(_RevParse(), "/r") is False
    unborn = subprocess.CalledProcessError(128, ["git"], stderr=UNBORN_STDERR)
    assert head_is_unborn(_RevParse(unborn), "/r") is True
    other = subprocess.CalledProcessError(128, ["git"], stderr="fatal: boom")
    with pytest.raises(subprocess.CalledProcessError):
        head_is_unborn(_RevParse(other), "/r")
