"""Tests for #677 fast-path scroll_points / delete_by_filter using PathIndex.

These tests assert that when filter_conditions contains a simple path == X
equality, scroll_points and delete_by_filter use the existing PathIndex and
never call Path.rglob.  They also verify lazy-rebuild, persistence, concurrent
correctness, and consistency under deletion.

8 test cases total:
  1. scroll_points with {path==X} never calls rglob
  2. scroll_points with {path==X, type==content} never calls rglob
  3. delete_by_filter with {path==X} never calls rglob
  4. Non-path filter falls through to rglob and returns correct results
  5. Path index fast path works after reload from disk
  6. Lazy rebuild on first call when path_index.bin is absent
  7. 8-thread concurrent upsert leaves path index complete (internal + public API check)
  8. delete_points removes entries — verified via get_point and scroll_points (public API only)
"""

import queue
import threading
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import numpy as np

from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VECTOR_SIZE = 64  # small vectors for speed in tests


def _make_vector() -> np.ndarray:
    return np.random.rand(VECTOR_SIZE).astype(np.float32)


def _upsert_file_points(
    store: FilesystemVectorStore,
    collection_name: str,
    file_path: str,
    num_chunks: int,
) -> List[str]:
    """Upsert num_chunks points for a file and return the list of point IDs."""
    points = []
    point_ids = []
    for i in range(num_chunks):
        pid = f"{file_path.replace('/', '_')}__chunk{i}"
        point_ids.append(pid)
        points.append(
            {
                "id": pid,
                "vector": _make_vector(),
                "payload": {
                    "path": file_path,
                    "type": "content",
                    "chunk_index": i,
                    "language": "python",
                },
            }
        )
    store.upsert_points(collection_name, points)
    return point_ids


def _populate_store(
    store: FilesystemVectorStore,
    collection_name: str,
    num_files: int,
    chunks_per_file: int,
) -> Dict[str, List[str]]:
    """Populate store and return {file_path: [point_ids]}."""
    file_to_ids: Dict[str, List[str]] = {}
    for i in range(num_files):
        fp = f"src/module_{i:04d}/file.py"
        ids = _upsert_file_points(store, collection_name, fp, chunks_per_file)
        file_to_ids[fp] = ids
    return file_to_ids


# ---------------------------------------------------------------------------
# Tests 1 & 2: Fast path — scroll_points with path filter never calls rglob
# ---------------------------------------------------------------------------


class TestScrollPointsFastPath:
    """scroll_points(filter={path == X}) must not call Path.rglob."""

    def test_scroll_points_path_only_filter_does_not_call_rglob(self, tmp_path):
        """Given 1000 points across 100 files, scroll_points with {path==X}
        must return the correct points and never call Path.rglob."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        file_to_ids = _populate_store(store, "col", num_files=100, chunks_per_file=10)

        target_file = "src/module_0042/file.py"
        expected_ids = set(file_to_ids[target_file])

        with patch.object(Path, "rglob") as mock_rglob:
            points, _ = store.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        mock_rglob.assert_not_called()

        returned_ids = {p["id"] for p in points}
        assert returned_ids == expected_ids

    def test_scroll_points_path_plus_type_filter_does_not_call_rglob(self, tmp_path):
        """scroll_points with {path==X, type==content} must also use fast path
        and not call rglob."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        file_to_ids = _populate_store(store, "col", num_files=50, chunks_per_file=10)

        target_file = "src/module_0010/file.py"
        expected_ids = set(file_to_ids[target_file])

        with patch.object(Path, "rglob") as mock_rglob:
            points, _ = store.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "type", "match": {"value": "content"}},
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        mock_rglob.assert_not_called()

        returned_ids = {p["id"] for p in points}
        assert returned_ids == expected_ids


# ---------------------------------------------------------------------------
# Test 3: Fast path — delete_by_filter with path filter never calls rglob
# ---------------------------------------------------------------------------


