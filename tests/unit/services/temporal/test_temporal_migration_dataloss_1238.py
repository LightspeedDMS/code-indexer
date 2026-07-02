"""Tests for Bug #1238: Data loss prevention in temporal migration service.

Three fixes validated:
1. Chunk the SHA lookup (_batch_get_commit_timestamps) to prevent E2BIG on large repos.
2. Post-condition guard in _migrate_one_collection: fail loud when vectors dropped,
   preserve monolith intact for re-run.
3. Never report success when any vectors are dropped (covered by fix 2).

Mandatory test cases per Bug #1238 spec:
  A. Empty-payload + git timestamps (multi-quarter) -> clean full migration
  B. git lookup fails + empty payloads -> FAIL LOUD, monolith preserved
  C. Re-run after B with working git -> completes cleanly
  D. Chunking: large SHA set triggers multiple subprocess.run calls (no E2BIG)
"""

import json
import os
import struct
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers shared across all test cases
# ---------------------------------------------------------------------------


def _write_id_index_bin_1238(path: Path, id_index: Dict[str, str]) -> None:
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


def _build_monolithic_empty_json(
    index_path: Path,
    collection_name: str,
    vectors: "np.ndarray",
    shas: List[str],
    space: str = "cosine",
) -> Path:
    """Build a monolithic temporal collection with empty JSON payload files.

    Uses real-format point_ids: myrepo:commit:{sha}:{i}
    JSON payload files contain only {} (production format — no commit_timestamp).
    Timestamps MUST come from git log, not from JSON.

    Returns the collection directory path.
    """
    import hnswlib

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
        # Production format: empty JSON object — no commit_timestamp field
        json_path.write_text("{}")
        id_mapping[str(i)] = point_id
        id_index_data[point_id] = rel_path

    _write_id_index_bin_1238(coll_dir / "id_index.bin", id_index_data)

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
    """Create a real git repo with commits at specific ISO 8601 dates.

    Uses GIT_COMMITTER_DATE and GIT_AUTHOR_DATE env vars to set commit timestamps,
    so the resulting SHAs will resolve to the specified dates via git log.

    Returns (repo_path, [sha40, ...]).
    """
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


MIGRATION_COMPLETE_MARKER = "migration_complete.marker"
COLLECTION_NAME = "code-indexer-temporal-voyage_code_3"

# Dates that span 3 different calendar quarters of 2024
_Q1_DATE = "2024-01-15T12:00:00+00:00"
_Q2_DATE = "2024-05-10T12:00:00+00:00"
_Q3_DATE = "2024-08-20T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Test A: Empty-payload + git timestamps (multi-quarter) -> clean full migration
# ---------------------------------------------------------------------------


class TestCaseA_FullMigrationEmptyPayloadGitTimestamps:
    """Test A: Empty JSON payloads + working git timestamps covering multiple quarters
    results in a clean full migration with all vectors distributed to quarterly shards.

    Bug #1238: before fix, _batch_get_commit_timestamps raised on large SHA sets
    (E2BIG), fell back to JSON payload timestamps, and since payloads are empty {},
    all vectors were silently dropped then the monolith was deleted — data loss.

    After fix: chunked git lookup succeeds, all vectors bucketed by quarter,
    shards are queryable.
    """

    def test_full_migration_with_empty_payloads_and_git_timestamps(self, tmp_path):
        """3 vectors with empty JSON payloads, 3 real commits in 3 different quarters.

        After migration:
        - 3 quarterly shards created (one per quarter)
        - All 3 vectors present in shards (queryable via hnswlib)
        - Monolith hnsw_index.bin and id_index.bin deleted
        - JSON payload files deleted from monolith
        - migration_complete.marker written
        """
        import hnswlib

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        # Create real git repo with commits in 3 different quarters
        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )

        dim = 8
        vectors = np.random.rand(3, dim).astype(np.float32)
        index_path = tmp_path / "index"
        index_path.mkdir()

        coll_dir = _build_monolithic_empty_json(
            index_path, COLLECTION_NAME, vectors, shas
        )

        # Verify preconditions
        assert (coll_dir / "hnsw_index.bin").exists()
        assert (coll_dir / "id_index.bin").exists()
        payload_jsons = [
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        ]
        assert len(payload_jsons) == 3, (
            f"Expected 3 payload files, got {len(payload_jsons)}"
        )

        # Run migration with real git timestamps
        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        # Assert: 3 quarterly shards created (one per quarter)
        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 3, (
            f"Expected 3 quarterly shards (one per quarter), "
            f"got: {[d.name for d in quarterly_shards]}"
        )

        # Assert: total vectors across all shards = 3 (all migrated, none dropped)
        total_migrated = 0
        for shard_dir in quarterly_shards:
            meta_file = shard_dir / "collection_meta.json"
            assert meta_file.exists(), (
                f"collection_meta.json missing in {shard_dir.name}"
            )
            with open(meta_file) as f:
                meta = json.load(f)
            count = meta["hnsw_index"]["vector_count"]
            assert count == 1, (
                f"Expected 1 vector per shard, got {count} in {shard_dir.name}"
            )
            total_migrated += count

            # Assert: shard is queryable via hnswlib (not just metadata)
            shard_hnsw = shard_dir / "hnsw_index.bin"
            assert shard_hnsw.exists(), f"hnsw_index.bin missing in {shard_dir.name}"
            idx = hnswlib.Index(space="cosine", dim=dim)
            idx.load_index(str(shard_hnsw), max_elements=100)
            assert idx.get_current_count() == 1, (
                f"Shard {shard_dir.name} HNSW has {idx.get_current_count()} vectors, expected 1"
            )

        assert total_migrated == 3, (
            f"Expected all 3 vectors migrated, got {total_migrated}. "
            f"Bug #1238: vectors dropped when git timestamps resolve from empty JSON payloads."
        )

        # Assert: monolith binaries deleted
        assert not (coll_dir / "hnsw_index.bin").exists(), (
            "monolith hnsw_index.bin must be deleted after successful migration"
        )
        assert not (coll_dir / "id_index.bin").exists(), (
            "monolith id_index.bin must be deleted after successful migration"
        )

        # Assert: JSON payload files deleted from monolith (moved to shards)
        remaining_jsons = [
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        ]
        assert len(remaining_jsons) == 0, (
            f"Monolith JSON files not cleaned up: {[str(f) for f in remaining_jsons]}"
        )

        # Assert: migration marker written
        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "migration_complete.marker must be written after successful migration"
        )


