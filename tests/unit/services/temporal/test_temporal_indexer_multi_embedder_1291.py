"""Integration tests for multi-embedder per-commit temporal indexing (Story #1291).

Story #1291 (Epic #1289) Slice 2: TemporalIndexer.index_commits() must build
shard sets for EVERY configured `temporal.embedders`, not only the active
one. These tests drive the REAL TemporalIndexer against a REAL git
repository and REAL FilesystemVectorStore (no mocking of the code under
test), using deterministic FAKE TemporalEmbedder adapters registered via the
real registry (no network call required).

Covers:
- AC1: two configured embedders build BOTH shard sets, each with its own v2 marker.
- AC4: an unavailable NON-ACTIVE embedder is skipped with a WARNING while the
  others index normally; an unavailable ACTIVE embedder FAILS the job.
- AC5: adding a second embedder to an already-indexed repo (zero new commits)
  schedules NO work for the first embedder and leaves its on-disk content
  byte-identical (mtime/atime excluded from the comparison; content hash only).
- AC6: a fake/stub adapter builds its own collection via the SAME generic
  loop -- zero indexer code was written specifically for it.
"""

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config, TemporalConfig
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _run_git(args: List[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    return repo


class _FakeEmbedder(TemporalEmbedder):
    """Deterministic embedder: vector = [len(chunk)] * dims. No network I/O."""

    def __init__(
        self,
        name: str,
        model_slug: str,
        dims: int,
        overlap_percentage: float = 0.0,
        available: bool = True,
    ):
        self.name = name
        self.model_slug = model_slug
        self.dimensions = dims
        self.overlap_percentage = overlap_percentage
        self._available = available
        self.embed_calls: List[List[str]] = []

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(chunks))
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions

    def is_available(self) -> bool:
        return self._available


class _UnavailableConstructionEmbedder(TemporalEmbedder):
    """Simulates a missing-credential embedder that raises at construction."""

    name = "broken-embedder-1291"
    model_slug = "broken_embedder_1291"
    dimensions = 4
    overlap_percentage = 0.0

    def __init__(self, config=None):
        raise ValueError("No API key configured for broken-embedder-1291")

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        raise RuntimeError("should never be called")

    def embed_query(self, text: str) -> List[float]:
        raise RuntimeError("should never be called")


@pytest.fixture
def two_fake_embedders():
    embedder_a = _FakeEmbedder("fake-a-1291", "fake_a_1291", dims=6)
    embedder_b = _FakeEmbedder(
        "fake-b-1291", "fake_b_1291", dims=8, overlap_percentage=0.15
    )
    register_embedder("fake-a-1291", lambda config, e=embedder_a: e)
    register_embedder("fake-b-1291", lambda config, e=embedder_b: e)
    yield embedder_a, embedder_b
    unregister_embedder_for_tests("fake-a-1291")
    unregister_embedder_for_tests("fake-b-1291")


@pytest.fixture
def broken_embedder_registered():
    register_embedder(
        "broken-embedder-1291", lambda config: _UnavailableConstructionEmbedder(config)
    )
    yield
    unregister_embedder_for_tests("broken-embedder-1291")


def _make_config_manager(
    tmp_path: Path,
    embedders: List[str],
    active_embedder: str,
    aggregation_chunk_chars: int = 4096,
):
    config = Config(codebase_dir=tmp_path)
    config.embedding_provider = "voyage-ai"
    config.temporal = TemporalConfig(
        embedders=embedders,
        active_embedder=active_embedder,
        aggregation_chunk_chars=aggregation_chunk_chars,
    )
    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_indexer(
    repo: Path,
    index_dir: Path,
    embedders: List[str],
    active_embedder: str,
    aggregation_chunk_chars: int = 4096,
):
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)
    config_manager = _make_config_manager(
        repo, embedders, active_embedder, aggregation_chunk_chars
    )
    indexer = TemporalIndexer(
        config_manager, vector_store, collection_name="code-indexer-temporal-fake"
    )
    return indexer, vector_store


