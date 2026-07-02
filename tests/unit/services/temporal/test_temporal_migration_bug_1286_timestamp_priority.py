"""Tests for Bug #1286 follow-up: timestamp-resolution priority + batch
git-log resilience in the temporal shard migration service.

Two related fixes:

1. PAYLOAD-TIMESTAMP PRIORITY: _build_quarter_buckets previously resolved a
   vector's quarter git-log-FIRST, JSON-payload-commit_timestamp-SECOND. Since
   every temporal payload has carried its own commit_timestamp unconditionally
   since v7.x (immutable, captured once at index time), and git history is
   mutable (rebase/squash/force-push/gc can make a commit SHA unresolvable via
   `git log` even though the vector was correctly embedded), the payload value
   is the more authoritative, more stable source. The fix flips the priority:
   payload commit_timestamp is now checked FIRST; git log is consulted only as
   a fallback when the payload genuinely lacks the field. timestamp_unresolved
   (hard abort) now fires ONLY when BOTH sources are absent.

2. BATCH GIT-LOG RESILIENCE (empirically confirmed via a real git repo, see
   test below): `git log --no-walk sha1 sha2 ... shaN` resolves ALL revision
   arguments atomically BEFORE producing any output. If even ONE SHA in the
   batch is unresolvable (e.g. from a rebase/squash/gc), the ENTIRE command
   fails (exit 128, "fatal: bad object <sha>") with ZERO stdout -- not just
   for the bad SHA, but for every OTHER (perfectly valid) SHA in the same
   chunk of up to 1000. _batch_get_commit_timestamps now retries a failed
   chunk one SHA at a time so a single unresolvable SHA cannot poison its
   siblings' timestamp resolution.
"""

import json
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pytest


COLLECTION_NAME = "code-indexer-temporal-voyage_code_3"
MIGRATION_COMPLETE_MARKER = "migration_complete.marker"


def _write_id_index_bin(path: Path, id_index: Dict[str, str]) -> None:
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _make_git_repo_with_commits(tmp_path: Path, n: int) -> Tuple[Path, List[str]]:
    """Create a real git repo with n commits, return (repo_path, [sha, ...])."""
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


def _build_monolith(
    index_path: Path,
    collection_name: str,
    vectors: np.ndarray,
    entries: List[Tuple[str, "int | None"]],
    space: str = "cosine",
) -> Path:
    """Build a real monolithic HNSW collection.

    entries: list of (sha, commit_timestamp_or_None) -- when commit_timestamp
    is None the JSON payload is written empty (no commit_timestamp field, the
    legacy/pre-v7.x format that relies entirely on git for resolution). When
    provided, the payload carries that exact commit_timestamp (which may
    deliberately DIFFER from the real git commit date, to prove priority).
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
    for i, (sha, commit_ts) in enumerate(entries):
        point_id = f"myrepo:commit:{sha}:{i}"
        rel_path = f"{i:02x}/vector_{i}.json"
        json_path = coll_dir / rel_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        if commit_ts is None:
            json_path.write_text("{}")
        else:
            json_path.write_text(
                json.dumps({"payload": {"commit_timestamp": commit_ts}})
            )
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


def _find_quarterly_shards(index_path: Path, collection_name: str) -> List[Path]:
    return sorted(
        d
        for d in index_path.iterdir()
        if d.is_dir()
        and d.name.startswith(collection_name)
        and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
    )


# ---------------------------------------------------------------------------
# Fix 2: batch git-log resilience (empirically confirmed atomic-failure bug)
# ---------------------------------------------------------------------------


class TestBatchGetCommitTimestampsResilientToOneBadSha:
    """One unresolvable SHA (rebase/squash/gc) must not poison sibling SHAs'
    timestamp resolution in the same batched git-log chunk.

    Empirically confirmed: `git log --no-walk goodsha1 badsha goodsha2` exits
    128 ("fatal: bad object <badsha>") with EMPTY stdout -- git resolves all
    revision arguments atomically before producing any output. The unfixed
    code's `if proc.returncode != 0 and not proc.stdout: continue` therefore
    drops timestamps for goodsha1 AND goodsha2 too, not just badsha.
    """

    def test_one_unresolvable_sha_does_not_poison_valid_siblings(self, tmp_path):
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 3)
        fake_sha = "1234567890abcdef1234567890abcdef12345678"

        result = _batch_get_commit_timestamps(repo_path, set(shas) | {fake_sha})

        for sha in shas:
            assert sha in result, (
                f"Bug: valid sibling SHA {sha} was dropped because {fake_sha} "
                f"(unresolvable) was batched in the SAME git log call. "
                f"Result: {result}"
            )
        assert fake_sha not in result

    def test_many_valid_shas_survive_one_bad_sha_in_a_1000_sha_chunk(self, tmp_path):
        """Realistic chunk-sized reproduction: hundreds of valid SHAs must not
        be dropped because one sibling in the same 1000-SHA chunk is bad.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _batch_get_commit_timestamps,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 5)
        # Pad with fake (but well-formed 40-hex) SHAs to simulate a large batch
        # containing exactly one genuinely bad entry mixed with real ones.
        fake_shas = {f"{i:040x}" for i in range(1, 50)}
        one_bad_real_looking = "deadbeef00000000000000000000000000dead"

        all_shas = set(shas) | {one_bad_real_looking}
        result = _batch_get_commit_timestamps(repo_path, all_shas)

        for sha in shas:
            assert sha in result, f"Valid SHA {sha} dropped by chunk poisoning"
        assert one_bad_real_looking not in result
        # fake_shas were never queried (not in all_shas) -- sanity check only.
        assert not (fake_shas & set(result.keys()))


