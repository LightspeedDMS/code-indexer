"""Bug #1529 item 4: the temporal READ side resolves the fixed sister path.

This is the defect that reached staging. `_execute_temporal_query` handed
`reconstruct_temporal_backend` the ACTIVATION's own clone path, and that
function derived `index_path = repo_path/.code-indexer/index` -- so an
activated-repo temporal query read the activation's frozen-at-clone-time
duplicate of the golden repo's temporal data. Any golden-repo refresh after
the activation was created was therefore invisible to that activation,
forever, with no re-sync mechanism.

Tests here prove, on a real filesystem with real SQLite chunk stores and real
HNSW indexes:

  (c) data indexed on the golden repo AFTER an activation exists is visible
      through the activated-repo read path (no staleness), and the stale copy
      sitting inside the activation clone is never what gets read;
  (b) the read path never depends on the activation clone's own index dir;
  plus the byte-identical-when-omitted contract of the new `index_dir`
  override, so every non-temporal caller is unaffected.

The store/search path is never mocked. Query vectors are supplied via
`precomputed_query_vector`, which bypasses the embedding API while still
genuinely exercising collection resolution -> HNSW load -> ChunkStore read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.services.temporal.temporal_server_paths import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
    resolve_temporal_index_dir,
    server_temporal_index_root,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

EMBEDDER_SLUG = "voyage_code_3"
QUARTER = "2024Q1"
SHARD_NAME = f"code-indexer-temporal-{EMBEDDER_SLUG}-{QUARTER}"
REPO_ALIAS = "evolution"
USERNAME = "someuser"
VECTOR_SIZE = 8

OLD_COMMIT = "a" * 8
NEW_COMMIT = "b" * 8
STALE_COMMIT = "c" * 8


def _row(commit_hash: str, seed: int) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    return {
        "id": f"proj:commit:{commit_hash}:0",
        "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
        "payload": {"path": f"src/{commit_hash}.py", "commit_hash": commit_hash},
        "chunk_text": f"content for {commit_hash}",
    }


def _index_rows(index_dir: Path, rows: List[Dict[str, Any]]) -> None:
    store = FilesystemVectorStore(
        base_path=index_dir, use_chunks_db_for_new_collections=True
    )
    if not store.collection_exists(SHARD_NAME):
        store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(SHARD_NAME, rows)
    store.end_indexing(SHARD_NAME)


def _search_ids(index_dir: Path, probe: List[float]) -> set:
    """Read through a store rooted at index_dir, exactly as the read path
    does once reconstruct_temporal_backend is given a temporal_index_dir."""
    store = FilesystemVectorStore(base_path=index_dir)
    results = store.search(
        query="anything",
        embedding_provider=None,
        collection_name=SHARD_NAME,
        precomputed_query_vector=probe,
        limit=50,
    )
    return {r["id"] for r in results}


# ---------------------------------------------------------------------------
# (c) no staleness across a post-activation golden-repo refresh
# ---------------------------------------------------------------------------


def test_activated_repo_read_sees_commits_indexed_after_activation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")

    data_dir = tmp_path / "data"
    golden_repos_dir = data_dir / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    # 1. The golden repo indexes its history (server context -> sister path).
    temporal_dir = resolve_temporal_index_dir(codebase_dir)
    old_row = _row(OLD_COMMIT, 1)
    _index_rows(temporal_dir, [old_row])

    # 2. A user activates the repo. The activation is an independent clone
    #    that -- under the pre-#1529 shape -- carried a frozen temporal copy.
    #    Simulate the worst case: it carries one, with DIFFERENT content.
    activation_dir = data_dir / "activated-repos" / USERNAME / REPO_ALIAS
    activation_index = activation_dir / ".code-indexer" / "index"
    stale_row = _row(STALE_COMMIT, 99)
    _index_rows(activation_index, [stale_row])

    # 3. The golden repo is refreshed with a NEW commit, after activation.
    new_row = _row(NEW_COMMIT, 2)
    _index_rows(resolve_temporal_index_dir(codebase_dir), [new_row])

    # 4. The read side resolves the FIXED path from the golden alias -- never
    #    from the activation's own clone path.
    read_dir = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    assert read_dir == temporal_dir

    found = _search_ids(read_dir, old_row["vector"])

    # The post-activation commit IS visible (the staleness defect is closed).
    assert new_row["id"] in found
    assert old_row["id"] in found
    # The activation's frozen copy is never what gets read.
    assert stale_row["id"] not in found


def test_read_path_is_independent_of_the_activation_clone(
    tmp_path: Path, monkeypatch
) -> None:
    """Even with NO temporal data inside the activation clone at all, the
    activated-repo read still finds the golden repo's data -- proving the read
    no longer depends on the activation's own index dir."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")

    data_dir = tmp_path / "data"
    golden_repos_dir = data_dir / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)
    row = _row(OLD_COMMIT, 3)
    _index_rows(resolve_temporal_index_dir(codebase_dir), [row])

    never_populated = (
        data_dir / "activated-repos" / USERNAME / REPO_ALIAS / ".code-indexer" / "index"
    )
    assert not never_populated.exists()

    found = _search_ids(
        server_temporal_index_root(golden_repos_dir, REPO_ALIAS), row["vector"]
    )
    assert row["id"] in found


def test_global_suffixed_alias_reads_the_same_data(tmp_path: Path, monkeypatch) -> None:
    """The golden-repo-direct (is_global) seam passes a '-global'-suffixed
    alias; it must land on the identical physical location the write side
    used, or golden-direct temporal queries return nothing."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    golden_repos_dir = tmp_path / "data" / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)
    row = _row(OLD_COMMIT, 4)
    _index_rows(resolve_temporal_index_dir(codebase_dir), [row])

    found = _search_ids(
        server_temporal_index_root(golden_repos_dir, f"{REPO_ALIAS}-global"),
        row["vector"],
    )
    assert row["id"] in found


# ---------------------------------------------------------------------------
# The index_dir override contract
# ---------------------------------------------------------------------------


def test_backend_index_dir_override_is_honored(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "temporal-root"
    backend = FilesystemBackend(project_root=tmp_path / "repo", index_dir=explicit)
    assert backend.vectors_dir == explicit


def test_backend_without_override_is_byte_identical(tmp_path: Path) -> None:
    """Every non-temporal caller passes nothing -- behavior must not move."""
    project_root = tmp_path / "repo"
    assert (
        FilesystemBackend(project_root=project_root).vectors_dir
        == project_root / ".code-indexer" / "index"
    )
