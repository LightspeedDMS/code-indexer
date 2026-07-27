"""Tests for the immutable-aware sister-location temporal repair branch
(Story #1457 gap fix, `process_sister_temporal_candidate`).

Real infrastructure throughout -- no mocking of check_integrity()/
repair_orphans(), SQLite (ChunkStore), or AliasManager. A published sister
temporal shard is corrupted by overwriting ONLY its hnsw_index.bin (chunks.db
and collection_meta.json untouched -- realistic: a corruption event damages
the binary index file, not the source-of-truth chunk store). Repair must
NEVER write in place to the old (immutable) version -- it builds a brand-new
version and atomically swaps the alias pointer, exactly like Story #1457's
own AC6 build+publish machinery.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_consolidated_build import (
    build_fresh_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    SisterTemporalCandidate,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
    process_sister_temporal_candidate,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

CORPUS_DIM = 1024
SINGLE_THREADED = 1

# Same AC5 shape-matrix recipe used throughout the epic -- guaranteed to
# produce a real, pre-broken orphan-bearing index.
AC5_FIXTURE_SIZE = 270
AC5_FIXTURE_NOISE_SCALE = 0.01
AC5_FIXTURE_POCKET_FRACTION = 1.0
AC5_FIXTURE_SEED = 42

SMALL_RECORD_COUNT = 5


def _orphan_count(check_integrity_result: dict) -> int:
    return sum(1 for e in check_integrity_result["errors"] if "orphan" in e)


def _make_records(n: int, dim: int, seed: int) -> List[Dict[str, Any]]:
    rng = np.random.RandomState(seed)
    records = []
    for i in range(n):
        vector = rng.randn(dim).astype(np.float32).tolist()
        records.append(
            {
                "id": f"proj:commit:{'a' * 40}{i}:0",
                "vector": vector,
                "payload": {"path": f"file_{i}.py", "chunk_text": f"chunk {i}"},
            }
        )
    return records


def _publish_sister_shard(
    golden_repos_dir: Path,
    repo_alias: str,
    embedder_slug: str,
    quarter: Optional[str],
    records: List[Dict[str, Any]],
    vector_dim: int,
) -> Path:
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    suffix = f"-{quarter}" if quarter else ""
    pointer_namespace = f"{repo_alias}-temporal-{embedder_slug}{suffix}"
    version_path: Path = build_fresh_consolidated_temporal_version(
        golden_repos_dir,
        pointer_namespace,
        [records],
        vector_dim,
        embedder_slug=embedder_slug,
    )
    publish_temporal_shard_version(alias_manager, pointer_namespace, version_path)
    return version_path


def _make_candidate(
    golden_repos_dir: Path,
    repo_alias: str,
    embedder_slug: str,
    quarter: Optional[str],
    version_path: Path,
) -> SisterTemporalCandidate:
    suffix = f"-{quarter}" if quarter else ""
    pointer_namespace = f"{repo_alias}-temporal-{embedder_slug}{suffix}"
    return SisterTemporalCandidate(
        sort_key=f"sister_temporal:{repo_alias}:{embedder_slug}:{quarter or '_'}",
        golden_repos_dir=golden_repos_dir,
        repo_alias=repo_alias,
        alias=repo_alias,
        embedder_slug=embedder_slug,
        quarter=quarter,
        legacy_index_path=golden_repos_dir / repo_alias / ".code-indexer" / "index",
        version_path=version_path,
        pointer_namespace=pointer_namespace,
    )


def _corrupt_hnsw_bin_in_place(version_path: Path) -> int:
    """Overwrite ONLY hnsw_index.bin with a genuinely pre-broken index at
    the same vector_dim -- chunks.db and collection_meta.json untouched.
    Returns the orphan count of the planted broken index (must be > 0)."""
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
    broken_index.save_index(str(version_path / "hnsw_index.bin"))
    return orphans_before


class TestProcessSisterTemporalCandidateClean:
    def test_clean_shard_returns_clean_no_new_version(self, tmp_path: Path) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        records = _make_records(SMALL_RECORD_COUNT, CORPUS_DIM, seed=1)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records, CORPUS_DIM
        )

        candidate = _make_candidate(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", version_path
        )

        outcome = process_sister_temporal_candidate(candidate)

        assert outcome == SweepOutcome.CLEAN

        # No new version was created -- resolver still resolves to the
        # SAME path.
        alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
        resolver = TemporalShardResolver(
            alias_manager=alias_manager,
            repo_alias="myrepo",
            sister_root=golden_repos_dir,
            legacy_index_path=golden_repos_dir / "myrepo" / ".code-indexer" / "index",
        )
        resolved = resolver.resolve("voyage_code_3", "2026Q1")
        assert resolved is not None
        assert resolved.path == version_path


class TestProcessSisterTemporalCandidateRepair:
    def test_corrupted_shard_is_repaired_immutably(self, tmp_path: Path) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        records = _make_records(SMALL_RECORD_COUNT, CORPUS_DIM, seed=2)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records, CORPUS_DIM
        )

        _corrupt_hnsw_bin_in_place(version_path)

        # Confirm via a throwaway check_integrity() call before asserting
        # the outcome (mirrors _plant_prebroken_fixture's own style).
        manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        broken_reload = manager.load_index(version_path, max_elements=AC5_FIXTURE_SIZE)
        assert broken_reload is not None
        assert _orphan_count(broken_reload.check_integrity()) > 0

        old_bin_path = version_path / "hnsw_index.bin"
        old_bin_bytes_before = old_bin_path.read_bytes()

        candidate = _make_candidate(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", version_path
        )

        outcome = process_sister_temporal_candidate(candidate)

        assert outcome == SweepOutcome.SISTER_TEMPORAL_REPAIRED

        # (a) A fresh resolve() now returns a DIFFERENT path -- pointer moved.
        alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
        resolver = TemporalShardResolver(
            alias_manager=alias_manager,
            repo_alias="myrepo",
            sister_root=golden_repos_dir,
            legacy_index_path=golden_repos_dir / "myrepo" / ".code-indexer" / "index",
        )
        resolved_after = resolver.resolve("voyage_code_3", "2026Q1")
        assert resolved_after is not None
        assert resolved_after.path != version_path

        # (b) Reloading the NEW version shows zero orphans.
        new_manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
        reloaded = new_manager.load_index(resolved_after.path)
        assert reloaded is not None
        final_integrity = reloaded.check_integrity()
        assert final_integrity["valid"] is True
        assert _orphan_count(final_integrity) == 0

        # (c) The OLD broken version directory is COMPLETELY UNMODIFIED --
        # immutability proof.
        assert old_bin_path.exists()
        assert old_bin_path.read_bytes() == old_bin_bytes_before

        # (d) The new version's chunks.db round-trips the same records.
        new_store = ChunkStore(
            resolved_after.path / "chunks.db", expected_dim=CORPUS_DIM, immutable=True
        )
        try:
            stored_ids = {r["id"] for r in new_store.stream_all()}
        finally:
            new_store.close()
        assert stored_ids == {r["id"] for r in records}

    def test_repair_invalidates_cache(self, tmp_path: Path) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        records = _make_records(SMALL_RECORD_COUNT, CORPUS_DIM, seed=3)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records, CORPUS_DIM
        )
        _corrupt_hnsw_bin_in_place(version_path)

        candidate = _make_candidate(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", version_path
        )

        invalidated: List[str] = []
        outcome = process_sister_temporal_candidate(
            candidate, cache_invalidator=invalidated.append
        )

        assert outcome == SweepOutcome.SISTER_TEMPORAL_REPAIRED
        assert len(invalidated) == 1
        assert invalidated[0] != str(version_path)


class TestProcessSisterTemporalCandidateEdgeCases:
    def test_capability_unavailable_degrades_gracefully(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        records = _make_records(SMALL_RECORD_COUNT, CORPUS_DIM, seed=4)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records, CORPUS_DIM
        )

        from code_indexer.server.services.hnsw_orphan_sweep import repair_executor

        monkeypatch.setattr(
            repair_executor,
            "check_hnswlib_capability",
            lambda: (False, "hnswlib fork missing"),
        )

        candidate = _make_candidate(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", version_path
        )

        with caplog.at_level(logging.WARNING):
            outcome = process_sister_temporal_candidate(candidate)

        assert outcome == SweepOutcome.CAPABILITY_UNAVAILABLE

    def test_pointer_vanished_between_discovery_and_repair_is_transient(
        self, tmp_path: Path
    ) -> None:
        """Simulates a genuine race: the candidate's version_path is
        corrupted but the alias pointer has since moved (or been deleted)
        so a fresh resolve() no longer resolves to a SISTER_POINTER for
        this exact version -- must be a transient skip, never an error."""
        golden_repos_dir = tmp_path / "golden-repos"
        records = _make_records(SMALL_RECORD_COUNT, CORPUS_DIM, seed=5)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records, CORPUS_DIM
        )
        _corrupt_hnsw_bin_in_place(version_path)

        # Delete the alias pointer entirely to simulate the race.
        aliases_dir = golden_repos_dir / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        alias_manager.delete_alias("myrepo-temporal-voyage_code_3-2026Q1")

        candidate = _make_candidate(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", version_path
        )

        outcome = process_sister_temporal_candidate(candidate)

        # No pointer and no in-repo legacy data for this namespace -> None
        # resolution -> transient skip (not an error, not a crash).
        assert outcome == SweepOutcome.TRANSIENT_SKIP


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
