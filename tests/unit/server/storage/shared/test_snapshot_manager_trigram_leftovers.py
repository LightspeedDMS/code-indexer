"""Versioned snapshots must not carry leftover trigram build temp files.

A trigram index build writes ``trigrams.<random>.db.building`` next to the
published ``trigrams.db`` and renames it on success. A process killed mid-build
(deploy restart, SIGKILL) leaves the temp behind in the golden repo base clone,
and every later snapshot would copy it into ``.versioned/`` and pin it.

These tests drive the REAL ``LocalCloneBackend`` (a real
``cp --reflink=auto -a``) -- no clone double -- so they prove what actually
lands in the snapshot directory.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from code_indexer.global_repos.trigram_index_manager import (
    _STALE_BUILD_FILE_AGE_SECONDS,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

_LEFTOVERS = ("trigrams.ab12cd34.db.building", "trigrams.zz99yy88.db.building")
# Comfortably past the module's staleness threshold: a leftover of a dead build.
_STALE_AGE = _STALE_BUILD_FILE_AGE_SECONDS + 60


def _aged(path: Path) -> None:
    old = time.time() - _STALE_AGE
    os.utime(path, (old, old))


def _make_source(root: Path) -> Path:
    source = root / "golden-repos" / "example-repo"
    tri_dir = source / ".code-indexer" / "trigram_index"
    tri_dir.mkdir(parents=True)
    (source / "main.py").write_text("print('hello')\n")
    (tri_dir / "trigrams.db").write_bytes(b"published-index")
    for name in _LEFTOVERS:
        (tri_dir / name).write_bytes(b"partial-build")
        _aged(tri_dir / name)
    return source


def _manager(root: Path) -> VersionedSnapshotManager:
    versioned_base = root / "golden-repos"
    return VersionedSnapshotManager(
        versioned_base=str(versioned_base),
        clone_backend=LocalCloneBackend(versioned_base=str(versioned_base)),
    )


def test_snapshot_does_not_contain_trigram_build_leftovers(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    snapshot = Path(_manager(tmp_path).create_snapshot("example-repo", str(source)))

    snap_tri = snapshot / ".code-indexer" / "trigram_index"
    assert (snap_tri / "trigrams.db").read_bytes() == b"published-index"
    assert sorted(p.name for p in snap_tri.glob("*.building")) == []
    assert (snapshot / "main.py").read_text() == "print('hello')\n"


def test_snapshot_removes_leftovers_from_base_clone_source(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    _manager(tmp_path).create_snapshot("example-repo", str(source))

    src_tri = source / ".code-indexer" / "trigram_index"
    assert sorted(p.name for p in src_tri.glob("*.building")) == []
    assert (src_tri / "trigrams.db").read_bytes() == b"published-index"


def test_fresh_build_file_survives_snapshot(tmp_path: Path) -> None:
    """A recently written temp belongs to a live build -- possibly a refresh or
    lazy build on another node that does not share any lock with this
    snapshot's caller -- and must not be deleted out from under it."""
    source = _make_source(tmp_path)
    live = source / ".code-indexer" / "trigram_index" / "trigrams.live1234.db.building"
    live.write_bytes(b"being written")

    _manager(tmp_path).create_snapshot("example-repo", str(source))

    assert live.read_bytes() == b"being written"


def test_source_without_trigram_dir_still_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "golden-repos" / "plain-repo"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("x")

    snapshot = Path(_manager(tmp_path).create_snapshot("plain-repo", str(source)))

    assert (snapshot / "a.txt").read_text() == "x"


def test_cleanup_failure_is_logged_and_snapshot_proceeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """trigram_index being a regular file makes the cleanup raise an OSError;
    the snapshot must still be created and the failure reported."""
    source = tmp_path / "golden-repos" / "odd-repo"
    (source / ".code-indexer").mkdir(parents=True)
    (source / ".code-indexer" / "trigram_index").write_text("not a directory")

    with caplog.at_level(logging.WARNING):
        snapshot = Path(_manager(tmp_path).create_snapshot("odd-repo", str(source)))

    assert (snapshot / ".code-indexer" / "trigram_index").read_text() == (
        "not a directory"
    )
    assert "Could not remove leftover trigram build files" in caplog.text


def test_versioned_snapshot_source_is_never_modified(tmp_path: Path) -> None:
    """A source that is itself a versioned snapshot is immutable: the leftover
    cleanup must not touch it (snapshot contents only change via the existing
    whole-snapshot deletion primitives)."""
    versioned_source = tmp_path / "golden-repos" / ".versioned" / "example-repo"
    versioned_source = versioned_source / "v_1700000000"
    tri_dir = versioned_source / ".code-indexer" / "trigram_index"
    tri_dir.mkdir(parents=True)
    leftover = tri_dir / "trigrams.ab12cd34.db.building"
    leftover.write_bytes(b"partial-build")
    _aged(leftover)

    _manager(tmp_path).create_snapshot("other-repo", str(versioned_source))

    assert leftover.read_bytes() == b"partial-build"
