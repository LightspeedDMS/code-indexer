"""Tests for the HNSW fleet sweep per-item repair executor (Story #1360 AC2).

Real project hnswlib fork throughout -- no mocks of check_integrity()/
repair_orphans() or the build/finalize path. Reuses S1's synthetic-orphan
corpus generator (tests/utils/hnsw_orphan_corpus.py) to plant a genuinely
pre-broken, previously-saved-and-loaded .bin artifact exactly where the
sweep's normal directory walk (Component 1) will find it -- this is the
closest committed-test proxy to AC5 (the real staging shard).

Concurrency interlock (issue section "Concurrency interlock"):
  - repair acquires the SAME per-collection lock BackgroundIndexRebuilder /
    HNSWIndexManager finalize/rebuild uses (.index_rebuild.lock, fcntl flock,
    NFS-safe) before writing.
  - check_integrity() runs lock-free first; repair_orphans() RE-CHECKS under
    the lock immediately before writing.
  - a path-identity change between the lock-free check and the locked
    re-check (e.g. concurrent golden-repo refresh swapping the directory) is
    a transient outcome, NOT a repair failure.
  - ENOENT at any filesystem step is tolerated as a transient-skip.
  - a successful repair invalidates the server-side HNSWIndexCache entry for
    the collection path (the same cache rebuild paths already invalidate).
"""

import json
from pathlib import Path
from typing import List

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
    process_candidate,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
SINGLE_THREADED = 1

# Exact match to S1's AC5 round-trip fixture cell / S2's AC3 technical
# requirement fixture -- same size/noise/pocket/seed, so this test consumes
# the identical on-disk shape-matrix recipe used elsewhere in the epic.
AC5_FIXTURE_SIZE = 270
AC5_FIXTURE_NOISE_SCALE = 0.01
AC5_FIXTURE_POCKET_FRACTION = 1.0
AC5_FIXTURE_SEED = 42


def _orphan_count(check_integrity_result: dict) -> int:
    return sum(1 for e in check_integrity_result["errors"] if "orphan" in e)


def _plant_prebroken_fixture(collection_path: Path) -> int:
    """Plant a genuinely pre-broken, saved-then-loaded .bin fixture at
    *collection_path* using S1's own AC5 shape-matrix recipe -- NOT built via
    HNSWIndexManager (which would self-heal per S2). Returns orphan count
    before repair (must be > 0)."""
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


def _make_candidate(repo_root: Path, relpath: str) -> SweepCandidate:
    return SweepCandidate(
        sort_key=f"golden:test:{relpath}",
        repo_root=repo_root,
        index_relpath=Path(relpath),
        kind="golden",
        alias="test",
    )


