"""
Story #1359 (Epic #1333, S2): wire HNSW orphan detect+repair into the shared
HNSWIndexManager build/finalize path so every NEWLY built or updated index
(regular, temporal, multimodal) leaves finalize with zero orphans.

AC1: post-finalize detect+repair runs on the shared path; one shared code
     path serves regular-shaped AND temporal-shaped builds.
AC2: automatic repair on detection; a failed repair fails LOUD (never a
     silent partial index).
AC3: both regimes covered end-to-end through the REAL production build path
     (rebuild_from_vectors -> build_hnsw_index_to_temp, build_index, and the
     incremental save_incremental_update finalize), not just repair_orphans()
     called in isolation. Technical requirement: at least one test consumes a
     pre-broken, saved-then-loaded .bin artifact built with S1's own AC5
     shape-matrix recipe (tests/utils/hnsw_orphan_corpus.py), not only a
     freshly-built in-memory index this test constructs.

Real project hnswlib fork throughout (verified importable with
check_integrity/repair_orphans in conftest-free module import below). Zero
mocks of the C++ layer or the index build/finalize path. The ONE exception is
TestDetectAndRepairGuardLogic, which uses a plain fake object (not a Mock, not
hnswlib) to exercise the Python-level "raise loud if repair does not converge"
guard -- a condition that cannot be genuinely reproduced against the real,
proven-deterministic repair_orphans() C++ method, since it always drives
orphan_count to 0 for both measured regimes (S1 AC1/AC2). That guard exists
purely for defense-in-depth against a hypothetical future regression, and unit
testing its own control flow with a controlled fake is legitimate per the
project's mocking hierarchy (a test double we control, not a mock of the
subject under test).
"""

import json
from pathlib import Path

import hnswlib
import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import (
    HNSWIndexManager,
    HNSWIntegrityRepairError,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
SINGLE_THREADED = 1

# AC1 fixture sizing: near-tie regime orphans "even single-threaded" per the
# spike; 1000-element temporal-shaped and regular-shaped pockets are the
# same recipe used by S1's own AC1 test module.
TEMPORAL_SIZE = 1000
TEMPORAL_NOISE_SCALE = 1e-6
TEMPORAL_POCKET_FRACTION = 1.0

REGULAR_SIZE = 1000
REGULAR_NOISE_SCALE = 1e-6
REGULAR_POCKET_FRACTION = 0.4

CORPUS_SEED = 42

# AC3 technical requirement: EXACT match to S1's AC5 round-trip fixture cell
# `TestNearTieTemporalShapedRoundTrip.test_size_270` (tests/unit/hnsw_orphan_repair/
# test_repair_orphans_round_trip_1358.py) -- same size/noise/pocket/seed, so
# this test consumes the identical on-disk shape-matrix recipe, not an
# ad hoc corpus of this test's own invention.
AC5_FIXTURE_SIZE = 270
AC5_FIXTURE_NOISE_SCALE = 0.01
AC5_FIXTURE_POCKET_FRACTION = 1.0
AC5_FIXTURE_SEED = 42


def _write_vector_files(collection_path: Path, vectors: np.ndarray) -> None:
    """Write production-shaped vector_*.json files for rebuild_from_vectors()."""
    for i, vec in enumerate(vectors):
        vector_file = collection_path / f"vector_{i}.json"
        with open(vector_file, "w") as f:
            json.dump({"id": f"vec_{i}", "vector": vec.tolist()}, f)


def _write_collection_meta(collection_path: Path, vector_dim: int) -> None:
    meta_file = collection_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump({"vector_dim": vector_dim}, f)


def _orphan_count(check_integrity_result: dict) -> int:
    return sum(1 for e in check_integrity_result["errors"] if "orphan" in e)


class _FakeOrphanedIndex:
    """Test double: simulates check_integrity()/repair_orphans() shapes
    identical to the real hnswlib fork, but with a repair that FAILS to
    converge -- a condition the real, proven-deterministic repair never
    produces. Exercises only the Python-level loud-failure guard.
    """

    def __init__(self, orphans_before: int, orphans_after: int):
        self._orphans_before = orphans_before
        self._orphans_after = orphans_after
        self._check_calls = 0
        self.repair_called = False

    def check_integrity(self):
        self._check_calls += 1
        n = self._orphans_before if self._check_calls == 1 else self._orphans_after
        errors = [f"orphan {i}: zero inbound edges" for i in range(n)]
        return {
            "valid": n == 0,
            "element_count": 10,
            "connections_checked": 10,
            "min_inbound": 0 if n else 1,
            "max_inbound": 5,
            "errors": errors,
        }

    def repair_orphans(self):
        self.repair_called = True
        return {
            "orphans_before": self._orphans_before,
            "orphans_after": self._orphans_after,
            "repaired_count": self._orphans_before - self._orphans_after,
            "passes_used": 1,
            "forced_evictions": 0,
            "valid": self._orphans_after == 0,
        }


class TestDetectAndRepairGuardLogic:
    """Python-level guard logic: detect -> repair -> re-verify -> loud failure."""

    def test_no_orphans_skips_repair_entirely(self):
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        fake = _FakeOrphanedIndex(orphans_before=0, orphans_after=0)

        result = manager._detect_and_repair_orphans(fake, context="unit-test")

        assert result is None
        assert fake.repair_called is False

    def test_orphans_detected_triggers_repair_and_succeeds(self):
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=0)

        result = manager._detect_and_repair_orphans(fake, context="unit-test")

        assert result is None
        assert fake.repair_called is True

    def test_repair_failing_to_converge_raises_loud(self):
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=2)

        with pytest.raises(HNSWIntegrityRepairError, match="2 orphan"):
            manager._detect_and_repair_orphans(fake, context="unit-test")

        assert fake.repair_called is True


