"""Tests for Bug #1207: CLI --index-commit shard-completion cleanup + predicate hardening.

Three root causes fixed:
1. get_overlapping_shards() includes base dir on marker-ABSENCE alone (no hnsw check).
2. CLI shard path never calls _cleanup_monolithic_collection() or writes the marker.
3. No unified has_real_monolith predicate shared by migration-detection and query fan-out.

Test matrix:
  TC-1: get_overlapping_shards EXCLUDES marker-less base dir with NO hnsw_index.bin.
  TC-2: get_overlapping_shards INCLUDES marker-less base dir WITH hnsw_index.bin.
  TC-3: get_overlapping_shards EXCLUDES base dir WITH migration_complete.marker
        regardless of hnsw presence (already migrated).
  TC-4: Unified has_real_monolith helper used by BOTH _needs_temporal_migration and
        get_overlapping_shards (same truth table on identical inputs).
  TC-5: TemporalIndexer.close() writes migration_complete.marker and removes
        hnsw_index.bin + id_index.bin from the base collection dir after sharding.
  TC-6: TemporalIndexer.close() leaves the marker absent when NO shards were processed
        (non-sharded path must not erroneously write the marker).
  TC-7: Regression — once base dir excluded by hardened predicate, get_overlapping_shards
        returns only real shards (no stale-monolith mixing).
"""

from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_shard_dir(index_path: Path, shard_name: str) -> Path:
    """Create a minimal quarterly shard directory (just the dir itself)."""
    d = index_path / shard_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_base_dir(
    index_path: Path,
    base_name: str,
    *,
    with_hnsw: bool = False,
    with_marker: bool = False,
) -> Path:
    """Create a base (monolithic) collection directory.

    Args:
        index_path: Parent index directory.
        base_name: Name of the base collection, e.g. 'code-indexer-temporal-voyage_code_3'.
        with_hnsw: If True, create hnsw_index.bin inside the dir.
        with_marker: If True, create migration_complete.marker inside the dir.
    """
    d = index_path / base_name
    d.mkdir(parents=True, exist_ok=True)
    if with_hnsw:
        (d / "hnsw_index.bin").write_bytes(b"fake_hnsw_data")
        (d / "id_index.bin").write_bytes(b"fake_id_data")
    if with_marker:
        (d / "migration_complete.marker").write_text("migration complete\n")
    return d


# ---------------------------------------------------------------------------
# TC-1: get_overlapping_shards EXCLUDES marker-less base dir with NO hnsw
# ---------------------------------------------------------------------------


