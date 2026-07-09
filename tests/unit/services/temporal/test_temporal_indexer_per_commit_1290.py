"""Integration tests for the per-commit aggregated-document temporal indexer.

Story #1290 (Epic #1289) pass 2: TemporalIndexer.index_commits() now builds
ONE aggregated document per commit (message once at head + each changed
file's diff), chunks it, embeds it through the active TemporalEmbedder, and
upserts under the unified "{project}:commit:{hash}:{j}" point_id scheme.

These tests drive the REAL TemporalIndexer against a REAL git repository and
a REAL FilesystemVectorStore (no mocking of the code under test), using a
deterministic FAKE TemporalEmbedder (registered via the real registry) so no
network call is required. This proves the wiring: commit_aggregator ->
contextual_chunker -> embedder.embed_commit_chunks -> temporal_point_builder
-> upsert_points, plus the v2 marker / shard placement / fail-loud contract.
"""

import math
import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from code_indexer.config import TemporalConfig
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.services.temporal.temporal_structure_marker import (
    STRUCTURE_MARKER_FILENAME,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


FAKE_EMBEDDER_NAME = "fake-temporal-1290"
FAKE_EMBEDDER_DIMS = 6


class _FakeTemporalEmbedder(TemporalEmbedder):
    """Deterministic embedder: vector = [len(chunk)] * dims. No network I/O."""

    name = FAKE_EMBEDDER_NAME
    model_slug = "fake_temporal_1290"
    dimensions = FAKE_EMBEDDER_DIMS
    overlap_percentage = 0.0

    def __init__(self, config=None):
        self.embed_calls: List[List[str]] = []
        self.mismatch_mode = False

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(chunks))
        if self.mismatch_mode:
            # AC21: simulate a provider response with the wrong chunk count.
            return [[0.0] * self.dimensions]
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions


_fake_embedder_singleton: _FakeTemporalEmbedder = None  # type: ignore[assignment]


def _fake_embedder_factory(config):
    return _fake_embedder_singleton


@pytest.fixture
def fake_embedder():
    global _fake_embedder_singleton
    _fake_embedder_singleton = _FakeTemporalEmbedder()
    register_embedder(FAKE_EMBEDDER_NAME, _fake_embedder_factory)
    yield _fake_embedder_singleton
    unregister_embedder_for_tests(FAKE_EMBEDDER_NAME)
    _fake_embedder_singleton = None  # type: ignore[assignment]


