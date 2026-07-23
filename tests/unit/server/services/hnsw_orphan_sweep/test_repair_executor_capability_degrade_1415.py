"""Bug #1415: the HNSW fleet orphan sweep's per-item repair executor
(repair_executor.py) must DEGRADE (not crash the sweep tick) when the
installed hnswlib lacks the custom LightspeedDMS fork's check_integrity()/
repair_orphans() methods.

Before this fix, `process_candidate()`'s lock-free `index.check_integrity()`
call (and the locked re-check/repair calls inside `_repair_under_lock`) is
wrapped ONLY in `except _TRANSIENT_LOAD_ERRORS` -- (OSError, RuntimeError,
ValueError, KeyError) -- which does NOT include AttributeError. A missing
capability therefore raises uncaught, exactly the third flagged call site
from Bug #1415's issue report (repair_executor.py lines 156/185/196/251).

Fix: `process_candidate()` checks capability (reusing the existing Bug #1392
server-side probe, `check_hnswlib_capability()` from
`hnswlib_capability_check.py`, per the "reuse the existing gate" guidance)
BEFORE the lock-free check_integrity() call. Missing capability logs ONE
WARNING and returns the new `SweepOutcome.CAPABILITY_UNAVAILABLE` -- never
raises, and the sweep's `_process_one` fail-soft wrapper (which would
otherwise count an uncaught exception as ERROR) is never even reached.

Real hnswlib fork throughout -- no mocking of the C++ layer. Missing
capability is simulated by temporarily delattr-ing check_integrity/
repair_orphans from the REAL hnswlib.Index class (restored after each test).
"""

import json
import logging
from pathlib import Path

import hnswlib
import numpy as np
import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
    process_candidate,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
SINGLE_THREADED = 1

# Exact match to Story #1360's own AC5 round-trip fixture cell -- same
# size/noise/pocket/seed, so this reuses the identical on-disk shape-matrix
# recipe used elsewhere in the epic rather than inventing a new one.
AC5_FIXTURE_SIZE = 270
AC5_FIXTURE_NOISE_SCALE = 0.01
AC5_FIXTURE_POCKET_FRACTION = 1.0
AC5_FIXTURE_SEED = 42


@pytest.fixture
def missing_capability():
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


def _orphan_count(check_integrity_result: dict) -> int:
    return sum(1 for e in check_integrity_result["errors"] if "orphan" in e)


def _make_candidate(repo_root: Path, relpath: str) -> SweepCandidate:
    return SweepCandidate(
        sort_key=f"golden:test:{relpath}",
        repo_root=repo_root,
        index_relpath=Path(relpath),
        kind="golden",
        alias="test",
    )


def _build_clean_index_no_manager(collection_path: Path) -> None:
    """Build a real, valid on-disk HNSW index directly via hnswlib (NOT
    HNSWIndexManager.build_index, so this helper is safe to call even while
    the missing_capability fixture is active)."""
    collection_path.mkdir(parents=True, exist_ok=True)
    vectors = near_tie_corpus(
        size=50, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=0.2, seed=7
    )
    index = hnswlib.Index(space="cosine", dim=CORPUS_DIM)
    index.init_index(
        max_elements=50, M=16, ef_construction=200, allow_replace_deleted=True
    )
    index.add_items(vectors, np.arange(50))
    index_file = collection_path / HNSWIndexManager.INDEX_FILENAME
    index.save_index(str(index_file))
    with open(collection_path / "collection_meta.json", "w") as f:
        json.dump({"vector_dim": CORPUS_DIM}, f)


def _plant_prebroken_fixture(collection_path: Path) -> int:
    """Plant a genuinely pre-broken, saved-then-loaded .bin fixture using
    Story #1360's own AC5 shape-matrix recipe -- NOT built via
    HNSWIndexManager (which would self-heal). Returns orphan count before
    repair (must be > 0)."""
    collection_path.mkdir(parents=True, exist_ok=True)
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

    index_file = collection_path / HNSWIndexManager.INDEX_FILENAME
    broken_index.save_index(str(index_file))

    id_mapping = {str(i): f"vec_{i}" for i in range(AC5_FIXTURE_SIZE)}
    meta_file = collection_path / "collection_meta.json"
    with open(meta_file, "w") as f:
        json.dump(
            {
                "vector_dim": CORPUS_DIM,
                "hnsw_index": {
                    "vector_count": AC5_FIXTURE_SIZE,
                    "vector_dim": CORPUS_DIM,
                    "space": "cosine",
                    "M": 16,
                    "ef_construction": 200,
                    "id_mapping": id_mapping,
                },
            },
            f,
        )
    return orphans_before


class TestProcessCandidateDegradesGracefully:
    def test_missing_capability_returns_capability_unavailable_not_raise(
        self, missing_capability, tmp_path, caplog
    ):
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _build_clean_index_no_manager(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )

        with caplog.at_level(logging.WARNING):
            outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.CAPABILITY_UNAVAILABLE

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "check_integrity" in r.getMessage()
        ]
        assert len(warnings) == 1


class TestRegressionCapabilityPresentUnchanged:
    """When the real fork IS present, Story #1360's CLEAN and REPAIRED
    outcomes are completely unchanged."""

    def test_clean_index_with_real_fork_still_returns_clean(self, tmp_path):
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _build_clean_index_no_manager(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.CLEAN

    def test_prebroken_fixture_with_real_fork_still_gets_repaired(self, tmp_path):
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.REPAIRED
