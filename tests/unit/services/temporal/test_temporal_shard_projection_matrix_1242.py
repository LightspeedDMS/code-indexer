"""Tests for Bug #1242: migrated temporal shards lack projection_matrix.npy.

Root cause:
- _build_one_shard builds a shard from a monolithic HNSW but never copies
  projection_matrix.npy, so the first upsert_points into that shard crashes
  with FileNotFoundError.
- The sharding loop in temporal_indexer.py has no self-heal for already-deployed
  broken shards.

Fixes tested here:
- Fix 1 (_build_one_shard): projection_matrix.npy copied from monolith (or
  regenerated if absent) before the atomic rename; quantization_range from
  monolith meta written into shard meta.
- Fix 2 (temporal_indexer.py loop else-branch): when a shard exists but lacks
  the matrix, copy from the base (monolith) collection or regenerate.
- End-to-end: after healing, load_matrix() and upsert_points() succeed without
  FileNotFoundError.
"""

import json
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DIM = 8  # small dimension for fast tests
_Q1_2024 = int(datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp())
_Q2_2024 = int(datetime(2024, 5, 10, tzinfo=timezone.utc).timestamp())


def _write_id_index_bin(path: Path, id_index: Dict[str, str]) -> None:
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _write_vector_json(json_path: Path, point_id: str, vector: list, ts: int) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "id": point_id,
        "vector": vector,
        "payload": {
            "commit_timestamp": ts,
            "commit_date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            ),
        },
    }
    with open(json_path, "w") as f:
        json.dump(data, f)


def _build_monolithic_collection(
    index_path: Path,
    collection_name: str,
    vectors: np.ndarray,
    timestamps: list,
    include_projection_matrix: bool = True,
    include_quantization_range: bool = True,
) -> Path:
    """Build a monolithic temporal HNSW collection on disk.

    Args:
        include_projection_matrix: If True, write a real projection_matrix.npy.
        include_quantization_range: If True, include quantization_range in meta.

    Returns:
        Collection directory path.
    """
    import hnswlib
    from code_indexer.storage.projection_matrix_manager import ProjectionMatrixManager

    coll_dir = index_path / collection_name
    coll_dir.mkdir(parents=True, exist_ok=True)

    n = len(vectors)
    dim = vectors.shape[1]

    hnsw_idx = hnswlib.Index(space="cosine", dim=dim)
    hnsw_idx.init_index(
        max_elements=n, M=16, ef_construction=200, allow_replace_deleted=True
    )
    hnsw_idx.add_items(vectors, np.arange(n))
    hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

    id_mapping: Dict[str, str] = {}
    id_index: Dict[str, str] = {}
    for i, (vec, ts) in enumerate(zip(vectors, timestamps)):
        point_id = f"repo:commit:{'a' * 40}:{i}"
        rel_path = f"{i:02x}/vec_{i}.json"
        _write_vector_json(coll_dir / rel_path, point_id, vec.tolist(), ts)
        id_mapping[str(i)] = point_id
        id_index[point_id] = rel_path

    _write_id_index_bin(coll_dir / "id_index.bin", id_index)

    output_dim = 64
    std = math.sqrt(output_dim / dim)
    meta: dict = {
        "name": collection_name,
        "vector_size": dim,
        "created_at": datetime.utcnow().isoformat(),
        "hnsw_index": {
            "version": 1,
            "vector_count": n,
            "vector_dim": dim,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": datetime.utcnow().isoformat(),
            "file_size_bytes": (coll_dir / "hnsw_index.bin").stat().st_size,
            "id_mapping": id_mapping,
            "is_stale": False,
            "last_marked_stale": None,
        },
    }
    if include_quantization_range:
        meta["quantization_range"] = {"min": float(-3 * std), "max": float(3 * std)}

    with open(coll_dir / "collection_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    if include_projection_matrix:
        manager = ProjectionMatrixManager()
        matrix = manager.create_projection_matrix(input_dim=dim, output_dim=output_dim)
        manager.save_matrix(matrix, coll_dir)

    return coll_dir


# ---------------------------------------------------------------------------
# Fix 1: _build_one_shard copies / regenerates projection_matrix.npy
# ---------------------------------------------------------------------------


class TestMigrationCopiesProjectionMatrix:
    """Fix 1: run_temporal_migration copies projection_matrix.npy into shards."""

    def test_shard_has_projection_matrix_byte_identical_to_monolith(self, tmp_path):
        """After migration each shard contains a projection_matrix.npy that is
        byte-identical to the one in the monolith collection."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(4, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q1_2024, _Q2_2024, _Q2_2024]

        coll_dir = _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=True,
        )

        # Read monolith matrix bytes BEFORE migration
        monolith_matrix_bytes = (coll_dir / "projection_matrix.npy").read_bytes()

        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        # Two shards expected (Q1 and Q2)
        shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.endswith("Q1")
            or (d.is_dir() and d.name.endswith("Q2"))
        ]
        assert len(shards) == 2, (
            f"Expected 2 shards, got {[d.name for d in index_path.iterdir() if d.is_dir()]}"
        )

        for shard in shards:
            matrix_file = shard / "projection_matrix.npy"
            assert matrix_file.exists(), (
                f"projection_matrix.npy missing in shard {shard.name}"
            )
            assert matrix_file.read_bytes() == monolith_matrix_bytes, (
                f"projection_matrix.npy in {shard.name} differs from monolith"
            )

    def test_shard_meta_carries_monolith_quantization_range(self, tmp_path):
        """Shard collection_meta.json must contain the monolith's quantization_range."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(2, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q2_2024]

        coll_dir = _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=True,
        )

        # Read monolith quantization_range
        with open(coll_dir / "collection_meta.json") as f:
            mono_meta = json.load(f)
        monolith_qr = mono_meta["quantization_range"]

        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith(collection_name)
            and d.name != collection_name
        ]
        assert len(shards) == 2

        for shard in shards:
            with open(shard / "collection_meta.json") as f:
                shard_meta = json.load(f)
            assert "quantization_range" in shard_meta, (
                f"quantization_range missing in {shard.name} meta"
            )
            assert shard_meta["quantization_range"]["min"] == pytest.approx(
                monolith_qr["min"]
            ), f"quantization_range min mismatch in {shard.name}"
            assert shard_meta["quantization_range"]["max"] == pytest.approx(
                monolith_qr["max"]
            ), f"quantization_range max mismatch in {shard.name}"


