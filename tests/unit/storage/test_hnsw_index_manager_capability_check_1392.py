"""Tests for Bug #1392: fail-loud hnswlib fork-capability check on the CLI
(storage) side.

Production bug: the CLI's separate system-wide Python environment can drift
to a stock PyPI hnswlib (missing check_integrity()/repair_orphans()) while
the server's own pipx venv stays on the custom fork. Every finalize-time
`_detect_and_repair_orphans()` call then fails with a bare AttributeError
deep inside `build_index`/`rebuild_from_vectors`/`save_incremental_update`,
after heavy indexing work has already run.

Fix: a new `_ensure_hnswlib_capability()` check runs as the VERY FIRST
statement of those three build/finalize methods, raising a new
`HNSWCapabilityError` immediately -- before any indexing work -- when the
installed `hnswlib.Index` lacks `check_integrity`/`repair_orphans`.
"""

import sys

import hnswlib
import pytest

from code_indexer.storage.hnsw_index_manager import (
    EXPECTED_HNSWLIB_FORK_COMMIT,
    HNSWCapabilityError,
    HNSWIndexManager,
)


@pytest.fixture
def missing_capability():
    """Temporarily remove check_integrity/repair_orphans from the REAL
    hnswlib.Index class, restoring them unconditionally afterward. Simulates
    a stock-PyPI environment drifted away from the custom fork (Bug #1392)."""
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


class TestEnsureHnswlibCapability:
    """RED 2: _ensure_hnswlib_capability() raises HNSWCapabilityError when
    the fork methods are missing."""

    def test_raises_when_check_integrity_and_repair_orphans_missing(
        self, missing_capability
    ):
        manager = HNSWIndexManager(vector_dim=4, space="cosine")
        with pytest.raises(HNSWCapabilityError):
            manager._ensure_hnswlib_capability()

    def test_message_names_expected_commit_interpreter_and_docs(
        self, missing_capability
    ):
        manager = HNSWIndexManager(vector_dim=4, space="cosine")
        with pytest.raises(HNSWCapabilityError) as exc_info:
            manager._ensure_hnswlib_capability()
        message = str(exc_info.value)
        assert EXPECTED_HNSWLIB_FORK_COMMIT in message
        assert sys.executable in message
        assert "docs/hnswlib-custom-build.md" in message


class TestBuildIndexCapabilityGate:
    """RED 4: build_index() gates on capability as its VERY FIRST statement --
    before the pre-existing vector-dimension ValueError validation runs."""

    def test_build_index_raises_before_validation(self, missing_capability, tmp_path):
        import numpy as np

        manager = HNSWIndexManager(vector_dim=4, space="cosine")
        # Mismatched dim (8 != 4) would normally raise ValueError -- proving
        # the capability check runs BEFORE that validation.
        vectors = np.zeros((2, 8), dtype=np.float32)
        with pytest.raises(HNSWCapabilityError):
            manager.build_index(tmp_path, vectors, ["a", "b"])


class TestRebuildFromVectorsCapabilityGate:
    """RED 5: rebuild_from_vectors() gates on capability as its VERY FIRST
    statement -- before the pre-existing missing-metadata FileNotFoundError."""

    def test_rebuild_from_vectors_raises_before_missing_metadata(
        self, missing_capability, tmp_path
    ):
        manager = HNSWIndexManager(vector_dim=4, space="cosine")
        # No collection_meta.json written -- would normally raise
        # FileNotFoundError, proving the capability check runs first.
        with pytest.raises(HNSWCapabilityError):
            manager.rebuild_from_vectors(tmp_path)


class TestSaveIncrementalUpdateCapabilityGate:
    """RED 6: save_incremental_update() gates on capability as its VERY
    FIRST statement -- before any access to the (here, deliberately invalid
    None) index argument."""

    def test_save_incremental_update_raises_before_touching_index(
        self, missing_capability, tmp_path
    ):
        manager = HNSWIndexManager(vector_dim=4, space="cosine")
        # index=None would normally raise AttributeError once the method
        # reaches _detect_and_repair_orphans() -- proving the capability
        # check runs first.
        with pytest.raises(HNSWCapabilityError):
            manager.save_incremental_update(None, tmp_path, {}, {}, 0)


class TestQueryPathUnaffected:
    """Regression guard (Query Is Everything invariant): query-only paths
    must NEVER be blocked by a missing hnswlib capability, even though
    build/finalize paths now are. __init__, index_exists, and is_stale must
    construct/run without raising HNSWCapabilityError."""

    def test_index_exists_and_is_stale_not_gated_by_capability(
        self, missing_capability, tmp_path
    ):
        manager = HNSWIndexManager(vector_dim=4, space="cosine")  # must not raise
        assert manager.index_exists(tmp_path) is False  # must not raise
        assert manager.is_stale(tmp_path) is True  # must not raise (no index yet)


class TestHNSWCapabilityErrorClass:
    """RED 1: HNSWCapabilityError exists as a distinct RuntimeError subclass."""

    def test_hnsw_capability_error_is_runtime_error_subclass(self):
        assert issubclass(HNSWCapabilityError, RuntimeError)

    def test_hnsw_capability_error_distinct_from_integrity_repair_error(self):
        from code_indexer.storage.hnsw_index_manager import HNSWIntegrityRepairError

        assert HNSWCapabilityError is not HNSWIntegrityRepairError
        assert not issubclass(HNSWCapabilityError, HNSWIntegrityRepairError)

    def test_expected_hnswlib_fork_commit_constant_defined(self):
        assert (
            EXPECTED_HNSWLIB_FORK_COMMIT == "878cfbe585395a8bdd95f593d071f778d2fac457"
        )
