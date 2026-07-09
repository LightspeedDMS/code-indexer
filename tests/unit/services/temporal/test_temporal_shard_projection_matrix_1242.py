"""Tests for Bug #1242: temporal shards must have projection_matrix.npy.

Story #1290: the migration/conversion machinery this file originally also
covered (run_temporal_migration, _build_one_shard) has been deleted as part
of the per-commit hard cut -- temporal_migration_service.py no longer
exists. What remains here is the RELOCATED self-heal helper
(_ensure_shard_has_projection_matrix, now in temporal_projection_matrix.py,
AC18) plus the end-to-end integration test proving it is still wired into
the real TemporalIndexer.index_commits() shard-prep loop.

Fixes tested here:
- Self-heal helper: projection_matrix.npy copied from a source collection
  (or regenerated if absent/source lacks it); quantization_range backfilled
  when missing.
- End-to-end: after healing, load_matrix() and upsert_points() succeed
  without FileNotFoundError.
- Integration: the shard-prep-loop else-branch in index_commits() detects a
  missing projection_matrix.npy and heals it before any write.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest


_DIM = 8  # small dimension for fast tests


def _build_broken_shard(
    index_path: Path,
    shard_name: str,
    vector_dim: int,
    include_quantization_range: bool = False,
) -> Path:
    """Create a shard directory with collection_meta.json but NO projection_matrix.npy."""
    shard_dir = index_path / shard_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

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


# ---------------------------------------------------------------------------
# Fix 2 (relocated, AC18): temporal_indexer.py else-branch self-heal
# ---------------------------------------------------------------------------


class TestSelfHealCopyFromBase:
    """Fix 2: self-heal copies projection_matrix.npy from base (monolith) collection."""

    def test_self_heal_copies_matrix_from_base_when_available(self, tmp_path):
        """_ensure_shard_has_projection_matrix copies matrix from base if present."""
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from code_indexer.services.temporal.temporal_projection_matrix import (
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
        from datetime import datetime, timezone

        _q1_2024 = int(datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp())
        test_point = {
            "id": "test:commit:" + "a" * 40 + ":0",
            "vector": np.random.rand(_DIM).astype(np.float32).tolist(),
            "payload": {"commit_timestamp": _q1_2024},
        }
        result = vector_store.upsert_points(
            collection_name, [test_point], watch_mode=True
        )
        assert result is not None


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
        mock_config.temporal.embedders = ["voyage-context-4"]
        mock_config.temporal.active_embedder = "voyage-context-4"
        mock_config.temporal.aggregation_chunk_chars = 4096
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
        # Story #1290: shards are named from the temporal active_embedder
        # ("voyage-context-4" -> slug "voyage_context_4"), not the regular
        # semantic-search provider's model.
        collection_base = "code-indexer-temporal-voyage_context_4"
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
            parent_hashes="",
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
