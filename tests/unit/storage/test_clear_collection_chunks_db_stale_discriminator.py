"""clear_collection() must not leave a stale chunks_db discriminator behind.

Discovered while fixing tests/unit/cli/test_index_commits_clear_bug.py's
counting helper (Bug #1528 made temporal indexing write the CHUNKS_DB layout
by default): `FilesystemVectorStore.clear_collection()` preserves
`collection_meta.json` verbatim (to keep the projection matrix / general
metadata fast-reindex behavior), but for a CHUNKS_DB-layout collection this
also blindly preserves the `chunks_db` discriminator -- even though the
`chunks.db` file the discriminator points at was just deleted by the
preceding `shutil.rmtree()`.

That leaves the collection directory in a genuinely inconsistent state:
`collection_meta.json` claims CHUNKS_DB layout (discriminator set, per
`resolve_chunk_layout()`), but `chunks.db` itself does not exist. Any
subsequent layout-aware caller that trusts the discriminator (e.g.
`consolidate_legacy_temporal_shards()`'s pending-shard scan) then finds a
missing content-integrity manifest for what looks like an unfinished
migration and raises `UnrecoverableConsolidationCorruptionError` --
confirmed live via `cidx index --index-commits --clear` failing with
exactly that error against a freshly-built, natively-CHUNKS_DB temporal
collection.

ROUND 1 fix: strip the `chunks_db` discriminator unconditionally, reverting
the on-disk layout to SHARDED_JSON-resolving after a clear. This broke the
NEXT `cidx index` write for a semantic (non-temporal) collection: unlike
temporal's `consolidate_legacy_temporal_shards()`, semantic collections have
no pre-flight step that re-commits the discriminator after a clear, so the
stripped discriminator was the only remaining signal for the follow-up
write's layout decision -- the write silently downgraded to legacy
SHARDED_JSON (the exact storage explosion Epic #1454 exists to prevent).

ROUND 2 fix (commit b97f7432, REJECTED): captured the pre-clear layout and,
if it was CHUNKS_DB, recorded a `self._chunks_db_mode[collection_name] =
True` in-process write-intent flag so the SAME store instance's next write
still built `chunks.db`. Rejected because `_chunks_db_mode` is per-process
RAM that does not survive a process boundary: `cidx clean` (its own process,
exits after clearing) and the daemon `clean` RPC (builds a local store,
clears, returns) both discard the instance that set the flag, so a fresh
`cidx index` process afterwards still silently downgraded to SHARDED_JSON.
It also introduced a new medium-severity bug: in-memory intent said
CHUNKS_DB while on-disk had no `chunks.db` file, so a read-only method
consulting `_is_chunks_db_collection` would call `open_chunk_store_for_path`,
which CREATES a `chunks.db` file as a side effect of merely reading --
violating the documented Story #1459 invariant that inspection must never
mutate.

ROUND 3 fix (this file): make the POST-CLEAR ON-DISK STATE ITSELF truthful
and durable instead of relying on in-memory intent. When the pre-clear
layout was CHUNKS_DB, `clear_collection()`:

  1. Strips both the `chunks_db` discriminator (`chunk_layout.
     clear_chunks_db_discriminator`) AND the migration-authoritative
     `vector_count` cross-check field (`collection_migration.
     strip_authoritative_vector_count`) from the restored
     `collection_meta.json` bytes -- a stale `vector_count` would make
     `_is_natively_built_chunks_db()` treat the freshly recommitted, purely
     native collection as though a migration had already run here.
  2. Creates a FRESH, EMPTY `chunks.db` file in the cleared collection
     directory (`with ChunkStore(collection_path / "chunks.db"): pass`).
  3. Re-commits the `chunks_db` discriminator via the existing
     `write_chunks_db_discriminator()` helper.

The result: ANY `FilesystemVectorStore` instance, in ANY process, that
inspects this collection afterwards sees a genuinely consistent CHUNKS_DB
collection -- discriminator present, `chunks.db` physically exists (empty),
both facts agreeing -- with zero reliance on which process/instance
performed the clear. `self._chunks_db_mode` is no longer written to by
`clear_collection()`/`delete_collection()` at all; it remains in use only
for `create_collection()`'s own, unrelated in-progress-fresh-build intent
window (before the discriminator can legitimately exist yet).
"""

import json

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import (
    ChunkStore,
    chunk_store_has_real_data,
)

VECTOR_DIM = 16


def _record(point_id: str) -> dict:
    return {
        "id": point_id,
        "vector": np.random.default_rng(0)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": {"path": f"{point_id}.py"},
        "chunk_text": f"content {point_id}",
    }


def _build_chunks_db_collection(
    store: FilesystemVectorStore, collection_name: str, records: list
):
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path(collection_name)

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


@pytest.fixture
def store(tmp_path):
    return FilesystemVectorStore(base_path=tmp_path)