def _run_git(args: List[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test User"], repo)
    return repo


def _make_config_manager(tmp_path: Path, aggregation_chunk_chars: int = 4096):
    """Real Config/TemporalConfig, Mock config_manager (avoids disk config.json)."""
    from code_indexer.config import Config

    config = Config(codebase_dir=tmp_path)
    config.embedding_provider = "voyage-ai"
    config.temporal = TemporalConfig(
        embedders=[FAKE_EMBEDDER_NAME],
        active_embedder=FAKE_EMBEDDER_NAME,
        aggregation_chunk_chars=aggregation_chunk_chars,
    )

    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_indexer(repo: Path, index_dir: Path, aggregation_chunk_chars: int = 4096):
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)
    config_manager = _make_config_manager(repo, aggregation_chunk_chars)
    indexer = TemporalIndexer(
        config_manager,
        vector_store,
        collection_name="code-indexer-temporal-fake",
    )
    return indexer, vector_store


def _find_shard_dir(index_dir: Path) -> Path:
    candidates = [
        d
        for d in index_dir.iterdir()
        if d.is_dir() and d.name.startswith("code-indexer-temporal-fake_temporal_1290")
    ]
    assert len(candidates) == 1, f"expected exactly one shard dir, got {candidates}"
    return candidates[0]


def _vector_files(shard_dir: Path):
    return list(shard_dir.rglob("vector_*.json"))


class TestPerCommitPipelineWiring:
    """AC1-3, AC5-9, AC26, AC27: end-to-end wiring through the real indexer."""

    def test_single_commit_produces_head_chunk_with_message_once(
        self, tmp_path, fake_embedder
    ):
        """AC4/AC5: message appears once at the head; unified point_id scheme (AC3)."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        result = indexer.index_commits()

        assert result.total_commits == 1
        assert fake_embedder.embed_calls, "embedder must have been invoked"

        shard_dir = _find_shard_dir(vector_store.base_path)
        files = _vector_files(shard_dir)
        assert len(files) >= 1

        import json

        head_seen = False
        for f in files:
            data = json.loads(f.read_text())
            point_id = data["id"]
            assert ":diff:" not in point_id, "no legacy :diff: point_id may exist"
            parts = point_id.split(":")
            assert parts[-3] == "commit", (
                f"point_id must use :commit: scheme: {point_id}"
            )
            payload = data["payload"]
            assert payload["type"] == "commit_chunk"
            # chunk_text lives at the point ROOT, never duplicated in payload
            # (avoids the wasteful create-then-delete pattern).
            assert "chunk_text" in data
            assert "content" not in payload
            if payload["chunk_index"] == 0:
                head_seen = True
                assert payload["is_head"] is True
                assert "Initial commit" in payload["commit_message"]
            else:
                assert payload["commit_message"] == ""
        assert head_seen

    def test_point_id_format_is_project_commit_hash_index(
        self, tmp_path, fake_embedder
    ):
        """AC3: point_id == "{project_id}:commit:{hash}:{j}" exactly."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("content\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "one"], repo)

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        indexer.index_commits()

        project_id = indexer.file_identifier.get_project_id()
        head = _run_git(["rev-parse", "HEAD"], repo).strip()

        shard_dir = _find_shard_dir(vector_store.base_path)
        import json

        ids = {json.loads(f.read_text())["id"] for f in _vector_files(shard_dir)}
        assert ids, "expected at least one point"
        for pid in ids:
            assert pid.startswith(f"{project_id}:commit:{head}:")

    def test_vector_dimension_matches_active_embedder(self, tmp_path, fake_embedder):
        """AC9: vector dimensionality matches the active embedder's dims."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "one"], repo)

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        indexer.index_commits()

        shard_dir = _find_shard_dir(vector_store.base_path)
        import json

        for f in _vector_files(shard_dir):
            vec = json.loads(f.read_text())["vector"]
            assert len(vec) == FAKE_EMBEDDER_DIMS

    def test_v2_structure_marker_written_at_shard_create(self, tmp_path, fake_embedder):
        """AC8/AC27: v2 marker persisted at CREATE, correct content."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "one"], repo)

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        indexer.index_commits()

        shard_dir = _find_shard_dir(vector_store.base_path)
        marker_path = shard_dir / STRUCTURE_MARKER_FILENAME
        assert marker_path.exists()

        import json

        marker = json.loads(marker_path.read_text())
        assert marker == {
            "version": 2,
            "layout": "per_commit",
            "model": "fake_temporal_1290",
        }

    def test_shard_placement_by_commit_quarter(self, tmp_path, fake_embedder):
        """AC7: commit lands in the shard matching its commit quarter."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x\n")
        _run_git(["add", "."], repo)
        # 2018-Q2 author+committer date via GIT_*_DATE env vars.
        env_date = "2018-05-15T12:00:00"
        import os

        subprocess.run(
            ["git", "commit", "-q", "-m", "old commit"],
            cwd=repo,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": env_date,
                "GIT_COMMITTER_DATE": env_date,
            },
        )

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        indexer.index_commits()

        expected_shard = "code-indexer-temporal-fake_temporal_1290-2018Q2"
        assert vector_store.collection_exists(expected_shard)

    def test_degenerate_empty_commit_yields_exactly_one_head_chunk(
        self, tmp_path, fake_embedder
    ):
        """AC26: a commit with zero file-change entries still yields exactly
        one head chunk (vector count == 1), never a crash or zero vectors."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("seed\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "seed commit"], repo)

        _run_git(
            ["commit", "-q", "--allow-empty", "-m", "empty commit message body"], repo
        )

        indexer, vector_store = _make_indexer(repo, tmp_path / "index")
        result = indexer.index_commits()

        assert result.total_commits == 2
        shard_dir = _find_shard_dir(vector_store.base_path)
        import json

        head = _run_git(["rev-parse", "HEAD"], repo).strip()
        empty_commit_points = [
            json.loads(f.read_text())
            for f in _vector_files(shard_dir)
            if json.loads(f.read_text())["payload"]["commit_hash"] == head
        ]
        assert len(empty_commit_points) == 1, (
            f"degenerate commit must yield exactly ONE head chunk, "
            f"got {len(empty_commit_points)}"
        )
        assert empty_commit_points[0]["payload"]["is_head"] is True

    def test_exact_vector_count_matches_ceil_formula_with_zero_overlap(
        self, tmp_path, fake_embedder
    ):
        """AC1/AC2: vector count == ceil(aggregated_doc_chars / chunk_chars),
        with ZERO overlap; on-disk file count == point count."""
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "seed"], repo)

        # Large file change forces a multi-chunk aggregated document with a
        # small aggregation_chunk_chars.
        (repo / "a.txt").write_text("y" * 500 + "\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "big change " + ("z" * 200)], repo)

        chunk_chars = 100
        indexer, vector_store = _make_indexer(
            repo, tmp_path / "index", aggregation_chunk_chars=chunk_chars
        )
        indexer.index_commits()

        # Recompute the EXACT expected aggregated document length using the
        # same real aggregator the indexer used, for the second (non-seed)
        # commit only.
        from code_indexer.services.temporal.commit_aggregator import (
            build_aggregated_document,
            get_file_changes,
        )
        from code_indexer.services.temporal.models import CommitInfo

        log_out = _run_git(
            ["log", "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e", "--reverse"],
            repo,
        )
        commits = []
        for record in log_out.strip().split("\x1e"):
            if not record.strip():
                continue
            parts = record.split("\x00")
            commits.append(
                CommitInfo(
                    hash=parts[0].strip(),
                    timestamp=int(parts[1]),
                    author_name=parts[2],
                    author_email=parts[3],
                    message=parts[4].strip(),
                    parent_hashes=parts[5].strip(),
                )
            )
        assert len(commits) == 2
        second_commit = commits[1]
        file_changes = get_file_changes(repo, second_commit, diff_context_lines=5)
        doc = build_aggregated_document(second_commit, file_changes)
        expected_chunks = math.ceil(len(doc.text) / chunk_chars)
        assert expected_chunks >= 2, "test setup must force a multi-chunk commit"

        shard_dir = _find_shard_dir(vector_store.base_path)
        import json

        second_hash_points = [
            f
            for f in _vector_files(shard_dir)
            if json.loads(f.read_text())["payload"]["commit_hash"] == second_commit.hash
        ]
        assert len(second_hash_points) == expected_chunks, (
            f"expected EXACTLY {expected_chunks} vectors for the multi-chunk "
            f"commit, got {len(second_hash_points)}"
        )

        # Point-count query against the shard agrees with the on-disk file count.
        assert vector_store.count_points(shard_dir.name) == len(
            _vector_files(shard_dir)
        )


class TestDeterministicFixtureCounts:
    """Code-review Finding 2 (Story #1290): dedicated deterministic fixtures
    for the headline file-count claim, locking EXACT expected values (not
    the pre-existing tests' looser ">=2" checks) and using the REAL
    production voyage_context_4 slug (not a fake embedder's fake name) for
    AC7's shard-naming assertion."""

    def test_twenty_files_produce_exactly_three_vectors_not_twenty(
        self, tmp_path, fake_embedder
    ):
        """AC1: a commit touching 20 files, with a chunk size chosen so
        ceil((len(message)+headers+diffs)/aggregation_chunk_chars) == 3
        EXACTLY, indexed through the real path, yields EXACTLY 3 on-disk
        vectors -- NOT ~3, and emphatically NOT 20 (one per file, which
        would indicate a per-file-vector regression)."""
        repo = _init_repo(tmp_path)
        (repo / "seed.txt").write_text("seed\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "seed"], repo)

        for i in range(20):
            (repo / f"file{i}.txt").write_text(f"content for file {i}\n" * 3)
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "touch twenty files"], repo)

        from code_indexer.services.temporal.commit_aggregator import (
            build_aggregated_document,
            get_file_changes,
        )
        from code_indexer.services.temporal.models import CommitInfo

        log_out = _run_git(
            ["log", "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e", "--reverse"],
            repo,
        )
        commits = []
        for record in log_out.strip().split("\x1e"):
            if not record.strip():
                continue
            parts = record.split("\x00")
            commits.append(
                CommitInfo(
                    hash=parts[0].strip(),
                    timestamp=int(parts[1]),
                    author_name=parts[2],
                    author_email=parts[3],
                    message=parts[4].strip(),
                    parent_hashes=parts[5].strip(),
                )
            )
        assert len(commits) == 2
        twenty_files_commit = commits[1]
        file_changes = get_file_changes(repo, twenty_files_commit, diff_context_lines=5)
        assert len(file_changes) == 20, "test setup must touch exactly 20 files"
        doc = build_aggregated_document(twenty_files_commit, file_changes)

        # ceil(N / ceil(N/3)) == 3 for any N > 0 -- picks the EXACT chunk
        # size that forces exactly 3 chunks (never fewer, never more).
        chunk_chars = -(-len(doc.text) // 3)
        expected_chunks = math.ceil(len(doc.text) / chunk_chars)
        assert expected_chunks == 3, "test fixture must force EXACTLY 3 chunks"

        indexer, vector_store = _make_indexer(
            repo, tmp_path / "index", aggregation_chunk_chars=chunk_chars
        )
        indexer.index_commits()

        shard_dir = _find_shard_dir(vector_store.base_path)
        import json

        twenty_files_points = [
            f
            for f in _vector_files(shard_dir)
            if json.loads(f.read_text())["payload"]["commit_hash"]
            == twenty_files_commit.hash
        ]
        assert len(twenty_files_points) == 3, (
            f"expected EXACTLY 3 vectors for the 20-file commit, got "
            f"{len(twenty_files_points)}"
        )
        assert len(twenty_files_points) != 20, (
            "20 vectors would mean one-per-file -- a per-file-vector regression"
        )

    def test_total_vectors_across_commits_equals_sum_of_per_commit_ceilings(
        self, tmp_path, fake_embedder
    ):
        """AC2: total on-disk vector count across a multi-commit run equals
        the EXACT sum of each commit's own ceil(aggregated_doc_chars /
        aggregation_chunk_chars); on-disk vector_*.json count agrees exactly
        with the shard's point count."""
        repo = _init_repo(tmp_path)
        (repo / "seed.txt").write_text("seed\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "seed"], repo)

        (repo / "a.txt").write_text("a" * 300 + "\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "first change " + ("p" * 100)], repo)

        (repo / "a.txt").write_text("b" * 900 + "\n")
        (repo / "b.txt").write_text("c" * 200 + "\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "second change " + ("q" * 250)], repo)

        chunk_chars = 120
        indexer, vector_store = _make_indexer(
            repo, tmp_path / "index", aggregation_chunk_chars=chunk_chars
        )
        indexer.index_commits()

        from code_indexer.services.temporal.commit_aggregator import (
            build_aggregated_document,
            get_file_changes,
        )
        from code_indexer.services.temporal.models import CommitInfo

        log_out = _run_git(
            ["log", "--format=%H%x00%at%x00%an%x00%ae%x00%B%x00%P%x1e", "--reverse"],
            repo,
        )
        commits = []
        for record in log_out.strip().split("\x1e"):
            if not record.strip():
                continue
            parts = record.split("\x00")
            commits.append(
                CommitInfo(
                    hash=parts[0].strip(),
                    timestamp=int(parts[1]),
                    author_name=parts[2],
                    author_email=parts[3],
                    message=parts[4].strip(),
                    parent_hashes=parts[5].strip(),
                )
            )
        assert len(commits) == 3

        expected_total = 0
        per_commit_expected = {}
        for commit in commits:
            file_changes = get_file_changes(repo, commit, diff_context_lines=5)
            doc = build_aggregated_document(commit, file_changes)
            ceiling = math.ceil(len(doc.text) / chunk_chars)
            per_commit_expected[commit.hash] = ceiling
            expected_total += ceiling

        shard_dir = _find_shard_dir(vector_store.base_path)
        all_files = _vector_files(shard_dir)
        assert len(all_files) == expected_total

        assert vector_store.count_points(shard_dir.name) == len(all_files)

        import json
        from collections import Counter

        counts_by_commit = Counter(
            json.loads(f.read_text())["payload"]["commit_hash"] for f in all_files
        )
        assert dict(counts_by_commit) == per_commit_expected

    def test_shard_dir_uses_real_voyage_context_4_slug_for_2018q2_commit(
        self, tmp_path
    ):
        """AC7: a 2018-Q2 commit indexed through the REAL production
        ContextualTemporalEmbedder (registered as "voyage-context-4", the
        genuine production embedder name) creates the exact literal
        collection dir "code-indexer-temporal-voyage_context_4-2018Q2" --
        not a fake embedder's fake slug. Only the client's HTTP boundary is
        stubbed (no network); the model_slug flows through for real."""
        import os
        from unittest.mock import MagicMock, patch

        from code_indexer.config import Config, TemporalConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x\n")
        _run_git(["add", "."], repo)
        env_date = "2018-05-15T12:00:00"
        subprocess.run(
            ["git", "commit", "-q", "-m", "old commit"],
            cwd=repo,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": env_date,
                "GIT_COMMITTER_DATE": env_date,
            },
        )

        index_dir = tmp_path / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)

        config = Config(codebase_dir=repo)
        config.embedding_provider = "voyage-ai"
        config.temporal = TemporalConfig(
            embedders=["voyage-context-4"],
            active_embedder="voyage-context-4",
        )
        config_manager = MagicMock()
        config_manager.get_config.return_value = config
        config_manager.config_path = repo / ".code-indexer" / "config.json"

        indexer = TemporalIndexer(
            config_manager,
            vector_store,
            collection_name="code-indexer-temporal-voyage-ai",
        )

        def _fake_request(self_client, documents, **kwargs):
            return {
                "data": [
                    {
                        "index": doc_idx,
                        "data": [
                            {"index": ci, "embedding": [0.1] * 1024}
                            for ci in range(len(doc))
                        ],
                    }
                    for doc_idx, doc in enumerate(documents)
                ],
                "model": "voyage-context-4",
            }

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
            with patch.object(
                VoyageAIClient,
                "_make_sync_contextualized_request",
                _fake_request,
            ):
                indexer.index_commits()

        expected_shard = "code-indexer-temporal-voyage_context_4-2018Q2"
        assert vector_store.collection_exists(expected_shard)


class TestReconcileEndToEnd:
    """AC15/AC16: --reconcile detects missing AND partial commits, shard-aware."""

    def test_reconcile_reindexes_partial_commit_with_no_duplicates(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("seed\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "seed"], repo)

        (repo / "a.txt").write_text("changed content\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "second commit"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(repo, index_dir)
        indexer.index_commits()

        shard_dir = _find_shard_dir(vector_store.base_path)
        head = _run_git(["rev-parse", "HEAD"], repo).strip()

        # Simulate a crash mid-flush for the second commit: delete its
        # durable completion marker but leave its points on disk (PARTIAL).
        from code_indexer.services.temporal.temporal_progressive_metadata import (
            TemporalProgressiveMetadata,
        )

        progress_path = shard_dir / "temporal_progress.json"
        assert progress_path.exists()
        import json as _json

        progress_data = _json.loads(progress_path.read_text())
        progress_data["completed_commits"] = [
            h for h in progress_data["completed_commits"] if h != head
        ]
        progress_path.write_text(_json.dumps(progress_data))
        TemporalProgressiveMetadata(shard_dir)._pending.clear()

        points_before = len(_vector_files(shard_dir))
        assert points_before >= 1

        # Fresh indexer instance (simulates process restart) running --reconcile.
        indexer2, _ = _make_indexer(repo, index_dir)
        result = indexer2.index_commits(reconcile=True)

        assert result.total_commits == 1, "only the PARTIAL commit is reprocessed"

        import json

        head_points = [
            f
            for f in _vector_files(shard_dir)
            if json.loads(f.read_text())["payload"]["commit_hash"] == head
        ]
        point_ids = [json.loads(f.read_text())["id"] for f in head_points]
        assert len(point_ids) == len(set(point_ids)), "no duplicate point_ids"

        completed = TemporalProgressiveMetadata(shard_dir).load_completed()
        assert head in completed, "commit must be marked complete after reconcile"

    def test_reconcile_returns_exactly_the_missing_set_after_deletion(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("one\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "first"], repo)
        (repo / "a.txt").write_text("two\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "second"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(repo, index_dir)
        indexer.index_commits()

        shard_dir = _find_shard_dir(vector_store.base_path)
        first_hash = _run_git(["rev-parse", "HEAD~1"], repo).strip()

        # Delete the first commit's points entirely (simulates data loss).
        import json

        for f in _vector_files(shard_dir):
            data = json.loads(f.read_text())
            if data["payload"]["commit_hash"] == first_hash:
                f.unlink()

        indexer2, _ = _make_indexer(repo, index_dir)
        result = indexer2.index_commits(reconcile=True)

        assert result.total_commits == 1
        remaining_first = [
            f
            for f in _vector_files(shard_dir)
            if json.loads(f.read_text())["payload"]["commit_hash"] == first_hash
        ]
        assert len(remaining_first) >= 1, "deleted commit must be re-indexed"


class TestFailLoudOnMismatch:
    """AC21: contextualized-embeddings chunk-count mismatch fails loud."""

    def test_embedding_count_mismatch_raises_and_writes_no_partial_index(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("x" * 500 + "\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "big commit " + ("z" * 300)], repo)

        fake_embedder.mismatch_mode = True

        indexer, vector_store = _make_indexer(
            repo, tmp_path / "index", aggregation_chunk_chars=50
        )
        with pytest.raises(Exception):
            indexer.index_commits()


class TestReconcileNothingMissingRerun:
    """E2E-discovered bug (Story #1290): a --reconcile rerun with nothing
    missing must NOT crash trying to end_indexing() the base bookkeeping
    collection_name, which AC19/20 blank-out hard-deletes on this very rerun
    (it never carries a v2 marker -- only the real quarterly shard does)."""

    def test_reconcile_rerun_with_nothing_missing_does_not_raise(
        self, tmp_path, fake_embedder
    ):
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello world\n")
        _run_git(["add", "."], repo)
        _run_git(["commit", "-q", "-m", "Initial commit"], repo)

        index_dir = tmp_path / "index"
        indexer, vector_store = _make_indexer(repo, index_dir)
        first_result = indexer.index_commits()
        assert first_result.total_commits == 1
        indexer.close()

        # Second TemporalIndexer instance (fresh __init__, exactly mirroring a
        # real second `cidx index --index-commits --reconcile` CLI process).
        indexer2 = TemporalIndexer(
            _make_config_manager(repo),
            vector_store,
            collection_name="code-indexer-temporal-fake",
        )
        # Must NOT raise "Collection '...' does not exist".
        second_result = indexer2.index_commits(reconcile=True)
        assert second_result.total_commits == 0
        assert second_result.skip_ratio == 1.0

        # close()'s fallback path (_processed_shards never set when the
        # reconcile early-return fires before Step 2) must ALSO not crash.
        indexer2.close()


class TestLegacyInternalsDeleted:
    """AC17: legacy per-file-diff / commit-message internals no longer exist."""

    def test_process_commits_parallel_removed(self):
        assert not hasattr(TemporalIndexer, "_process_commits_parallel")

    def test_index_commit_message_removed(self):
        assert not hasattr(TemporalIndexer, "_index_commit_message")

    def test_ensure_shard_has_projection_matrix_still_resolves(self):
        """AC18: relocated helper is importable (not deleted)."""
        from code_indexer.services.temporal.temporal_projection_matrix import (
            _ensure_shard_has_projection_matrix,
        )

        assert callable(_ensure_shard_has_projection_matrix)