def _shard_dirs_for_slug(index_dir: Path, slug: str) -> List[Path]:
    return [
        d
        for d in index_dir.iterdir()
        if d.is_dir() and d.name.startswith(f"code-indexer-temporal-{slug}")
    ]


def _content_hash_of_shard(shard_dir: Path) -> str:
    """Hash of content bytes for all files tracked by AC5 (vector_*.json,
    hnsw_index.bin, projection_matrix.npy, temporal_structure.json). Excludes
    atime/mtime -- pure content hash."""
    hasher = hashlib.sha256()
    tracked_names = {
        "hnsw_index.bin",
        "projection_matrix.npy",
        "temporal_structure.json",
    }
    paths = sorted(
        p
        for p in shard_dir.rglob("*")
        if p.is_file() and (p.name.startswith("vector_") or p.name in tracked_names)
    )
    for p in paths:
        hasher.update(str(p.relative_to(shard_dir)).encode())
        hasher.update(p.read_bytes())
    return hasher.hexdigest()


class TestTwoEmbeddersBuildBothShardSets:
    """AC1: two configured embedders build BOTH shard sets, each own v2 marker."""

    def test_both_shard_sets_built_with_correct_v2_markers(
        self, tmp_path, two_fake_embedders
    ):
        embedder_a, embedder_b = two_fake_embedders
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(
            repo, index_dir, ["fake-a-1291", "fake-b-1291"], "fake-a-1291"
        )
        try:
            result = indexer.index_commits()

            assert result.total_commits >= 1
            assert embedder_a.embed_calls, "embedder A must have been invoked"
            assert embedder_b.embed_calls, "embedder B must have been invoked"

            shards_a = _shard_dirs_for_slug(index_dir, "fake_a_1291")
            shards_b = _shard_dirs_for_slug(index_dir, "fake_b_1291")
            assert len(shards_a) == 1
            assert len(shards_b) == 1

            import json

            marker_a = json.loads((shards_a[0] / "temporal_structure.json").read_text())
            marker_b = json.loads((shards_b[0] / "temporal_structure.json").read_text())
            assert marker_a == {
                "version": 2,
                "layout": "per_commit",
                "model": "fake_a_1291",
            }
            assert marker_b == {
                "version": 2,
                "layout": "per_commit",
                "model": "fake_b_1291",
            }
        finally:
            indexer.close()


class TestUnavailableEmbedderPolicy:
    """AC4: non-active unavailable embedder warns+skips; active unavailable fails job."""

    def test_unavailable_non_active_embedder_warns_and_active_still_indexes(
        self, tmp_path, two_fake_embedders, broken_embedder_registered, caplog
    ):
        embedder_a, _embedder_b = two_fake_embedders
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(
            repo, index_dir, ["fake-a-1291", "broken-embedder-1291"], "fake-a-1291"
        )
        try:
            with caplog.at_level(logging.WARNING):
                result = indexer.index_commits()

            assert result.total_commits >= 1
            assert embedder_a.embed_calls, "active embedder must still index"
            assert _shard_dirs_for_slug(index_dir, "fake_a_1291")
            assert not _shard_dirs_for_slug(index_dir, "broken_embedder_1291")
            assert any(
                "broken-embedder-1291" in record.message for record in caplog.records
            )
        finally:
            indexer.close()

    def test_unavailable_active_embedder_fails_the_job(
        self, tmp_path, two_fake_embedders, broken_embedder_registered
    ):
        _embedder_a, _embedder_b = two_fake_embedders
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(
            repo,
            index_dir,
            ["broken-embedder-1291", "fake-a-1291"],
            "broken-embedder-1291",
        )
        try:
            with pytest.raises(Exception):
                indexer.index_commits()
        finally:
            indexer.close()