class TestGetOverlappingShardsPredicate:
    """Bug #1207 Fix 2: harden get_overlapping_shards to require hnsw_index.bin."""

    def _get_overlapping_shards(self):
        from code_indexer.services.temporal.temporal_collection_naming import (
            get_overlapping_shards,
        )

        return get_overlapping_shards

    def test_tc1_excludes_markerless_base_dir_without_hnsw(self, tmp_path):
        """TC-1: marker absent + NO hnsw_index.bin -> base dir EXCLUDED from results.

        This is the core bug: the original code returned has_legacy=True whenever
        the marker was absent, even if there was no actual monolithic HNSW on disk.
        After the fix, the base dir is only included if hnsw_index.bin exists.
        """
        model_name = "voyage-code-3"
        base_name = "code-indexer-temporal-voyage_code_3"
        index_path = tmp_path / "index"
        index_path.mkdir()

        # Create a shard
        _make_shard_dir(index_path, f"{base_name}-2024Q1")
        # Create base dir WITHOUT hnsw and WITHOUT marker (the post-shard state)
        _make_base_dir(index_path, base_name, with_hnsw=False, with_marker=False)

        fn = self._get_overlapping_shards()
        result = fn(model_name, index_path, None, None)

        # Only the shard should appear — base dir excluded (no real monolith)
        assert f"{base_name}-2024Q1" in result
        assert base_name not in result, (
            "Base dir without hnsw_index.bin must NOT appear in overlapping shards "
            "(Bug #1207: marker-absence-only predicate caused spurious HNSW-stale warnings)"
        )

    def test_tc2_includes_base_dir_with_hnsw_but_no_marker(self, tmp_path):
        """TC-2: marker absent + hnsw_index.bin EXISTS -> base dir INCLUDED (real monolith).

        This is the genuine legacy monolith case — migration has not yet run.
        """
        model_name = "voyage-code-3"
        base_name = "code-indexer-temporal-voyage_code_3"
        index_path = tmp_path / "index"
        index_path.mkdir()

        _make_base_dir(index_path, base_name, with_hnsw=True, with_marker=False)

        fn = self._get_overlapping_shards()
        result = fn(model_name, index_path, None, None)

        assert base_name in result, (
            "Base dir WITH hnsw_index.bin and NO marker must be included as legacy monolith"
        )

    def test_tc3_excludes_base_dir_with_marker_regardless_of_hnsw(self, tmp_path):
        """TC-3: marker PRESENT -> base dir excluded even if hnsw_index.bin exists.

        The marker means migration is complete; the hnsw (if still there) is stale.
        """
        model_name = "voyage-code-3"
        base_name = "code-indexer-temporal-voyage_code_3"
        index_path = tmp_path / "index"
        index_path.mkdir()

        _make_shard_dir(index_path, f"{base_name}-2024Q3")
        # After CLI cleanup: marker present, hnsw deleted — but test with hnsw still there
        _make_base_dir(index_path, base_name, with_hnsw=True, with_marker=True)

        fn = self._get_overlapping_shards()
        result = fn(model_name, index_path, None, None)

        assert base_name not in result, (
            "Base dir WITH marker must be excluded even if hnsw_index.bin still present"
        )
        assert f"{base_name}-2024Q3" in result

    def test_tc7_regression_only_shards_returned_after_exclusion(self, tmp_path):
        """TC-7: When base dir is excluded, only real quarterly shards are returned.

        Ensures no stale monolith mixing in query fan-out.
        """
        model_name = "voyage-code-3"
        base_name = "code-indexer-temporal-voyage_code_3"
        index_path = tmp_path / "index"
        index_path.mkdir()

        _make_shard_dir(index_path, f"{base_name}-2023Q4")
        _make_shard_dir(index_path, f"{base_name}-2024Q1")
        _make_shard_dir(index_path, f"{base_name}-2024Q2")
        # Post-shard state: base dir exists (empty-ish) but no hnsw, no marker
        _make_base_dir(index_path, base_name, with_hnsw=False, with_marker=False)

        fn = self._get_overlapping_shards()
        result = fn(model_name, index_path, None, None)

        # Exactly 3 shards, no base dir
        assert result == [
            f"{base_name}-2023Q4",
            f"{base_name}-2024Q1",
            f"{base_name}-2024Q2",
        ]
        assert base_name not in result


# ---------------------------------------------------------------------------
# TC-4: Unified has_real_monolith predicate
# ---------------------------------------------------------------------------


class TestUnifiedHasRealMonolithPredicate:
    """TC-4: Single has_real_monolith helper used by both callers."""

    def test_tc4_helper_exists_and_is_importable(self):
        """The shared predicate must be importable from temporal_collection_naming."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            has_real_monolith,
        )

        assert callable(has_real_monolith)

    def test_tc4_helper_true_when_hnsw_present_no_marker(self, tmp_path):
        """has_real_monolith returns True when hnsw exists and marker is absent."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            has_real_monolith,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()
        (coll_dir / "hnsw_index.bin").write_bytes(b"data")
        # No marker
        assert has_real_monolith(coll_dir) is True

    def test_tc4_helper_false_when_no_hnsw_no_marker(self, tmp_path):
        """has_real_monolith returns False when hnsw absent (even without marker)."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            has_real_monolith,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()
        # No hnsw, no marker
        assert has_real_monolith(coll_dir) is False

    def test_tc4_helper_false_when_marker_present(self, tmp_path):
        """has_real_monolith returns False when migration_complete.marker present."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            has_real_monolith,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()
        (coll_dir / "hnsw_index.bin").write_bytes(b"data")
        (coll_dir / "migration_complete.marker").write_text("migration complete\n")
        assert has_real_monolith(coll_dir) is False

    def test_tc4_needs_temporal_migration_uses_same_logic(self, tmp_path):
        """_needs_temporal_migration and has_real_monolith agree on all cases.

        Both must treat a collection as needing migration iff has_real_monolith is True.
        This test drives them over identical on-disk states and asserts equal truth values.
        """
        from code_indexer.services.temporal.temporal_collection_naming import (
            has_real_monolith,
        )
        from code_indexer.services.temporal.temporal_migration_service import (
            _needs_temporal_migration,
        )

        index_path = tmp_path / "index"
        index_path.mkdir()

        # Case A: base dir with hnsw, no marker -> both True
        base_name = "code-indexer-temporal-voyage_code_3"
        coll_dir = index_path / base_name
        _make_base_dir(index_path, base_name, with_hnsw=True, with_marker=False)
        assert has_real_monolith(coll_dir) is True
        assert _needs_temporal_migration(index_path) is True

        # Case B: add marker -> both False
        (coll_dir / "migration_complete.marker").write_text("done\n")
        assert has_real_monolith(coll_dir) is False
        assert _needs_temporal_migration(index_path) is False

        # Case C: remove hnsw too -> both False
        (coll_dir / "hnsw_index.bin").unlink()
        assert has_real_monolith(coll_dir) is False
        assert _needs_temporal_migration(index_path) is False


