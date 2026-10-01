"""Bug #1998: reconcile's per-file "is this file in the database" check must
be O(1), not a rebuild-and-scan of every indexed path per file on disk.

The pre-fix loop evaluated ``relative_path in [<list comprehension over all
indexed paths>]`` for every file on disk -- O(files_on_disk x indexed_files).

Scaling is measured as an OPERATION COUNT (Python-level function calls
observed by ``sys.setprofile`` during ``_do_reconcile_with_database``), not
wall time, so the assertion is insensitive to machine load. Quadrupling the
repository size must roughly quadruple the work (linear), never multiply it
by ~16 (quadratic).

Driven against a REAL git repository with the in-memory vector-store
substitute from the Issue #1505 tests (no git mocking).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from tests.unit.services.test_reconcile_batch_content_id_1505 import (
    FakeVectorStoreClient,
    _blob_hash_at_head,
    _filter_matches,
    _commit_all,
    _init_repo,
    _make_indexer,
    _run_reconcile,
)


def _build_repo(root: Path, n_files: int) -> Tuple[FakeVectorStoreClient, List[str]]:
    """Half the files are indexed and unchanged, half are missing from the DB."""
    root.mkdir()
    _init_repo(root)
    pkg = root / "pkg"
    pkg.mkdir()
    rel_paths = [f"pkg/mod_{i}.py" for i in range(n_files)]
    for rel_path in rel_paths:
        (root / rel_path).write_text(f"# {rel_path}\n")
    _commit_all(root, "init")

    store = FakeVectorStoreClient()
    indexed = rel_paths[::2]
    for rel_path in indexed:
        store.add_committed_point(rel_path, _blob_hash_at_head(root, rel_path))
    missing = sorted(set(rel_paths) - set(indexed))
    return store, missing


def _reconcile_call_count(tmp_path: Path, n_files: int) -> int:
    root = tmp_path / f"repo_{n_files}"
    store, missing = _build_repo(root, n_files)
    indexer = _make_indexer(root, store)

    counter = {"calls": 0}

    def profile(frame: Any, event: str, arg: Any) -> None:
        if event == "call":
            counter["calls"] += 1

    result: Dict[str, List[str]] = {}
    sys.setprofile(profile)
    try:
        result = _run_reconcile(indexer)
    finally:
        sys.setprofile(None)

    # Behaviour preserved: exactly the files absent from the DB are re-indexed.
    changed = sorted(
        str(Path(p).relative_to(root)) if Path(p).is_absolute() else str(p)
        for p in result["changed_files"]
    )
    assert changed == missing
    return counter["calls"]


class _HidingVectorStore(FakeVectorStoreClient):
    """In-memory substitute that also applies branch-visibility updates."""

    def _batch_update_points(
        self, points: List[Dict[str, Any]], collection_name: str
    ) -> bool:
        by_id = {p["id"]: p for p in self.points}
        for update in points:
            by_id[update["id"]]["payload"].update(update["payload"])
        return True


def test_reconcile_completes_with_foreign_absolute_indexed_path(
    tmp_path: Path,
) -> None:
    """``_get_indexed_files_snapshot`` explicitly tolerates a stored absolute
    payload path outside the codebase root (it keys it by its own string).
    Reconcile must tolerate it the same way: complete the run (including
    crash recovery), analyse every on-disk file normally, and treat the
    foreign entry like any other indexed path that no longer exists on disk.
    """
    root = tmp_path / "repo"
    built, missing = _build_repo(root, 6)
    store = _HidingVectorStore()
    store.points = built.points
    foreign = str(tmp_path / "elsewhere" / "foreign.py")  # never created
    store.add_committed_point(foreign, "0" * 40)
    indexer = _make_indexer(root, store)

    result = _run_reconcile(indexer)

    changed = sorted(
        str(Path(p).relative_to(root)) if Path(p).is_absolute() else str(p)
        for p in result["changed_files"]
    )
    assert changed == missing
    assert indexer.progressive_metadata.metadata.get("failed_file_paths", []) == []
    hidden = {
        p["payload"]["path"]: p["payload"].get("hidden_branches", [])
        for p in store.points
    }
    # The vanished foreign entry went through branch-aware deletion (hidden
    # on the current branch); no on-disk file's entry was touched.
    assert hidden.pop(foreign) == ["master"]
    assert all(branches == [] for branches in hidden.values())


class _DeletingVectorStore(FakeVectorStoreClient):
    """In-memory substitute whose delete_by_filter really removes points."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted_paths: List[str] = []

    def delete_by_filter(self, collection_name: Any, filter_conditions: Any) -> bool:
        doomed = [p for p in self.points if _filter_matches(p, filter_conditions)]
        self.deleted_paths.extend(p["payload"]["path"] for p in doomed)
        self.points = [p for p in self.points if p not in doomed]
        return True


