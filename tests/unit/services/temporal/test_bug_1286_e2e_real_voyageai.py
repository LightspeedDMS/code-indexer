"""Real end-to-end tests for GitHub issue #1286 (Story #1172 temporal shard
migration data loss / false marker / destructive finalize / expensive recovery).

Satisfies the issue's "Definition of Done -- Comprehensive E2E Test Criteria"
(E2E-1 through E2E-8). Uses REAL VoyageAI embeddings (via .local-testing /
VOYAGE_API_KEY), REAL git repositories, REAL hnswlib HNSW indexes, and REAL
filesystem I/O. Nothing about the migration function, the embedding provider,
HNSW, or the filesystem is mocked -- only a call-COUNTING wrapper is layered
around the real VoyageAIClient.get_embeddings_batch method (it still delegates
to the real implementation whenever invoked; it is used purely to PROVE zero
calls happen on the migration/recovery paths).

Automatically skipped when no VoyageAI API key is available, matching the
project's established real-API-test convention (see
test_embedded_voyage_tokenizer_cache.py's TestIntegrationCacheVsNetwork
skipif pattern).
"""

import json
import os
import re
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Real VoyageAI API key resolution (never hardcode the secret value itself)
# ---------------------------------------------------------------------------


def _read_voyage_api_key() -> Optional[str]:
    """Resolve a real VoyageAI API key from env or the gitignored .local-testing
    file (project convention: "Credentials from .local-testing or env").
    Never hardcode the secret value in source -- read it at test-collection time.
    """
    key = os.environ.get("VOYAGE_API_KEY") or os.environ.get("E2E_VOYAGE_API_KEY")
    if key:
        return key
    project_root = Path(__file__).resolve().parents[4]
    local_testing = project_root / ".local-testing"
    if not local_testing.exists():
        return None
    try:
        text = local_testing.read_text()
    except OSError:
        return None
    m = re.search(r"^E2E_VOYAGE_API_KEY=(\S+)$", text, re.MULTILINE)
    return m.group(1) if m else None


_VOYAGE_API_KEY = _read_voyage_api_key()

pytestmark = pytest.mark.skipif(
    not _VOYAGE_API_KEY,
    reason=(
        "Real VoyageAI API key not available (.local-testing / VOYAGE_API_KEY "
        "env) -- Bug #1286 E2E-1..8 require genuine embedding-provider calls"
    ),
)


# ---------------------------------------------------------------------------
# Real fixture-construction helpers (git repo + real VoyageAI embeddings +
# real hnswlib monolith on disk -- established pattern from the sibling
# test_temporal_migration_*.py files in this same directory)
# ---------------------------------------------------------------------------

COLLECTION_NAME = "code-indexer-temporal-voyage_code_3"
MIGRATION_COMPLETE_MARKER = "migration_complete.marker"

# Quarters chosen to be clearly distinct and to explicitly include an OLD
# (historical) quarter -- the exact class that went missing in production.
_OLD_DATE = "2018-05-15T09:00:00+00:00"  # 2018Q2
_MID_DATE = "2021-11-20T14:30:00+00:00"  # 2021Q4
_RECENT_DATE_1 = "2025-01-10T08:00:00+00:00"  # 2025Q1
_RECENT_DATE_2 = "2025-01-25T16:45:00+00:00"  # 2025Q1 (same quarter as above)
_LATEST_DATE = "2026-02-01T11:15:00+00:00"  # 2026Q1


def _make_git_repo_with_dated_commits(
    tmp_path: Path, date_iso_strs: List[str]
) -> Tuple[Path, List[str]]:
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


