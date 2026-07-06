"""Tests for Bug #1264: temporal write chokepoint hard-crashes on missing
projection_matrix.npy despite the Bug #1242 shard-prep self-heal.

Root cause (confirmed by direct code reading, not speculation):
- Bug #1242 (commit 76e8c8e2) added a self-heal in the
  TemporalIndexer.index_commits() shard-prep loop (temporal_indexer.py). That
  heal only fires for shard names enumerated in THIS run's shard_commit_map,
  immediately before begin_indexing()/_process_commits_parallel() runs for
  that specific shard.
- The actual write chokepoint is a SEPARATE code location:
  FilesystemVectorStore.upsert_points() (storage/filesystem_vector_store.py)
  calls ProjectionMatrixManager.load_matrix() unconditionally and lets
  FileNotFoundError propagate when projection_matrix.npy is absent for the
  collection directory it is about to write into. This is a different module
  entirely from the temporal_indexer.py prep loop, so guarding the loop's
  entry point does not guarantee the write call itself is protected -- any
  caller that reaches upsert_points() for a collection whose matrix is
  missing on disk (a stale pre-#1242 migrated shard that the prep loop's
  shard_commit_map does not happen to cover on a given run, or any future/
  alternate write path) still hard-crashes with exactly the production
  stack trace:
      vector_store.upsert_points -> matrix_manager.load_matrix
        -> raise FileNotFoundError("Projection matrix not found at ...")

Fix: self-heal AT the write chokepoint itself (upsert_points), reusing the
existing Bug #1242 _ensure_shard_has_projection_matrix helper (copy from the
base/monolith collection when available, else regenerate a fresh matrix)
instead of duplicating matrix-creation logic. This is defense-in-depth: the
write path is protected regardless of what did or didn't happen upstream.

No mocks of the vector store, matrix manager, or quantizer anywhere in this
file -- FilesystemVectorStore, ProjectionMatrixManager, and VectorQuantizer
are all real, exercised through the real upsert_points() write path.
"""

from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.projection_matrix_manager import ProjectionMatrixManager

_DIM = 8


def _make_point(point_id: str, dim: int, ts: int) -> dict:
    """Build a minimal, real temporal-style point dict for upsert_points()."""
    return {
        "id": point_id,
        "vector": np.random.rand(dim).astype(np.float32).tolist(),
        "payload": {"commit_timestamp": ts},
    }