class TestDeleteByFilterFastPath:
    """delete_by_filter({path == X}) must not call Path.rglob."""

    def test_delete_by_filter_path_filter_does_not_call_rglob(self, tmp_path):
        """Given 1000 points across 100 files, delete_by_filter({path==X})
        must not call rglob and must actually delete the right points."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        file_to_ids = _populate_store(store, "col", num_files=100, chunks_per_file=10)

        target_file = "src/module_0007/file.py"
        target_ids = set(file_to_ids[target_file])

        with patch.object(Path, "rglob") as mock_rglob:
            result = store.delete_by_filter(
                collection_name="col",
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        mock_rglob.assert_not_called()
        assert result is True

        # Verify points are actually deleted
        for pid in target_ids:
            assert store.get_point(pid, "col") is None

        # Verify other files still intact
        other_file = "src/module_0008/file.py"
        for pid in file_to_ids[other_file]:
            assert store.get_point(pid, "col") is not None


# ---------------------------------------------------------------------------
# Test 4: Legacy safety valve — unrecognised filter still works via rglob
# ---------------------------------------------------------------------------


class TestScrollPointsFallbackPath:
    """A filter that is not path==X must still work (rglob fallback)."""

    def test_non_path_filter_still_returns_correct_results(self, tmp_path):
        """filter={type==content} (no path key) must still work via rglob
        and return all content-type points."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        file_to_ids = _populate_store(store, "col", num_files=5, chunks_per_file=3)
        all_content_ids = {pid for ids in file_to_ids.values() for pid in ids}

        # This filter has no path key — must fall through to rglob
        points, _ = store.scroll_points(
            collection_name="col",
            limit=10000,
            filter_conditions={
                "must": [
                    {"key": "type", "match": {"value": "content"}},
                ]
            },
        )

        returned_ids = {p["id"] for p in points}
        assert returned_ids == all_content_ids


# ---------------------------------------------------------------------------
# Test 5: Path index persistence across instances
# ---------------------------------------------------------------------------


class TestPathIndexPersistence:
    """Path index survives across FilesystemVectorStore instances."""

    def test_path_index_fast_path_after_reload(self, tmp_path):
        """After upsert, save, and reopen, scroll_points with path filter
        must still use the fast path (not rglob)."""
        # First instance — upsert points
        store1 = FilesystemVectorStore(base_path=tmp_path)
        store1.create_collection("col", vector_size=VECTOR_SIZE)
        file_to_ids = _populate_store(store1, "col", num_files=20, chunks_per_file=5)
        # Persist path index explicitly (as end_indexing would)
        store1._save_path_index("col", store1._path_indexes["col"])
        del store1

        # Second instance — cold start, index loaded from disk
        store2 = FilesystemVectorStore(base_path=tmp_path)
        target_file = "src/module_0010/file.py"
        expected_ids = set(file_to_ids[target_file])

        with patch.object(Path, "rglob") as mock_rglob:
            points, _ = store2.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        mock_rglob.assert_not_called()
        returned_ids = {p["id"] for p in points}
        assert returned_ids == expected_ids


# ---------------------------------------------------------------------------
# Test 6: Lazy rebuild when path index file is absent
# ---------------------------------------------------------------------------


class TestLazyRebuildWhenPathIndexAbsent:
    """When path_index.bin is absent, scroll_points must rebuild lazily."""

    def test_lazy_rebuild_on_first_scroll(self, tmp_path):
        """When path_index.bin does not exist on disk, the first scroll_points
        with a path filter must rebuild the index via rglob (called at least
        once during the rebuild), persist it, and return correct results.
        The second call must not call rglob again."""
        # Build a store and upsert points
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)
        file_to_ids = _populate_store(store, "col", num_files=10, chunks_per_file=5)

        # Persist id_index so get_point works, but forcibly DELETE path_index.bin
        store._save_path_index("col", store._path_indexes["col"])
        path_index_file = tmp_path / "col" / "path_index.bin"
        path_index_file.unlink()

        # Evict in-memory path index so the store must reload from disk
        with store._path_index_lock:
            store._path_indexes.pop("col", None)

        target_file = "src/module_0005/file.py"
        expected_ids = set(file_to_ids[target_file])

        # First call — path_index.bin absent, must rebuild lazily using rglob.
        # Use a spy that records calls while still delegating to the real rglob.
        rglob_called = []
        _real_rglob = Path.rglob

        def _spy_rglob(self_path: Path, pattern: str):
            rglob_called.append((self_path, pattern))
            return _real_rglob(self_path, pattern)

        with patch.object(Path, "rglob", _spy_rglob):
            points, _ = store.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        # rglob must have been called at least once for the rebuild
        assert len(rglob_called) > 0, (
            "rglob must be called during lazy rebuild when path_index.bin is absent"
        )

        returned_ids = {p["id"] for p in points}
        assert returned_ids == expected_ids

        # After rebuild, path_index.bin must now exist on disk
        assert path_index_file.exists()

        # Second call — fast path active, rglob must not be called
        with patch.object(Path, "rglob") as mock_rglob_second:
            points2, _ = store.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )

        mock_rglob_second.assert_not_called()
        returned_ids2 = {p["id"] for p in points2}
        assert returned_ids2 == expected_ids