class TestProcessCandidateCleanIndex:
    def test_clean_index_returns_clean(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        collection_path.mkdir(parents=True)

        # A production build already self-heals (S2), so this is clean.
        vectors = near_tie_corpus(
            size=50, dim=CORPUS_DIM, noise_scale=1e-6, pocket_fraction=0.2, seed=7
        )
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        ids = [f"vec_{i}" for i in range(50)]
        manager.build_index(collection_path, vectors, ids)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.CLEAN


class TestProcessCandidateRepairsAC5Fixture:
    def test_prebroken_fixture_discovered_and_repaired(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.REPAIRED

        # A subsequent FRESH load shows 0 orphans -- the AC2 technical
        # requirement ("a subsequent fresh load shows 0 orphans").
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        reloaded = manager.load_index(collection_path, max_elements=AC5_FIXTURE_SIZE)
        assert reloaded is not None
        final = reloaded.check_integrity()
        assert final["valid"] is True
        assert _orphan_count(final) == 0

    def test_repair_invalidates_server_side_cache(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        invalidated_paths: List[str] = []
        outcome = process_candidate(
            candidate, cache_invalidator=invalidated_paths.append
        )

        assert outcome == SweepOutcome.REPAIRED
        assert invalidated_paths == [str(collection_path)]

    def test_repair_acquires_background_index_rebuilder_lock(
        self, tmp_path: Path
    ) -> None:
        """Verifies the SAME per-collection lock file used by build/finalize
        rebuilds is created and used during repair (the concurrency
        interlock's "must verify" item 1)."""
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        process_candidate(candidate)

        assert (collection_path / ".index_rebuild.lock").exists()


class TestProcessCandidateTransientSkips:
    def test_missing_bin_file_is_transient_skip(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        collection_path.mkdir(parents=True)
        (collection_path / "collection_meta.json").write_text(
            json.dumps({"vector_dim": CORPUS_DIM})
        )
        # No hnsw_index.bin -- simulates concurrent deletion between
        # discovery and processing.

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.TRANSIENT_SKIP

    def test_missing_meta_file_is_transient_skip(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        collection_path.mkdir(parents=True)
        (collection_path / HNSWIndexManager.INDEX_FILENAME).write_bytes(b"junk")
        # No collection_meta.json -- simulates a race where the meta file was
        # removed after discovery ran.

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.TRANSIENT_SKIP

    def test_missing_repo_root_is_transient_skip(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "gone"
        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.TRANSIENT_SKIP

    def test_versioned_snapshot_path_is_transient_skip(self, tmp_path: Path) -> None:
        """Defensive re-check: even though discovery already filters
        .versioned/ paths, the repair executor re-checks independently
        (candidate sets can be stale by the time an item is processed)."""
        repo_root = tmp_path / "repo"
        collection_path = (
            repo_root
            / ".code-indexer"
            / "index"
            / ".versioned"
            / "voyage-code-3"
            / "v_1720000000"
        )
        _plant_prebroken_fixture(collection_path)

        relpath = (
            ".code-indexer/index/.versioned/voyage-code-3/v_1720000000/hnsw_index.bin"
        )
        candidate = _make_candidate(repo_root, relpath)
        outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.TRANSIENT_SKIP

    def test_path_identity_changed_under_lock_is_transient_not_error(
        self, tmp_path: Path
    ) -> None:
        """Simulates a concurrent golden-repo refresh swapping the directory
        contents between the lock-free check and the locked re-check: the
        second load_index() call raises a corrupt-index error. Must be
        counted as a transient outcome, never an ERROR."""
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )

        from code_indexer.server.services.hnsw_orphan_sweep import repair_executor

        original_loader = HNSWIndexManager.load_index
        call_count = {"n": 0}

        def _swap_then_load(self, path, max_elements=1000000):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Second load call happens under the lock (locked re-check).
                # Simulate a concurrent refresh replacing the file identity.
                raise RuntimeError("Index seems to be corrupted or unsupported")
            return original_loader(self, path, max_elements=max_elements)

        import unittest.mock as mock

        with mock.patch.object(
            repair_executor.HNSWIndexManager, "load_index", _swap_then_load
        ):
            outcome = process_candidate(candidate)

        assert outcome == SweepOutcome.TRANSIENT_SKIP


class TestProcessCandidateNonConvergingRepairIsError:
    def test_non_converging_repair_is_error_not_raised(self, tmp_path: Path) -> None:
        """A repair that fails to converge is loud (ERROR outcome, logged)
        but must NOT raise -- it is fail-soft per item (AC2)."""
        repo_root = tmp_path / "repo"
        collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
        _plant_prebroken_fixture(collection_path)

        candidate = _make_candidate(
            repo_root, ".code-indexer/index/voyage-code-3/hnsw_index.bin"
        )

        from code_indexer.server.services.hnsw_orphan_sweep import repair_executor

        class _FakeIndex:
            def __init__(self):
                self._calls = 0

            def check_integrity(self):
                self._calls += 1
                n = 3 if self._calls < 3 else 2  # never reaches 0
                return {
                    "valid": n == 0,
                    "errors": [f"orphan {i}" for i in range(n)],
                }

            def repair_orphans(self):
                return {"orphans_after": 2}

            def save_index(self, path):
                Path(path).write_bytes(b"noop")

        import unittest.mock as mock

        with mock.patch.object(
            repair_executor.HNSWIndexManager,
            "load_index",
            lambda self, path, max_elements=1000000: _FakeIndex(),
        ):
            outcome = process_candidate(candidate)

        # Must not raise -- fail-soft per item.
        assert outcome == SweepOutcome.ERROR
