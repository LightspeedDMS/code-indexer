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

#: The seed record, written before the readers start. Its vector is reused
#: verbatim as the search probe, so it must always be the top-ranked hit --
#: that is what makes label/point_id mis-resolution detectable.
SEED_RECORD_INDEX = 0


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


def _read_chunk_rows(shard_dir: Path) -> List[Dict[str, Any]]:
    store = ChunkStore(shard_dir / "chunks.db", immutable=True)
    try:
        return list(store.stream_all())
    finally:
        store.close()


def _read_chunk_ids(shard_dir: Path) -> Set[str]:
    return {r["id"] for r in _read_chunk_rows(shard_dir)}


def _index_of(point_id: str) -> int:
    """Recover the record index encoded in a `proj:commit:{hash}:0` id."""
    return int(point_id.split(":")[2], 16)


def _assert_row_self_consistent(row: Dict[str, Any], source: str) -> None:
    """Every field of a returned row must belong to the id it arrived under.

    Bug #1529 finding #7(b): the original oracle only asserted set
    membership (`seen <= all_ids`), which passes for ANY subset of known ids
    -- so it could not detect a row whose CONTENT belongs to a different
    record than its id claims. That is the failure mode a full HNSW label
    renumbering can produce, and the one commit 93bfa68b's own message
    flagged as a bounded risk. Content is checked against the id, per row.

    Both read paths expose the same three content fields (verified against
    the real store row and the real hydrated query result), so all three are
    asserted unconditionally -- nothing here can silently skip.
    """
    index = _index_of(row["id"])
    expected = _record(index)

    payload = row.get("payload") or {}
    assert payload.get("commit_hash") == expected["payload"]["commit_hash"], (
        f"{source}: row {row['id']} carries commit_hash "
        f"{payload.get('commit_hash')!r}, which belongs to a DIFFERENT "
        "record -- id/content mis-resolution"
    )
    assert payload.get("path") == expected["payload"]["path"], (
        f"{source}: row {row['id']} carries path {payload.get('path')!r}, "
        "which belongs to a DIFFERENT record -- id/content mis-resolution"
    )
    assert row["chunk_text"] == expected["chunk_text"], (
        f"{source}: row {row['id']} carries chunk_text {row.get('chunk_text')!r}, "
        "which belongs to a DIFFERENT record -- id/content mis-resolution"
    )


def test_concurrent_readers_never_see_a_torn_in_place_refresh(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    index_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    shard_dir = index_root / SHARD_NAME

    # Seed the shard so both readers have something to open from the start.
    _refresh(index_root, [_record(0)])
    probe_vector = _record(0)["vector"]

    all_ids = {_point_id(i) for i in range(REFRESH_ROUNDS + 1)}
    chunk_observations: List[List[Dict[str, Any]]] = []
    search_observations: List[List[Dict[str, Any]]] = []
    errors: List[BaseException] = []
    stop = threading.Event()

    # Bug #1529 finding #7(d): a real synchronization barrier, so both readers
    # are provably live and looping before the FIRST refresh begins. Without
    # it, overlap depended on thread-start timing luck and the test could pass
    # having never actually read during a write.
    ready = threading.Barrier(3, timeout=30)

    def chunk_reader() -> None:
        try:
            ready.wait()
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)
            return
        while not stop.is_set():
            try:
                chunk_observations.append(_read_chunk_rows(shard_dir))
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)
                return

    def search_reader() -> None:
        """Reads through the REAL query path (HNSW load + chunk hydration)."""
        try:
            ready.wait()
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)
            return
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
                search_observations.append(list(results))
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
        ready.wait()
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

    # Chunk reads: always a valid intermediate -- never a phantom row, the
    # already-committed seed row is never lost mid-refresh, and every row's
    # CONTENT belongs to its own id (set membership alone cannot show that).
    for rows in chunk_observations:
        seen = {r["id"] for r in rows}
        assert seen <= all_ids, f"chunk reader saw unknown ids: {seen - all_ids}"
        assert _point_id(0) in seen, (
            "an already-committed row disappeared mid-refresh -- the write is "
            "not transactionally isolated"
        )
        for row in rows:
            _assert_row_self_consistent(row, "chunk reader")

    # Search reads go through HNSW label -> point_id resolution, so they get
    # the stronger oracle. The probe vector IS record 0's vector, so record 0
    # must rank FIRST in every observation: a label resolving to the wrong
    # point_id during a renumbering rebuild shows up here and NOWHERE in a
    # set-membership check. Fewer results mid-rebuild remains accepted
    # additive-update behavior (incomplete, never incorrect).
    ranked_observations = 0
    expected_top_id = _point_id(SEED_RECORD_INDEX)
    for rows in search_observations:
        seen = {r["id"] for r in rows}
        assert seen <= all_ids, f"search reader saw unknown ids: {seen - all_ids}"
        for row in rows:
            _assert_row_self_consistent(row, "search reader")
        if not rows:
            # An empty result set mid-rebuild is accepted (incomplete, never
            # incorrect) per this test's contract, so it is skipped rather
            # than indexed into. The counter below stops that from letting
            # the ranking oracle pass vacuously.
            continue
        ranked_observations += 1
        assert rows[0]["id"] == expected_top_id, (
            "the exact-vector match did not rank first: the HNSW label "
            f"resolved to {rows[0]['id']} instead of {expected_top_id} -- "
            "label/point_id mis-resolution"
        )

    assert ranked_observations, (
        "every search observation was empty, so the ranking oracle never "
        "actually ran -- the concurrency proof would be vacuous"
    )

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