class TestMigrationFallbackRegeneratesMatrix:
    """Fix 1 fallback: monolith missing projection_matrix.npy -> shard gets fresh matrix."""

    def test_fallback_regenerates_matrix_when_monolith_missing_matrix(self, tmp_path):
        """When monolith lacks projection_matrix.npy, migration regenerates one for each shard."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(2, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q2_2024]

        _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=False,  # ← monolith missing matrix
        )

        # Must not raise
        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith(collection_name)
            and d.name != collection_name
        ]
        assert len(shards) == 2

        for shard in shards:
            matrix_file = shard / "projection_matrix.npy"
            assert matrix_file.exists(), (
                f"projection_matrix.npy missing in shard {shard.name} after fallback"
            )
            matrix = np.load(str(matrix_file))
            assert matrix.shape == (_DIM, 64), (
                f"Unexpected matrix shape {matrix.shape} in {shard.name}"
            )

    def test_fallback_computes_quantization_range_when_monolith_meta_missing_it(
        self, tmp_path
    ):
        """When monolith meta has no quantization_range, shards get the computed formula."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(2, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q2_2024]

        _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=False,
            include_quantization_range=False,  # ← monolith has no qr
        )

        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith(collection_name)
            and d.name != collection_name
        ]
        assert len(shards) == 2

        expected_std = math.sqrt(64 / _DIM)
        expected_min = -3 * expected_std
        expected_max = 3 * expected_std

        for shard in shards:
            with open(shard / "collection_meta.json") as f:
                meta = json.load(f)
            assert "quantization_range" in meta, (
                f"quantization_range missing in {shard.name}"
            )
            assert meta["quantization_range"]["min"] == pytest.approx(expected_min)
            assert meta["quantization_range"]["max"] == pytest.approx(expected_max)


# ---------------------------------------------------------------------------
# Fix 2: temporal_indexer.py else-branch self-heal
# ---------------------------------------------------------------------------


def _build_broken_shard(
    index_path: Path,
    shard_name: str,
    vector_dim: int,
    include_quantization_range: bool = False,
) -> Path:
    """Create a shard directory with collection_meta.json but NO projection_matrix.npy."""
    shard_dir = index_path / shard_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": shard_name,
        "vector_size": vector_dim,
        "created_at": datetime.utcnow().isoformat(),
        "hnsw_index": {
            "version": 1,
            "vector_count": 0,
            "vector_dim": vector_dim,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "id_mapping": {},
        },
    }
    if include_quantization_range:
        std = math.sqrt(64 / vector_dim)
        meta["quantization_range"] = {"min": float(-3 * std), "max": float(3 * std)}
    with open(shard_dir / "collection_meta.json", "w") as f:
        json.dump(meta, f)
    return shard_dir


