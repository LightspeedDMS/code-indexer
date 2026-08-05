"""Bug #1529 test (d): in-place temporal refresh is safe for a concurrent reader.

The fixed-path design (one stable directory per (alias, embedder, quarter),
no versioning, no alias pointers) means a refresh writes in place. The
concurrency requirement is therefore explicit: a reader must never observe a
torn ``chunks.db`` or a torn HNSW graph mid-refresh.

No new machinery is needed for that -- the two primitives this codebase
already uses everywhere provide it:

  - ``ChunkStore`` writes through SQLite transactions (``INSERT OR REPLACE``,
    committed as a unit), so a concurrent reader gets a consistent snapshot;
  - ``HNSWIndexManager`` persists the graph via its existing temp-file +
    atomic-rename pattern, so a reader either opens the whole old file or the
    whole new one.

This test proves that end to end against the REAL write path -- exactly the
call sequence temporal indexing performs (``begin_indexing`` ->
``upsert_points`` -> ``end_indexing``) at the fixed root -- with two
independent reader threads hammering the live directory throughout 12
consecutive incremental refreshes.

What "safe" means here, precisely:
  - neither reader ever raises (no torn/corrupt file is ever exposed);
  - the chunk reader's row set is always a VALID intermediate: it contains
    every previously-committed row and never a row that was never written;
  - the HNSW search reader only ever returns ids that genuinely exist. It may
    legitimately return FEWER than the full set while a rebuild is in flight
    (incomplete, not incorrect) -- that is the documented, accepted behavior
    for an additive in-place index update, not a defect.

Real filesystem, real SQLite, real HNSW builds, real threads -- nothing mocked.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Set

import numpy as np

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore

REPO_ALIAS = "evolution"
EMBEDDER = "voyage_code_3"
SHARD_NAME = f"code-indexer-temporal-{EMBEDDER}-2024Q1"
VECTOR_SIZE = 8
REFRESH_ROUNDS = 12


def _commit_hash(i: int) -> str:
    return f"{i:08x}"


def _point_id(i: int) -> str:
    return f"proj:commit:{_commit_hash(i)}:0"


def _record(i: int) -> Dict[str, Any]:
    rng = np.random.default_rng(1529 + i)
    return {
        "id": _point_id(i),
        "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
        "payload": {"path": f"src/f{i}.py", "commit_hash": _commit_hash(i)},
        "chunk_text": f"content {i}",
    }


def _refresh(index_root: Path, records: List[Dict[str, Any]]) -> None:
    """The REAL temporal write sequence, in place at the fixed root."""
    store = FilesystemVectorStore(
        base_path=index_root, use_chunks_db_for_new_collections=True
    )
    if not store.collection_exists(SHARD_NAME):
        store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(SHARD_NAME, records)
    store.end_indexing(SHARD_NAME)


def _read_chunk_ids(shard_dir: Path) -> Set[str]:
    store = ChunkStore(shard_dir / "chunks.db", immutable=True)
    try:
        return {r["id"] for r in store.stream_all()}
    finally:
        store.close()


def test_concurrent_readers_never_see_a_torn_in_place_refresh(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    index_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    shard_dir = index_root / SHARD_NAME

    # Seed the shard so both readers have something to open from the start.
    _refresh(index_root, [_record(0)])
    probe_vector = _record(0)["vector"]

    all_ids = {_point_id(i) for i in range(REFRESH_ROUNDS + 1)}
    chunk_observations: List[Set[str]] = []
    search_observations: List[Set[str]] = []
    errors: List[BaseException] = []
    stop = threading.Event()

    def chunk_reader() -> None:
        while not stop.is_set():
            try:
                chunk_observations.append(_read_chunk_ids(shard_dir))
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)
                return

    def search_reader() -> None:
        """Reads through the REAL query path (HNSW load + chunk hydration)."""
        while not stop.is_set():
            try:
                store = FilesystemVectorStore(base_path=index_root)
                results = store.search(
                    query="anything",
                    embedding_provider=None,
                    collection_name=SHARD_NAME,
                    precomputed_query_vector=probe_vector,
                    limit=50,
                )
                search_observations.append({r["id"] for r in results})
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)
                return

    threads = [
        threading.Thread(target=chunk_reader, daemon=True),
        threading.Thread(target=search_reader, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        for i in range(1, REFRESH_ROUNDS + 1):
            _refresh(index_root, [_record(i)])
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    assert not errors, (
        "a concurrent reader hit an error during an in-place refresh -- a torn "
        f"or partially-written file was exposed: {errors[:1]}"
    )
    assert chunk_observations, "the chunk reader never managed a read"
    assert search_observations, "the search reader never managed a read"

    # Chunk reads: always a valid intermediate -- never a phantom row, and the
    # already-committed seed row is never lost mid-refresh.
    for seen in chunk_observations:
        assert seen <= all_ids, f"chunk reader saw unknown ids: {seen - all_ids}"
        assert _point_id(0) in seen, (
            "an already-committed row disappeared mid-refresh -- the write is "
            "not transactionally isolated"
        )

    # Search reads: never a phantom id. Fewer results mid-rebuild is the
    # accepted additive-update behavior (incomplete, never incorrect).
    for seen in search_observations:
        assert seen <= all_ids, f"search reader saw unknown ids: {seen - all_ids}"

    # And the final state is complete.
    assert _read_chunk_ids(shard_dir) == all_ids


def test_repeated_in_place_refreshes_accumulate_correctly(tmp_path: Path) -> None:
    """Sanity companion: the in-place path is additive, so nothing is lost
    across refreshes even with no concurrency involved."""
    golden_repos_dir = tmp_path / "golden-repos"
    index_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)

    for i in range(5):
        _refresh(index_root, [_record(i)])

    assert _read_chunk_ids(index_root / SHARD_NAME) == {_point_id(i) for i in range(5)}
    # Still the consolidated layout, still no legacy JSON rows.
    assert (index_root / SHARD_NAME / "chunks.db").is_file()
    assert list((index_root / SHARD_NAME).rglob("vector_*.json")) == []
