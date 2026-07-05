"""Regression test for Bug #1296: temporal watch mode called an undefined
TemporalIndexer.index_commits_list() method.

Every pre-existing test for TemporalWatchHandler's incremental-commit path
(tests/unit/cli/test_temporal_incremental_indexing.py) fully mocks
`temporal_indexer`, so `Mock().index_commits_list(...)` silently
auto-vivifies the attribute instead of raising -- these tests could never
have caught that the REAL TemporalIndexer class has no such method. This
module drives the watch handler against a REAL TemporalIndexer (real git
repository, real FilesystemVectorStore, a fake registered TemporalEmbedder
adapter so no network call is required) to reproduce the AttributeError and
then prove the fix actually indexes the new commit through the real
post-#1290 adapter-driven per-commit pipeline.
"""

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, Mock, patch

from code_indexer.cli_temporal_watch_handler import TemporalWatchHandler
from code_indexer.config import Config, TemporalConfig
from code_indexer.services.temporal.embedders.base import TemporalEmbedder
from code_indexer.services.temporal.embedders.registry import (
    register_embedder,
    unregister_embedder_for_tests,
)
from code_indexer.services.temporal.temporal_indexer import TemporalIndexer
from code_indexer.services.temporal.temporal_progressive_metadata import (
    TemporalProgressiveMetadata,
)
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

    def __init__(self, name: str, dims: int = 4):
        self.name = name
        self.model_slug = name.replace("-", "_")
        self.dimensions = dims
        self.overlap_percentage = 0.0
        self.embed_calls: List[List[str]] = []

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        self.embed_calls.append(list(chunks))
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions

    def is_available(self) -> bool:
        return True


EMBEDDER_NAME = "fake-embedder-1296"


def _make_config_manager(tmp_path: Path):
    config = Config(codebase_dir=tmp_path)
    config.embedding_provider = "voyage-ai"
    config.temporal = TemporalConfig(
        embedders=[EMBEDDER_NAME],
        active_embedder=EMBEDDER_NAME,
    )
    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = tmp_path / ".code-indexer" / "config.json"
    return config_manager


def _make_real_indexer(repo: Path, index_dir: Path):
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)
    config_manager = _make_config_manager(repo)
    indexer = TemporalIndexer(
        config_manager, vector_store, collection_name="code-indexer-temporal-1296"
    )
    return indexer, vector_store


def _shard_dirs_for_slug(index_dir: Path, slug: str) -> List[Path]:
    return [
        d
        for d in index_dir.iterdir()
        if d.is_dir() and d.name.startswith(f"code-indexer-temporal-{slug}")
    ]


class TestWatchModeRealPipelineBug1296:
    """Drives the real TemporalWatchHandler incremental-commit path with a
    REAL TemporalIndexer -- no mocking of the code under test."""

    def test_handle_commit_detected_indexes_new_commit_through_real_pipeline(
        self, tmp_path
    ):
        fake_embedder = _FakeEmbedder(EMBEDDER_NAME)
        register_embedder(EMBEDDER_NAME, lambda config, e=fake_embedder: e)
        try:
            repo = _init_repo(tmp_path)
            (repo / "a.txt").write_text("hello world\n")
            _run_git(["add", "."], repo)
            _run_git(["commit", "-q", "-m", "Initial commit"], repo)

            index_dir = tmp_path / "index"
            indexer, _vector_store = _make_real_indexer(repo, index_dir)

            progressive_metadata = TemporalProgressiveMetadata(indexer.temporal_dir)

            handler = TemporalWatchHandler(
                repo,
                temporal_indexer=indexer,
                progressive_metadata=progressive_metadata,
            )

            with patch(
                "code_indexer.progress.progress_display.RichLiveProgressManager"
            ) as mock_progress_cls:
                mock_pm = Mock()
                mock_progress_cls.return_value = mock_pm

                # Act: this is the exact incremental-commit path Bug #1296
                # describes (cli_temporal_watch_handler.py:242). Prior to the
                # fix this raises AttributeError because TemporalIndexer has
                # no index_commits_list method.
                handler._handle_commit_detected()

            # Assert: the REAL per-commit adapter-driven pipeline actually
            # ran -- the fake embedder was invoked and a shard collection
            # with real vectors was created on disk (not just "no
            # exception").
            assert fake_embedder.embed_calls, (
                "real TemporalEmbedder.embed_commit_chunks() must have been "
                "invoked by the watch incremental path"
            )
            shard_dirs = _shard_dirs_for_slug(index_dir, "fake_embedder_1296")
            assert shard_dirs, (
                "expected the watch incremental path to create a real "
                "per-commit shard collection on disk"
            )
            vector_files = list(shard_dirs[0].rglob("vector_*.json"))
            assert vector_files, "expected real vector files written to the shard"
        finally:
            unregister_embedder_for_tests(EMBEDDER_NAME)


class TestBranchSwitchCatchUpRealPipelineBug1296:
    """Drives the SECOND call site named in Bug #1296
    (cli_temporal_watch_handler.py:413, reached via _catch_up_temporal_index
    -> _index_commits_incremental on branch switch) against a REAL
    TemporalIndexer."""

    def test_catch_up_temporal_index_indexes_new_commit_through_real_pipeline(
        self, tmp_path
    ):
        fake_embedder = _FakeEmbedder(EMBEDDER_NAME)
        register_embedder(EMBEDDER_NAME, lambda config, e=fake_embedder: e)
        try:
            repo = _init_repo(tmp_path)
            (repo / "a.txt").write_text("hello world\n")
            _run_git(["add", "."], repo)
            _run_git(["commit", "-q", "-m", "Initial commit"], repo)

            index_dir = tmp_path / "index"
            indexer, _vector_store = _make_real_indexer(repo, index_dir)

            progressive_metadata = TemporalProgressiveMetadata(indexer.temporal_dir)

            handler = TemporalWatchHandler(
                repo,
                temporal_indexer=indexer,
                progressive_metadata=progressive_metadata,
            )
            # completed_commits_set is loaded once at __init__ from an empty
            # progressive_metadata store, so it is already empty here --
            # _catch_up_temporal_index will see the one real commit as
            # unindexed and attempt to catch it up.

            with patch(
                "code_indexer.progress.progress_display.RichLiveProgressManager"
            ) as mock_progress_cls:
                mock_progress_cls.return_value = Mock()

                # Act: this is the branch-switch catch-up path Bug #1296
                # describes (cli_temporal_watch_handler.py:413). Prior to
                # the fix this raises AttributeError because TemporalIndexer
                # has no index_commits_list method.
                handler._catch_up_temporal_index()

            assert fake_embedder.embed_calls, (
                "real TemporalEmbedder.embed_commit_chunks() must have been "
                "invoked by the branch-switch catch-up path"
            )
            shard_dirs = _shard_dirs_for_slug(index_dir, "fake_embedder_1296")
            assert shard_dirs, (
                "expected the catch-up path to create a real per-commit "
                "shard collection on disk"
            )
            vector_files = list(shard_dirs[0].rglob("vector_*.json"))
            assert vector_files, "expected real vector files written to the shard"
        finally:
            unregister_embedder_for_tests(EMBEDDER_NAME)