class TestWriteChokepointSelfHeal:
    """Bug #1264: upsert_points() self-heals a missing projection_matrix.npy
    at the exact chokepoint identified in the production stack trace, instead
    of hard-crashing the whole indexing job."""

    def test_upsert_points_self_heals_missing_matrix_instead_of_raising(self, tmp_path):
        """Reproduces the production crash at the exact chokepoint, then proves
        the fix: the same upsert_points() call must not raise, and the matrix
        file must exist on disk afterward.
        """
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        collection_name = "code-indexer-temporal-voyage_code_3-2009Q4"
        vector_store.create_collection(collection_name, _DIM)

        coll_path = vector_store._get_collection_path(collection_name)
        matrix_file = coll_path / "projection_matrix.npy"
        assert matrix_file.exists(), "precondition: create_collection writes a matrix"

        # Simulate the deployed-broken-shard state from the bug report: the
        # matrix is gone but the collection otherwise looks healthy (valid
        # collection_meta.json with vector_size present).
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()
        assert not matrix_file.exists(), "precondition: matrix genuinely absent"

        point = _make_point("repo:commit:" + "a" * 40 + ":0", _DIM, 1_262_304_000)

        # This is the exact call from the production stack trace:
        #   vector_store.upsert_points -> matrix_manager.load_matrix
        #     -> raise FileNotFoundError(...)
        # Before the fix this raises. After the fix it must self-heal and
        # return normally.
        result = vector_store.upsert_points(collection_name, [point], watch_mode=True)

        assert result is not None
        assert result["status"] == "ok"
        assert matrix_file.exists(), (
            "projection_matrix.npy must be recreated on disk after self-heal"
        )

        # The point must actually have been written, not silently dropped.
        assert vector_store.count_points(collection_name) >= 1 or (
            collection_name in vector_store._id_index
            and point["id"] in vector_store._id_index[collection_name]
        )

    def test_upsert_points_self_heal_copies_matrix_from_base_collection(self, tmp_path):
        """When a base (monolith) collection with a matrix exists, the healed
        shard matrix must be byte-identical to the base's -- consistent with
        the Bug #1242 "copy from base" preference so bucket layout stays
        aligned with any vectors the base/monolith already wrote.
        """
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        base_name = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{base_name}-2024Q1"

        # Base (monolith) collection has its own matrix.
        vector_store.create_collection(base_name, _DIM)
        base_path = vector_store._get_collection_path(base_name)
        base_matrix_bytes = (base_path / "projection_matrix.npy").read_bytes()

        # Shard collection exists but loses its matrix.
        vector_store.create_collection(shard_name, _DIM)
        shard_path = vector_store._get_collection_path(shard_name)
        (shard_path / "projection_matrix.npy").unlink()
        ProjectionMatrixManager._matrix_cache.clear()

        point = _make_point("repo:commit:" + "b" * 40 + ":0", _DIM, 1_704_067_200)
        vector_store.upsert_points(shard_name, [point], watch_mode=True)

        healed_bytes = (shard_path / "projection_matrix.npy").read_bytes()
        assert healed_bytes == base_matrix_bytes, (
            "self-healed shard matrix must be copied from the base collection, "
            "not freshly regenerated, when the base matrix is available"
        )

    def test_upsert_points_self_heal_regenerates_when_no_base_available(self, tmp_path):
        """When there is no base/monolith collection to copy from, the
        chokepoint self-heal must still succeed by regenerating a fresh,
        correctly-shaped matrix rather than raising.
        """
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        shard_name = "code-indexer-temporal-voyage_code_3-2026Q2"
        vector_store.create_collection(shard_name, _DIM)

        shard_path = vector_store._get_collection_path(shard_name)
        matrix_file = shard_path / "projection_matrix.npy"
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()
        assert not matrix_file.exists()

        point = _make_point("repo:commit:" + "c" * 40 + ":0", _DIM, 1_769_904_000)

        # Must not raise even though no base collection exists anywhere.
        result = vector_store.upsert_points(shard_name, [point], watch_mode=True)
        assert result is not None

        assert matrix_file.exists()
        healed_matrix = np.load(str(matrix_file))
        assert healed_matrix.shape == (_DIM, 64)

    def test_upsert_points_raises_for_never_created_collection_pre_heal(self, tmp_path):
        """Anti-silent-failure: if the collection itself does not really exist
        (no valid collection_meta.json), upsert_points must still raise --
        the chokepoint self-heal only covers a missing matrix on an otherwise
        valid collection, never a collection that was never created. This is
        the PRE-heal ValueError guard (collection_exists() check), not a
        failure surfacing after a heal attempt was made.
        """
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")

        with pytest.raises(ValueError, match="does not exist"):
            vector_store.upsert_points(
                "code-indexer-temporal-voyage_code_3-1999Q1",
                [_make_point("repo:commit:" + "d" * 40 + ":0", _DIM, 915148800)],
                watch_mode=True,
            )