def test_non_git_reconcile_keeps_existing_foreign_path_and_removes_vanished_one(
    tmp_path: Path,
) -> None:
    """Non-git deletion is a HARD delete. A stored absolute path outside the
    codebase root can never appear in the relative on-disk scan, so the
    file's real location must be checked before its vectors are removed."""
    root = tmp_path / "plain"  # deliberately NOT a git repository
    (root / "pkg").mkdir(parents=True)
    in_root = ["pkg/a.py", "pkg/b.py"]
    for rel_path in in_root:
        (root / rel_path).write_text(f"# {rel_path}\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    kept = outside / "still_here.py"
    kept.write_text("# exists outside the codebase root\n")
    vanished = str(outside / "gone.py")  # never created

    store = _DeletingVectorStore()
    for rel_path in in_root:
        stat = (root / rel_path).stat()
        store.add_working_dir_point(rel_path, stat.st_mtime, stat.st_size)
    store.add_working_dir_point(str(kept), kept.stat().st_mtime, kept.stat().st_size)
    store.add_working_dir_point(vanished, 1000.0, 10)
    indexer = _make_indexer(root, store)
    assert not indexer.is_git_aware()

    _run_reconcile(indexer)

    remaining = {p["payload"]["path"] for p in store.points}
    assert str(kept) in remaining, "vectors of an existing file were deleted"
    assert str(kept) not in store.deleted_paths
    assert vanished in store.deleted_paths
    assert vanished not in remaining
    # (In-root entries go through the non-git "modified file: delete, then
    # re-index" refresh, whose re-index half _run_reconcile stubs out -- so
    # their presence afterwards is not asserted here.)


def test_non_git_reconcile_removes_existing_in_root_file_not_in_scan(
    tmp_path: Path,
) -> None:
    """In-root semantics are unchanged by the out-of-root existence check: a
    file under the codebase root that is no longer in the scanned set (here:
    an extension the config does not index, standing in for a newly
    excluded file) is removed from the index even though it still exists."""
    root = tmp_path / "plain"  # deliberately NOT a git repository
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("# a\n")
    excluded = root / "pkg" / "notes.xyz"
    excluded.write_text("no longer indexed\n")

    store = _DeletingVectorStore()
    for path in (root / "pkg" / "a.py", excluded):
        stat = path.stat()
        store.add_working_dir_point(
            str(path.relative_to(root)), stat.st_mtime, stat.st_size
        )
    indexer = _make_indexer(root, store)
    assert not indexer.is_git_aware()

    _run_reconcile(indexer)

    assert excluded.exists()
    assert "pkg/notes.xyz" in store.deleted_paths
    assert "pkg/notes.xyz" not in {p["payload"]["path"] for p in store.points}


def test_set_failed_file_paths_dedup_preserves_order(tmp_path: Path) -> None:
    meta = ProgressiveMetadata(tmp_path / "metadata.json")
    # Deliberately mixed: entries are stringified, Path("a.py") == "a.py".
    mixed: List[Any] = ["b.py", Path("a.py"), "b.py", "c.py", "a.py"]
    meta.set_failed_file_paths(mixed)
    assert meta.metadata["failed_file_paths"] == ["b.py", "a.py", "c.py"]


_EQ_CALLS = {"n": 0}


class _CountingStr(str):
    """A str whose equality checks are counted (hash unchanged)."""

    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        _EQ_CALLS["n"] += 1
        return str.__eq__(self, other) is True


class _PathLike:
    """Stringifies to a _CountingStr, so the dedup's own comparisons --
    which happen in C and are invisible to a profiler -- are counted."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return _CountingStr(self._text)


def test_set_failed_file_paths_comparisons_linear(tmp_path: Path) -> None:
    """Every file can fail in one run (e.g. an embedding-provider outage), so
    this list is as large as the repository. Operation count, not wall time:
    the pre-fix list-membership dedup performs ~N^2/2 equality checks (about
    8 million for 4000 distinct paths); a hash-based dedup performs ~0."""
    n_paths = 4000
    paths: List[Any] = [_PathLike(f"pkg/mod_{i}.py") for i in range(n_paths)]
    meta = ProgressiveMetadata(tmp_path / "metadata.json")
    _EQ_CALLS["n"] = 0
    meta.set_failed_file_paths(paths)
    comparisons = _EQ_CALLS["n"]

    assert meta.metadata["failed_file_paths"] == [
        f"pkg/mod_{i}.py" for i in range(n_paths)
    ]
    assert comparisons <= n_paths, (
        f"{comparisons} equality checks to dedup {n_paths} distinct paths"
    )


def test_reconcile_analysis_call_count_scales_linearly(tmp_path: Path) -> None:
    small = _reconcile_call_count(tmp_path, 200)
    large = _reconcile_call_count(tmp_path, 800)
    ratio = large / small
    # Linear: ~4x. The pre-fix quadratic membership check gave ~15x
    # (measured 15.6x for 300 -> 1200 files).
    assert ratio < 6.0, (
        f"reconcile work grew {ratio:.1f}x for 4x the files "
        f"(200 files -> {small} calls, 800 files -> {large} calls)"
    )
