"""A stale FTS index must actually be REBUILT, not just enqueued.

The engine enqueueing a job proves nothing on its own: `ensure_fts_index` used to
short-circuit on `fts_index_present` alone, so for exactly the case the staleness
rebuild targets -- index present, commit drifted -- the job returned PRESENT and
built nothing. These drive the real function rather than a fake policy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List


from code_indexer.server.services.fts_index_builder import (
    FTS_INDEX_DIRNAME,
    FtsBuildStatus,
    ensure_fts_index,
)
from code_indexer.server.services.index_freshness import (
    BUILD_COMMIT_MARKER,
    current_commit,
    record_build_commit,
)

_SHA = "a" * 40
_MOVED = "b" * 40


def _repo(tmp_path: Path, *, with_index: bool, sha: str = _SHA) -> Path:
    repo = tmp_path / "repo"
    ci = repo / ".code-indexer"
    ci.mkdir(parents=True)
    (ci / "config.json").write_text("{}")
    git = repo / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/master\n")
    (git / "refs" / "heads" / "master").write_text(sha + "\n")
    if with_index:
        idx = ci / FTS_INDEX_DIRNAME
        idx.mkdir(parents=True)
        (idx / "meta.json").write_text("{}")
    return repo


def _runner(calls: List[Any], *, writes_index: Path | None = None):
    def _run(cmd, **kwargs):
        calls.append(cmd)
        if writes_index is not None:
            writes_index.mkdir(parents=True, exist_ok=True)
            (writes_index / "meta.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_present_and_current_still_short_circuits(tmp_path: Path) -> None:
    repo = _repo(tmp_path, with_index=True)
    record_build_commit(repo / ".code-indexer" / FTS_INDEX_DIRNAME, repo)
    calls: List[Any] = []
    out = ensure_fts_index(repo, alias="r", runner=_runner(calls))
    assert out.status is FtsBuildStatus.PRESENT
    assert calls == []  # no build attempted


def test_present_but_stale_actually_rebuilds(tmp_path: Path) -> None:
    """THE case this fix targets. Previously returned PRESENT and built nothing."""
    repo = _repo(tmp_path, with_index=True)
    idx = repo / ".code-indexer" / FTS_INDEX_DIRNAME
    record_build_commit(idx, repo)
    # the clone moves on
    (repo / ".git" / "refs" / "heads" / "master").write_text(_MOVED + "\n")

    calls: List[Any] = []
    out = ensure_fts_index(repo, alias="r", runner=_runner(calls, writes_index=idx))
    assert out.status is FtsBuildStatus.REBUILT
    assert calls, "no cidx index subprocess was run for a stale index"


def test_rebuild_records_the_new_commit(tmp_path: Path) -> None:
    """Otherwise the repo stays 'stale' forever and re-enqueues every cooldown."""
    repo = _repo(tmp_path, with_index=True)
    idx = repo / ".code-indexer" / FTS_INDEX_DIRNAME
    record_build_commit(idx, repo)
    (repo / ".git" / "refs" / "heads" / "master").write_text(_MOVED + "\n")

    ensure_fts_index(repo, alias="r", runner=_runner([], writes_index=idx))
    assert (idx / BUILD_COMMIT_MARKER).read_text().strip() == _MOVED
    assert current_commit(repo) == _MOVED


def test_unrecorded_index_rebuilds_once_then_settles(tmp_path: Path) -> None:
    """The rollout case: every pre-existing index has no marker. It must rebuild
    once and then stop, not re-enqueue on every tick."""
    repo = _repo(tmp_path, with_index=True)
    idx = repo / ".code-indexer" / FTS_INDEX_DIRNAME
    assert not (idx / BUILD_COMMIT_MARKER).exists()

    first: List[Any] = []
    out1 = ensure_fts_index(repo, alias="r", runner=_runner(first, writes_index=idx))
    assert out1.status is FtsBuildStatus.REBUILT
    assert first

    second: List[Any] = []
    out2 = ensure_fts_index(repo, alias="r", runner=_runner(second))
    assert out2.status is FtsBuildStatus.PRESENT
    assert second == []  # settled -- no second build