# ---------------------------------------------------------------------------
# Test 7: Concurrent upsert correctness
# ---------------------------------------------------------------------------


class TestConcurrentUpsertCorrectness:
    """8 threads upserting disjoint batches — final path index must be complete.

    Verification strategy: internal state inspection (_path_indexes) is used as
    a behavioral probe to confirm the in-memory index structure is complete, in
    addition to the public-API verification via scroll_points.
    """

    def test_8_threads_upsert_disjoint_batches(self, tmp_path):
        """8 threads upserting disjoint file batches must leave the path
        index containing all path->id mappings.

        Internal state (_path_indexes) is inspected as a behavioral probe;
        scroll_points is also used to verify the public-API view is consistent.
        """
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        num_threads = 8
        files_per_thread = 10
        chunks_per_file = 5

        lock = threading.Lock()
        all_expected: Dict[str, set] = {}
        errors: List[Exception] = []

        def worker(thread_idx: int) -> None:
            local_map: Dict[str, set] = {}
            try:
                for j in range(files_per_thread):
                    fp = f"thread_{thread_idx:02d}/file_{j:04d}.py"
                    ids = _upsert_file_points(store, "col", fp, chunks_per_file)
                    local_map[fp] = set(ids)
            except Exception as exc:
                with lock:
                    errors.append(exc)
                return
            with lock:
                all_expected.update(local_map)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
            assert not t.is_alive(), f"Thread {t.name} did not complete in time"

        assert not errors, f"Worker threads raised errors: {errors}"

        # Behavioral probe: inspect internal path index structure for completeness
        with store._path_index_lock:
            path_index = store._path_indexes.get("col")
        assert path_index is not None, "Path index must exist for 'col'"

        for fp, expected_ids in all_expected.items():
            actual_ids = path_index.get_point_ids(fp)
            assert actual_ids == expected_ids, (
                f"Internal path index mismatch for {fp}: "
                f"expected {expected_ids}, got {actual_ids}"
            )

        # Public API verification: scroll_points must also return the correct IDs
        # (spot-check a subset to keep the test fast)
        sample_files = list(all_expected.keys())[:5]
        for fp in sample_files:
            expected_ids = all_expected[fp]
            with patch.object(Path, "rglob") as mock_rglob:
                points, _ = store.scroll_points(
                    collection_name="col",
                    limit=1000,
                    filter_conditions={
                        "must": [
                            {"key": "path", "match": {"value": fp}},
                        ]
                    },
                )
            mock_rglob.assert_not_called()
            returned_ids = {p["id"] for p in points}
            assert returned_ids == expected_ids, (
                f"Public API mismatch for {fp}: expected {expected_ids}, got {returned_ids}"
            )


# ---------------------------------------------------------------------------
# Test 8: Consistency under delete — verified via public API only
# ---------------------------------------------------------------------------


