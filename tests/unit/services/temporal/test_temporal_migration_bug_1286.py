"""Tests for Bug #1286: temporal shard migration data-loss / false-marker / destructive
finalize / expensive-recovery defects (Story #1172 quarterly-shard migration).

Four defects fixed here:
  1. Structural orphans (missing_id_index, missing_json) now HARD ABORT the
     migration (raise) instead of WARNING-and-continue. Combined with the
     pre-existing #1238 fix for timestamp_unresolved, ANY unmatched point now
     aborts the whole migration -- losslessness is all-or-nothing.
  2. migration_complete.marker is written ONLY after an explicit, defensive
     invariant check: vectors_migrated == len(label_to_point_id) AND every
     quarter bucket has a completed shard directory on disk.
  3. _cleanup_monolithic_collection scopes its JSON deletion to the precise
     payload pattern "vector_*.json" (matching temporal_reconciliation.py and
     the real production payload-file naming convention) instead of a blanket
     "*.json minus collection_meta.json" glob, which previously also deleted
     temporal_progress.json / temporal_meta.json bookkeeping files.
  4. TemporalIndexer.index_commits() runs a cheap monolith-recovery pass
     (_recover_from_monolith_if_needed) BEFORE ever walking git history, so a
     collection with an intact-but-unmigrated monolith is repaired via cheap
     HNSW extraction (zero embedding-provider calls) instead of falling
     through to an expensive full git-history re-embed.
"""

