"""TemporalIndexer wires validate_embedder_slug_uniqueness at the embedder-set
materialization point (Story #1457 AC6, round-13 Codex N13-1).

Drives the REAL TemporalIndexer against a REAL git repository (no mocking of
the code under test), mirroring the established pattern in
test_temporal_indexer_multi_embedder_1291.py (test_unknown_embedder_in_scope_raises).

Two configured embedder names that sanitize to the SAME collection slug
("collide-a.1457" and "collide-a-1457" both -> "collide_a_1457") must fail the
index run loudly at materialization time, BEFORE any per-embedder shard build
work begins -- proving the guard is wired as a hard precondition, not merely
unit-tested in isolation.
"""

from __future__ import annotations

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
    def __init__(self, name: str, model_slug: str, dims: int = 4):
        self.name = name
        self.model_slug = model_slug
        self.dimensions = dims
        self.overlap_percentage = 0.0

    def embed_commit_chunks(self, chunks: List[str]) -> List[List[float]]:
        return [[float(len(c))] * self.dimensions for c in chunks]

    def embed_query(self, text: str) -> List[float]:
        return [float(len(text))] * self.dimensions

    def is_available(self) -> bool:
        return True


@pytest.fixture
def colliding_slug_embedders():
    embedder_a = _FakeEmbedder("collide-a.1457", "collide_a_1457")
    embedder_b = _FakeEmbedder("collide-a-1457", "collide_a_1457")
    register_embedder("collide-a.1457", lambda config, e=embedder_a: e)
    register_embedder("collide-a-1457", lambda config, e=embedder_b: e)
    yield "collide-a.1457", "collide-a-1457"
    unregister_embedder_for_tests("collide-a.1457")
    unregister_embedder_for_tests("collide-a-1457")


def test_colliding_embedder_slugs_fail_index_run_loudly(
    tmp_path, colliding_slug_embedders
):
    name_a, name_b = colliding_slug_embedders
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("hello world\n")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "Initial commit"], repo)

    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    vector_store = FilesystemVectorStore(base_path=index_dir, project_root=repo)

    config = Config(codebase_dir=repo)
    config.embedding_provider = "voyage-ai"
    config.temporal = TemporalConfig(embedders=[name_a, name_b], active_embedder=name_a)
    config_manager = MagicMock()
    config_manager.get_config.return_value = config
    config_manager.config_path = repo / ".code-indexer" / "config.json"

    indexer = TemporalIndexer(
        config_manager, vector_store, collection_name="code-indexer-temporal-collide"
    )
    try:
        with pytest.raises(ValueError, match="collapse to the same collection slug"):
            indexer.index_commits(reconcile=True)
    finally:
        indexer.close()