# ---------------------------------------------------------------------------
# TC-5: TemporalIndexer.close() writes marker + removes monolithic bins
# ---------------------------------------------------------------------------


class TestTemporalIndexerCloseWritesMarker:
    """TC-5 / TC-6: close() must invoke shared cleanup after shard-based indexing."""

    def _make_indexer(self, tmp_path: Path):
        """Create a TemporalIndexer with minimal mocks.

        EmbeddingProviderFactory is lazy-imported inside _ensure_temporal_collection()
        via '...services.embedding_factory', so we patch at the module level where it
        actually lives.
        """
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
        from unittest.mock import Mock

        mock_config = Mock()
        mock_config.embedding_provider = "voyage-ai"
        mock_config.voyage_ai.model = "voyage-code-3"
        mock_config.temporal.diff_context_lines = 3
        mock_config.file_extensions = []
        mock_config.override_config = None
        mock_config.voyage_ai.parallel_requests = 4
        mock_config.voyage_ai.temporal_parallel_requests = None
        mock_config.voyage_ai.max_concurrent_batches_per_commit = 10

        mock_config_manager = Mock()
        mock_config_manager.get_config.return_value = mock_config
        mock_config_manager.config_path = tmp_path / ".code-indexer" / "config.json"

        index_dir = tmp_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True, exist_ok=True)

        mock_vector_store = Mock()
        mock_vector_store.project_root = tmp_path
        mock_vector_store.base_path = index_dir
        mock_vector_store.collection_exists.return_value = True
        mock_vector_store.load_id_index.return_value = set()
        mock_vector_store.end_indexing.return_value = None

        # EmbeddingProviderFactory is lazy-imported inside _ensure_temporal_collection
        # using the relative path ...services.embedding_factory, which resolves to
        # code_indexer.services.embedding_factory.
        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_epf:
            mock_epf.get_provider_model_info.return_value = {"dimensions": 1024}
            indexer = TemporalIndexer(
                mock_config_manager,
                mock_vector_store,
                collection_name="code-indexer-temporal-voyage_code_3",
            )

        return indexer, index_dir

    def test_tc5_close_writes_marker_and_removes_bins_after_sharding(self, tmp_path):
        """TC-5: After shard-based indexing, close() must write the marker and remove bins.

        This test will FAIL before the fix because TemporalIndexer.close() does not
        currently call _cleanup_monolithic_collection (or equivalent).
        """
        indexer, index_dir = self._make_indexer(tmp_path)

        # Set up the base collection dir with monolithic binaries (pre-cleanup state)
        base_name = "code-indexer-temporal-voyage_code_3"
        base_dir = index_dir / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "hnsw_index.bin").write_bytes(b"old_monolith_vectors")
        (base_dir / "id_index.bin").write_bytes(b"old_id_data")

        # Create some quarterly shards (simulates post-index_commits state)
        shard_q1 = index_dir / f"{base_name}-2024Q1"
        shard_q1.mkdir()
        shard_q2 = index_dir / f"{base_name}-2024Q2"
        shard_q2.mkdir()

        # Simulate that index_commits ran sharded mode and completed successfully
        indexer._processed_shards = [f"{base_name}-2024Q1", f"{base_name}-2024Q2"]
        indexer._indexing_complete = True

        indexer.close()

        # After close(): marker must exist, monolithic bins must be gone
        marker = base_dir / "migration_complete.marker"
        assert marker.exists(), (
            "Bug #1207 Fix 1: close() must write migration_complete.marker after sharding "
            "so that get_overlapping_shards() knows migration is complete"
        )
        assert not (base_dir / "hnsw_index.bin").exists(), (
            "Bug #1207 Fix 1: close() must delete hnsw_index.bin from base dir "
            "to prevent stale-monolith mixing in query fan-out"
        )
        assert not (base_dir / "id_index.bin").exists(), (
            "Bug #1207 Fix 1: close() must delete id_index.bin from base dir"
        )

    def test_tc5_close_is_anti_orphan_cleanup_called_on_shard_path(self, tmp_path):
        """TC-5 anti-orphan: cleanup must be invoked; removing the invocation fails this test.

        Uses patch to confirm that _cleanup_monolithic_collection (or an equivalent
        cleanup helper) is actually called on the real shard-completion path.
        """
        indexer, index_dir = self._make_indexer(tmp_path)

        base_name = "code-indexer-temporal-voyage_code_3"
        base_dir = index_dir / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "hnsw_index.bin").write_bytes(b"data")

        indexer._processed_shards = [f"{base_name}-2024Q1"]
        indexer._indexing_complete = True

        # Patch the shared cleanup function to verify it is called
        with patch(
            "code_indexer.services.temporal.temporal_indexer._cleanup_monolithic_collection"
        ) as mock_cleanup:
            indexer.close()
            mock_cleanup.assert_called_once_with(base_dir)

    def test_tc6_close_does_not_write_marker_on_non_sharded_path(self, tmp_path):
        """TC-6: When no shards were processed, close() must NOT write the marker.

        The non-sharded (legacy) path builds HNSW in close(); it must not trigger
        the post-shard cleanup.
        """
        indexer, index_dir = self._make_indexer(tmp_path)

        base_name = "code-indexer-temporal-voyage_code_3"
        base_dir = index_dir / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "hnsw_index.bin").write_bytes(b"live_vectors")

        # No shards processed (non-sharded path)
        indexer._processed_shards = []

        # end_indexing must not raise for this test
        indexer.vector_store.end_indexing.return_value = None

        indexer.close()

        marker = base_dir / "migration_complete.marker"
        assert not marker.exists(), (
            "close() must NOT write migration_complete.marker on the non-sharded path"
        )
        # hnsw must still be there (not deleted by accident)
        assert (base_dir / "hnsw_index.bin").exists(), (
            "close() must NOT delete hnsw_index.bin on the non-sharded path"
        )

    def test_tc8_partial_failure_does_not_delete_monolith(self, tmp_path):
        """TC-8: BLOCKER 1 — partial failure mid-shard loop must NOT delete the monolith.

        Scenario: index_commits() raises after one shard was appended to _processed_shards
        (simulating a crash or API error partway through a multi-shard run).  The `finally`
        block in cli.py calls close() on the extra-provider indexer unconditionally.
        Without the _indexing_complete guard, close() sees non-empty _processed_shards and
        calls _cleanup_monolithic_collection, DELETING the monolith that is the only good
        copy of the vectors (data loss).

        The fix: gate cleanup on BOTH _processed_shards non-empty AND _indexing_complete=True.
        _indexing_complete is set only at the very end of index_commits() — after the shard
        loop completes fully without exception.

        This test FAILS before the _indexing_complete guard is added to close().
        """
        indexer, index_dir = self._make_indexer(tmp_path)

        base_name = "code-indexer-temporal-voyage_code_3"
        base_dir = index_dir / base_name
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "hnsw_index.bin").write_bytes(b"precious_vectors_only_copy")
        (base_dir / "id_index.bin").write_bytes(b"precious_id_data")

        # Simulate: one shard was appended before the crash (partial success)
        indexer._processed_shards = [f"{base_name}-2024Q1"]
        # _indexing_complete is NOT set (default False) — simulates mid-loop crash

        indexer.close()

        # Monolith must survive — it's the only good copy
        assert (base_dir / "hnsw_index.bin").exists(), (
            "BLOCKER 1: close() must NOT delete hnsw_index.bin when _indexing_complete "
            "is False (partial failure — monolith is the only good copy)"
        )
        assert (base_dir / "id_index.bin").exists(), (
            "BLOCKER 1: close() must NOT delete id_index.bin on partial failure"
        )
        marker = base_dir / "migration_complete.marker"
        assert not marker.exists(), (
            "BLOCKER 1: close() must NOT write migration_complete.marker on partial failure"
        )
