"""Tests for Story #1172: Background Startup Migration — Monolithic Temporal Indexes to Quarterly Shards.

AC8 required unit tests.
"""

import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock

import numpy as np


# ---------------------------------------------------------------------------
# Helper: build a minimal monolithic temporal collection on disk
# ---------------------------------------------------------------------------


def _write_id_index_bin(path: Path, id_index: Dict[str, str]) -> None:
    """Write a binary id_index.bin file: {point_id: relative_json_path}."""
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _write_vector_json(
    json_path: Path,
    point_id: str,
    vector: list,
    commit_timestamp: int,
) -> None:
    """Write a minimal vector JSON payload file."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "id": point_id,
        "vector": vector,
        "payload": {
            "commit_timestamp": commit_timestamp,
            "commit_date": datetime.fromtimestamp(
                commit_timestamp, tz=timezone.utc
            ).strftime("%Y-%m-%d"),
        },
    }
    with open(json_path, "w") as f:
        json.dump(data, f)


def _build_monolithic_collection(
    index_path: Path,
    collection_name: str,
    vectors: np.ndarray,
    timestamps: list,
    space: str = "cosine",
) -> Path:
    """Build a complete monolithic HNSW collection on disk.

    Returns the collection directory path.
    """
    import hnswlib

    coll_dir = index_path / collection_name
    coll_dir.mkdir(parents=True, exist_ok=True)

    n = len(vectors)
    dim = vectors.shape[1]

    # Build HNSW index
    hnsw_idx = hnswlib.Index(space=space, dim=dim)
    hnsw_idx.init_index(
        max_elements=n, M=16, ef_construction=200, allow_replace_deleted=True
    )
    hnsw_idx.add_items(vectors, np.arange(n))
    hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

    # Write vector JSON files and build id_index mapping
    id_mapping = {}  # label_int -> point_id
    id_index = {}  # point_id -> relative_path

    for i, (vec, ts) in enumerate(zip(vectors, timestamps)):
        point_id = f"commit:abc{i:04d}:file.py:{i}"
        rel_path = f"{i:02x}/vector_{point_id.replace(':', '_')}.json"
        json_path = coll_dir / rel_path
        _write_vector_json(json_path, point_id, vec.tolist(), ts)
        id_mapping[str(i)] = point_id
        id_index[point_id] = rel_path

    # Write id_index.bin
    _write_id_index_bin(coll_dir / "id_index.bin", id_index)

    # Write collection_meta.json (including hnsw_index.id_mapping)
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


# Unix timestamps for quarters of 2024
_Q1_2024 = int(datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp())  # 2024Q1
_Q2_2024 = int(datetime(2024, 5, 10, tzinfo=timezone.utc).timestamp())  # 2024Q2
_Q3_2024 = int(datetime(2024, 8, 20, tzinfo=timezone.utc).timestamp())  # 2024Q3


# ---------------------------------------------------------------------------
# AC1: _needs_temporal_migration
# ---------------------------------------------------------------------------


class TestNeedsTemporalMigration:
    """AC1: Startup detection logic."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_migration_service import (
            _needs_temporal_migration,
        )

        self._needs_temporal_migration = _needs_temporal_migration

    def test_needs_temporal_migration_true_for_unsharded_with_hnsw(self, tmp_path):
        """Creates temporal-voyage_code_3/hnsw_index.bin, returns True."""
        index_path = tmp_path / "index"
        coll_dir = index_path / "code-indexer-temporal-voyage_code_3"
        coll_dir.mkdir(parents=True)
        (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
        assert self._needs_temporal_migration(index_path) is True

    def test_needs_temporal_migration_false_when_marker_present(self, tmp_path):
        """Same dir + migration_complete.marker, returns False."""
        index_path = tmp_path / "index"
        coll_dir = index_path / "code-indexer-temporal-voyage_code_3"
        coll_dir.mkdir(parents=True)
        (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
        (coll_dir / "migration_complete.marker").write_text("done")
        assert self._needs_temporal_migration(index_path) is False

    def test_needs_temporal_migration_false_when_already_sharded(self, tmp_path):
        """Dir is code-indexer-temporal-voyage_code_3-2024Q1, returns False."""
        index_path = tmp_path / "index"
        coll_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        coll_dir.mkdir(parents=True)
        (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
        # Sharded collection — must NOT be detected as needing migration
        assert self._needs_temporal_migration(index_path) is False

    def test_needs_temporal_migration_false_when_no_hnsw(self, tmp_path):
        """Temporal dir without hnsw_index.bin is not a migration target."""
        index_path = tmp_path / "index"
        coll_dir = index_path / "code-indexer-temporal-voyage_code_3"
        coll_dir.mkdir(parents=True)
        # No hnsw_index.bin
        assert self._needs_temporal_migration(index_path) is False

    def test_needs_temporal_migration_false_for_non_temporal_dirs(self, tmp_path):
        """Non-temporal collection directories are not migration targets."""
        index_path = tmp_path / "index"
        coll_dir = index_path / "code-indexer"
        coll_dir.mkdir(parents=True)
        (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
        assert self._needs_temporal_migration(index_path) is False

    def test_needs_temporal_migration_false_for_empty_index_path(self, tmp_path):
        """Empty index path returns False without error."""
        index_path = tmp_path / "index"
        index_path.mkdir()
        assert self._needs_temporal_migration(index_path) is False

    def test_needs_temporal_migration_false_for_nonexistent_index_path(self, tmp_path):
        """Nonexistent index path returns False without raising."""
        index_path = tmp_path / "no_such_dir"
        assert self._needs_temporal_migration(index_path) is False


# ---------------------------------------------------------------------------
# AC2: Duplicate-job guard
# ---------------------------------------------------------------------------


class TestStartupSkipsIfJobAlreadyRunning:
    """AC2: Skip submission if migration job already PENDING/RUNNING."""

    def test_startup_skips_submission_if_job_already_running(self, tmp_path):
        """Mock BGM raises DuplicateJobError, verify no double-submit."""
        from code_indexer.server.repositories.background_jobs import DuplicateJobError
        from code_indexer.services.temporal.temporal_migration_service import (
            submit_temporal_migration_jobs,
        )

        # submit_temporal_migration_jobs builds: clone_path / ".code-indexer" / "index"
        clone_path = tmp_path / "repo"
        clone_path.mkdir()
        index_path = clone_path / ".code-indexer" / "index"
        coll_dir = index_path / "code-indexer-temporal-voyage_code_3"
        coll_dir.mkdir(parents=True)
        (coll_dir / "hnsw_index.bin").write_bytes(b"fake")

        bgm = MagicMock()
        bgm.submit_job.side_effect = DuplicateJobError(
            "temporal_index_migration", "test-repo", "existing-job-id"
        )

        # Should not raise, should log at DEBUG
        repos = [{"alias": "test-repo", "clone_path": str(clone_path)}]
        submit_temporal_migration_jobs(bgm, repos)

        # submit_job was called once but DuplicateJobError was swallowed
        bgm.submit_job.assert_called_once()


# ---------------------------------------------------------------------------
# AC3 + AC5: Migration groups vectors by quarter and creates shard dirs
# ---------------------------------------------------------------------------


class TestMigrationGroupsVectorsByQuarter:
    """AC3: Migration groups vectors into quarterly shards."""

    def test_migration_groups_vectors_by_quarter(self, tmp_path):
        """Mock HNSW with timestamps spanning 3 quarters, verify 3 shard dirs created."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024, _Q1_2024, _Q2_2024, _Q3_2024, _Q3_2024]
        vectors = np.random.rand(5, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        progress_messages = []
        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=lambda msg: progress_messages.append(msg),
        )

        # Filter to quarterly shards only
        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 3, (
            f"Expected 3 shards, got {[d.name for d in index_path.iterdir() if d.is_dir()]}"
        )


# ---------------------------------------------------------------------------
# AC4: Atomic per-shard creation via .migrating dirs
# ---------------------------------------------------------------------------


class TestMigrationAtomicRenaming:
    """AC4: Each shard written to .migrating, renamed atomically."""

    def test_migration_creates_migrating_temp_dirs_and_renames_atomically(
        self, tmp_path
    ):
        """Verify .migrating temp dirs don't exist after migration, final shards do."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024, _Q2_2024]
        vectors = np.random.rand(2, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        # No .migrating dirs should remain
        migrating_dirs = list(index_path.glob("*.migrating"))
        assert len(migrating_dirs) == 0, (
            f"Found leftover .migrating dirs: {migrating_dirs}"
        )

        # Final shard dirs should exist
        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 2


# ---------------------------------------------------------------------------
# AC5: Idempotent — skip existing completed shards
# ---------------------------------------------------------------------------


class TestMigrationSkipsExistingShards:
    """AC5: Idempotent on restart — skip existing completed shard dirs."""

    def test_migration_skips_existing_completed_shards(self, tmp_path):
        """Pre-existing shard dir present, verify not rebuilt."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024, _Q2_2024]
        vectors = np.random.rand(2, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        # Pre-create Q1 shard with a sentinel file to verify it is NOT rebuilt
        pre_existing_shard = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        pre_existing_shard.mkdir(parents=True, exist_ok=True)
        sentinel = pre_existing_shard / "sentinel_do_not_overwrite.txt"
        sentinel.write_text("original")
        # Write a minimal collection_meta.json to satisfy shard-exists check
        with open(pre_existing_shard / "collection_meta.json", "w") as f:
            json.dump({"name": pre_existing_shard.name}, f)

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        # Sentinel file must still exist (shard was skipped, not rebuilt)
        assert sentinel.exists(), (
            "Pre-existing shard was overwritten — idempotency violated"
        )
        assert sentinel.read_text() == "original"


# ---------------------------------------------------------------------------
# AC3: JSON files are copied, not hard-linked
# ---------------------------------------------------------------------------


class TestMigrationCopiesJsonFilesNotHardlinks:
    """JSON payload files must be shutil.copy2(), not os.link()."""

    def test_migration_copies_not_hardlinks_json_files(self, tmp_path):
        """Verify each shard's JSON files have distinct inode from originals."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024]
        vectors = np.random.rand(1, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        # Collect inodes of original JSON files
        coll_dir = index_path / collection_name
        original_inodes = {
            f.stat().st_ino
            for f in coll_dir.rglob("*.json")
            if f.name != "collection_meta.json"
        }

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        # Collect inodes of JSON files in shard dirs
        shard_inodes = set()
        for shard_dir in index_path.iterdir():
            if not shard_dir.is_dir():
                continue
            if not (
                shard_dir.name.startswith("code-indexer-temporal-")
                and any(shard_dir.name.endswith(f"Q{q}") for q in range(1, 5))
            ):
                continue
            for f in shard_dir.rglob("*.json"):
                if f.name != "collection_meta.json":
                    shard_inodes.add(f.stat().st_ino)

        # No shard inode should match any original inode
        overlap = original_inodes & shard_inodes
        assert len(overlap) == 0, (
            f"Hard links detected — inodes {overlap} appear in both source and shard"
        )


# ---------------------------------------------------------------------------
# AC3 step 6: migration_complete.marker written after all shards
# ---------------------------------------------------------------------------


class TestMigrationCompleteMarker:
    """migration_complete.marker must be written after all shards complete."""

    def test_migration_complete_marker_written_after_all_shards(self, tmp_path):
        """Verify migration_complete.marker exists in monolithic dir after migration."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024, _Q2_2024]
        vectors = np.random.rand(2, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        marker = index_path / collection_name / "migration_complete.marker"
        assert marker.exists(), "migration_complete.marker was not written"


# ---------------------------------------------------------------------------
# AC3 step 7: monolithic HNSW and id_index deleted after success
# ---------------------------------------------------------------------------


class TestMigrationDeletesMonolithicBinaries:
    """Monolithic hnsw_index.bin and id_index.bin must be deleted after success."""

    def test_migration_deletes_monolithic_hnsw_and_id_index_after_success(
        self, tmp_path
    ):
        """Verify hnsw_index.bin and id_index.bin removed from monolithic dir."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024]
        vectors = np.random.rand(1, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        coll_dir = index_path / collection_name
        assert (coll_dir / "hnsw_index.bin").exists()
        assert (coll_dir / "id_index.bin").exists()

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        assert not (coll_dir / "hnsw_index.bin").exists(), (
            "hnsw_index.bin was not deleted"
        )
        assert not (coll_dir / "id_index.bin").exists(), "id_index.bin was not deleted"


# ---------------------------------------------------------------------------
# AC3 step 8: monolithic JSON payload files deleted after success
# ---------------------------------------------------------------------------


class TestMigrationDeletesMonolithicJsonFiles:
    """Monolithic JSON payload files deleted after all shards complete."""

    def test_migration_deletes_monolithic_json_files_after_success(self, tmp_path):
        """Verify JSON files removed from monolithic dir (each exists in quarterly shards)."""
        index_path = tmp_path / "index"
        dim = 4
        timestamps = [_Q1_2024, _Q2_2024]
        vectors = np.random.rand(2, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection(index_path, collection_name, vectors, timestamps)

        # Verify JSON files exist before migration
        coll_dir = index_path / collection_name
        original_jsons = [
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        ]
        assert len(original_jsons) > 0, "Test setup: expected JSON payload files"

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            progress_callback=None,
        )

        # JSON payload files in monolithic dir must be gone
        remaining_jsons = [
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        ]
        assert len(remaining_jsons) == 0, (
            f"Monolithic JSON files not cleaned up: {[str(f) for f in remaining_jsons]}"
        )


# ---------------------------------------------------------------------------
# AC-git-1: _extract_sha_from_point_id
# ---------------------------------------------------------------------------


class TestExtractShaFromPointId:
    """SHA extraction from real-format point_ids."""

    def setup_method(self):
        from code_indexer.services.temporal.temporal_migration_service import (
            _extract_sha_from_point_id,
        )

        self._extract_sha_from_point_id = _extract_sha_from_point_id

    def test_extracts_sha_from_commit_point_id(self):
        """Real commit point_id: {repo}:commit:{sha}:{idx}."""
        pid = "code-indexer:commit:2421d586942eb5c4eca700fbf6bfc0c99af679ef:0"
        sha = self._extract_sha_from_point_id(pid)
        assert sha == "2421d586942eb5c4eca700fbf6bfc0c99af679ef"

    def test_extracts_sha_from_diff_point_id(self):
        """Real diff point_id: {repo}:diff:{sha}:{file_path}:{chunk_idx}."""
        pid = "code-indexer:diff:136c68705c5edc62b068d4e4492df383944c7486:plans/roadmap.md:12"
        sha = self._extract_sha_from_point_id(pid)
        assert sha == "136c68705c5edc62b068d4e4492df383944c7486"

    def test_returns_none_for_synthetic_test_ids(self):
        """Synthetic test point_ids like 'commit:abc0000:file.py:0' have no repo prefix."""
        pid = "commit:abc0000:file.py:0"
        # Third colon-field is 'file.py', not a valid 40-char hex SHA
        sha = self._extract_sha_from_point_id(pid)
        assert sha is None

    def test_returns_none_for_too_few_parts(self):
        """Point_ids with fewer than 4 colon-separated parts return None."""
        assert self._extract_sha_from_point_id("only:two") is None
        assert self._extract_sha_from_point_id("one") is None
        assert self._extract_sha_from_point_id("") is None

    def test_returns_none_for_non_hex_sha(self):
        """A 40-char non-hex string at position 2 returns None."""
        pid = "repo:commit:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ:0"
        assert self._extract_sha_from_point_id(pid) is None

    def test_returns_none_for_wrong_sha_length(self):
        """A hex string at position 2 that is not 40 chars returns None."""
        pid = "repo:commit:abc123:0"
        assert self._extract_sha_from_point_id(pid) is None


# ---------------------------------------------------------------------------
# AC-git-2: _batch_get_commit_timestamps uses real git
# ---------------------------------------------------------------------------


def _make_git_repo_with_commits(tmp_path: Path, n: int):
    """Create a real git repo with n commits, return (repo_path, [sha, ...])."""
    import subprocess

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
    for i in range(n):
        (repo_path / f"file{i}.txt").write_text(f"content {i}")
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
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


class TestBatchGetCommitTimestamps:
    """Batch git-based timestamp lookup using a real git repo."""

    def test_returns_timestamps_for_real_commits(self, tmp_path):
        """Create a real git repo with a commit, verify timestamp returned."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 1)
        result = _batch_get_commit_timestamps(repo_path, {shas[0]})

        assert shas[0] in result
        assert isinstance(result[shas[0]], datetime)
        assert result[shas[0]].tzinfo is not None  # must be timezone-aware

    def test_returns_empty_for_nonexistent_sha(self, tmp_path):
        """Unknown SHA returns empty result (not a crash)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path, _ = _make_git_repo_with_commits(tmp_path, 1)
        fake_sha = "a" * 40
        result = _batch_get_commit_timestamps(repo_path, {fake_sha})
        assert fake_sha not in result

    def test_returns_empty_for_nonexistent_repo_path(self, tmp_path):
        """Non-existent repo path returns empty dict without raising."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        no_repo = tmp_path / "no_such_repo"
        result = _batch_get_commit_timestamps(no_repo, {"a" * 40})
        assert result == {}

    def test_batch_handles_multiple_commits(self, tmp_path):
        """Multiple commits in one batch call, all timestamps returned."""
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 3)
        result = _batch_get_commit_timestamps(repo_path, set(shas))

        assert len(result) == 3
        for sha in shas:
            assert sha in result
            assert isinstance(result[sha], datetime)


# ---------------------------------------------------------------------------
# AC-git-3: Migration with empty JSON files (production scenario)
# ---------------------------------------------------------------------------


def _build_monolithic_collection_empty_json(
    index_path: Path,
    collection_name: str,
    vectors: "np.ndarray",
    sha_list: list,
    space: str = "cosine",
) -> Path:
    """Build a monolithic collection with real-format point_ids and empty JSON files.

    Mirrors production data format:
    - point_ids are myrepo:commit:{sha}:{idx}
    - JSON payload files exist but contain only {}
    - Timestamps must come from git log, not from JSON
    """
    import hnswlib

    coll_dir = index_path / collection_name
    coll_dir.mkdir(parents=True, exist_ok=True)

    n = len(vectors)
    dim = vectors.shape[1]

    hnsw_idx = hnswlib.Index(space=space, dim=dim)
    hnsw_idx.init_index(max_elements=n, M=16, ef_construction=200, allow_replace_deleted=True)
    hnsw_idx.add_items(vectors, np.arange(n))
    hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

    id_mapping = {}
    id_index = {}

    for i, sha in enumerate(sha_list):
        point_id = f"myrepo:commit:{sha}:{i}"
        rel_path = f"{i:02x}/vector_{i}.json"
        json_path = coll_dir / rel_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        # Production format: empty JSON object
        json_path.write_text("{}")
        id_mapping[str(i)] = point_id
        id_index[point_id] = rel_path

    bin_path = coll_dir / "id_index.bin"
    with open(bin_path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)

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


class TestMigrationWithEmptyJsonFilesUsesGit:
    """Production scenario: empty JSON files, timestamps from git."""

    def test_migration_with_empty_json_groups_by_quarter_via_git(self, tmp_path):
        """Vectors with empty JSON payloads; timestamps come from git log."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        # All commits happen now (same quarter) -- expect exactly 1 shard
        repo_path, shas = _make_git_repo_with_commits(tmp_path, 4)

        index_path = tmp_path / "index"
        index_path.mkdir()
        dim = 4
        vectors = np.random.rand(4, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection_empty_json(
            index_path=index_path,
            collection_name=collection_name,
            vectors=vectors,
            sha_list=shas,
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
            progress_callback=None,
        )

        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 1, (
            f"Expected 1 shard (all commits in same quarter), "
            f"got {[d.name for d in quarterly_shards]}"
        )

    def test_migration_repo_path_derived_from_index_path_when_not_provided(self, tmp_path):
        """When repo_path not given, derived as index_path/../../ (standard layout)."""
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        # Standard layout: {repo_root}/.code-indexer/index/
        # _make_git_repo_with_commits puts the repo at tmp_path/gitrepo
        # We need the index inside that same dir: tmp_path/gitrepo/.code-indexer/index
        repo_path, shas = _make_git_repo_with_commits(tmp_path, 1)

        index_path = repo_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        dim = 4
        vectors = np.random.rand(1, dim).astype(np.float32)
        collection_name = "code-indexer-temporal-voyage_code_3"

        _build_monolithic_collection_empty_json(
            index_path=index_path,
            collection_name=collection_name,
            vectors=vectors,
            sha_list=shas,
        )

        # Call WITHOUT explicit repo_path -- must derive from index_path.parent.parent
        run_temporal_migration(
            index_path=index_path,
            repo_alias="myrepo",
        )

        quarterly_shards = [
            d
            for d in index_path.iterdir()
            if d.is_dir()
            and d.name.startswith("code-indexer-temporal-")
            and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
        ]
        assert len(quarterly_shards) == 1