# ---------------------------------------------------------------------------
# Test B: git lookup fails + empty payloads -> FAIL LOUD, NO data loss
# ---------------------------------------------------------------------------


class TestCaseB_GitFailEmptyPayloads_FailLoudNoDataLoss:
    """Test B: When git lookup returns {} (e.g. pointing at non-git dir) AND
    JSON payloads are empty {}, migration MUST:
    - RAISE a clear RuntimeError (fail loud)
    - Preserve monolith files (hnsw_index.bin, id_index.bin, payload JSONs)
    - NOT write the migration_complete.marker
    - Leave state retryable for a subsequent run

    Bug #1238: before fix, the code silently counted 0 migrated vectors,
    called _cleanup_monolithic_collection unconditionally, deleted the only copy
    of all vectors, then logged "Migration complete: 0 vectors" — unrecoverable.
    """

    def test_git_failure_with_empty_payloads_raises_and_preserves_monolith(
        self, tmp_path
    ):
        """Non-git repo_path -> git returns {} -> empty JSON -> raise, monolith intact."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        # Build real git repo to get valid-format SHAs (real 40-char hex)
        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )

        dim = 8
        vectors = np.random.rand(3, dim).astype(np.float32)
        index_path = tmp_path / "index"
        index_path.mkdir()

        coll_dir = _build_monolithic_empty_json(
            index_path, COLLECTION_NAME, vectors, shas
        )

        # Capture monolith file inventory before migration
        payload_jsons_before = {
            str(f.relative_to(coll_dir))
            for f in coll_dir.rglob("*.json")
            if f.name != "collection_meta.json"
        }
        assert len(payload_jsons_before) == 3, "Test setup: expected 3 payload files"

        # Point at a non-git directory so git lookup fails -> returns {}
        non_git_path = tmp_path / "not_a_git_repo"
        non_git_path.mkdir()

        # Migration MUST raise because all vectors were dropped (no timestamp source)
        with pytest.raises(RuntimeError, match=r"aborted|dropped|data.?loss|vectors"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=non_git_path,  # not a git repo -> git fails -> {} timestamps
            )

        # Assert: monolith hnsw_index.bin preserved (NOT deleted)
        assert (coll_dir / "hnsw_index.bin").exists(), (
            "DATA LOSS: hnsw_index.bin was deleted even though migration failed. "
            "Bug #1238: _cleanup_monolithic_collection called unconditionally."
        )

        # Assert: monolith id_index.bin preserved
        assert (coll_dir / "id_index.bin").exists(), (
            "DATA LOSS: id_index.bin was deleted even though migration failed."
        )

        # Assert: JSON payload files preserved (all 3 must still exist)
        payload_jsons_after = {
            str(f.relative_to(coll_dir))
            for f in coll_dir.rglob("*.json")
            if f.name != "collection_meta.json"
        }
        assert payload_jsons_after == payload_jsons_before, (
            f"DATA LOSS: JSON payload files deleted on failed migration. "
            f"Missing: {payload_jsons_before - payload_jsons_after}"
        )

        # Assert: migration marker NOT written (so re-run is possible)
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "migration_complete.marker must NOT be written on failed migration "
            "(would block re-run)"
        )

    def test_monkeypatch_batch_returns_empty_raises_and_preserves_monolith(
        self, tmp_path
    ):
        """Monkeypatch _batch_get_commit_timestamps to return {} (simulates E2BIG
        or any git failure) -> migration raises, monolith stays intact.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _migrate_one_collection,
        )

        # Build real git repo for valid SHAs
        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )

        dim = 8
        vectors = np.random.rand(3, dim).astype(np.float32)
        index_path = tmp_path / "index"
        index_path.mkdir()

        coll_dir = _build_monolithic_empty_json(
            index_path, COLLECTION_NAME, vectors, shas
        )

        # Force _batch_get_commit_timestamps to return {} (simulates E2BIG or any failure)
        with patch(
            "code_indexer.services.temporal.temporal_migration_service"
            "._batch_get_commit_timestamps",
            return_value={},
        ):
            with pytest.raises(
                RuntimeError, match=r"aborted|dropped|data.?loss|vectors"
            ):
                _migrate_one_collection(
                    collection_path=coll_dir,
                    index_path=index_path,
                    progress_callback=None,
                    repo_path=repo_path,
                )

        # Monolith must be completely intact
        assert (coll_dir / "hnsw_index.bin").exists(), (
            "hnsw_index.bin must survive a failed migration"
        )
        assert (coll_dir / "id_index.bin").exists(), (
            "id_index.bin must survive a failed migration"
        )
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "Marker must not be written on failed migration"
        )
        surviving_jsons = [
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        ]
        assert len(surviving_jsons) == 3, (
            f"Expected all 3 payload JSONs intact after failure, found {len(surviving_jsons)}"
        )


