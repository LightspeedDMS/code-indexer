"""Reconcile compares ONE content-id format on both sides.

Bug: for a repository without git, the indexed side of reconcile derived
``path:working_dir:<mtime>:<size>`` from the chunk payload while the current
side computed ``path:working_dir_<float mtime>_<size>`` from a stat -- never
equal, so every reconcile of a local non-git repository re-indexed every file.
A dirty file of a git repository had the same problem: it is indexed under the
blob hash of its working bytes, but reconcile compared an mtime/size id.

The indexed side here is produced by the REAL producers
(``FileIdentifier.get_file_metadata`` -> ``FileChunkingManager._create_vector_point``),
so a drift in either half of the format fails these tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.services.smart_indexer import (
    unreadable_content_id,
    working_dir_content_id,
)
from code_indexer.utils.blob_hash import git_blob_hash
from tests.unit.services.test_reconcile_batch_content_id_1505 import (
    FakeVectorStoreClient,
    _commit_all,
    _init_repo,
    _make_indexer,
    _run_reconcile,
    _write_file,
)


def _indexed_point(root: Path, rel_path: str) -> Dict[str, Any]:
    """The content point the real indexing path stores for ``rel_path``."""
    path = root / rel_path
    metadata = FileIdentifier(root).get_file_metadata(path)
    manager = FileChunkingManager(
        MagicMock(), MagicMock(), MagicMock(), 1, MagicMock(), codebase_dir=root
    )
    chunk = {
        "text": path.read_text(),
        "file_extension": path.suffix.lstrip("."),
        "chunk_index": 0,
        "total_chunks": 1,
    }
    point = manager._create_vector_point(chunk, [0.0], metadata, path)
    point["payload"]["type"] = "content"
    return point


def _store_with(root: Path, rel_paths: List[str]) -> FakeVectorStoreClient:
    store = FakeVectorStoreClient()
    store.points.extend(_indexed_point(root, p) for p in rel_paths)
    return store


def _non_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _write_file(root, "a.py", "print('a')\n")
    _write_file(root, "b.py", "print('b')\n")
    return root


def test_unchanged_non_git_repo_reconciles_nothing(tmp_path: Path) -> None:
    root = _non_git_repo(tmp_path)
    indexer = _make_indexer(root, _store_with(root, ["a.py", "b.py"]))
    assert indexer.git_topology_service.is_git_available() is False

    result = _run_reconcile(indexer)

    assert result["changed_files"] == []


def test_changed_non_git_file_is_reindexed(tmp_path: Path) -> None:
    root = _non_git_repo(tmp_path)
    indexer = _make_indexer(root, _store_with(root, ["a.py", "b.py"]))
    _write_file(root, "b.py", "print('b changed and longer')\n")
    st = (root / "b.py").stat()
    os.utime(root / "b.py", (st.st_atime, st.st_mtime + 5))

    result = _run_reconcile(indexer)

    assert [Path(p).name for p in result["changed_files"]] == ["b.py"]


def test_both_sides_produce_the_same_id(tmp_path: Path) -> None:
    root = _non_git_repo(tmp_path)
    indexer = _make_indexer(root, FakeVectorStoreClient())
    st = (root / "a.py").stat()

    current = indexer._get_effective_content_id_for_reconcile("a.py")
    indexed = indexer._derive_db_content_id_from_point(
        "a.py", _indexed_point(root, "a.py")
    )

    assert (
        current
        == indexed
        == working_dir_content_id("a.py", int(st.st_mtime), st.st_size)
    )


def test_unstattable_non_git_file_never_matches(tmp_path: Path) -> None:
    root = _non_git_repo(tmp_path)
    indexer = _make_indexer(root, FakeVectorStoreClient())

    assert indexer._get_effective_content_id_for_reconcile(
        "gone.py"
    ) == unreadable_content_id("gone.py")


def _dirty_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _write_file(root, "a.py", "print('a')\n")
    _commit_all(root, "initial")
    _write_file(root, "a.py", "print('a dirty')\n")
    return root


def test_dirty_git_file_already_indexed_is_not_reindexed(tmp_path: Path) -> None:
    root = _dirty_git_repo(tmp_path)
    indexer = _make_indexer(root, _store_with(root, ["a.py"]))

    result = _run_reconcile(indexer)

    assert result["changed_files"] == []


def test_dirty_git_file_changed_again_is_reindexed(tmp_path: Path) -> None:
    root = _dirty_git_repo(tmp_path)
    indexer = _make_indexer(root, _store_with(root, ["a.py"]))
    _write_file(root, "a.py", "print('a dirty, then edited again')\n")

    result = _run_reconcile(indexer)

    assert [Path(p).name for p in result["changed_files"]] == ["a.py"]


def test_dirty_git_file_id_is_its_working_blob_hash(tmp_path: Path) -> None:
    root = _dirty_git_repo(tmp_path)
    indexer = _make_indexer(root, FakeVectorStoreClient())
    indexer._reconcile_modified_files = {"a.py"}
    indexer._reconcile_head_blob_hashes = {}

    assert indexer._get_effective_content_id_for_reconcile("a.py") == (
        f"a.py:blob:{git_blob_hash(root / 'a.py')}"
    )