import json
import os
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Shared fixture-construction helpers (mirrors established pattern from
# test_temporal_migration_dataloss_1238.py / test_temporal_migration_1172.py)
# ---------------------------------------------------------------------------

COLLECTION_NAME = "code-indexer-temporal-voyage_code_3"
MIGRATION_COMPLETE_MARKER = "migration_complete.marker"

_Q1_DATE = "2024-01-15T12:00:00+00:00"
_Q2_DATE = "2024-05-10T12:00:00+00:00"
_Q3_DATE = "2024-08-20T12:00:00+00:00"


def _write_id_index_bin(path: Path, id_index: Dict[str, str]) -> None:
    """Write binary id_index.bin: {point_id: relative_json_path}."""
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _build_monolithic_collection(
    index_path: Path,
    collection_name: str,
    vectors: np.ndarray,
    shas: List[str],
    space: str = "cosine",
    commit_timestamps: "List[int] | None" = None,
) -> Path:
    """Build a complete real monolithic HNSW collection on disk.

    Uses real-format point_ids ({repo}:commit:{sha40}:{idx}) and real payload
    files named vector_<n>.json (matching production naming convention), each
    carrying a genuine commit_timestamp value.

    Bug #1286 follow-up: payload commit_timestamp is now the PRIMARY
    timestamp source (see _build_quarter_buckets). commit_timestamps lets
    callers supply per-entry values matching the real git commit dates used
    to build `shas`, so payload and git agree (as a real indexer would
    produce). Defaults to "now" for callers that do not care about specific
    quarters (e.g. structural-orphan/abort tests). Must be the same length as
    shas when provided.
    """
    import hnswlib

    if commit_timestamps is not None and len(commit_timestamps) != len(shas):
        raise ValueError(
            f"commit_timestamps length ({len(commit_timestamps)}) must match "
            f"shas length ({len(shas)})"
        )

    coll_dir = index_path / collection_name
    coll_dir.mkdir(parents=True, exist_ok=True)

    n = len(vectors)
    dim = vectors.shape[1]

    hnsw_idx = hnswlib.Index(space=space, dim=dim)
    hnsw_idx.init_index(
        max_elements=n, M=16, ef_construction=200, allow_replace_deleted=True
    )
    hnsw_idx.add_items(vectors, np.arange(n))
    hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

    id_mapping: Dict[str, str] = {}
    id_index_data: Dict[str, str] = {}

    for i, sha in enumerate(shas):
        point_id = f"myrepo:commit:{sha}:{i}"
        rel_path = f"{i:02x}/vector_{i}.json"
        json_path = coll_dir / rel_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        if commit_timestamps is not None:
            commit_ts = commit_timestamps[i]
        else:
            commit_ts = int(datetime.now(timezone.utc).timestamp())
        json_path.write_text(json.dumps({"payload": {"commit_timestamp": commit_ts}}))
        id_mapping[str(i)] = point_id
        id_index_data[point_id] = rel_path

    _write_id_index_bin(coll_dir / "id_index.bin", id_index_data)

    meta = {
        "name": collection_name,
        "vector_size": dim,
        "created_at": datetime.utcnow().isoformat(),
        "hnsw_index": {
            "version": 1,
            "vector_count": n,
            "vector_dim": dim,
            "M": 16,
            "ef_construction": 200,
            "space": space,
            "last_rebuild": datetime.utcnow().isoformat(),
            "file_size_bytes": (coll_dir / "hnsw_index.bin").stat().st_size,
            "id_mapping": id_mapping,
            "is_stale": False,
            "last_marked_stale": None,
        },
    }
    with open(coll_dir / "collection_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    return coll_dir


def _make_git_repo_with_dated_commits(
    tmp_path: Path, date_iso_strs: List[str]
) -> Tuple[Path, List[str]]:
    """Create a real git repo with commits at specific ISO 8601 dates."""
    repo_path = tmp_path / "gitrepo"
    repo_path.mkdir()
    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    shas = []
    for i, date_str in enumerate(date_iso_strs):
        (repo_path / f"file{i}.txt").write_text(f"content {i}")
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True
        )
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date_str
        env["GIT_AUTHOR_DATE"] = date_str
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            env=env,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        shas.append(sha)
    return repo_path, shas


def _iso_to_epoch(iso_str: str) -> int:
    """Parse an ISO-8601 date string (e.g. _Q1_DATE) to a UNIX epoch int."""
    return int(datetime.fromisoformat(iso_str).timestamp())


def _setup_monolith(tmp_path: Path, dim: int = 8) -> Tuple[Path, Path, Path, List[str]]:
    """Build a 3-vector real monolith with 3 real git commits (Q1/Q2/Q3 2024).

    Payload commit_timestamps match the real git commit dates (Bug #1286
    follow-up: payload is now the primary timestamp source), so both sources
    agree on the same quarters, as a real indexer would produce.

    Returns (repo_path, index_path, coll_dir, shas).
    """
    repo_path, shas = _make_git_repo_with_dated_commits(
        tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
    )
    index_path = tmp_path / "index"
    index_path.mkdir()
    coll_dir = _build_monolithic_collection(
        index_path,
        COLLECTION_NAME,
        np.random.rand(3, dim).astype(np.float32),
        shas,
        commit_timestamps=[_iso_to_epoch(d) for d in (_Q1_DATE, _Q2_DATE, _Q3_DATE)],
    )
    return repo_path, index_path, coll_dir, shas


def _payload_jsons(coll_dir: Path) -> List[Path]:
    return sorted(
        f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
    )


# ---------------------------------------------------------------------------
# Defect 1: structural orphans must HARD ABORT (no more warn-and-continue)
# ---------------------------------------------------------------------------


class TestDefect1StructuralOrphansHardAbort:
    """A structurally-orphaned point (missing id_index entry or missing JSON
    payload) must raise RuntimeError and preserve the monolith -- exactly like
    the pre-existing timestamp_unresolved case. There is no "recoverable" case
    for a point that cannot be matched: it is permanent data loss unless the
    migration is aborted so an operator can investigate.
    """

    def test_missing_json_payload_raises_and_preserves_monolith(self, tmp_path):
        """Delete one real payload JSON (id_index still points at it) -> raise."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)

        payloads_before = _payload_jsons(coll_dir)
        assert len(payloads_before) == 3, "Test setup: expected 3 payload files"
        payloads_before[0].unlink()  # missing_json orphan

        with pytest.raises(RuntimeError, match=r"aborted|orphan|structural|vectors"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=repo_path,
            )

        # No shard was ever built -- the check happens BEFORE the shard loop
        quarterly_shards = [
            d for d in index_path.iterdir() if d.is_dir() and d.name != COLLECTION_NAME
        ]
        assert quarterly_shards == [], (
            f"Expected zero shards built on abort, found: "
            f"{[d.name for d in quarterly_shards]}"
        )

        # Monolith fully intact
        assert (coll_dir / "hnsw_index.bin").exists()
        assert (coll_dir / "id_index.bin").exists()
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists()
        remaining = _payload_jsons(coll_dir)
        assert len(remaining) == 2, (
            "The 2 surviving payload files must remain untouched on abort"
        )

    def test_missing_id_index_entry_raises_and_preserves_monolith(self, tmp_path):
        """label_to_point_id references a point_id absent from id_index.bin."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _migrate_one_collection,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)

        # Corrupt collection_meta.json's id_mapping to reference a point_id that
        # does NOT exist in id_index.bin (structural missing_id_index orphan).
        meta_path = coll_dir / "collection_meta.json"
        with open(meta_path) as f:
            meta = json.load(f)
        meta["hnsw_index"]["id_mapping"]["0"] = "myrepo:commit:" + ("0" * 40) + ":999"
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        with pytest.raises(RuntimeError, match=r"aborted|orphan|structural|vectors"):
            _migrate_one_collection(
                collection_path=coll_dir,
                index_path=index_path,
                progress_callback=None,
                repo_path=repo_path,
            )

        assert (coll_dir / "hnsw_index.bin").exists()
        assert (coll_dir / "id_index.bin").exists()
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists()

    def test_zero_orphans_migrates_cleanly(self, tmp_path):
        """Sanity/characterization: with no orphans, migration succeeds as before."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists()
        assert not (coll_dir / "hnsw_index.bin").exists()

    def test_timestamp_unresolved_still_raises_git_failure_and_no_payload_fallback(
        self, tmp_path
    ):
        """Characterization test (pre-existing #1238 behavior must be untouched):
        a point whose commit SHA fails git resolution AND whose JSON payload has
        no commit_timestamp field must still raise (never silently dropped).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )
        index_path = tmp_path / "index"
        index_path.mkdir()

        # Build monolith manually with ONE point whose JSON payload has no
        # commit_timestamp AND whose SHA is real but we force git resolution to
        # fail for it by pointing repo_path at a directory that IS a git repo
        # but does not contain this SHA (a fresh empty repo).
        import hnswlib

        dim = 8
        vectors = np.random.rand(1, dim).astype(np.float32)
        coll_dir = index_path / COLLECTION_NAME
        coll_dir.mkdir(parents=True)
        hnsw_idx = hnswlib.Index(space="cosine", dim=dim)
        hnsw_idx.init_index(max_elements=1, M=16, ef_construction=200)
        hnsw_idx.add_items(vectors, np.arange(1))
        hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

        point_id = f"myrepo:commit:{shas[0]}:0"
        rel_path = "00/vector_0.json"
        json_path = coll_dir / rel_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}")  # no commit_timestamp fallback available

        _write_id_index_bin(coll_dir / "id_index.bin", {point_id: rel_path})

        meta = {
            "name": COLLECTION_NAME,
            "vector_size": dim,
            "created_at": datetime.utcnow().isoformat(),
            "hnsw_index": {
                "version": 1,
                "vector_count": 1,
                "vector_dim": dim,
                "M": 16,
                "ef_construction": 200,
                "space": "cosine",
                "last_rebuild": datetime.utcnow().isoformat(),
                "file_size_bytes": (coll_dir / "hnsw_index.bin").stat().st_size,
                "id_mapping": {"0": point_id},
                "is_stale": False,
                "last_marked_stale": None,
            },
        }
        with open(coll_dir / "collection_meta.json", "w") as f:
            json.dump(meta, f)

        # A fresh empty git repo (different from repo_path) -- git log for this
        # SHA finds nothing (whole SHA-chunk "failure" scenario: git succeeds but
        # returns zero matching lines for an unknown SHA in an unrelated repo).
        empty_repo = tmp_path / "unrelated_repo"
        empty_repo.mkdir()
        subprocess.run(
            ["git", "init", str(empty_repo)], check=True, capture_output=True
        )

        with pytest.raises(RuntimeError, match=r"unresolved|timestamp"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=empty_repo,
            )

        assert (coll_dir / "hnsw_index.bin").exists()
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists()


