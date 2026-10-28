from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _BRANCH,
    _FAKE_KEY,
    _FAIL_NEXT_BRANCH_ISOLATION,
    _cli_semantic_hits,
    _git,
    _git_init,
    cluster_mode,  # noqa: F401  (pytest fixture: solo epoch and cluster fail-closed)
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server
from tests.utils.source_tree_age import age_source_files

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

_DOOMED = "doomed_000"
_KEPT = "kept_000"

# ^ why mark the real result cancelled, not signal the child; a cancelled run reports it only through BranchIndexingResult
_CANCEL_NEXT_BRANCH_PROCESSING = """
import os as _os

if _os.path.exists(_CANCEL_FLAG):
    from code_indexer.services import high_throughput_processor as _processor

    _real_process = _processor.HighThroughputProcessor.process_branch_changes_high_throughput

    def _process_cancelled_once(self, *args, **kwargs):
        result = _real_process(self, *args, **kwargs)
        if _os.path.exists(_CANCEL_FLAG):
            _os.unlink(_CANCEL_FLAG)
            result.cancelled = True
        return result

    _processor.HighThroughputProcessor.process_branch_changes_high_throughput = (
        _process_cancelled_once
    )
"""


def _head(repo: FakeProviderRepo) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _watermark(repo: FakeProviderRepo) -> Optional[str]:
    watermarks: Any = repo.metadata().get("branch_commit_watermarks") or {}
    value = watermarks.get(_BRANCH)
    return None if value is None else str(value)


def _commit_all(repo: FakeProviderRepo, message: str) -> None:
    _git(repo.repo, "add", "-A")
    _git(repo.repo, "commit", "-qm", message)


def _fts_hits(repo: FakeProviderRepo, name: str) -> str:
    proc = repo.query(f"{name}_function_7", "--fts")
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


def _base_indexed_repo(
    tmp_path: Path, server: Any, cluster: bool, extra_sitecustomize: str = ""
) -> FakeProviderRepo:
    from code_indexer.storage.shared.hnsw_sync_state import (
        CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
    )

    repo = FakeProviderRepo(tmp_path, server, _FAKE_KEY, extra_sitecustomize)
    if cluster:
        repo.env[CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV] = "1"
    repo.write_files("kept", 3)
    _git_init(repo.repo, ".code-indexer/\n")
    # ^ hazard fresh mtimes => later incrementals re-plan kept files and branch isolation hides doomed; RED needs a zero-file plan
    age_source_files(repo.repo)
    indexed = repo.cidx(*repo.index_args(), "--fts")
    assert indexed.returncode == 0, indexed.stderr[-3000:]
    return repo


def _add_doomed_commit(repo: FakeProviderRepo) -> Path:
    doomed = repo.repo / "src" / f"{_DOOMED}.py"
    doomed.write_text(unique_python_text(_DOOMED, 40))
    _commit_all(repo, "add")
    age_source_files(repo.repo)
    return doomed


def _reconcile(repo: FakeProviderRepo) -> "subprocess.CompletedProcess[str]":
    return repo.cidx(*repo.index_args(), "--fts", "--reconcile")


def test_incremental_after_a_reconcile_removes_a_file_deleted_in_the_next_commit(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = _base_indexed_repo(tmp_path, server, cluster_mode)
        doomed = _add_doomed_commit(repo)
        reconciled = _reconcile(repo)
        assert reconciled.returncode == 0, reconciled.stderr[-3000:]
        indexed = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in indexed, indexed

        doomed.unlink()
        _commit_all(repo, "remove")
        server.ledger.begin_run("quiet-incremental")
        incremental = repo.cidx(*repo.index_args(), "--fts")

        assert incremental.returncode == 0, incremental.stderr[-3000:]
        assert server.ledger.run_stats("quiet-incremental")["inputs"] == 0
        semantic = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in semantic, semantic
        assert f"{_KEPT}.py" in semantic, semantic
        fts_doomed = _fts_hits(repo, _DOOMED)
        fts_kept = _fts_hits(repo, _KEPT)
        assert f"{_DOOMED}.py" not in fts_doomed, fts_doomed
        assert f"{_KEPT}.py" in fts_kept, fts_kept


@pytest.mark.parametrize("work", ["quiet", "indexing"])
def test_successful_reconcile_advances_the_watermark_to_the_listed_head(
    tmp_path: Path,
    work: str,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = _base_indexed_repo(tmp_path, server, cluster_mode)
        base = _watermark(repo)
        if work == "indexing":
            _add_doomed_commit(repo)
        else:
            (repo.repo / "notes.unindexed").write_text("not indexed\n")
            _commit_all(repo, "unindexed change")
        assert _head(repo) != base

        reconciled = _reconcile(repo)

        assert reconciled.returncode == 0, reconciled.stderr[-3000:]
        assert _watermark(repo) == _head(repo)


def test_failed_reconcile_keeps_the_watermark(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "fail-next-branch-isolation"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _FAIL_NEXT_BRANCH_ISOLATION
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = _base_indexed_repo(tmp_path, server, cluster_mode, injection)
        base = _watermark(repo)
        _add_doomed_commit(repo)

        flag.touch()
        failed = _reconcile(repo)

        assert failed.returncode != 0, failed.stdout[-2000:]
        assert not flag.exists(), "the injected failure never fired"
        assert _watermark(repo) == base


def test_cancelled_reconcile_keeps_the_watermark(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "cancel-next-branch-processing"
    injection = f"_CANCEL_FLAG = {str(flag)!r}\n" + _CANCEL_NEXT_BRANCH_PROCESSING
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = _base_indexed_repo(tmp_path, server, cluster_mode, injection)
        base = _watermark(repo)
        _add_doomed_commit(repo)

        flag.touch()
        _reconcile(repo)

        assert not flag.exists(), "the injected cancellation never fired"
        assert _watermark(repo) == base
        assert repo.metadata().get("status") != "completed"