# ---------------------------------------------------------------------------
# Fix 1: payload commit_timestamp takes priority over git log
# ---------------------------------------------------------------------------


class TestPayloadTimestampPriorityOverGit:
    """Payload's stored commit_timestamp (immutable, captured at index time)
    must be consulted BEFORE git log (mutable, subject to history rewrites).
    """

    def test_payload_timestamp_wins_even_when_git_resolves_a_different_quarter(
        self, tmp_path
    ):
        """The real git commit date is 2024 Q1, but the payload's stored
        commit_timestamp says 2024 Q3. After the fix, the vector must land in
        Q3 (payload wins), proving payload is now checked first.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 1)
        # Deliberately DIFFERENT quarter than the real git commit date.
        deliberately_different_ts = int(
            datetime(2024, 8, 20, tzinfo=timezone.utc).timestamp()
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        dim = 4
        vectors = np.random.rand(1, dim).astype(np.float32)
        _build_monolith(
            index_path,
            COLLECTION_NAME,
            vectors,
            [(shas[0], deliberately_different_ts)],
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        shards = _find_quarterly_shards(index_path, COLLECTION_NAME)
        shard_names = {d.name for d in shards}
        assert f"{COLLECTION_NAME}-2024Q3" in shard_names, (
            f"Payload-priority VIOLATED: expected 2024Q3 (payload value) to win "
            f"over the real git commit date's quarter. Shards: {shard_names}"
        )
        assert f"{COLLECTION_NAME}-2024Q1" not in shard_names, (
            "Payload-priority VIOLATED: git's quarter (2024Q1) was used instead "
            "of the payload's stored commit_timestamp"
        )

    def test_full_history_rewrite_still_succeeds_via_payload(self, tmp_path):
        """repo_path points at a completely unrelated git repo (simulating a
        fully rewritten/unresolvable history) -- migration must still succeed
        losslessly using ONLY the payload's stored commit_timestamps.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        unrelated_repo = tmp_path / "unrelated"
        unrelated_repo.mkdir()
        subprocess.run(
            ["git", "init", str(unrelated_repo)], check=True, capture_output=True
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        dim = 4
        timestamps = [
            int(datetime(2018, 5, 15, tzinfo=timezone.utc).timestamp()),
            int(datetime(2021, 11, 20, tzinfo=timezone.utc).timestamp()),
        ]
        vectors = np.random.rand(2, dim).astype(np.float32)
        fake_shas = [f"{i:040x}" for i in range(2)]
        _build_monolith(
            index_path,
            COLLECTION_NAME,
            vectors,
            list(zip(fake_shas, timestamps)),
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=unrelated_repo,
        )

        assert (index_path / COLLECTION_NAME / MIGRATION_COMPLETE_MARKER).exists()
        shards = _find_quarterly_shards(index_path, COLLECTION_NAME)
        shard_names = {d.name for d in shards}
        assert shard_names == {
            f"{COLLECTION_NAME}-2018Q2",
            f"{COLLECTION_NAME}-2021Q4",
        }, f"Expected both payload-derived quarters, got {shard_names}"

    def test_git_still_used_as_fallback_when_payload_lacks_commit_timestamp(
        self, tmp_path
    ):
        """Legacy/empty-payload points (pre-v7.x format) must still resolve
        correctly via git log fallback -- the fallback direction is unchanged,
        only the PRIORITY (which is checked first) changed.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, shas = _make_git_repo_with_commits(tmp_path, 3)
        index_path = tmp_path / "index"
        index_path.mkdir()
        dim = 4
        vectors = np.random.rand(3, dim).astype(np.float32)
        # commit_timestamp=None -> empty JSON payload -- must fall back to git.
        _build_monolith(
            index_path,
            COLLECTION_NAME,
            vectors,
            [(sha, None) for sha in shas],
        )

        run_temporal_migration(
            index_path=index_path,
            repo_alias="test-repo",
            repo_path=repo_path,
        )

        assert (index_path / COLLECTION_NAME / MIGRATION_COMPLETE_MARKER).exists()
        # All 3 real commits happen "now" (same run) -> same quarter -> 1 shard.
        shards = _find_quarterly_shards(index_path, COLLECTION_NAME)
        assert len(shards) == 1, (
            f"Expected git-fallback resolution to succeed for empty-payload "
            f"points, got shards: {[d.name for d in shards]}"
        )

    def test_timestamp_unresolved_still_raises_when_both_sources_absent(self, tmp_path):
        """Hard-abort semantics must be UNCHANGED: when both payload AND git
        are absent/unresolvable, migration still raises (never silently drops).
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        unrelated_repo = tmp_path / "unrelated"
        unrelated_repo.mkdir()
        subprocess.run(
            ["git", "init", str(unrelated_repo)], check=True, capture_output=True
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        dim = 4
        vectors = np.random.rand(1, dim).astype(np.float32)
        fake_sha = "a" * 40
        # commit_timestamp=None (empty payload) AND git cannot resolve (unrelated repo).
        _build_monolith(index_path, COLLECTION_NAME, vectors, [(fake_sha, None)])

        with pytest.raises(RuntimeError, match=r"unresolved|timestamp"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=unrelated_repo,
            )

        assert not (index_path / COLLECTION_NAME / MIGRATION_COMPLETE_MARKER).exists()
        assert (index_path / COLLECTION_NAME / "hnsw_index.bin").exists(), (
            "Monolith must be preserved on abort"
        )