# ---------------------------------------------------------------------------
# Defect 2: explicit defensive invariant gate before marker + cleanup
# ---------------------------------------------------------------------------


class TestDefect2ExplicitInvariantGate:
    """The marker/cleanup gate must be an INDEPENDENT line of defense (Messi #15
    defensive invariants), not merely an accidental consequence of Defect 1's
    abort-on-any-drop check. Prove this by calling the gate function directly
    with a deliberately tampered/mismatched state that Defect 1's guard would
    never itself construct.
    """

    def test_gate_rejects_vector_count_mismatch(self, tmp_path):
        """vectors_migrated != len(label_to_point_id) must raise, independent of
        drop_counts (e.g. a future bug that double-counts or drops silently).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _verify_migration_lossless_and_complete,
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        # Build one real shard dir with a valid collection_meta.json so the
        # per-quarter existence check alone would pass -- only the count
        # mismatch should trip the gate.
        shard_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir.mkdir(parents=True)
        (shard_dir / "collection_meta.json").write_text("{}")

        with pytest.raises(RuntimeError, match=r"count|mismatch|migrated"):
            _verify_migration_lossless_and_complete(
                collection_name="code-indexer-temporal-voyage_code_3",
                index_path=index_path,
                label_to_point_id={0: "a", 1: "b"},  # expects 2
                quarter_buckets={"2024Q1": [(0, "a", Path("x"))]},
                vectors_migrated=1,  # only 1 actually migrated -- mismatch
            )

    def test_gate_rejects_missing_shard_directory(self, tmp_path):
        """A quarter bucket with no corresponding shard dir on disk must raise,
        even when the reported vectors_migrated count matches.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _verify_migration_lossless_and_complete,
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        # Deliberately do NOT create the 2024Q1 shard dir.

        with pytest.raises(RuntimeError, match=r"shard|missing|collection_meta"):
            _verify_migration_lossless_and_complete(
                collection_name="code-indexer-temporal-voyage_code_3",
                index_path=index_path,
                label_to_point_id={0: "a"},
                quarter_buckets={"2024Q1": [(0, "a", Path("x"))]},
                vectors_migrated=1,
            )

    def test_gate_accepts_lossless_complete_state(self, tmp_path):
        """Sanity: matching count + all shard dirs present passes without raising."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _verify_migration_lossless_and_complete,
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        shard_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        shard_dir.mkdir(parents=True)
        (shard_dir / "collection_meta.json").write_text("{}")

        # Must not raise.
        _verify_migration_lossless_and_complete(
            collection_name="code-indexer-temporal-voyage_code_3",
            index_path=index_path,
            label_to_point_id={0: "a"},
            quarter_buckets={"2024Q1": [(0, "a", Path("x"))]},
            vectors_migrated=1,
        )

    def test_gate_invoked_before_cleanup_on_real_migration(self, tmp_path):
        """Integration: a real end-to-end lossless migration passes the gate and
        proceeds to write the marker + delete the monolith (regression guard that
        the gate wiring does not block the legitimate happy path).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists()
        assert not (coll_dir / "hnsw_index.bin").exists()


