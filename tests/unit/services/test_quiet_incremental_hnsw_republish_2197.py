from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _BRANCH,
    _DOOMED,
    _FAIL_NEXT_HNSW_REBUILD,
    _FAKE_KEY,
    _KEPT,
    _cli_semantic_hits,
    _hnsw_fingerprint,
    _indexed_git_repo,
    _remove_and_commit,
    cluster_mode,  # noqa: F401  (pytest fixture: solo epoch and cluster fail-closed)
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server
from tests.utils.source_tree_age import age_source_files

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

# ^ why KeyboardInterrupt, not OSError; an interrupt keeps status completed => the next plain run takes the quiet incremental return
_INTERRUPT_NEXT_HNSW_REBUILD = _FAIL_NEXT_HNSW_REBUILD.replace(
    'raise OSError("injected HNSW publish failure")',
    'raise KeyboardInterrupt("injected interrupt during the HNSW publish")',
)


def _run_state(repo: FakeProviderRepo) -> Dict[str, Any]:
    metadata = repo.metadata()
    watermarks: Any = metadata.get("branch_commit_watermarks") or {}
    return {"status": metadata.get("status"), "watermark": watermarks.get(_BRANCH)}


def _head(repo: FakeProviderRepo) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _incremental(repo: FakeProviderRepo) -> "subprocess.CompletedProcess[str]":
    return repo.cidx(*repo.index_args(), "--fts")


def _injected_repo(
    tmp_path: Path, server: Any, cluster: bool, script: str
) -> Tuple[FakeProviderRepo, Path, Path]:
    flag = tmp_path / "fail-next-hnsw-rebuild"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + script
    repo, doomed = _indexed_git_repo(tmp_path, server, cluster, injection)
    # ^ hazard fresh mtimes => the incremental re-plans kept files and branch isolation publishes; RED needs a zero-file plan
    age_source_files(repo.repo)
    return repo, doomed, flag


# ^ why no --fts here; a rebuilt FTS index alone sets last_run_changed_index => masks the republish's own change report
@pytest.mark.parametrize(
    "healing", [[], ["--reconcile"]], ids=["quiet-incremental", "reconcile"]
)
def test_run_after_an_interrupted_reconcile_republishes_the_owed_publish(
    tmp_path: Path,
    healing: List[str],
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed, flag = _injected_repo(
            tmp_path, server, cluster_mode, _INTERRUPT_NEXT_HNSW_REBUILD
        )
        doomed.unlink()

        flag.touch()
        interrupted = repo.cidx(*repo.index_args(), "--reconcile")
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected interrupt never fired"
        state = _run_state(repo)
        assert state == {"status": "completed", "watermark": _head(repo)}, state
        stale = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in stale, stale

        server.ledger.begin_run("quiet-incremental")
        quiet = repo.cidx(*repo.index_args(), *healing)

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        assert server.ledger.run_stats("quiet-incremental")["inputs"] == 0
        # ^ coupled with server/services/metadata_reader.py:index_unchanged_since; False => the refresh skips the snapshot
        assert repo.metadata()["last_run_changed_index"] is True
        healed = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in healed, healed
        assert f"{_KEPT}.py" in healed, healed


def test_run_after_an_interrupted_incremental_hide_publish_removes_the_file(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed, flag = _injected_repo(
            tmp_path, server, cluster_mode, _FAIL_NEXT_HNSW_REBUILD
        )
        _remove_and_commit(repo, doomed)

        flag.touch()
        interrupted = _incremental(repo)
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected HNSW failure never fired"
        stale = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in stale, stale

        server.ledger.begin_run("next-run")
        following = _incremental(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        assert server.ledger.run_stats("next-run")["inputs"] == 0
        assert repo.metadata()["last_run_changed_index"] is True
        healed = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in healed, healed
        assert f"{_KEPT}.py" in healed, healed


def test_clean_quiet_incremental_keeps_the_hnsw_index(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, cluster_mode)
        age_source_files(repo.repo)
        before = _hnsw_fingerprint(repo.collection_dir)

        server.ledger.begin_run("clean-quiet-incremental")
        quiet = _incremental(repo)

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        assert server.ledger.run_stats("clean-quiet-incremental")["inputs"] == 0
        after = _hnsw_fingerprint(repo.collection_dir)
        assert after["inode"] == before["inode"]
        assert after["bin"] == before["bin"]
        assert after["last_rebuild"] == before["last_rebuild"]
        assert after["is_stale"] == before["is_stale"]
        assert after["sync_state"] == before["sync_state"]