class TestDetectAndRepairLogLevels:
    """Code-review finding: near-tie detect+repair is now the EXPECTED happy
    path (every temporal near-tie rebuild triggers it), so the "detected,
    running repair_orphans()" line must NOT log at WARNING -- that would
    trip the Story #1122 post-E2E automated log-audit gate on ordinary,
    successful rebuilds. Only genuine non-convergence (a real
    repair-pipeline regression) should log at ERROR.
    """

    def test_repair_detected_and_succeeded_logs_at_info_not_warning(self, caplog):
        import logging

        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=0)

        with caplog.at_level(
            logging.INFO, logger="code_indexer.storage.hnsw_index_manager"
        ):
            manager._detect_and_repair_orphans(fake, context="unit-test")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records == [], (
            f"expected no WARNING-level log records on the happy repair path, "
            f"got: {[r.getMessage() for r in warning_records]}"
        )
        info_messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO
        ]
        assert any("detected" in m and "repair_orphans" in m for m in info_messages)

    def test_repair_non_convergence_still_logs_at_error(self, caplog):
        import logging

        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        fake = _FakeOrphanedIndex(orphans_before=3, orphans_after=2)

        with caplog.at_level(
            logging.INFO, logger="code_indexer.storage.hnsw_index_manager"
        ):
            with pytest.raises(HNSWIntegrityRepairError):
                manager._detect_and_repair_orphans(fake, context="unit-test")

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "remain after repair" in error_records[0].getMessage()


class TestRebuildFromVectorsFinalizeRepairsOrphans:
    """AC1/AC3: rebuild_from_vectors -> build_hnsw_index_to_temp -> finalize,
    driven through the REAL production path (vector_*.json files on disk),
    for both temporal-shaped and regular-shaped near-tie corpora -- proving
    ONE shared code path handles both build shapes.
    """

    def test_temporal_shaped_corpus_finalizes_with_zero_orphans(self, tmp_path: Path):
        vectors = near_tie_corpus(
            size=TEMPORAL_SIZE,
            dim=CORPUS_DIM,
            noise_scale=TEMPORAL_NOISE_SCALE,
            pocket_fraction=TEMPORAL_POCKET_FRACTION,
            seed=CORPUS_SEED,
        )
        # Sanity: confirm this recipe genuinely produces orphans pre-repair
        # at production parameters (fixture must start broken).
        sanity_index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        assert _orphan_count(sanity_index.check_integrity()) > 0

        _write_vector_files(tmp_path, vectors)
        _write_collection_meta(tmp_path, CORPUS_DIM)

        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        vector_count = manager.rebuild_from_vectors(tmp_path)
        assert vector_count == TEMPORAL_SIZE

        loaded = manager.load_index(tmp_path, max_elements=TEMPORAL_SIZE)
        assert loaded is not None
        final = loaded.check_integrity()
        assert final["valid"] is True
        assert _orphan_count(final) == 0

    def test_regular_shaped_corpus_finalizes_with_zero_orphans(self, tmp_path: Path):
        vectors = near_tie_corpus(
            size=REGULAR_SIZE,
            dim=CORPUS_DIM,
            noise_scale=REGULAR_NOISE_SCALE,
            pocket_fraction=REGULAR_POCKET_FRACTION,
            seed=CORPUS_SEED,
        )
        sanity_index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        assert _orphan_count(sanity_index.check_integrity()) > 0

        _write_vector_files(tmp_path, vectors)
        _write_collection_meta(tmp_path, CORPUS_DIM)

        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        vector_count = manager.rebuild_from_vectors(tmp_path)
        assert vector_count == REGULAR_SIZE

        loaded = manager.load_index(tmp_path, max_elements=REGULAR_SIZE)
        assert loaded is not None
        final = loaded.check_integrity()
        assert final["valid"] is True
        assert _orphan_count(final) == 0


