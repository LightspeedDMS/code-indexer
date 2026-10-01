"""Refresh step 1b removes leftover trigram build temps before building.

A build killed mid-way (deploy restart, SIGKILL) leaves its
``trigrams.<random>.db.building`` temp in the base clone's trigram_index
directory. The next refresh build for that directory must remove it -- but only
when it is stale: a recently written temp may belong to a live build that
shares no lock with the refresh (e.g. a lazy build on another node).

Real filesystem, real TrigramIndexManager build; nothing is mocked. Files are
aged with os.utime.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from code_indexer.global_repos.regex_trigram import trigrams
from code_indexer.global_repos.refresh_scheduler import _build_source_trigram_index
from code_indexer.global_repos.trigram_index_manager import (
    _STALE_BUILD_FILE_AGE_SECONDS,
    TrigramIndexManager,
    trigram_index_dir,
)

_STALE_AGE = _STALE_BUILD_FILE_AGE_SECONDS + 60


def _aged(path: Path) -> None:
    old = time.time() - _STALE_AGE
    os.utime(path, (old, old))


def _base_clone(tmp_path: Path) -> Path:
    source = tmp_path / "golden-repos" / "example-repo"
    source.mkdir(parents=True)
    (source / "auth.py").write_text("def authenticate_user(): pass\n")
    (source / "readme.md").write_text("nothing relevant here\n")
    return source


def test_refresh_build_removes_stale_leftover_build_files(tmp_path: Path) -> None:
    source = _base_clone(tmp_path)
    tri_dir = trigram_index_dir(source)
    tri_dir.mkdir(parents=True)
    for name in ("trigrams.ab12cd34.db.building", "trigrams.zz99yy88.db.building"):
        (tri_dir / name).write_bytes(b"partial-build")
        _aged(tri_dir / name)

    _build_source_trigram_index("example-repo-global", str(source))

    assert sorted(p.name for p in tri_dir.iterdir()) == ["trigrams.db"]
    mgr = TrigramIndexManager(tri_dir)
    assert mgr.exists()
    assert mgr.query(trigrams("authenticate_user")) == ["auth.py"]


def test_refresh_build_keeps_fresh_build_file(tmp_path: Path) -> None:
    source = _base_clone(tmp_path)
    tri_dir = trigram_index_dir(source)
    tri_dir.mkdir(parents=True)
    live = tri_dir / "trigrams.live1234.db.building"
    live.write_bytes(b"being written")

    _build_source_trigram_index("example-repo-global", str(source))

    assert live.read_bytes() == b"being written"
    assert TrigramIndexManager(tri_dir).exists()


def test_refresh_build_keeps_unrelated_files(tmp_path: Path) -> None:
    source = _base_clone(tmp_path)
    tri_dir = trigram_index_dir(source)
    tri_dir.mkdir(parents=True)
    unrelated = tri_dir / "notes.building"
    unrelated.write_text("not a trigram build temp")
    _aged(unrelated)

    _build_source_trigram_index("example-repo-global", str(source))

    assert unrelated.read_text() == "not a trigram build temp"
    assert TrigramIndexManager(tri_dir).exists()
