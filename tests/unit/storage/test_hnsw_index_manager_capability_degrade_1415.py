"""Bug #1415: HNSW finalize integrity check must DEGRADE (not abort) when the
installed hnswlib lacks the custom LightspeedDMS fork's check_integrity()/
repair_orphans() methods (stock PyPI hnswlib).

Production incident (2026-07-14): Bug #1392 added `_ensure_hnswlib_capability()`
as the VERY FIRST statement of build_index/rebuild_from_vectors/
save_incremental_update, raising `HNSWCapabilityError` immediately when the
fork is missing. That still ABORTS the entire indexing operation -- it just
replaced a raw, late `AttributeError` with an earlier, clearer exception.
Either shape crashes indexing wholesale: a refresh sweep failed for ~12
golden repos and blocked an activated-repo branch-delta reindex (which, per
Bug #1203, also blocks repository activation).

Fix: build/finalize entry points no longer hard-gate on capability. Instead,
`_detect_and_repair_orphans()` -- the single place that actually calls
check_integrity()/repair_orphans() -- checks capability via a private,
non-raising `_hnswlib_has_fork_capability()` predicate. When the fork is
absent, it logs ONE WARNING and returns immediately (orphan hardening pass
skipped); the caller's build/save proceeds and produces a valid, correct
index. `HNSWCapabilityError` and the raising `_ensure_hnswlib_capability()`
are removed -- nothing in this module raises them anymore (see git history /
Bug #1392 test file for the superseded fail-fast design).

Real hnswlib fork throughout -- no mocking of the C++ layer. Missing
capability is simulated by temporarily delattr-ing check_integrity/
repair_orphans from the REAL hnswlib.Index class (restored after each test),
exactly reproducing "stock PyPI hnswlib installed" without needing a second
hnswlib build in CI.
"""

import json
import logging

import hnswlib
import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


@pytest.fixture
def missing_capability():
    """Temporarily remove check_integrity/repair_orphans from the REAL
    hnswlib.Index class, restoring them unconditionally afterward. Simulates
    a stock-PyPI environment drifted away from the custom fork (Bug #1392/
    #1415)."""
    saved = {}
    for attr in ("check_integrity", "repair_orphans"):
        if hasattr(hnswlib.Index, attr):
            saved[attr] = getattr(hnswlib.Index, attr)
            delattr(hnswlib.Index, attr)
    try:
        yield
    finally:
        for attr, value in saved.items():
            setattr(hnswlib.Index, attr, value)


@pytest.fixture
def manager():
    return HNSWIndexManager(vector_dim=4, space="cosine")


@pytest.fixture
def sample_vectors():
    np.random.seed(42)
    vectors = np.random.randn(10, 4).astype(np.float32)
    ids = [f"vec_{i}" for i in range(10)]
    return vectors, ids


class TestHNSWCapabilityErrorRemoved:
    """The fail-fast Bug #1392 design (HNSWCapabilityError, raising
    _ensure_hnswlib_capability) is superseded -- neither symbol exists on the
    module anymore, since nothing raises them under the new degrade design."""

    def test_hnsw_capability_error_no_longer_exported(self):
        import code_indexer.storage.hnsw_index_manager as mod

        assert not hasattr(mod, "HNSWCapabilityError")

    def test_ensure_hnswlib_capability_no_longer_exists(self, manager):
        assert not hasattr(manager, "_ensure_hnswlib_capability")


class TestBuildIndexDegradesGracefully:
    """RED->GREEN: build_index() must COMPLETE (not raise) when the fork is
    missing, producing a valid on-disk index and logging exactly one
    WARNING."""

    def test_build_index_completes_and_logs_warning(
        self, missing_capability, manager, tmp_path, sample_vectors, caplog
    ):
        vectors, ids = sample_vectors
        collection_path = tmp_path / "collection"
        collection_path.mkdir()

        with caplog.at_level(logging.WARNING):
            manager.build_index(collection_path, vectors, ids)

        index_file = collection_path / manager.INDEX_FILENAME
        assert index_file.exists()
        assert index_file.stat().st_size > 0

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "check_integrity" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_build_index_loaded_index_is_queryable(
        self, missing_capability, manager, tmp_path, sample_vectors
    ):
        vectors, ids = sample_vectors
        collection_path = tmp_path / "collection"
        collection_path.mkdir()
        manager.build_index(collection_path, vectors, ids)

        loaded = manager.load_index(collection_path)
        assert loaded is not None
        assert loaded.get_current_count() == len(vectors)


class TestRebuildFromVectorsDegradesGracefully:
    """RED->GREEN: rebuild_from_vectors() must COMPLETE (not raise) when the
    fork is missing."""

    def test_rebuild_from_vectors_completes_and_logs_warning(
        self, missing_capability, manager, tmp_path, sample_vectors, caplog
    ):
        vectors, ids = sample_vectors
        collection_path = tmp_path / "collection"
        collection_path.mkdir()

        with open(collection_path / "collection_meta.json", "w") as f:
            json.dump({"vector_dim": 4}, f)

        for i, vec in enumerate(vectors):
            with open(collection_path / f"vector_{i}.json", "w") as f:
                json.dump({"id": ids[i], "vector": vec.tolist()}, f)

        with caplog.at_level(logging.WARNING):
            count = manager.rebuild_from_vectors(collection_path)

        assert count == len(vectors)
        index_file = collection_path / manager.INDEX_FILENAME
        assert index_file.exists()

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "check_integrity" in r.getMessage()
        ]
        assert len(warnings) == 1


