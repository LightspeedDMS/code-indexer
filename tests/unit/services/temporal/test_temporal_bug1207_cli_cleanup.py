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


# ---------------------------------------------------------------------------
# Story #1290: TemporalIndexer.close() no longer writes migration_complete.marker
# or calls _cleanup_monolithic_collection -- the hard cut always builds shards
# fresh (no monolith is ever created), and blank-out (not migration) is now the
# mechanism that handles any pre-existing legacy monolith. The former
# TestTemporalIndexerCloseWritesMarker (TC-5/6/8) tested behavior that has been
# deliberately removed; see temporal_blank_out.py / AC19-20 for the replacement.
# ---------------------------------------------------------------------------