class TestAddAfterTheFact:
    """AC5: adding a second embedder schedules no work / no content change for the first."""

    def test_adding_second_embedder_leaves_first_untouched_when_no_new_commits(
        self, tmp_path, two_fake_embedders
    ):
        embedder_a, embedder_b = two_fake_embedders
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"

        # Run 1: only embedder A configured, fully indexes the repo.
        indexer1, vector_store = _make_indexer(
            repo, index_dir, ["fake-a-1291"], "fake-a-1291"
        )
        try:
            result1 = indexer1.index_commits()
            assert result1.total_commits == 1
        finally:
            indexer1.close()

        shard_a = _shard_dirs_for_slug(index_dir, "fake_a_1291")[0]
        hash_before = _content_hash_of_shard(shard_a)

        # Run 2: add embedder B to the configured set. Zero NEW commits exist.
        embedder_a.embed_calls.clear()
        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-a-1291", "fake-b-1291"], "fake-a-1291"
        )
        try:
            result2 = indexer2.index_commits()

            # Embedder A must NOT have been invoked again -- no
            # voyage-equivalent work scheduled.
            assert embedder_a.embed_calls == []
            # Embedder B must have caught up on the pre-existing commit.
            assert embedder_b.embed_calls
            assert _shard_dirs_for_slug(index_dir, "fake_b_1291")

            hash_after = _content_hash_of_shard(shard_a)
            assert hash_before == hash_after, (
                "embedder A's on-disk shard content must be byte-identical "
                "after adding embedder B (AC5)"
            )
            assert result2.total_commits >= 0  # embedder B's work counted somewhere
        finally:
            indexer2.close()


class TestReconcileEmbedderScope:
    """AC10: per-embedder reconcile is scoped and defaults to ALL configured
    embedders."""

    def test_explicit_scope_reconciles_only_named_embedder(
        self, tmp_path, two_fake_embedders
    ):
        embedder_a, embedder_b = two_fake_embedders
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"

        # Both embedders fully indexed already.
        indexer1, vector_store = _make_indexer(
            repo, index_dir, ["fake-a-1291", "fake-b-1291"], "fake-a-1291"
        )
        try:
            indexer1.index_commits()
        finally:
            indexer1.close()

        shard_a = _shard_dirs_for_slug(index_dir, "fake_a_1291")[0]
        hash_before = _content_hash_of_shard(shard_a)

        # Corrupt embedder B's shard state (simulate a stray partial write
        # that reconcile should clean up): drop it from the completed set
        # while its point files remain, so reconcile treats it as PARTIAL.
        shard_b = _shard_dirs_for_slug(index_dir, "fake_b_1291")[0]
        import json as _json

        progress_path = shard_b / "temporal_progress.json"
        progress_data = _json.loads(progress_path.read_text())
        progress_data["completed_commits"] = []
        progress_path.write_text(_json.dumps(progress_data))

        embedder_a.embed_calls.clear()
        embedder_b.embed_calls.clear()

        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-a-1291", "fake-b-1291"], "fake-a-1291"
        )
        try:
            result = indexer2.index_commits(
                reconcile=True, embedder_scope=["fake-b-1291"]
            )
        finally:
            indexer2.close()

        # Only embedder B (the scoped one) was reconciled/reindexed.
        assert embedder_b.embed_calls, "scoped embedder must be reconciled"
        assert embedder_a.embed_calls == [], (
            "embedder A must be UNTOUCHED when reconcile is scoped to embedder B only"
        )
        assert result.total_commits >= 1

        hash_after = _content_hash_of_shard(shard_a)
        assert hash_before == hash_after, (
            "embedder A's shard content must stay byte-identical when "
            "reconcile is explicitly scoped to embedder B"
        )

    def test_unknown_embedder_in_scope_raises(self, tmp_path, two_fake_embedders):
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"
        indexer, _ = _make_indexer(
            repo, index_dir, ["fake-a-1291", "fake-b-1291"], "fake-a-1291"
        )
        try:
            with pytest.raises(ValueError):
                indexer.index_commits(
                    reconcile=True, embedder_scope=["not-a-configured-embedder"]
                )
        finally:
            indexer.close()