class TestPathIndexConsistencyUnderDelete:
    """delete_points must remove entries from path index.

    Verification strategy: public APIs only (get_point and scroll_points).
    No internal state access in this test.
    """

    def test_delete_points_removes_from_path_index(self, tmp_path):
        """After delete_points, scroll_points with path filter must return
        only the surviving points; deleted ones must not appear.
        Verified exclusively via get_point() and scroll_points()."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)

        file_to_ids = _populate_store(store, "col", num_files=10, chunks_per_file=10)

        target_file = "src/module_0003/file.py"
        all_ids = list(file_to_ids[target_file])

        # Delete half the points
        ids_to_delete = all_ids[:5]
        ids_to_keep = set(all_ids[5:])

        store.delete_points("col", ids_to_delete)

        # Public API check 1: get_point returns None for deleted IDs
        for pid in ids_to_delete:
            assert store.get_point(pid, "col") is None

        # Public API check 2: scroll_points (fast path) returns only surviving points
        with patch.object(Path, "rglob") as mock_rglob:
            points, _ = store.scroll_points(
                collection_name="col",
                limit=1000,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": target_file}},
                    ]
                },
            )
        mock_rglob.assert_not_called()

        returned_ids = {p["id"] for p in points}
        assert returned_ids == ids_to_keep
        for pid in ids_to_delete:
            assert pid not in returned_ids


# ---------------------------------------------------------------------------
# Tests M1: Fast path must NOT be taken when filter has must_not / should keys
# ---------------------------------------------------------------------------

_M1_FILE_MUST_NOT = "src/module_must_not/file.py"
_M1_FILE_SHOULD = "src/module_should/file.py"
_SCROLL_LIMIT = 1000


class TestFastPathNotTakenWithExtraFilterKeys:
    """M1: fast path must not activate when filter_conditions has must_not/should."""

    def test_fast_path_not_taken_when_filter_has_must_not(self, tmp_path) -> None:
        """must_not clause must exclude matching points; fast path must not discard it."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)
        store.upsert_points(
            "col",
            [
                {
                    "id": "pt_py",
                    "vector": _make_vector(),
                    "payload": {
                        "path": _M1_FILE_MUST_NOT,
                        "type": "content",
                        "language": "python",
                    },
                },
                {
                    "id": "pt_js",
                    "vector": _make_vector(),
                    "payload": {
                        "path": _M1_FILE_MUST_NOT,
                        "type": "content",
                        "language": "javascript",
                    },
                },
            ],
        )
        filter_conds = {
            "must": [{"key": "path", "match": {"value": _M1_FILE_MUST_NOT}}],
            "must_not": [{"key": "language", "match": {"value": "python"}}],
        }
        points, _ = store.scroll_points(
            "col", _SCROLL_LIMIT, filter_conditions=filter_conds
        )
        returned_ids = {p["id"] for p in points}
        assert "pt_py" not in returned_ids, "must_not silently discarded by fast path"
        assert "pt_js" in returned_ids

    def test_fast_path_not_taken_when_filter_has_should(self, tmp_path) -> None:
        """should clause must restrict results; fast path must not discard it."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("col", vector_size=VECTOR_SIZE)
        store.upsert_points(
            "col",
            [
                {
                    "id": "pt_py",
                    "vector": _make_vector(),
                    "payload": {
                        "path": _M1_FILE_SHOULD,
                        "type": "content",
                        "language": "python",
                    },
                },
                {
                    "id": "pt_js",
                    "vector": _make_vector(),
                    "payload": {
                        "path": _M1_FILE_SHOULD,
                        "type": "content",
                        "language": "javascript",
                    },
                },
            ],
        )
        filter_conds = {
            "must": [{"key": "path", "match": {"value": _M1_FILE_SHOULD}}],
            "should": [{"key": "language", "match": {"value": "python"}}],
        }
        points, _ = store.scroll_points(
            "col", _SCROLL_LIMIT, filter_conditions=filter_conds
        )
        returned_ids = {p["id"] for p in points}
        assert "pt_js" not in returned_ids, (
            "should clause silently discarded by fast path"
        )
        assert "pt_py" in returned_ids


# ---------------------------------------------------------------------------
# Test M2: Lazy rebuild must preserve concurrent upserts (merge-safe)
# ---------------------------------------------------------------------------

_M2_ITERATIONS = 5
_M2_INITIAL_FILES = 10
_M2_INITIAL_CHUNKS = 5
_M2_NEW_CHUNKS = 3
_M2_THREAD_TIMEOUT_S = 15
_M2_UPSERT_OVERLAP_DELAY_S = 0.001


def _m2_setup_legacy_store(iter_path: Path) -> FilesystemVectorStore:
    """Create a store, populate it, persist path index, then delete path_index.bin."""
    store = FilesystemVectorStore(base_path=iter_path)
    store.create_collection("col", vector_size=VECTOR_SIZE)
    _populate_store(store, "col", _M2_INITIAL_FILES, _M2_INITIAL_CHUNKS)
    store._save_path_index("col", store._path_indexes["col"])
    store._path_indexes.clear()
    bin_file = store.base_path / "col" / "path_index.bin"
    if bin_file.exists():
        bin_file.unlink()
    return store


def _m2_build_new_points(iteration: int) -> tuple:
    """Return (new_file, new_pids, new_points) for a fresh concurrent upsert."""
    new_file = f"src/concurrent_new_{iteration}.py"
    new_pids = [f"new_pid_{iteration}_{j}" for j in range(_M2_NEW_CHUNKS)]
    new_points = [
        {
            "id": new_pids[j],
            "vector": _make_vector(),
            "payload": {"path": new_file, "type": "content"},
        }
        for j in range(_M2_NEW_CHUNKS)
    ]
    return new_file, new_pids, new_points


def _m2_run_threads(
    store: FilesystemVectorStore,
    new_points: list,
    iteration: int,
    errors: "queue.Queue[str]",
) -> None:
    """Spawn scroll+upsert threads, join with timeout, check is_alive."""
    import time

    def do_scroll() -> None:
        try:
            store.scroll_points(
                collection_name="col",
                limit=_SCROLL_LIMIT,
                filter_conditions={
                    "must": [
                        {"key": "path", "match": {"value": "src/module_0000/file.py"}}
                    ]
                },
            )
        except Exception as exc:
            errors.put(f"scroll iter {iteration}: {exc}")

    def do_upsert() -> None:
        try:
            time.sleep(_M2_UPSERT_OVERLAP_DELAY_S)
            store.upsert_points("col", new_points)
        except Exception as exc:
            errors.put(f"upsert iter {iteration}: {exc}")

    t_scroll = threading.Thread(target=do_scroll)
    t_upsert = threading.Thread(target=do_upsert)
    t_scroll.start()
    t_upsert.start()
    t_scroll.join(timeout=_M2_THREAD_TIMEOUT_S)
    t_upsert.join(timeout=_M2_THREAD_TIMEOUT_S)
    if t_scroll.is_alive():
        errors.put(f"scroll thread hung iter {iteration}")
    if t_upsert.is_alive():
        errors.put(f"upsert thread hung iter {iteration}")


def _m2_assert_new_file_visible(
    store: FilesystemVectorStore,
    new_file: str,
    new_pids: list,
    iteration: int,
    errors: "queue.Queue[str]",
) -> None:
    """Assert that all new_pids are visible for new_file via scroll_points."""
    surviving, _ = store.scroll_points(
        collection_name="col",
        limit=_SCROLL_LIMIT,
        filter_conditions={"must": [{"key": "path", "match": {"value": new_file}}]},
    )
    surviving_ids = {p["id"] for p in surviving}
    if surviving_ids != set(new_pids):
        errors.put(
            f"Iter {iteration}: rebuild discarded concurrent upserts. "
            f"Expected {set(new_pids)}, got {surviving_ids}"
        )


class TestLazyRebuildPreservesConcurrentUpserts:
    """M2: lazy rebuild must merge, not replace, the live PathIndex."""

    def test_lazy_rebuild_preserves_concurrent_upserts(self, tmp_path) -> None:
        """Race scroll (triggers rebuild) vs upsert; new file must survive."""
        import queue

        errors: queue.Queue[str] = queue.Queue()
        for iteration in range(_M2_ITERATIONS):
            iter_path = tmp_path / f"iter_{iteration}"
            iter_path.mkdir()
            store = _m2_setup_legacy_store(iter_path)
            new_file, new_pids, new_points = _m2_build_new_points(iteration)
            _m2_run_threads(store, new_points, iteration, errors)
            _m2_assert_new_file_visible(store, new_file, new_pids, iteration, errors)

        collected = []
        while not errors.empty():
            collected.append(errors.get_nowait())
        assert not collected, f"Concurrent rebuild race failures: {collected}"