# ---------------------------------------------------------------------------
# Defect 3: _cleanup_monolithic_collection must not delete bookkeeping JSON
# ---------------------------------------------------------------------------


class TestDefect3BookkeepingFilesSurviveCleanup:
    """temporal_progress.json (crash-resume tracker) and temporal_meta.json
    (last_indexed_commit anchor) must survive migration cleanup. Before the
    fix, the blanket "*.json minus collection_meta.json" glob destroyed both,
    forcing the next indexing run to lose its incremental-resume anchor and
    fall back to a full git-history walk (the "expensive recovery" symptom).
    """

    def test_temporal_progress_json_survives_cleanup(self, tmp_path):
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)

        # Simulate real bookkeeping files that a live TemporalIndexer would have
        # written into this same collection directory before migration runs.
        progress_file = coll_dir / "temporal_progress.json"
        progress_file.write_text(json.dumps({"completed_commits": shas}))
        meta_file = coll_dir / "temporal_meta.json"
        meta_file.write_text(json.dumps({"last_commit": shas[-1]}))

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "Test setup: migration must have succeeded"
        )
        assert progress_file.exists(), (
            "DATA LOSS: temporal_progress.json was deleted by migration cleanup — "
            "this destroys the crash-resume completed-commits tracker"
        )
        assert json.loads(progress_file.read_text())["completed_commits"] == shas
        assert meta_file.exists(), (
            "DATA LOSS: temporal_meta.json was deleted by migration cleanup — "
            "this destroys the last_indexed_commit incremental-resume anchor, "
            "forcing the next index run to walk full git history"
        )
        assert json.loads(meta_file.read_text())["last_commit"] == shas[-1]

    def test_vector_payload_json_still_deleted(self, tmp_path):
        """Regression: the precise vector_*.json pattern must still delete the
        actual payload files (that part of cleanup is unchanged).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir, shas = _setup_monolith(tmp_path)
        payloads_before = _payload_jsons(coll_dir)
        assert len(payloads_before) == 3

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        remaining_payloads = list(coll_dir.rglob("vector_*.json"))
        assert remaining_payloads == [], (
            f"Vector payload JSON files must still be deleted: {remaining_payloads}"
        )


# ---------------------------------------------------------------------------
# Defect 4: cheap monolith recovery guard runs BEFORE any git-history walk
# ---------------------------------------------------------------------------


def _make_temporal_indexer(repo_path: Path):
    """Construct a real TemporalIndexer with a mocked config_manager but a REAL
    FilesystemVectorStore rooted at repo_path (mirrors the established pattern
    in test_temporal_indexer_production_fixes.py).
    """
    from unittest.mock import MagicMock
    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    config_manager = MagicMock()
    config_manager.get_config.return_value = MagicMock(
        embedding_provider="voyage-ai",
        voyage_ai=MagicMock(parallel_requests=4, api_key=None, model="voyage-code-3"),
        temporal=MagicMock(diff_context_lines=3),
        file_extensions=None,
        override_config=None,
    )
    config_manager.config_path = repo_path / ".code-indexer" / "config.json"

    index_base_path = repo_path / ".code-indexer" / "index"
    vector_store = FilesystemVectorStore(
        base_path=index_base_path, project_root=repo_path
    )

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
    ) as mock_factory:
        mock_factory.get_provider_model_info.return_value = {
            "dimensions": 8,
            "provider": "voyage-ai",
            "model": "voyage-code-3",
        }
        indexer = TemporalIndexer(
            config_manager, vector_store, collection_name=COLLECTION_NAME
        )
    return indexer


class TestDefect4RecoveryGuardPrefersCheapExtraction:
    """A collection with an intact-but-unmigrated monolith must be repaired via
    the cheap migration re-extraction path (zero embedding-provider calls,
    real hnswlib get_items()) BEFORE index_commits() ever walks git history.
    """

    def test_noop_when_no_monolith_present(self, tmp_path):
        """No unmigrated monolith on disk -> recovery guard does nothing."""
        repo_path, shas = _make_git_repo_with_dated_commits(tmp_path, [_Q1_DATE])
        indexer = _make_temporal_indexer(repo_path)

        called = {"n": 0}
        with patch(
            "code_indexer.services.temporal.temporal_indexer.run_temporal_migration",
            side_effect=lambda **kw: called.__setitem__("n", called["n"] + 1),
        ):
            indexer._recover_from_monolith_if_needed()

        assert called["n"] == 0, (
            "run_temporal_migration must NOT be called when no monolith exists"
        )

    def test_calls_real_migration_when_monolith_present_zero_embed_calls(
        self, tmp_path
    ):
        """An unmigrated monolith present under the indexer's own index dir is
        repaired via the REAL run_temporal_migration (genuine hnswlib
        extraction) with zero embedding-provider calls.
        """
        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )
        indexer = _make_temporal_indexer(repo_path)

        index_path = indexer.vector_store.base_path
        index_path.mkdir(parents=True, exist_ok=True)
        _build_monolithic_collection(
            index_path,
            COLLECTION_NAME,
            np.random.rand(3, 8).astype(np.float32),
            shas,
            commit_timestamps=[
                _iso_to_epoch(d) for d in (_Q1_DATE, _Q2_DATE, _Q3_DATE)
            ],
        )

        embed_calls = {"n": 0}

        def _counting_embed(self_client, texts, *a, **kw):
            embed_calls["n"] += len(texts)
            return [[0.0] * 8 for _ in texts]

        with patch(
            "code_indexer.services.voyage_ai.VoyageAIClient.get_embeddings_batch",
            _counting_embed,
        ):
            indexer._recover_from_monolith_if_needed()

        assert embed_calls["n"] == 0, (
            f"Recovery must make ZERO embedding-provider calls, got {embed_calls['n']}"
        )

        # Real migration ran: monolith is gone, shards exist for all 3 quarters.
        assert not (index_path / COLLECTION_NAME / "hnsw_index.bin").exists()
        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith(COLLECTION_NAME)
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 3, (
            f"Expected 3 quarterly shards recovered from monolith, "
            f"got {[d.name for d in quarterly_shards]}"
        )

    def test_index_commits_runs_recovery_before_get_commit_history(self, tmp_path):
        """Ordering guarantee: recovery must run BEFORE _get_commit_history() so a
        recoverable monolith is never bypassed in favor of a full git-history walk.
        """
        repo_path, shas = _make_git_repo_with_dated_commits(tmp_path, [_Q1_DATE])
        indexer = _make_temporal_indexer(repo_path)

        call_order = []

        def _fake_recover(*a, **kw):
            call_order.append("recover")

        def _fake_get_history(*a, **kw):
            call_order.append("get_commit_history")
            return []  # short-circuit index_commits() early, no embedding needed

        with (
            patch.object(
                indexer, "_recover_from_monolith_if_needed", side_effect=_fake_recover
            ),
            patch.object(indexer, "_get_commit_history", side_effect=_fake_get_history),
        ):
            indexer.index_commits()

        assert call_order == ["recover", "get_commit_history"], (
            f"Expected recovery to run BEFORE _get_commit_history, got: {call_order}"
        )
