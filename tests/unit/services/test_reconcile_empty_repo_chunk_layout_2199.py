from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _FAKE_KEY,
    _cli_semantic_hits,
    _git,
    _git_init,
    cluster_mode,  # noqa: F401  (pytest fixture: solo epoch and cluster fail-closed)
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

_ADDED = "added_000"


def _reconcile(repo: FakeProviderRepo) -> None:
    proc = repo.cidx(*repo.index_args(), "--fts", "--reconcile")
    assert proc.returncode == 0, proc.stderr[-3000:]


def _legacy_files(repo: FakeProviderRepo) -> List[str]:
    collection = repo.collection_dir
    legacy = list(collection.rglob("vector_*.json")) + list(
        collection.rglob("id_index.bin")
    )
    return sorted(str(path.relative_to(collection)) for path in legacy)


def _layout(repo: FakeProviderRepo) -> Any:
    return resolve_chunk_layout(repo.collection_dir)


@pytest.mark.parametrize("git_aware", [True, False], ids=["git", "non-git"])
def test_reconcile_of_an_empty_repo_builds_the_collection_as_chunks_db(
    tmp_path: Path,
    git_aware: bool,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = FakeProviderRepo(tmp_path, server, _FAKE_KEY)
        if cluster_mode:
            repo.env[CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV] = "1"
        (repo.repo / "README.unindexed").write_text("no indexable files yet\n")
        if git_aware:
            _git_init(repo.repo, ".code-indexer/\n")

        _reconcile(repo)

        assert _layout(repo) == ChunkLayout.CHUNKS_DB

        (repo.repo / "src" / f"{_ADDED}.py").write_text(unique_python_text(_ADDED, 40))
        if git_aware:
            _git(repo.repo, "add", "-A")
            _git(repo.repo, "commit", "-qm", "add")
        _reconcile(repo)

        assert _layout(repo) == ChunkLayout.CHUNKS_DB
        assert _legacy_files(repo) == []
        hits = _cli_semantic_hits(repo)
        assert f"{_ADDED}.py" in hits, hits