class TestBuildIndexFinalizeRepairsOrphans:
    """AC1/AC3: build_index() (the other add_items site, line ~191) also
    finalizes with zero orphans on the real production path.
    """

    def test_build_index_finalizes_with_zero_orphans(self, tmp_path: Path):
        vectors = near_tie_corpus(
            size=TEMPORAL_SIZE,
            dim=CORPUS_DIM,
            noise_scale=TEMPORAL_NOISE_SCALE,
            pocket_fraction=TEMPORAL_POCKET_FRACTION,
            seed=CORPUS_SEED,
        )
        sanity_index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        assert _orphan_count(sanity_index.check_integrity()) > 0

        ids = [f"vec_{i}" for i in range(TEMPORAL_SIZE)]
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        manager.build_index(tmp_path, vectors, ids)

        loaded = manager.load_index(tmp_path, max_elements=TEMPORAL_SIZE)
        assert loaded is not None
        final = loaded.check_integrity()
        assert final["valid"] is True
        assert _orphan_count(final) == 0


class TestIncrementalFinalizeConsumesAC5OnDiskFixture:
    """AC3 Technical Requirement: this test's fixture is a genuinely-persisted
    broken artifact -- built with S1's own AC5 shape-matrix recipe (same
    size/noise/pocket/seed as the round-trip test's `test_size_270` cell),
    saved to a real .bin, then loaded via the REAL production
    `load_for_incremental_update()` entry point and finalized via the REAL
    `save_incremental_update()` -- proving this story's wiring repairs a
    genuinely-persisted broken artifact, not only an index this test builds
    fresh in memory.
    """

    def test_persisted_broken_fixture_repaired_at_incremental_finalize(
        self, tmp_path: Path
    ):
        vectors = near_tie_corpus(
            size=AC5_FIXTURE_SIZE,
            dim=CORPUS_DIM,
            noise_scale=AC5_FIXTURE_NOISE_SCALE,
            pocket_fraction=AC5_FIXTURE_POCKET_FRACTION,
            seed=AC5_FIXTURE_SEED,
        )
        broken_index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
        orphans_before = _orphan_count(broken_index.check_integrity())
        assert orphans_before > 0, "AC5 fixture recipe must start broken"

        # Persist the pre-broken index as the production artifact
        # (hnsw_index.bin) plus a matching collection_meta.json id_mapping --
        # this IS the "pre-broken, saved-then-loaded .bin artifact".
        index_file = tmp_path / HNSWIndexManager.INDEX_FILENAME
        broken_index.save_index(str(index_file))

        id_mapping = {str(i): f"vec_{i}" for i in range(AC5_FIXTURE_SIZE)}
        meta_file = tmp_path / "collection_meta.json"
        with open(meta_file, "w") as f:
            json.dump(
                {
                    "vector_dim": CORPUS_DIM,
                    "hnsw_index": {
                        "vector_count": AC5_FIXTURE_SIZE,
                        "vector_dim": CORPUS_DIM,
                        "M": 16,
                        "ef_construction": 200,
                        "id_mapping": id_mapping,
                    },
                },
                f,
            )

        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)

        # Real production incremental-load entry point: loads the persisted
        # broken .bin into a FRESH hnswlib.Index object.
        index, id_to_label, label_to_id, next_label = (
            manager.load_for_incremental_update(tmp_path)
        )
        assert index is not None
        assert next_label == AC5_FIXTURE_SIZE

        # Confirm the corruption survives the fresh load (S1 AC5 invariant).
        assert _orphan_count(index.check_integrity()) == orphans_before

        # Real production incremental finalize -- no new vectors added, this
        # is purely exercising the finalize/save path's detect+repair hook.
        manager.save_incremental_update(
            index, tmp_path, id_to_label, label_to_id, vector_count=AC5_FIXTURE_SIZE
        )

        reloaded = manager.load_index(tmp_path, max_elements=AC5_FIXTURE_SIZE)
        assert reloaded is not None
        final = reloaded.check_integrity()
        assert final["valid"] is True
        assert _orphan_count(final) == 0


def test_hnswlib_fork_has_repair_bindings():
    """Environment sanity: the rebuilt fork must expose both S1 bindings."""
    idx = hnswlib.Index(space="cosine", dim=4)
    assert hasattr(idx, "check_integrity")
    assert hasattr(idx, "repair_orphans")