# ---------------------------------------------------------------------------
# Test C: Re-run after B completes cleanly
# ---------------------------------------------------------------------------


class TestCaseC_RerunAfterFailureSucceeds:
    """Test C: After a failed migration (Test B), fixing the git path and re-running
    results in a clean successful migration — the monolith is retryable.

    This validates that the post-condition guard DOES NOT corrupt the state
    needed for a successful re-run: monolith intact, no marker.
    """

    def test_rerun_with_valid_git_path_succeeds_after_previous_failure(self, tmp_path):
        """Fail first run (git patched empty), succeed second run (real git).

        Verifies:
        - First run raises (covered by Test B)
        - After first run monolith is still intact (covered by Test B)
        - Second run succeeds: 3 shards created, marker written, monolith deleted
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        # Build real git repo with commits in 3 quarters
        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
        )

        dim = 8
        vectors = np.random.rand(3, dim).astype(np.float32)
        index_path = tmp_path / "index"
        index_path.mkdir()

        _build_monolithic_empty_json(index_path, COLLECTION_NAME, vectors, shas)
        coll_dir = index_path / COLLECTION_NAME

        # FIRST RUN: force failure by patching git timestamps to return {}
        with patch(
            "code_indexer.services.temporal.temporal_migration_service"
            "._batch_get_commit_timestamps",
            return_value={},
        ):
            with pytest.raises(RuntimeError):
                run_temporal_migration(
                    index_path=index_path,
                    repo_alias="test-repo",
                    repo_path=repo_path,
                )

        # Verify monolith still intact after first (failed) run
        assert (coll_dir / "hnsw_index.bin").exists(), (
            "Monolith must be intact after first failed run to allow re-run"
        )
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "No marker must exist after failed run"
        )

        # SECOND RUN: use the real repo_path -> must complete successfully
        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,  # correct path this time
        )

        # Assert: migration completed — 3 quarterly shards
        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 3, (
            f"Expected 3 shards after re-run, got {len(quarterly_shards)}: "
            f"{[d.name for d in quarterly_shards]}"
        )

        # Assert: migration marker written
        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "Migration marker must exist after successful re-run"
        )

        # Assert: monolith cleaned up
        assert not (coll_dir / "hnsw_index.bin").exists(), (
            "Monolith must be cleaned up after successful re-run"
        )


# ---------------------------------------------------------------------------
# Test D: Chunking proves no E2BIG
# ---------------------------------------------------------------------------


class TestCaseD_ChunkingPreventsE2BIG:
    """Test D: Large SHA set is split into multiple subprocess.run calls (chunking),
    making E2BIG impossible regardless of repository history size.

    Bug #1238: before fix, a single git log call with ALL SHAs on a 50k+-commit
    repo exceeded Linux ARG_MAX -> OSError(E2BIG) -> except returned {} ->
    all vectors silently dropped -> data loss.

    After fix: SHAs are chunked (e.g. 1000 per call), each call is well within
    ARG_MAX, and results are accumulated across all chunks.
    """

    def test_large_sha_set_triggers_multiple_subprocess_calls(self, tmp_path):
        """2100 SHAs must generate at least 3 separate git log subprocess calls.

        Before fix: 1 subprocess call (causes E2BIG at ~50k SHAs on Linux).
        After fix: ceil(2100 / chunk_size) calls (chunk_size <= 1000).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        # Generate 2100 unique fake SHAs (40-char hex, but not in any real git repo)
        num_shas = 2100
        fake_shas = {f"{i:040x}" for i in range(num_shas)}
        assert len(fake_shas) == num_shas  # all unique

        git_calls_sha_sets: List[List[str]] = []

        def spy_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            """Spy: records SHA args of every 'git log' call; returns empty output."""
            if cmd and len(cmd) > 3 and cmd[0] == "git" and cmd[1] == "log":
                # SHAs start after "git log --no-walk --format=..."
                sha_args = [
                    arg
                    for arg in cmd[4:]
                    if len(arg) == 40 and all(c in "0123456789abcdef" for c in arg)
                ]
                git_calls_sha_sets.append(sha_args)
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""  # no real commits -> empty result
            return proc

        with patch(
            "code_indexer.services.temporal.temporal_migration_service.subprocess.run",
            side_effect=spy_subprocess_run,
        ):
            result = _batch_get_commit_timestamps(repo_path, fake_shas)

        # No timestamps returned (none of the fake SHAs exist in any repo)
        assert result == {}

        # KEY ASSERTION: multiple git calls were made — chunking happened
        assert len(git_calls_sha_sets) > 1, (
            f"Expected multiple chunked git log calls for {num_shas} SHAs, "
            f"but got {len(git_calls_sha_sets)} call(s). "
            f"Bug #1238: a single git log call with {num_shas} SHAs would exceed "
            f"ARG_MAX on large production repos."
        )

        # Each chunk must have at most 1000 SHAs (well within ARG_MAX)
        for i, chunk_shas in enumerate(git_calls_sha_sets):
            assert len(chunk_shas) <= 1000, (
                f"Chunk {i} has {len(chunk_shas)} SHAs — exceeds max chunk size of 1000"
            )

        # All 2100 input SHAs must be covered across all chunks (no SHA lost)
        all_covered_shas = set()
        for chunk_shas in git_calls_sha_sets:
            all_covered_shas.update(chunk_shas)
        assert all_covered_shas == fake_shas, (
            f"Not all SHAs were covered by chunked calls. "
            f"Missing {len(fake_shas - all_covered_shas)} SHAs out of {num_shas}."
        )

    def test_small_sha_set_uses_minimal_calls(self, tmp_path):
        """With fewer than chunk_size SHAs, one call is sufficient (no overhead)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        # Only 5 fake SHAs — well below any reasonable chunk size
        small_shas = {f"{i:040x}" for i in range(5)}

        git_calls: List[list] = []

        def spy_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            if cmd and cmd[0] == "git":
                git_calls.append(cmd)
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            return proc

        with patch(
            "code_indexer.services.temporal.temporal_migration_service.subprocess.run",
            side_effect=spy_subprocess_run,
        ):
            result = _batch_get_commit_timestamps(repo_path, small_shas)

        assert result == {}
        # Small set: exactly 1 call — no unnecessary overhead
        assert len(git_calls) == 1, (
            f"Expected 1 git call for 5 SHAs (no chunking overhead), "
            f"got {len(git_calls)}"
        )

    def test_chunked_accumulates_results_across_multiple_calls(self, tmp_path):
        """Results from multiple chunk calls are accumulated into one dict."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        # Named constants
        _NUM_SHAS = 2100
        _SHA_LEN = 40
        _GIT_SHA_OFFSET = 4  # SHAs start after "git log --no-walk --format=..."
        _MIN_CHUNKS = 3  # ceil(2100 / 1000)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        shas_input = {f"{i:040x}" for i in range(_NUM_SHAS)}

        chunks_seen: List[List[str]] = []
        resolved_by_spy: List[str] = []

        def spy_and_resolve(cmd: list, **kwargs: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            if cmd and len(cmd) > 3 and cmd[0] == "git" and cmd[1] == "log":
                sha_args = [a for a in cmd[_GIT_SHA_OFFSET:] if len(a) == _SHA_LEN]
                chunks_seen.append(sha_args)
                if sha_args:
                    first = sha_args[0]
                    resolved_by_spy.append(first)
                    proc.stdout = f"{first} 2024-01-15T12:00:00+00:00\n"
            return proc

        with patch(
            "code_indexer.services.temporal.temporal_migration_service.subprocess.run",
            side_effect=spy_and_resolve,
        ):
            result = _batch_get_commit_timestamps(repo_path, shas_input)

        assert len(chunks_seen) >= _MIN_CHUNKS, (
            f"Expected >= {_MIN_CHUNKS} chunk calls for {_NUM_SHAS} SHAs, "
            f"got {len(chunks_seen)}"
        )
        for sha in resolved_by_spy:
            assert sha in result, (
                "SHA resolved in chunk but missing from final result — "
                "cross-chunk accumulation broken"
            )
        assert len(result) == len(chunks_seen)


# ---------------------------------------------------------------------------
# Shared helpers for shard inspection (Test E and beyond)
# ---------------------------------------------------------------------------


def _find_quarterly_shards(index_path: Path) -> List[Path]:
    """Return sorted quarterly shard dirs under index_path."""
    return sorted(
        d
        for d in index_path.iterdir()
        if d.is_dir()
        and d.name.startswith("code-indexer-temporal-")
        and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
    )


def _read_shard_vector_count(shard_dir: Path) -> int:
    """Return vector_count from shard collection_meta.json (context-managed)."""
    with open(shard_dir / "collection_meta.json") as fh:
        return int(json.load(fh)["hnsw_index"]["vector_count"])


def _setup_collection_with_missing_json_orphan(
    tmp_path: Path, dim: int = 8
) -> Tuple[Path, Path, Path]:
    """Build a 3-vector monolith with one JSON payload deleted (structural orphan).

    Creates a real git repo with 3 commits in Q1/Q2/Q3 of 2024, then builds a
    monolithic collection with 3 empty JSON payloads and deletes the first one,
    leaving missing_json=1.

    Returns (repo_path, index_path, coll_dir).
    """
    repo_path, shas = _make_git_repo_with_dated_commits(
        tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
    )
    index_path = tmp_path / "index"
    index_path.mkdir()
    coll_dir = _build_monolithic_empty_json(
        index_path, COLLECTION_NAME, np.random.rand(3, dim).astype(np.float32), shas
    )
    all_payloads = sorted(
        f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
    )
    assert len(all_payloads) == 3, (
        f"Setup: expected 3 payloads, got {len(all_payloads)}"
    )
    # Delete first payload → structural orphan (id_index.bin entry points to missing file)
    all_payloads[0].unlink()
    return repo_path, index_path, coll_dir


# ---------------------------------------------------------------------------
# Test E: Structural orphan (missing JSON) HARD ABORTS (Bug #1286 supersedes
# the #1238 "proceed with warning" policy below)
# ---------------------------------------------------------------------------


class TestCaseE_StructuralOrphanHardAborts:
    """Test E: A collection with a structural orphan (missing JSON payload) MUST
    raise, write no marker, and leave the monolith fully intact.

    SUPERSEDES the original Bug #1238 policy (structural orphan => WARNING,
    proceed). Bug #1286 production forensics showed this policy silently
    discarded ~32K already-embedded vectors while still reporting success —
    losslessness must be all-or-nothing: ANY unmatched point aborts the whole
    migration, never a partial silent skip.
    """

    def test_missing_json_orphan_raises_no_marker_monolith_intact(self, tmp_path):
        """One deleted JSON payload (missing_json=1) must ABORT the migration.

        3 real git commits (Q1/Q2/Q3) + 3 empty JSON payloads.  First payload
        deleted => structural orphan.  Expected: RuntimeError raised, zero
        shards built, no marker written, monolith fully intact.
        """
        import pytest

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir = _setup_collection_with_missing_json_orphan(
            tmp_path
        )

        with pytest.raises(RuntimeError, match=r"aborted|orphan|structural|vectors"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=repo_path,
            )

        shards = _find_quarterly_shards(index_path)
        assert shards == [], (
            f"Expected zero shards built on abort, got: {[d.name for d in shards]}"
        )
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "marker must NOT be written when a structural orphan aborts the migration"
        )
        assert (coll_dir / "hnsw_index.bin").exists(), (
            "monolith hnsw_index.bin must be preserved on abort"
        )
        assert (coll_dir / "id_index.bin").exists(), (
            "monolith id_index.bin must be preserved on abort"
        )