class TestSelfHealCopyFromBase:
    """Fix 2: self-heal copies projection_matrix.npy from base (monolith) collection."""

    def test_self_heal_copies_matrix_from_base_when_available(self, tmp_path):
        """_ensure_shard_has_projection_matrix copies matrix from base if present."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        index_path = tmp_path / "index"
        base_name = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{base_name}-2024Q1"

        # Base (monolith) collection has projection_matrix.npy
        base_dir = index_path / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        manager = ProjectionMatrixManager()
        base_matrix = manager.create_projection_matrix(input_dim=_DIM, output_dim=64)
        manager.save_matrix(base_matrix, base_dir)
        base_matrix_bytes = (base_dir / "projection_matrix.npy").read_bytes()

        # Shard exists but lacks matrix
        shard_dir = _build_broken_shard(index_path, shard_name, _DIM)
        assert not (shard_dir / "projection_matrix.npy").exists()

        _ensure_shard_has_projection_matrix(shard_dir, base_dir, _DIM)

        matrix_file = shard_dir / "projection_matrix.npy"
        assert matrix_file.exists(), "projection_matrix.npy not created after self-heal"
        assert matrix_file.read_bytes() == base_matrix_bytes, (
            "Shard matrix not byte-identical to base matrix"
        )

    def test_self_heal_backfills_quantization_range_in_shard_meta(self, tmp_path):
        """After self-heal, shard meta has quantization_range (from base or computed)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        index_path = tmp_path / "index"
        base_name = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{base_name}-2024Q1"

        # Base has a matrix AND a quantization_range in meta
        base_dir = index_path / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        std = math.sqrt(64 / _DIM)
        expected_qr = {"min": float(-3 * std), "max": float(3 * std)}
        with open(base_dir / "collection_meta.json", "w") as f:
            json.dump(
                {
                    "name": base_name,
                    "vector_size": _DIM,
                    "quantization_range": expected_qr,
                },
                f,
            )
        manager = ProjectionMatrixManager()
        base_matrix = manager.create_projection_matrix(input_dim=_DIM, output_dim=64)
        manager.save_matrix(base_matrix, base_dir)

        # Shard has no matrix, no quantization_range
        shard_dir = _build_broken_shard(
            index_path, shard_name, _DIM, include_quantization_range=False
        )

        _ensure_shard_has_projection_matrix(shard_dir, base_dir, _DIM)

        with open(shard_dir / "collection_meta.json") as f:
            shard_meta = json.load(f)
        assert "quantization_range" in shard_meta, "quantization_range not backfilled"
        assert shard_meta["quantization_range"]["min"] == pytest.approx(
            expected_qr["min"]
        )
        assert shard_meta["quantization_range"]["max"] == pytest.approx(
            expected_qr["max"]
        )

    def test_self_heal_idempotent_when_matrix_already_present(self, tmp_path):
        """_ensure_shard_has_projection_matrix is a no-op when matrix already exists."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        index_path = tmp_path / "index"
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir = index_path / shard_name
        shard_dir.mkdir(parents=True, exist_ok=True)

        manager = ProjectionMatrixManager()
        matrix = manager.create_projection_matrix(input_dim=_DIM, output_dim=64)
        manager.save_matrix(matrix, shard_dir)
        original_bytes = (shard_dir / "projection_matrix.npy").read_bytes()
        original_mtime = (shard_dir / "projection_matrix.npy").stat().st_mtime

        # Call again — must not overwrite
        _ensure_shard_has_projection_matrix(shard_dir, None, _DIM)

        assert (shard_dir / "projection_matrix.npy").read_bytes() == original_bytes
        assert (shard_dir / "projection_matrix.npy").stat().st_mtime == original_mtime


class TestSelfHealRegenerateFallback:
    """Fix 2 fallback: no base matrix available → regenerate fresh matrix."""

    def test_self_heal_regenerates_matrix_when_no_base(self, tmp_path):
        """When source_collection_path is None, _ensure_shard_has_projection_matrix regenerates."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )

        index_path = tmp_path / "index"
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir = _build_broken_shard(index_path, shard_name, _DIM)

        _ensure_shard_has_projection_matrix(
            shard_dir, source_collection_path=None, vector_dim=_DIM
        )

        matrix_file = shard_dir / "projection_matrix.npy"
        assert matrix_file.exists(), "Matrix not regenerated when source=None"
        matrix = np.load(str(matrix_file))
        assert matrix.shape == (_DIM, 64)

    def test_self_heal_regenerates_matrix_when_base_missing_matrix(self, tmp_path):
        """When base collection exists but lacks matrix, shard still gets one regenerated."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )

        index_path = tmp_path / "index"
        base_name = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{base_name}-2024Q1"

        # Base dir exists but has NO projection_matrix.npy
        base_dir = index_path / base_name
        base_dir.mkdir(parents=True, exist_ok=True)

        shard_dir = _build_broken_shard(index_path, shard_name, _DIM)

        _ensure_shard_has_projection_matrix(shard_dir, base_dir, _DIM)

        matrix_file = shard_dir / "projection_matrix.npy"
        assert matrix_file.exists(), "Matrix not regenerated when base matrix absent"
        matrix = np.load(str(matrix_file))
        assert matrix.shape == (_DIM, 64)

    def test_self_heal_computes_quantization_range_formula_when_no_base(self, tmp_path):
        """No base → quantization_range computed as ±3·sqrt(64/dim)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )

        index_path = tmp_path / "index"
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir = _build_broken_shard(
            index_path, shard_name, _DIM, include_quantization_range=False
        )

        _ensure_shard_has_projection_matrix(shard_dir, None, _DIM)

        with open(shard_dir / "collection_meta.json") as f:
            meta = json.load(f)
        assert "quantization_range" in meta

        std = math.sqrt(64 / _DIM)
        assert meta["quantization_range"]["min"] == pytest.approx(-3 * std)
        assert meta["quantization_range"]["max"] == pytest.approx(3 * std)


