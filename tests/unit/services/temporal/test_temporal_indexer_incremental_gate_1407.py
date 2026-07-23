"""Bug #1407: integration tests for TemporalIndexer.index_commits()'s new
incremental-gate orchestration -- the automatic (non-reconcile) path must
skip the expensive full multi-shard disk-scan reconcile when an embedder is
already caught up, while remaining crash-safe (durable stale barrier) and
never regressing the explicit operator --reconcile full-repair path.

Reuses the REAL TemporalIndexer + REAL git repo + REAL FilesystemVectorStore
harness established by test_temporal_indexer_multi_embedder_1291.py (no
mocking of the code under test), with deterministic FAKE TemporalEmbedder
adapters registered via the real registry (no network call required).
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from code_indexer.config import Config, TemporalConfig
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)
from code_indexer.services.temporal.temporal_collection_naming import quarter_suffix
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


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


def _commit_file(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content)
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", message], repo)


class _FakeEmbedder(TemporalEmbedder):
    """Deterministic embedder: vector = [len(chunk)] * dims. No network I/O."""

    def __init__(
        self,
        name: str,
        model_slug: str,
        dims: int,
        overlap_percentage: float = 0.0,
        available: bool = True,
        fail_after: int = -1,
    ):
        self.name = name
        self.model_slug = model_slug
        self.dimensions = dims
        self.overlap_percentage = overlap_percentage
        self._available = available
        self.embed_calls: List[List[str]] = []
        self._fail_after = fail_after  # -1 = never fail

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(chunks))
        if self._fail_after >= 0 and len(self.embed_calls) > self._fail_after:
            raise RuntimeError("simulated crash mid-commit-processing")
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions

    def is_available(self) -> bool:
        return self._available


@pytest.fixture
def fake_embedder():
    embedder = _FakeEmbedder("fake-gate-1407", "fake_gate_1407", dims=6)
    register_embedder("fake-gate-1407", lambda config, e=embedder: e)
    yield embedder
    unregister_embedder_for_tests("fake-gate-1407")


def _make_config_manager(tmp_path: Path, embedders: List[str], active_embedder: str):
    config = Config(codebase_dir=tmp_path)
    config.embedding_provider = "voyage-ai"
    config.temporal = TemporalConfig(
        embedders=embedders,
        active_embedder=active_embedder,
        aggregation_chunk_chars=4096,
    )
    config_manager_get_config_target = config
    from unittest.mock import MagicMock

    config_manager = MagicMock()
    config_manager.get_config.return_value = config_manager_get_config_target
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_indexer(repo: Path, index_dir: Path, embedders: List[str], active: str):
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)
    config_manager = _make_config_manager(repo, embedders, active)
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


class TestNoOpRefreshSkipsFullDiskScan:
    """The core perf claim at the index_commits() level: a second,
    fully-caught-up run must do ZERO work on the shard -- proven by the
    real on-disk index_rebuild_uuid/mtimes being byte-identical before and
    after (the only things that could change them are
    rebuild_from_vectors()/save_incremental_update(), neither of which run
    unless begin_indexing/end_indexing are invoked for that shard)."""

    def test_second_run_with_no_new_commits_never_rescans_vectors(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        _commit_file(repo, "a.txt", "hello\n", "Initial commit")

        index_dir = tmp_path / "index"
        indexer1, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            result1 = indexer1.index_commits()
            assert result1.total_commits == 1
        finally:
            indexer1.close()

        shard = _shard_dirs_for_slug(index_dir, "fake_gate_1407")[0]
        meta_before = json.loads((shard / "collection_meta.json").read_text())
        uuid_before = meta_before["hnsw_index"]["index_rebuild_uuid"]
        id_index_mtime_before = (shard / "id_index.bin").stat().st_mtime_ns

        fake_embedder.embed_calls.clear()
        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            result2 = indexer2.index_commits()
        finally:
            indexer2.close()

        meta_after = json.loads((shard / "collection_meta.json").read_text())
        uuid_after = meta_after["hnsw_index"]["index_rebuild_uuid"]
        id_index_mtime_after = (shard / "id_index.bin").stat().st_mtime_ns

        assert uuid_after == uuid_before, (
            "no-op tick must not rebuild/incrementally-update the HNSW "
            "index -- index_rebuild_uuid must be untouched"
        )
        assert id_index_mtime_after == id_index_mtime_before, (
            "no-op tick must not rewrite id_index.bin (which only happens "
            "via end_indexing(), which this shard must never enter)"
        )
        assert fake_embedder.embed_calls == []
        assert result2.skip_ratio == 1.0


class TestNewCommitIncrementalUpdate:
    def test_new_commit_after_clean_run_is_indexed(self, tmp_path, fake_embedder):
        repo = _init_repo(tmp_path)
        _commit_file(repo, "a.txt", "hello\n", "Initial commit")

        index_dir = tmp_path / "index"
        indexer1, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer1.index_commits()
        finally:
            indexer1.close()

        _commit_file(repo, "b.txt", "world\n", "Second commit")
        fake_embedder.embed_calls.clear()

        indexer2, vector_store = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            result2 = indexer2.index_commits()
        finally:
            indexer2.close()

        assert result2.total_commits == 1
        assert fake_embedder.embed_calls, "new commit must be embedded"

        shard = _shard_dirs_for_slug(index_dir, "fake_gate_1407")[0]
        hnsw_manager = HNSWIndexManager(vector_dim=6, space="cosine")
        assert hnsw_manager.is_stale(shard) is False


class TestPhysicallyStaleShardHealedOnNextRun:
    """Crash-injection proxy: a shard left is_stale=True (simulating a crash
    between the durable barrier's mark_stale and clear_stale) with ZERO new
    commits must self-heal on the next run -- clear_stale() reached."""

    def test_stale_shard_with_no_new_commits_is_repaired_next_run(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        _commit_file(repo, "a.txt", "hello\n", "Initial commit")

        index_dir = tmp_path / "index"
        indexer1, vector_store = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer1.index_commits()
        finally:
            indexer1.close()

        shard = _shard_dirs_for_slug(index_dir, "fake_gate_1407")[0]
        hnsw_manager = HNSWIndexManager(vector_dim=6, space="cosine")
        hnsw_manager.mark_stale(shard)  # simulate crash-left-stale
        assert hnsw_manager.is_stale(shard) is True

        fake_embedder.embed_calls.clear()
        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer2.index_commits()
        finally:
            indexer2.close()

        assert hnsw_manager.is_stale(shard) is False
        # Zero new commits -- embedder was NOT invoked for embedding work,
        # only the shard's HNSW was rescanned/republished.
        assert fake_embedder.embed_calls == []


class TestCrashMidCommitProcessingStaysStale:
    """A fault during commit processing (embed_commit_chunks raises) must
    leave the shard stale -- end_indexing()/clear_stale() are never
    reached, proving the mark_stale-before / clear_stale-after ordering.
    Uses a real injected collaborator fault (not a mock of production
    code)."""

    def test_embedder_failure_mid_commit_leaves_shard_stale(self, tmp_path):
        failing_embedder = _FakeEmbedder(
            "fake-crash-1407", "fake_crash_1407", dims=6, fail_after=0
        )
        register_embedder("fake-crash-1407", lambda config, e=failing_embedder: e)
        try:
            repo = _init_repo(tmp_path)
            _commit_file(repo, "a.txt", "hello\n", "commit 1")
            _commit_file(repo, "b.txt", "world\n", "commit 2")

            index_dir = tmp_path / "index"
            indexer, vector_store = _make_indexer(
                repo, index_dir, ["fake-crash-1407"], "fake-crash-1407"
            )

            with pytest.raises(RuntimeError):
                indexer.index_commits()

            quarter = quarter_suffix(datetime.now(timezone.utc))
            shard_dir = index_dir / f"code-indexer-temporal-fake_crash_1407-{quarter}"
            hnsw_manager = HNSWIndexManager(vector_dim=6, space="cosine")
            assert hnsw_manager.is_stale(shard_dir) is True
        finally:
            unregister_embedder_for_tests("fake-crash-1407")


class TestOperatorReconcileStillFullRepairs:
    def test_reconcile_repairs_corrupted_shard_and_ends_non_stale(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        _commit_file(repo, "a.txt", "hello\n", "Initial commit")

        index_dir = tmp_path / "index"
        indexer1, vector_store = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer1.index_commits()
        finally:
            indexer1.close()

        shard = _shard_dirs_for_slug(index_dir, "fake_gate_1407")[0]
        # Corrupt: drop completed marker so the commit looks PARTIAL.
        progress_path = shard / "temporal_progress.json"
        data = json.loads(progress_path.read_text())
        data["completed_commits"] = []
        progress_path.write_text(json.dumps(data))

        fake_embedder.embed_calls.clear()
        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            result = indexer2.index_commits(reconcile=True)
        finally:
            indexer2.close()

        assert fake_embedder.embed_calls, "reconcile must re-embed the partial commit"
        assert result.total_commits >= 1
        hnsw_manager = HNSWIndexManager(vector_dim=6, space="cosine")
        assert hnsw_manager.is_stale(shard) is False

    def test_reconcile_rebuilds_shard_that_was_already_stale_with_complete_commits(
        self, tmp_path, fake_embedder
    ):
        """HIGH-severity defect: a shard left is_stale=True by a REAL prior
        crash (e.g. completed_commits flushed, but the HNSW rebuild never
        finished) has commits that look fully COMPLETE to
        reconcile_temporal_index (marker present, points present) -- so it
        is absent from missing_shard_map. --reconcile must still force a
        rebuild for this shard (it was already broken coming in), never
        silently clear_stale() it as if the pre-scan's own mark_stale() was
        the only thing marking it -- that would permanently bless a
        genuinely inconsistent on-disk index as fresh and destroy the
        self-heal signal for good.
        """
        repo = _init_repo(tmp_path)
        _commit_file(repo, "a.txt", "hello\n", "Initial commit")

        index_dir = tmp_path / "index"
        indexer1, vector_store = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer1.index_commits()  # clean, complete, non-stale finalize
        finally:
            indexer1.close()

        shard = _shard_dirs_for_slug(index_dir, "fake_gate_1407")[0]
        # Do NOT corrupt the completion marker -- the commit stays fully
        # COMPLETE from reconcile_temporal_index's point of view. Simulate
        # the real crash window instead: mark the shard stale directly, as
        # if a crash hit after completed_commits was flushed but before the
        # HNSW rebuild finished.
        hnsw_manager = HNSWIndexManager(vector_dim=6, space="cosine")
        hnsw_manager.mark_stale(shard)
        assert hnsw_manager.is_stale(shard) is True

        meta_before = json.loads((shard / "collection_meta.json").read_text())
        uuid_before = meta_before["hnsw_index"]["index_rebuild_uuid"]

        fake_embedder.embed_calls.clear()
        indexer2, _ = _make_indexer(
            repo, index_dir, ["fake-gate-1407"], "fake-gate-1407"
        )
        try:
            indexer2.index_commits(reconcile=True)
        finally:
            indexer2.close()

        # The shard must end up FRESH...
        assert hnsw_manager.is_stale(shard) is False
        # ...but ONLY because it was genuinely rebuilt, not because the
        # pre-existing incoming staleness was silently cleared.
        meta_after = json.loads((shard / "collection_meta.json").read_text())
        uuid_after = meta_after["hnsw_index"]["index_rebuild_uuid"]
        assert uuid_after != uuid_before, (
            "shard must be force-rebuilt (new index_rebuild_uuid), not "
            "just clear_stale()'d in place"
        )