class TestAtomicShardHeal:
    """Code-review follow-up on Bug #1264: two temporal worker threads that
    both first-write the SAME missing-matrix shard concurrently must not hit
    a torn-read window. _ensure_shard_has_projection_matrix() must write via
    a temp file in the same directory then atomically os.replace() onto
    projection_matrix.npy -- for BOTH the copy-from-base branch and the
    regenerate branch -- so a concurrent reader/healer never observes a
    partially-written file: either the old (absent) state, or the fully
    written new file, never something in between.
    """

    def test_copy_branch_writes_via_tmp_file_then_atomic_replace(
        self, tmp_path, monkeypatch
    ):
        """Copy-from-base branch: os.replace(tmp, projection_matrix.npy) must
        be the final step, never a direct shutil.copy2 straight onto the
        target path.
        """
        from code_indexer.services.temporal.temporal_projection_matrix import (
            _ensure_shard_has_projection_matrix,
        )

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        base_name = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{base_name}-2024Q1"

        vector_store.create_collection(base_name, _DIM)
        base_path = vector_store._get_collection_path(base_name)

        vector_store.create_collection(shard_name, _DIM)
        shard_path = vector_store._get_collection_path(shard_name)
        matrix_file = shard_path / "projection_matrix.npy"
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()

        import os as os_module

        real_replace = os_module.replace
        replace_calls = []

        def spy_replace(src, dst):
            replace_calls.append((os_module.fspath(src), os_module.fspath(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(
            "code_indexer.services.temporal.temporal_projection_matrix.os.replace",
            spy_replace,
        )

        _ensure_shard_has_projection_matrix(shard_path, base_path, _DIM)

        assert matrix_file.exists()
        assert len(replace_calls) == 1, (
            f"expected exactly one os.replace() call for the atomic write, "
            f"got {replace_calls}"
        )
        tmp_src, dst = replace_calls[0]
        assert dst == str(matrix_file)
        assert tmp_src != str(matrix_file), (
            "must replace FROM a distinct temp file, not write matrix_file directly"
        )
        assert Path(tmp_src).parent == matrix_file.parent
        assert not Path(tmp_src).exists(), "temp file must be gone after the rename"

    def test_regenerate_branch_writes_via_tmp_file_then_atomic_replace(
        self, tmp_path, monkeypatch
    ):
        """Regenerate branch (no base collection available): same atomic
        temp-file-then-os.replace contract applies.
        """
        from code_indexer.services.temporal.temporal_projection_matrix import (
            _ensure_shard_has_projection_matrix,
        )

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        shard_name = "code-indexer-temporal-voyage_code_3-2026Q2"
        vector_store.create_collection(shard_name, _DIM)
        shard_path = vector_store._get_collection_path(shard_name)
        matrix_file = shard_path / "projection_matrix.npy"
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()

        import os as os_module

        real_replace = os_module.replace
        replace_calls = []

        def spy_replace(src, dst):
            replace_calls.append((os_module.fspath(src), os_module.fspath(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(
            "code_indexer.services.temporal.temporal_projection_matrix.os.replace",
            spy_replace,
        )

        _ensure_shard_has_projection_matrix(shard_path, None, _DIM)

        assert matrix_file.exists()
        assert len(replace_calls) == 1
        tmp_src, dst = replace_calls[0]
        assert dst == str(matrix_file)
        assert tmp_src != str(matrix_file)
        assert Path(tmp_src).parent == matrix_file.parent
        assert not Path(tmp_src).exists()

        healed = np.load(str(matrix_file))
        assert healed.shape == (_DIM, 64)

    def test_concurrent_healers_of_same_shard_both_succeed_with_valid_matrix(
        self, tmp_path
    ):
        """Real-concurrency regression: two threads both self-healing the
        SAME missing-matrix shard at the same time must both complete
        without exception, and the file left on disk afterward must be a
        valid, correctly-shaped, loadable matrix -- never a torn/corrupt
        write. Uses a real threading.Barrier to line the threads up at the
        same starting point (deterministic synchronization, not a sleep) to
        maximize the chance both threads overlap on the write.

        No mocks: real FilesystemVectorStore, real ProjectionMatrixManager,
        real threads, real filesystem.
        """
        import threading

        from code_indexer.services.temporal.temporal_projection_matrix import (
            _ensure_shard_has_projection_matrix,
        )

        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        shard_name = "code-indexer-temporal-voyage_code_3-2025Q4"
        vector_store.create_collection(shard_name, _DIM)
        shard_path = vector_store._get_collection_path(shard_name)
        matrix_file = shard_path / "projection_matrix.npy"
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()

        thread_count = 4
        barrier = threading.Barrier(thread_count)
        errors: list = []

        def worker():
            try:
                barrier.wait(timeout=10)
                _ensure_shard_has_projection_matrix(shard_path, None, _DIM)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "healer thread did not finish in time"

        assert not errors, f"concurrent self-heal raised: {errors}"
        assert matrix_file.exists()

        # The file left behind must be a fully-formed, loadable matrix -- not
        # truncated/corrupt from an interleaved partial write.
        healed = np.load(str(matrix_file))
        assert healed.shape == (_DIM, 64)