# ---------------------------------------------------------------------------
# End-to-end: after self-heal, load_matrix and upsert_points succeed
# ---------------------------------------------------------------------------


class TestEndToEndNoCrashAfterHealing:
    """After healing, projection_matrix.npy loads cleanly (no FileNotFoundError)."""

    def test_load_matrix_succeeds_after_self_heal(self, tmp_path):
        """ProjectionMatrixManager.load_matrix() raises FileNotFoundError before heal
        and succeeds after _ensure_shard_has_projection_matrix is called."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        index_path = tmp_path / "index"
        shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir = _build_broken_shard(index_path, shard_name, _DIM)

        manager = ProjectionMatrixManager()

        # Before heal: FileNotFoundError
        with pytest.raises(FileNotFoundError):
            manager.load_matrix(shard_dir)

        # Heal
        _ensure_shard_has_projection_matrix(shard_dir, None, _DIM)

        # After heal: no exception, correct shape
        # Clear singleton cache so load_matrix re-reads from disk
        ProjectionMatrixManager._matrix_cache.clear()
        matrix = manager.load_matrix(shard_dir)
        assert matrix.shape == (_DIM, 64)

    def test_upsert_points_succeeds_into_healed_shard(self, tmp_path):
        """upsert_points does not raise FileNotFoundError after projection matrix is healed.

        Bug #1264 note: upsert_points() now self-heals a missing matrix
        automatically at the write chokepoint (see
        test_temporal_write_chokepoint_self_heal_1264.py), so the pre-#1264
        "upsert_points crashes without the matrix" assertion that used to
        live here is no longer true and has been removed. This test now
        focuses on what it always meant to prove: the manual
        _ensure_shard_has_projection_matrix heal (used by the
        temporal_indexer.py shard-prep loop) is idempotent and upsert_points
        succeeds against a shard it healed.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _ensure_shard_has_projection_matrix,
        )
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        base_path = tmp_path / "index"
        vector_store = FilesystemVectorStore(base_path=base_path)

        collection_name = "code-indexer-temporal-voyage_code_3-2024Q1"
        vector_store.create_collection(collection_name, _DIM)

        # Simulate the bug: remove the matrix that create_collection wrote
        coll_path = vector_store._get_collection_path(collection_name)
        matrix_file = coll_path / "projection_matrix.npy"
        assert matrix_file.exists()
        matrix_file.unlink()
        # Clear cache so load_matrix re-reads disk
        ProjectionMatrixManager._matrix_cache.clear()

        # Heal via the shard-prep-loop helper (Bug #1242) before any write.
        _ensure_shard_has_projection_matrix(coll_path, None, _DIM)
        ProjectionMatrixManager._matrix_cache.clear()

        # upsert_points succeeds against the pre-healed shard.
        test_point = {
            "id": "test:commit:" + "a" * 40 + ":0",
            "vector": np.random.rand(_DIM).astype(np.float32).tolist(),
            "payload": {"commit_timestamp": _Q1_2024},
        }
        result = vector_store.upsert_points(
            collection_name, [test_point], watch_mode=True
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Regression: existing migration tests still pass (smoke check via re-import)
# ---------------------------------------------------------------------------


class TestRegressionExistingMigration:
    """Smoke: the migration still handles multi-quarter bucketing after Fix 1."""

    def test_migration_still_groups_vectors_by_quarter(self, tmp_path):
        """After Fix 1, quarterly shards are still created correctly."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(4, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q1_2024, _Q2_2024, _Q2_2024]

        _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=True,
        )

        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        q1_shard = index_path / f"{collection_name}-2024Q1"
        q2_shard = index_path / f"{collection_name}-2024Q2"
        assert q1_shard.exists() and (q1_shard / "collection_meta.json").exists()
        assert q2_shard.exists() and (q2_shard / "collection_meta.json").exists()

    def test_migration_writes_migration_complete_marker(self, tmp_path):
        """Marker is still written after successful migration (no regression)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
            MIGRATION_COMPLETE_MARKER,
        )

        index_path = tmp_path / "index"
        collection_name = "code-indexer-temporal-voyage_code_3"
        vectors = np.random.rand(2, _DIM).astype(np.float32)
        timestamps = [_Q1_2024, _Q2_2024]

        _build_monolithic_collection(
            index_path,
            collection_name,
            vectors,
            timestamps,
            include_projection_matrix=True,
        )

        run_temporal_migration(
            index_path=index_path, repo_alias="test-repo", progress_callback=None
        )

        monolith_dir = index_path / collection_name
        assert (monolith_dir / MIGRATION_COMPLETE_MARKER).exists()


