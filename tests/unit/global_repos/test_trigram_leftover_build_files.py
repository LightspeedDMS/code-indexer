"""remove_leftover_build_files / trigram_index_dir (real filesystem).

Liveness is decided by staleness: a live build keeps updating its temp file's
mtime, so only temps whose mtime is old enough are leftovers. Tests age files
with os.utime rather than waiting.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from code_indexer.global_repos.trigram_index_manager import (
    _STALE_BUILD_FILE_AGE_SECONDS,
    TrigramIndexManager,
    remove_leftover_build_files,
    trigram_index_dir,
)

# Just past the module's staleness threshold: a dead build's leftover.
_STALE_AGE = _STALE_BUILD_FILE_AGE_SECONDS + 60


def _aged(path: Path, age: float = _STALE_AGE) -> Path:
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def test_threshold_boundary(tmp_path: Path) -> None:
    """Younger than the threshold is kept, older is removed."""
    idx = tmp_path / "idx"
    idx.mkdir()
    young = idx / "trigrams.young123.db.building"
    old = idx / "trigrams.old12345.db.building"
    young.write_bytes(b"x")
    old.write_bytes(b"y")
    _aged(young, _STALE_BUILD_FILE_AGE_SECONDS - 60)
    _aged(old, _STALE_BUILD_FILE_AGE_SECONDS + 60)

    assert remove_leftover_build_files(idx) == 1

    assert young.exists()
    assert not old.exists()


def test_trigram_index_dir_layout(tmp_path: Path) -> None:
    assert trigram_index_dir(tmp_path) == tmp_path / ".code-indexer" / "trigram_index"


def test_missing_directory_removes_nothing(tmp_path: Path) -> None:
    assert remove_leftover_build_files(tmp_path / "absent") == 0


def test_removes_only_aged_build_temps_and_counts_them(tmp_path: Path) -> None:
    idx = tmp_path / "idx"
    idx.mkdir()
    keep = {
        "trigrams.db": b"published",
        "notes.building": b"x",
        "trigrams.db.bak": b"y",
        "other.ab12.db.building": b"z",
    }
    for name, data in keep.items():
        (idx / name).write_bytes(data)
        _aged(idx / name)
    for name in ("trigrams.ab12cd34.db.building", "trigrams.q_9x7w2e.db.building"):
        (idx / name).write_bytes(b"partial")
        _aged(idx / name)

    assert remove_leftover_build_files(idx) == 2

    assert {p.name: p.read_bytes() for p in idx.iterdir()} == keep


def test_fresh_build_file_is_kept(tmp_path: Path) -> None:
    """A temp whose mtime is recent belongs to a build that is still writing
    it (possibly on another node) and must never be removed."""
    idx = tmp_path / "idx"
    idx.mkdir()
    live = idx / "trigrams.live1234.db.building"
    live.write_bytes(b"being written")

    assert remove_leftover_build_files(idx) == 0

    assert live.read_bytes() == b"being written"


def test_symlink_with_matching_name_is_never_removed(tmp_path: Path) -> None:
    """Only regular files are build temps: a symlink named like one is neither
    followed (its target's age is irrelevant) nor removed."""
    idx = tmp_path / "idx"
    idx.mkdir()
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"not ours")
    _aged(target)
    link = idx / "trigrams.link1234.db.building"
    link.symlink_to(target)
    old = time.time() - _STALE_AGE
    os.utime(link, (old, old), follow_symlinks=False)

    assert remove_leftover_build_files(idx) == 0

    assert link.is_symlink()
    assert target.read_bytes() == b"not ours"


def test_published_index_stays_queryable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def authenticate_user(): pass\n")
    mgr = TrigramIndexManager(tmp_path / "idx")
    mgr.build(repo, file_list=["a.py"])
    _aged(tmp_path / "idx" / "trigrams.db")
    leftover = tmp_path / "idx" / "trigrams.ab12cd34.db.building"
    leftover.write_bytes(b"partial")
    _aged(leftover)

    assert remove_leftover_build_files(tmp_path / "idx") == 1

    assert mgr.exists()


def test_non_directory_path_raises(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with pytest.raises(NotADirectoryError):
        remove_leftover_build_files(not_a_dir)
