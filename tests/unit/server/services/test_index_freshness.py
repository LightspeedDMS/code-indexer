"""One freshness convention, shared by FTS and SCIP.

SCIP already had a `.build_commit` sidecar; FTS had nothing, so its index was never
rebuilt and drifted behind the clone. These pin the shared primitive -- the same
marker both types use -- not a second parallel mechanism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.index_freshness import (
    BUILD_COMMIT_MARKER,
    build_commit_is_current,
    current_commit,
    record_build_commit,
    recorded_build_commit,
)

_SHA = "a" * 40
_OTHER = "b" * 40


def _clone(
    tmp_path, *, head="ref: refs/heads/master", ref_sha=_SHA, packed=False
) -> Path:
    repo = tmp_path / "repo"
    git = repo / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n")
    if ref_sha is not None:
        if packed:
            (git / "packed-refs").write_text(f"# header\n{ref_sha} refs/heads/master\n")
        else:
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "refs" / "heads" / "master").write_text(ref_sha + "\n")
    return repo


def test_shares_scips_marker_name() -> None:
    """One filename on disk, not two mechanisms."""
    from code_indexer.server.services import scip_lifecycle

    assert scip_lifecycle._BUILD_COMMIT_MARKER == BUILD_COMMIT_MARKER


def test_reads_a_loose_ref(tmp_path: Path) -> None:
    assert current_commit(_clone(tmp_path)) == _SHA


def test_reads_packed_refs(tmp_path: Path) -> None:
    assert current_commit(_clone(tmp_path, packed=True)) == _SHA


def test_reads_a_detached_head(tmp_path: Path) -> None:
    assert current_commit(_clone(tmp_path, head=_SHA, ref_sha=None)) == _SHA


def test_reads_without_spawning_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep walks ~900 repos every tick; a subprocess each is not affordable."""
    import code_indexer.utils.git_runner as gr

    def _boom(*a, **k):
        raise AssertionError("spawned git for the common layout")

    monkeypatch.setattr(gr, "get_current_commit", _boom)
    assert current_commit(_clone(tmp_path)) == _SHA


def test_falls_back_for_an_unrecognised_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_indexer.utils.git_runner as gr

    monkeypatch.setattr(gr, "get_current_commit", lambda p, short=False: _OTHER)
    repo = tmp_path / "worktree"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert current_commit(repo) == _OTHER


def test_matching_commit_is_current(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    idx = repo / ".code-indexer" / "idx"
    record_build_commit(idx, repo)
    assert recorded_build_commit(idx) == _SHA
    assert build_commit_is_current(repo, idx) is True


def test_moved_clone_is_not_current(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    idx = repo / ".code-indexer" / "idx"
    record_build_commit(idx, repo)
    (repo / ".git" / "refs" / "heads" / "master").write_text(_OTHER + "\n")
    assert build_commit_is_current(repo, idx) is False


def test_unrecorded_is_not_current_so_the_fleet_converges(tmp_path: Path) -> None:
    """Every index predating this convention has no marker; treating that as current
    would leave the existing fleet permanently stale."""
    repo = _clone(tmp_path)
    idx = repo / ".code-indexer" / "idx"
    idx.mkdir(parents=True)
    assert build_commit_is_current(repo, idx) is False


def test_unknown_commit_is_treated_as_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo whose commit cannot be read must not be rebuilt on every tick."""
    import code_indexer.utils.git_runner as gr

    monkeypatch.setattr(gr, "get_current_commit", lambda p, short=False: None)
    repo = tmp_path / "plain"
    (repo / "idx").mkdir(parents=True)
    assert build_commit_is_current(repo, repo / "idx") is True


def test_ignores_scips_trailing_token(tmp_path: Path) -> None:
    """SCIP writes '<commit> no-index'; identity is the commit alone."""
    idx = tmp_path / "idx"
    idx.mkdir()
    (idx / BUILD_COMMIT_MARKER).write_text(f"{_SHA} no-index\n")
    assert recorded_build_commit(idx) == _SHA


def test_recording_is_best_effort(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    blocked = repo / "blocked"
    blocked.write_text("a file, not a directory")
    record_build_commit(blocked, repo)  # must not raise
    assert recorded_build_commit(blocked) is None