# ---------------------------------------------------------------------------
# Integration: self-heal fires through the real TemporalIndexer.index_commits()
# ---------------------------------------------------------------------------


class TestIndexCommitsSelfHeal:
    """End-to-end recovery through the real TemporalIndexer.index_commits() entry point.

    Validates that the else-branch in the sharding loop (Bug #1242 Fix 2) detects
    a missing projection_matrix.npy and heals it BEFORE begin_indexing / upsert_points
    would raise FileNotFoundError.

    Unlike TestSelfHealCopyFromBase / TestSelfHealRegenerateFallback (which call
    _ensure_shard_has_projection_matrix directly), this test goes through the full
    index_commits() code path so the sharding loop guard is exercised.

    Real collaborators used:
    - FilesystemVectorStore  — _get_collection_path returns a real Path (not Mock)
    - ProjectionMatrixManager — real matrix create / save / load
    - hnswlib (via create_collection) — valid HNSW on disk

    Mocked external boundaries (same policy as test_temporal_sharding_1171.py):
    - _get_commit_history — subprocess git log
    - _get_current_branch — subprocess git branch
    - EmbeddingProviderFactory — remote embedding API
    - VectorCalculationManager — worker threads; cancellation_event.is_set() = True
      so workers exit immediately without real API calls
    """

    def test_index_commits_heals_missing_projection_matrix(self, tmp_path):
        """index_commits() self-heals a broken shard without raising FileNotFoundError.

        Production scenario:
          1. A shard exists on disk from a pre-fix migration run:
             collection_meta.json + hnsw_index.bin present, projection_matrix.npy absent.
          2. A new commit whose quarter maps to that shard is submitted to index_commits().
          3. The sharding loop detects the missing matrix in the else-branch and heals it.
          4. begin_indexing / end_indexing succeed; no FileNotFoundError is raised.
          5. After the call, projection_matrix.npy is present and has the correct shape.
        """
        from datetime import datetime, timezone
        from unittest.mock import Mock, patch

        from code_indexer.services.temporal.models import CommitInfo
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
        from code_indexer.storage.projection_matrix_manager import (
            ProjectionMatrixManager,
        )

        vector_dim = _DIM  # 8 — small so HNSW ops are fast

        # --- Config mock (mirrors _make_indexer_mocks in test_temporal_sharding_1171) ---
        mock_config = Mock()
        mock_config.voyage_ai = Mock()
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.voyage_ai.parallel_requests = 4
        mock_config.voyage_ai.temporal_parallel_requests = None
        mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
        mock_config.cohere = Mock()
        mock_config.cohere.parallel_requests = 4
        mock_config.cohere.temporal_parallel_requests = None
        mock_config.embedding_provider = "voyage-ai"
        mock_config.temporal = Mock()
        mock_config.temporal.diff_context_lines = 3
        mock_config.file_extensions = []
        mock_config.override_config = None
        mock_config.codebase_dir = tmp_path

        cfg_dir = tmp_path / ".code-indexer"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        mock_config_manager = Mock()
        mock_config_manager.get_config.return_value = mock_config
        mock_config_manager.config_path = cfg_dir / "config.json"

        # --- Real FilesystemVectorStore so _get_collection_path returns Path ---
        index_dir = cfg_dir / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        vector_store = FilesystemVectorStore(base_path=index_dir)

        # --- Pre-create broken shard (simulates pre-fix migration output) ---
        collection_base = "code-indexer-temporal-voyage_code_3"
        shard_name = f"{collection_base}-2024Q1"

        # create_collection writes collection_meta.json + hnsw_index.bin + matrix
        vector_store.create_collection(shard_name, vector_dim)

        shard_dir = vector_store._get_collection_path(shard_name)
        matrix_file = shard_dir / "projection_matrix.npy"
        assert matrix_file.exists(), (
            "Precondition failed: create_collection must write projection_matrix.npy"
        )

        # Simulate the Bug #1242 state: remove the matrix
        matrix_file.unlink()
        ProjectionMatrixManager._matrix_cache.clear()
        assert not matrix_file.exists(), "Precondition: matrix must be absent"

        # --- TemporalIndexer with the real vector_store ---
        indexer = TemporalIndexer(
            mock_config_manager,
            vector_store,
            collection_name=collection_base,
        )

        # --- Commit in Q1 2024 (timestamp maps to the broken shard) ---
        ts_q1 = int(datetime(2024, 2, 15, tzinfo=timezone.utc).timestamp())
        commit = CommitInfo(
            hash="deadbeef01",
            timestamp=ts_q1,
            author_name="Test Author",
            author_email="test@example.com",
            message="Bug #1242 integration commit",
            parent_hashes=[],
        )

        _factory_patch = (
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        )

        # Workers exit immediately — no real git diffs or embedding API calls
        mock_vcm_instance = Mock()
        mock_vcm_instance.cancellation_event = Mock()
        mock_vcm_instance.cancellation_event.is_set.return_value = True

        # Drive through the real entry point — must NOT raise FileNotFoundError
        with patch.object(indexer, "_get_commit_history", return_value=[commit]):
            with patch.object(indexer, "_get_current_branch", return_value="main"):
                with patch(_factory_patch) as mock_factory:
                    mock_factory.create.return_value = Mock()
                    mock_factory.get_provider_model_info.return_value = {
                        "dimensions": vector_dim
                    }
                    with patch(
                        "code_indexer.services.temporal.temporal_indexer"
                        ".VectorCalculationManager"
                    ) as mock_vcm_cls:
                        mock_vcm_cls.return_value.__enter__ = Mock(
                            return_value=mock_vcm_instance
                        )
                        mock_vcm_cls.return_value.__exit__ = Mock(return_value=False)
                        indexer.index_commits()  # must not raise

        # --- Assert self-heal completed ---
        ProjectionMatrixManager._matrix_cache.clear()  # re-read from disk
        assert matrix_file.exists(), (
            "projection_matrix.npy must be present after index_commits() self-heal"
        )
        healed_matrix = np.load(str(matrix_file))
        # The matrix input_dim is driven by the provider's reported dimensions
        # (_shard_vector_size inside index_commits), not by the test's _DIM.
        # Assert the invariants that matter for the write path:
        #   - 2-D array (input_dim × output_dim)
        #   - output_dim == 64 (fixed quantization target used by upsert_points)
        assert healed_matrix.ndim == 2, (
            f"Healed matrix must be 2D, got shape {healed_matrix.shape!r}"
        )
        assert healed_matrix.shape[1] == 64, (
            f"Healed matrix output_dim must be 64, got {healed_matrix.shape[1]}"
        )
