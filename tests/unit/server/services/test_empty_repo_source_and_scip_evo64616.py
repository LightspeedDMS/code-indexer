"""issue-64616: an empty golden repo (unborn HEAD) is skipped, not raised as a failure.

Follows !220 (``git_head.is_unborn_head``). Two more HEAD readers met empty
repositories on the delegated fleet:

* ``golden_repo_source`` -- what the orchestrator invoker provisions an
  analysis from -- raised "git-mcp could not resolve HEAD", so description /
  lifecycle analysis of an empty repo became a crashed orchestrator job and a
  per-repo failure on every run. It now raises the typed
  ``GoldenRepoEmptyError`` (still a ``GoldenRepoSourceError``: no caller that
  does not know it is ever handed "no repository"); the lifecycle invoker
  resolves the source BEFORE submitting a job, and the batch runner skips the
  repo at INFO.
* SCIP's delegated HEAD read raised ``ScipRepoReadError``. "No commit" is
  git's answer there: ``head_commit`` returns None (the local reader's answer)
  and ``repo_has_root_build_file`` returns False (no files at all).

Every other failure still raises.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import lifecycle_claude_cli_invoker as lifecycle
from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.server.repositories import golden_repo_manager
from cidx_scip.server import repo_reads as scip_repo_reads
from cidx_scip.server.repo_reads import (
    ScipRepoEmptyError,
    ScipRepoReadError,
)
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from code_indexer.server.startup.content_backend_wiring import (
    GoldenRepoEmptyError,
    GoldenRepoSourceError,
    golden_repo_source,
)

ALIAS = "example-repo"
UNBORN = (
    "HEAD\nfatal: ambiguous argument 'HEAD': unknown revision or path not in "
    "the working tree."
)


def _unborn() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(128, "git-mcp", stderr=UNBORN)


def _broken() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(128, "git-mcp", stderr="fatal: bad object")


class _Git:
    def __init__(self, outcome: BaseException) -> None:
        self.outcome = outcome
        self.calls: List[str] = []

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append(repo_path)
        raise self.outcome


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "golden-repos"
    registry = SimpleNamespace(
        golden_repos_dir=str(root),
        get_golden_repo=lambda alias: SimpleNamespace(
            alias=alias,
            repo_url=f"https://example.com/g/{alias}.git",
            default_branch="main",
        )
        if alias == ALIAS
        else None,
    )
    monkeypatch.setattr(
        golden_repo_manager, "get_golden_repo_manager", lambda: registry
    )
    return root


def _git_mcp(monkeypatch: pytest.MonkeyPatch, outcome: BaseException) -> _Git:
    git = _Git(outcome)
    monkeypatch.setattr(git_backend_wiring, "server_delegated_git_backend", lambda: git)
    return git


# -- golden_repo_source --------------------------------------------------------


class TestGoldenRepoSource:
    def test_empty_repo_is_the_typed_empty_error(self, golden, monkeypatch) -> None:
        _git_mcp(monkeypatch, _unborn())

        with pytest.raises(GoldenRepoEmptyError, match="no commit yet"):
            golden_repo_source(golden / ALIAS)

    def test_the_empty_error_is_still_a_source_error(self) -> None:
        assert issubclass(GoldenRepoEmptyError, GoldenRepoSourceError)

    def test_any_other_git_failure_is_not_an_empty_repo(
        self, golden, monkeypatch
    ) -> None:
        _git_mcp(monkeypatch, _broken())

        with pytest.raises(GoldenRepoSourceError) as raised:
            golden_repo_source(golden / ALIAS)
        assert not isinstance(raised.value, GoldenRepoEmptyError)


# -- lifecycle invoker + batch runner ------------------------------------------


class TestLifecycleSkipsEmptyRepos:
    def test_invoker_raises_before_submitting_a_job(self, golden, monkeypatch) -> None:
        _git_mcp(monkeypatch, _unborn())
        dispatcher = MagicMock()
        monkeypatch.setattr(lifecycle, "lifecycle_analysis_refusal", lambda: None)
        monkeypatch.setattr(lifecycle, "is_content_seam_delegated", lambda: True)
        monkeypatch.setattr(lifecycle, "get_config_service", MagicMock())
        monkeypatch.setattr(
            lifecycle, "build_dep_map_dispatcher", lambda _config: dispatcher
        )

        with pytest.raises(GoldenRepoEmptyError):
            lifecycle.LifecycleClaudeCliInvoker()(ALIAS, golden / ALIAS)

        dispatcher.dispatch.assert_not_called()

    def test_batch_runner_skips_an_empty_repo_without_a_failure(
        self, tmp_path: Path, caplog
    ) -> None:
        (tmp_path / "cidx-meta").mkdir()
        tracker = MagicMock()

        def _invoker(alias: str, repo_path: Path, **_kw: object) -> Any:
            raise GoldenRepoEmptyError(f"golden repo {alias!r} has no commit yet")

        runner = LifecycleBatchRunner(
            golden_repos_dir=tmp_path,
            job_tracker=tracker,
            refresh_scheduler=MagicMock(),
            debouncer=MagicMock(),
            claude_cli_invoker=_invoker,
            concurrency=1,
            ttl_seconds=3600,
            estimated_seconds_per_repo=30,
        )

        with caplog.at_level(logging.INFO):
            failed = runner.run([ALIAS], parent_job_id="job-1")

        assert ALIAS not in failed
        tracker.fail_job.assert_not_called()
        assert not (tmp_path / "cidx-meta" / f"{ALIAS}.md").exists()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no commit yet" in r.getMessage() for r in caplog.records)


# -- SCIP HEAD reads -------------------------------------------------------------


@pytest.fixture
def scip_delegated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(scip_repo_reads, "_content_root", lambda root: Path(root))
    scip_repo_reads.reset_cache()


def _never(_path: Path) -> Optional[str]:
    raise AssertionError("delegated must not read local git")


class TestScipHeadReads:
    def test_empty_repo_has_no_commit(self, scip_delegated, monkeypatch, tmp_path):
        _git_mcp(monkeypatch, _unborn())

        assert scip_repo_reads.head_commit(tmp_path, local_reader=_never) is None

    def test_other_failure_still_raises(self, scip_delegated, monkeypatch, tmp_path):
        _git_mcp(monkeypatch, _broken())

        with pytest.raises(ScipRepoReadError) as raised:
            scip_repo_reads.head_commit(tmp_path, local_reader=_never)
        assert not isinstance(raised.value, ScipRepoEmptyError)

    def test_empty_repo_has_no_root_build_file(
        self, scip_delegated, monkeypatch, tmp_path
    ):
        _git_mcp(monkeypatch, _unborn())

        assert scip_repo_reads.repo_has_root_build_file(tmp_path) is False

    def test_root_build_file_other_failure_still_raises(
        self, scip_delegated, monkeypatch, tmp_path
    ):
        _git_mcp(monkeypatch, _broken())

        with pytest.raises(ScipRepoReadError):
            scip_repo_reads.repo_has_root_build_file(tmp_path)