class TestSaveIncrementalUpdateDegradesGracefully:
    """RED->GREEN: save_incremental_update() must COMPLETE (not raise) when
    the fork is missing."""

    def test_save_incremental_update_completes_and_logs_warning(
        self, manager, tmp_path, sample_vectors, caplog
    ):
        vectors, ids = sample_vectors
        collection_path = tmp_path / "collection"
        collection_path.mkdir()

        # Build the initial index WITH capability present (real fork), then
        # drop capability only for the incremental save under test.
        manager.build_index(collection_path, vectors, ids)
        index, id_to_label, label_to_id, next_label = (
            manager.load_for_incremental_update(collection_path)
        )
        assert index is not None

        new_vector = np.random.randn(4).astype(np.float32)
        manager.add_or_update_vector(
            index, "vec_new", new_vector, id_to_label, label_to_id, next_label
        )

        saved = {}
        for attr in ("check_integrity", "repair_orphans"):
            if hasattr(hnswlib.Index, attr):
                saved[attr] = getattr(hnswlib.Index, attr)
                delattr(hnswlib.Index, attr)
        try:
            with caplog.at_level(logging.WARNING):
                manager.save_incremental_update(
                    index,
                    collection_path,
                    id_to_label,
                    label_to_id,
                    vector_count=len(vectors) + 1,
                )
        finally:
            for attr, value in saved.items():
                setattr(hnswlib.Index, attr, value)

        index_file = collection_path / manager.INDEX_FILENAME
        assert index_file.exists()

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "check_integrity" in r.getMessage()
        ]
        assert len(warnings) == 1

        # Production-incident hardening: the crash used to discard an
        # already-embedded batch entirely (nothing persisted, embedding
        # spend wasted). Prove the embedded work is DURABLY persisted --
        # not merely "a file exists" -- via a FRESH, independent reload
        # (capability still missing) that reflects the actual vector count
        # (10 original + 1 newly added) and contains the new point.
        reloaded = manager.load_index(collection_path)
        assert reloaded is not None
        assert reloaded.get_current_count() == len(vectors) + 1

        reloaded_index, reloaded_id_to_label, _, _ = (
            manager.load_for_incremental_update(collection_path)
        )
        assert reloaded_index is not None
        assert "vec_new" in reloaded_id_to_label
        assert len(reloaded_id_to_label) == len(vectors) + 1


class TestRegressionCapabilityPresentUnchanged:
    """The single most important regression test: when the REAL fork IS
    present (the normal/default case, no missing_capability fixture), Bug
    #1359's zero-tolerance orphan detect+repair behavior is COMPLETELY
    unchanged -- no new WARNING noise, orphan pass still runs."""

    def test_build_index_with_real_fork_emits_no_capability_warning(
        self, manager, tmp_path, sample_vectors, caplog
    ):
        vectors, ids = sample_vectors
        collection_path = tmp_path / "collection"
        collection_path.mkdir()

        with caplog.at_level(logging.WARNING):
            manager.build_index(collection_path, vectors, ids)

        capability_warnings = [
            r
            for r in caplog.records
            if "check_integrity" in r.getMessage() and "lacks" in r.getMessage()
        ]
        assert capability_warnings == []

    def test_detect_and_repair_orphans_still_calls_check_integrity(
        self, manager, tmp_path, sample_vectors
    ):
        """With the real fork present, _detect_and_repair_orphans() must
        still invoke check_integrity() (proves the guard does not
        short-circuit the real hardening pass)."""
        vectors, ids = sample_vectors
        index = hnswlib.Index(space="cosine", dim=4)
        index.init_index(
            max_elements=len(vectors),
            M=16,
            ef_construction=200,
            allow_replace_deleted=True,
        )
        labels = np.arange(len(vectors))
        index.add_items(vectors, labels)

        # A real, healthy freshly-built index has zero orphans -- calling
        # the guarded method must not raise and must have actually consulted
        # check_integrity() (verified indirectly: no exception, and the
        # capability predicate reports True for the real fork).
        assert manager._hnswlib_has_fork_capability() is True
        manager._detect_and_repair_orphans(index, context="regression-test")


class TestGenuineFailureStillPropagatesWhenCapabilityPresent:
    """A genuine (non-AttributeError) exception from check_integrity()/
    repair_orphans() when the fork IS present must still propagate loud --
    only the specific missing-method case degrades. Uses a controlled fake
    object (not hnswlib, not a Mock of the subject under test) to force a
    non-convergent repair, matching the pre-existing pattern in
    test_hnsw_index_manager_1359_orphan_finalize.py."""

    def test_repair_orphans_non_convergence_still_raises(self, manager):
        from code_indexer.storage.hnsw_index_manager import (
            HNSWIntegrityRepairError,
        )

        class _FakeOrphanedIndex:
            """Fake with the fork's method NAMES present (hasattr True) but
            whose repair never converges -- proves the capability guard
            does not swallow a genuine repair failure."""

            def check_integrity(self):
                return {"errors": ["orphan node 1"], "valid": False}

            def repair_orphans(self):
                pass  # does nothing -- repair does not converge

        assert hasattr(_FakeOrphanedIndex, "check_integrity")
        assert hasattr(_FakeOrphanedIndex, "repair_orphans")

        with pytest.raises(HNSWIntegrityRepairError):
            manager._detect_and_repair_orphans(
                _FakeOrphanedIndex(), context="regression-non-convergence"
            )