def _commit_new_file(
    repo_path: Path, filename: str, content: str, date_str: str
) -> str:
    """Add+commit a single new file at a specific date; return the new SHA.

    Uses the module-level `subprocess`, `os`, and `Path` imports already
    present at the top of this file (same pattern as
    _make_git_repo_with_dated_commits above).
    """
    (repo_path / filename).write_text(content)
    subprocess.run(
        ["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True
    )
    env = dict(os.environ)
    env["GIT_COMMITTER_DATE"] = date_str
    env["GIT_AUTHOR_DATE"] = date_str
    subprocess.run(
        ["git", "commit", "-m", f"add {filename}"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


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


def _embed_real(texts: List[str]) -> np.ndarray:
    """Call the REAL VoyageAI embedding provider (genuine HTTP request) and
    return a (N, dim) float32 array. This is the ONLY network call in the
    fixture-construction phase; migration/recovery must make ZERO further calls.
    """
    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    assert _VOYAGE_API_KEY is not None  # pytestmark skips the module otherwise
    os.environ["VOYAGE_API_KEY"] = _VOYAGE_API_KEY
    client = VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))
    vectors = client.get_embeddings_batch(texts, embedding_purpose="document")
    return np.array(vectors, dtype=np.float32)


def _iso_to_epoch(iso_str: str) -> int:
    """Parse an ISO-8601 date string (e.g. _OLD_DATE) to a UNIX epoch int."""
    return int(datetime.fromisoformat(iso_str).timestamp())


def _build_monolithic_collection_with_vectors(
    index_path: Path,
    collection_name: str,
    vectors: np.ndarray,
    shas: List[str],
    space: str = "cosine",
    commit_timestamps: Optional[List[int]] = None,
) -> Path:
    """Build a real monolithic HNSW collection using genuinely-embedded vectors.

    Bug #1286 follow-up: payload commit_timestamp is now the PRIMARY
    timestamp source (see _build_quarter_buckets). commit_timestamps lets
    callers supply per-entry values matching the real git commit dates used
    to build `shas` (via _iso_to_epoch), so payload and git agree, as a real
    indexer would produce. Defaults to "now" when the caller does not care
    about specific quarters. Must be the same length as shas when provided.
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
        json_path.write_text(
            json.dumps({"id": point_id, "payload": {"commit_timestamp": commit_ts}})
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


def _shard_dirs(index_path: Path, collection_name: str) -> List[Path]:
    return sorted(
        d
        for d in index_path.iterdir()
        if d.is_dir()
        and d.name.startswith(collection_name)
        and any(d.name.endswith(f"Q{q}") for q in range(1, 5))
    )


class _EmbedCallCounter:
    """Context manager wrapping VoyageAIClient.get_embeddings_batch with a
    counting shim that STILL delegates to the real implementation. Used to
    PROVE zero calls occur during migration/recovery, never to fake results.
    """

    def __init__(self) -> None:
        self.count = 0
        self._patcher: Optional[Any] = None

    def __enter__(self) -> "_EmbedCallCounter":
        from code_indexer.services.voyage_ai import VoyageAIClient

        real_method = VoyageAIClient.get_embeddings_batch
        counter = self

        def _counting_wrapper(self_client, texts, *args, **kwargs):
            counter.count += len(texts)
            return real_method(self_client, texts, *args, **kwargs)

        self._patcher = patch.object(
            VoyageAIClient, "get_embeddings_batch", _counting_wrapper
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._patcher is not None
        self._patcher.stop()


# ---------------------------------------------------------------------------
# E2E-1, E2E-2, E2E-3, E2E-7, E2E-8: full lifecycle with real embeddings
# ---------------------------------------------------------------------------


class TestE2EFullMigrationLifecycle:
    """One coherent real end-to-end flow: real git repo (5 commits across 4
    quarters incl. an OLD quarter) -> real VoyageAI embeddings -> real
    monolithic HNSW on disk -> real migration -> exhaustive verification.
    """

    def test_e2e_1_2_3_7_8_lossless_all_quarters_no_reembed_query_parity_idempotent(
        self, tmp_path, caplog
    ):
        import logging
        import hnswlib
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        dates = [_OLD_DATE, _MID_DATE, _RECENT_DATE_1, _RECENT_DATE_2, _LATEST_DATE]
        repo_path, shas = _make_git_repo_with_dated_commits(tmp_path, dates)

        texts = [
            f"diff --git a/file{i}.txt b/file{i}.txt\n"
            f"+def handler_{i}():\n+    return {i}\n"
            for i in range(len(shas))
        ]
        vectors = _embed_real(texts)
        assert vectors.shape == (5, 1024), (
            f"Expected 5 real voyage-code-3 embeddings of dim 1024, "
            f"got shape {vectors.shape}"
        )

        index_path = tmp_path / "index"
        index_path.mkdir()
        coll_dir = _build_monolithic_collection_with_vectors(
            index_path,
            COLLECTION_NAME,
            vectors,
            shas,
            commit_timestamps=[_iso_to_epoch(d) for d in dates],
        )

        # Capture exact pre-migration point_id -> vector mapping via REAL hnswlib.
        pre_idx = hnswlib.Index(space="cosine", dim=1024)
        pre_idx.load_index(str(coll_dir / "hnsw_index.bin"), max_elements=100)
        with open(coll_dir / "collection_meta.json") as f:
            pre_meta = json.load(f)
        pre_id_mapping = {
            int(k): v for k, v in pre_meta["hnsw_index"]["id_mapping"].items()
        }
        pre_labels = sorted(pre_id_mapping.keys())
        pre_vectors_by_point_id = {
            pre_id_mapping[label]: vec
            for label, vec in zip(pre_labels, pre_idx.get_items(pre_labels))
        }
        assert len(pre_vectors_by_point_id) == 5

        # ---- E2E-3 (migration half): zero embedding-provider calls -------
        migration_logger_name = (
            "code_indexer.services.temporal.temporal_migration_service"
        )
        with _EmbedCallCounter() as counter:
            with caplog.at_level(logging.DEBUG, logger=migration_logger_name):
                run_temporal_migration(
                    index_path=index_path,
                    repo_alias="test-repo",
                    repo_path=repo_path,
                )

        assert counter.count == 0, (
            f"E2E-3 VIOLATED: migration made {counter.count} real "
            f"embedding-provider call(s) -- vectors must come from "
            f"hnswlib.get_items()/JSON only, never re-embedded"
        )

        # ---- E2E-1: losslessness ------------------------------------------
        skip_msgs = [
            r.getMessage()
            for r in caplog.records
            if "skipping" in r.getMessage().lower()
        ]
        assert skip_msgs == [], f"E2E-1 VIOLATED: 'skipping' log(s) found: {skip_msgs}"

        assert (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "Migration must have completed successfully"
        )
        assert not (coll_dir / "hnsw_index.bin").exists()

        shards = _shard_dirs(index_path, COLLECTION_NAME)
        total_migrated = 0
        post_vectors_by_point_id: Dict[str, List[float]] = {}
        for shard_dir in shards:
            with open(shard_dir / "collection_meta.json") as f:
                shard_meta = json.load(f)
            shard_id_mapping = {
                int(k): v for k, v in shard_meta["hnsw_index"]["id_mapping"].items()
            }
            shard_idx = hnswlib.Index(space="cosine", dim=1024)
            shard_idx.load_index(str(shard_dir / "hnsw_index.bin"), max_elements=100)
            labels = sorted(shard_id_mapping.keys())
            shard_vectors = shard_idx.get_items(labels)
            for label, vec in zip(labels, shard_vectors):
                post_vectors_by_point_id[shard_id_mapping[label]] = vec
            total_migrated += len(labels)

        assert total_migrated == 5, (
            f"E2E-1 count-in==count-out VIOLATED: expected 5, got {total_migrated}"
        )
        assert set(post_vectors_by_point_id.keys()) == set(
            pre_vectors_by_point_id.keys()
        ), "E2E-1 VIOLATED: point_id set mismatch pre vs post migration"

        for point_id, pre_vec in pre_vectors_by_point_id.items():
            post_vec = post_vectors_by_point_id[point_id]
            np.testing.assert_allclose(
                np.array(pre_vec, dtype=np.float32),
                np.array(post_vec, dtype=np.float32),
                rtol=1e-5,
                atol=1e-6,
                err_msg=(
                    f"E2E-1 VIOLATED: vector for {point_id} differs after "
                    f"migration (extracted-not-re-embedded guarantee broken)"
                ),
            )

        # ---- E2E-2: every quarter present, INCLUDING the old quarter -----
        shard_names = {d.name for d in shards}
        assert f"{COLLECTION_NAME}-2018Q2" in shard_names, (
            f"E2E-2 VIOLATED: OLD quarter (2018Q2) shard missing. "
            f"Shards present: {shard_names}"
        )
        expected_quarters = {"2018Q2", "2021Q4", "2025Q1", "2026Q1"}
        actual_quarters = {name.rsplit("-", 1)[-1] for name in shard_names}
        assert actual_quarters == expected_quarters, (
            f"E2E-2 VIOLATED: expected quarters {expected_quarters}, "
            f"got {actual_quarters}"
        )

        # ---- E2E-7: query parity (monolith vs shards) ---------------------
        probe_point_id = pre_id_mapping[0]  # the OLD-quarter commit's vector
        probe_vector = pre_vectors_by_point_id[probe_point_id]

        # Query parity is proven structurally: the exact same vector that was
        # retrievable from the monolith (pre_vectors_by_point_id) is now
        # retrievable, byte-for-byte, from its quarterly shard
        # (post_vectors_by_point_id) -- already asserted above via
        # np.testing.assert_allclose. Additionally run a REAL nearest-neighbor
        # search against the shard containing this point and confirm the
        # probe vector's own point_id is the top-1 self-match result.
        old_shard_dir = index_path / f"{COLLECTION_NAME}-2018Q2"
        vector_store = FilesystemVectorStore(
            base_path=index_path, project_root=repo_path
        )
        results = vector_store.search(
            query="unused-because-precomputed",
            embedding_provider=object(),
            collection_name=old_shard_dir.name,
            limit=1,
            precomputed_query_vector=list(probe_vector),
        )
        assert isinstance(results, list) and len(results) == 1, (
            f"E2E-7 VIOLATED: expected exactly 1 search result, got {results}"
        )
        assert results[0]["id"] == probe_point_id, (
            f"E2E-7 VIOLATED: query parity broken -- expected top-1 result "
            f"'{probe_point_id}', got '{results[0]['id']}'"
        )

        # ---- E2E-8: idempotency (second run is a true no-op) --------------
        shard_vector_counts_before = {
            d.name: json.loads((d / "collection_meta.json").read_text())["hnsw_index"][
                "vector_count"
            ]
            for d in shards
        }

        with _EmbedCallCounter() as counter2:
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=repo_path,
            )

        assert counter2.count == 0, (
            "E2E-8 VIOLATED: re-running migration must never re-embed"
        )
        shards_after = _shard_dirs(index_path, COLLECTION_NAME)
        assert {d.name for d in shards_after} == shard_names, (
            "E2E-8 VIOLATED: shard set changed on idempotent re-run"
        )
        shard_vector_counts_after = {
            d.name: json.loads((d / "collection_meta.json").read_text())["hnsw_index"][
                "vector_count"
            ]
            for d in shards_after
        }
        assert shard_vector_counts_after == shard_vector_counts_before, (
            "E2E-8 VIOLATED: vector counts changed on idempotent re-run "
            "(drop or duplication)"
        )


# ---------------------------------------------------------------------------
# E2E-4, E2E-5: marker honesty + non-destructive finalize (negative test)
# ---------------------------------------------------------------------------


class TestE2EMarkerHonestyAndNonDestructiveFinalize:
    """Inject one unmatchable point (missing JSON payload) into a REAL,
    real-embedded monolith. Migration must FAIL LOUD, write no marker, and
    leave the monolith fully intact and queryable.
    """

    def test_e2e_4_5_unmatchable_point_aborts_marker_absent_monolith_queryable(
        self, tmp_path
    ):
        import hnswlib
        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        dates = [_OLD_DATE, _RECENT_DATE_1]
        repo_path, shas = _make_git_repo_with_dated_commits(tmp_path, dates)
        texts = [f"content for commit {i}" for i in range(len(shas))]
        vectors = _embed_real(texts)

        index_path = tmp_path / "index"
        index_path.mkdir()
        coll_dir = _build_monolithic_collection_with_vectors(
            index_path,
            COLLECTION_NAME,
            vectors,
            shas,
            commit_timestamps=[_iso_to_epoch(d) for d in dates],
        )

        # Inject a structural orphan: delete one payload JSON (id_index.bin
        # still references it -- missing_json condition).
        payload_files = sorted(
            f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
        )
        assert len(payload_files) == 2
        payload_files[0].unlink()

        with pytest.raises(RuntimeError, match=r"aborted|orphan|structural|vectors"):
            run_temporal_migration(
                index_path=index_path,
                repo_alias="test-repo",
                repo_path=repo_path,
            )

        # E2E-4: no marker written
        assert not (coll_dir / MIGRATION_COMPLETE_MARKER).exists(), (
            "E2E-4 VIOLATED: marker written despite an unmatchable point"
        )
        # E2E-4/5: no shard directories were created at all
        assert _shard_dirs(index_path, COLLECTION_NAME) == [], (
            "E2E-4/5 VIOLATED: shard(s) built despite an aborting unmatchable point"
        )
        # E2E-5: monolith fully intact and queryable
        assert (coll_dir / "hnsw_index.bin").exists()
        assert (coll_dir / "id_index.bin").exists()
        idx = hnswlib.Index(space="cosine", dim=1024)
        idx.load_index(str(coll_dir / "hnsw_index.bin"), max_elements=100)
        assert idx.get_current_count() == 2, (
            "E2E-5 VIOLATED: monolith must remain fully queryable after abort"
        )


# ---------------------------------------------------------------------------
# E2E-6: recovery re-extracts from the monolith (zero embed calls)
# ---------------------------------------------------------------------------


class TestE2ERecoveryReExtractsFromMonolith:
    """A fresh, unmigrated, real-embedded monolith is repaired via
    TemporalIndexer's recovery guard (Defect 4) -- zero embedding-provider
    calls, no full git-history walk.
    """

    def test_e2e_6_recovery_extracts_zero_embed_calls(self, tmp_path):
        from unittest.mock import MagicMock
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        dates = [_OLD_DATE, _RECENT_DATE_1]
        repo_path, shas = _make_git_repo_with_dated_commits(tmp_path, dates)
        texts = [f"recovery content {i}" for i in range(len(shas))]
        vectors = _embed_real(texts)

        index_base_path = repo_path / ".code-indexer" / "index"
        _build_monolithic_collection_with_vectors(
            index_base_path,
            COLLECTION_NAME,
            vectors,
            shas,
            commit_timestamps=[_iso_to_epoch(d) for d in dates],
        )

        config_manager = MagicMock()
        config_manager.get_config.return_value = MagicMock(
            embedding_provider="voyage-ai",
            voyage_ai=MagicMock(
                parallel_requests=4, api_key=None, model="voyage-code-3"
            ),
            temporal=MagicMock(diff_context_lines=3),
            file_extensions=None,
            override_config=None,
        )
        config_manager.config_path = repo_path / ".code-indexer" / "config.json"
        vector_store = FilesystemVectorStore(
            base_path=index_base_path, project_root=repo_path
        )

        with patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory:
            mock_factory.get_provider_model_info.return_value = {
                "dimensions": 1024,
                "provider": "voyage-ai",
                "model": "voyage-code-3",
            }
            indexer = TemporalIndexer(
                config_manager, vector_store, collection_name=COLLECTION_NAME
            )

        with _EmbedCallCounter() as counter:
            indexer._recover_from_monolith_if_needed()

        assert counter.count == 0, (
            f"E2E-6 VIOLATED: recovery made {counter.count} real embedding call(s)"
        )
        assert not (index_base_path / COLLECTION_NAME / "hnsw_index.bin").exists(), (
            "E2E-6: monolith must have been consumed by the cheap recovery migration"
        )
        recovered_shards = _shard_dirs(index_base_path, COLLECTION_NAME)
        assert len(recovered_shards) == 2, (
            f"E2E-6 VIOLATED: expected 2 quarterly shards recovered from the "
            f"monolith, got {[d.name for d in recovered_shards]}"
        )


# ---------------------------------------------------------------------------
# Layer B item 2(a)/(b): delta refresh embeds ONLY new commits; no-new-commits
# refresh is a true zero-embed no-op (real git + real config + real VoyageAI)
# ---------------------------------------------------------------------------


def _run_indexed_with_counter(indexer: object) -> Tuple[Any, int]:
    """Run indexer.index_commits() under _EmbedCallCounter; return (result, count)."""
    with _EmbedCallCounter() as counter:
        result = indexer.index_commits()  # type: ignore[attr-defined]
    return result, counter.count


class TestE2EDeltaRefreshOnlyEmbedsNewCommits:
    """A refresh with N genuinely new commits must embed ONLY those N commits
    (never a full-history re-walk); a refresh with zero new commits must make
    ZERO embedding-provider calls (true no-op). Proves the incremental
    last_indexed_commit anchor (temporal_meta.json) and progressive metadata
    (temporal_progress.json) -- preserved across a migration/recovery cycle
    by Defect 3's precise cleanup glob -- correctly drive delta-only indexing
    using the REAL TemporalIndexer, REAL git history, and REAL VoyageAI
    embeddings (no mocks of the indexer, git, or the embedding provider).
    """

    def test_refresh_embeds_only_new_commits_then_noop(self, tmp_path):
        from code_indexer.config import ConfigManager
        from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        assert _VOYAGE_API_KEY is not None
        os.environ["VOYAGE_API_KEY"] = _VOYAGE_API_KEY

        repo_path, shas = _make_git_repo_with_dated_commits(
            tmp_path, [_RECENT_DATE_1, _RECENT_DATE_2]
        )
        # Real production repos exclude the index directory from version
        # control. Without this, the next `git add .` (via _commit_new_file)
        # would pick up the temporal indexer's OWN freshly-written vector
        # payload JSON files (created by the first index_commits() run below)
        # as "new tracked content" in the delta commit, and the diff scanner
        # would (correctly, per its design) embed them -- a test-fixture
        # artifact, not a production defect. Confirmed empirically: without
        # this .gitignore the delta run embeds 17 extra unrelated texts.
        (repo_path / ".gitignore").write_text(".code-indexer/\n")

        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_manager = ConfigManager(config_path=config_dir / "config.json")
        config_manager.create_default_config(codebase_dir=repo_path)
        config_manager.update_config(embedding_provider="voyage-ai")

        index_base_path = repo_path / ".code-indexer" / "index"
        vector_store = FilesystemVectorStore(
            base_path=index_base_path, project_root=repo_path
        )
        indexer = TemporalIndexer(
            config_manager, vector_store, collection_name=COLLECTION_NAME
        )

        result_initial, count_initial = _run_indexed_with_counter(indexer)
        assert result_initial.total_commits == 2
        assert count_initial > 0, "Initial indexing must make real embed calls"

        _commit_new_file(repo_path, "new_file.txt", "brand new content", _RECENT_DATE_2)

        result_delta, count_delta = _run_indexed_with_counter(indexer)
        assert result_delta.total_commits == 1, (
            f"E2E delta-refresh VIOLATED: expected exactly 1 NEW commit "
            f"processed (not the full 3-commit history), got "
            f"{result_delta.total_commits}"
        )
        assert count_delta > 0, "The new commit must be embedded"
        assert count_delta < count_initial + 5, (
            f"Delta refresh made {count_delta} embed calls vs initial "
            f"{count_initial} -- suggests a full-history re-embed"
        )

        result_noop, count_noop = _run_indexed_with_counter(indexer)
        assert count_noop == 0, (
            f"E2E no-op-refresh VIOLATED: {count_noop} real embed call(s) "
            f"on a refresh with zero new commits"
        )
        assert result_noop.total_commits == 0