def test_clear_replaces_chunks_db_with_fresh_empty_file(store):
    """A previously-CHUNKS_DB collection's clear must leave a genuinely
    existing, but EMPTY, chunks.db behind -- not delete it outright (round 1/
    round 2 behavior) and not leave the pre-clear records in it."""
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)
    assert (collection_path / "chunks.db").exists()

    result = store.clear_collection(collection_name="coll")

    assert result is True
    chunks_db_path = collection_path / "chunks.db"
    assert chunks_db_path.exists()
    assert chunk_store_has_real_data(chunks_db_path) is False


def test_clear_recommits_discriminator_backed_by_fresh_chunks_db(store):
    """Bug #1644 round 3: after clearing a previously-CHUNKS_DB collection,
    the on-disk discriminator and the on-disk chunks.db file must AGREE --
    both must claim/be CHUNKS_DB. This is the specific defect the whole
    investigation started from: a discriminator claiming CHUNKS_DB while
    nothing backs it (round 0), or a discriminator stripped while the next
    write has no signal to rebuild CHUNKS_DB (round 1/round 2)."""
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)
    assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

    result = store.clear_collection(collection_name="coll")

    assert result is True
    assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
    assert (collection_path / "chunks.db").exists()


def test_clear_preserves_other_collection_metadata(store):
    """Only the stale chunks_db-related fields are touched -- the rest of
    collection_meta.json (name, vector_size) is preserved exactly like the
    pre-existing SHARDED_JSON clear_collection contract. For a previously
    CHUNKS_DB collection, the discriminator is correctly back (re-committed,
    backed by a fresh empty chunks.db)."""
    records = [_record("v0")]
    collection_path = _build_chunks_db_collection(store, "coll", records)

    result = store.clear_collection(collection_name="coll")

    assert result is True
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    assert meta.get("name") == "coll"
    assert meta.get("vector_size") == VECTOR_DIM
    assert "chunks_db" in meta


def test_clear_strips_stale_authoritative_vector_count(store):
    """Bug #1644 round 3, reviewer-flagged load-bearing check:
    `_is_natively_built_chunks_db()` treats ANY present top-level
    `vector_count` field in collection_meta.json as proof "a migration ran
    here". If clear_collection() left a stale pre-clear `vector_count`
    behind while recommitting a fresh native CHUNKS_DB collection, the
    freshly-cleared collection would be misclassified as an
    incomplete/stale migration instead of a native build. Verify it is
    stripped."""
    records = [_record("v0")]
    collection_path = _build_chunks_db_collection(store, "coll", records)
    meta_path = collection_path / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["vector_count"] = 2
    meta_path.write_text(json.dumps(meta))

    result = store.clear_collection(collection_name="coll")

    assert result is True
    post_meta = json.loads(meta_path.read_text())
    assert "vector_count" not in post_meta
    assert post_meta.get("name") == "coll"
    assert post_meta.get("vector_size") == VECTOR_DIM


def test_clear_on_sharded_json_collection_is_unaffected(store):
    """A plain SHARDED_JSON collection's clear_collection behavior must stay
    byte-identical -- this fix is scoped to CHUNKS_DB collections only."""
    store.create_collection("legacy_coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("legacy_coll")
    meta_path = collection_path / "collection_meta.json"
    assert meta_path.exists()
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

    result = store.clear_collection(collection_name="legacy_coll")

    assert result is True
    assert meta_path.exists()
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON
    assert not (collection_path / "chunks.db").exists()


def test_clear_layout_survives_brand_new_store_instance_cross_process(store, tmp_path):
    """The critical cross-process regression test round 2 was missing:
    clear a CHUNKS_DB collection with one FilesystemVectorStore instance,
    then construct a BRAND NEW instance sharing the same base_path
    (simulating a fresh process with zero shared RAM state) and confirm it
    independently sees CHUNKS_DB layout -- proving the fix is genuinely
    on-disk and does not depend on `self._chunks_db_mode` surviving."""
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)

    result = store.clear_collection(collection_name="coll")
    assert result is True

    # Brand-new instance, zero shared in-memory state with `store`.
    new_store = FilesystemVectorStore(base_path=tmp_path)
    assert new_store._chunks_db_mode.get("coll") is None
    assert new_store._is_chunks_db_collection("coll", collection_path) is True
    assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB


def test_clear_then_read_never_orphans_chunks_db_discriminator(store):
    """Regression guard for the round-2 medium-severity finding: a
    read-only operation must never change whether chunks.db exists or
    whether the discriminator's claim matches reality. Before and after a
    real read (get_all_indexed_files, which internally consults
    _is_chunks_db_collection), on-disk existence and resolved layout must
    be identical."""
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)

    result = store.clear_collection(collection_name="coll")
    assert result is True

    chunks_db_path = collection_path / "chunks.db"
    before_exists = chunks_db_path.exists()
    before_layout = resolve_chunk_layout(collection_path)

    # Real read-only call that internally consults _is_chunks_db_collection.
    files = store.get_all_indexed_files("coll")
    assert files == []

    after_exists = chunks_db_path.exists()
    after_layout = resolve_chunk_layout(collection_path)

    assert before_exists == after_exists is True
    assert before_layout == after_layout == ChunkLayout.CHUNKS_DB
