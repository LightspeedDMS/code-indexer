"""Tests for Issue #1459 AC1 item 4: `collection_has_vector_shards` must
recognize a CHUNKS_DB-layout collection's real data.

`collection_has_vector_shards` (repository_health_aggregator.py) is used by
`discover_incomplete_collections` to catch a partially-built collection:
real chunk data on disk but no `hnsw_index.bin` yet (indexing interrupted
before the graph was finalized). It only checked `vector_*.json` shards --
invisible for a CHUNKS_DB-layout collection, whose real data lives in
`chunks.db`. A genuinely partially-built CHUNKS_DB collection was therefore
silently skipped by BOTH discover_health_collections() (no hnsw_index.bin to
check) AND discover_incomplete_collections() (no vector_*.json shards to
see) -- vanishing from health results entirely (a false green), rather than
being reported unhealthy.

Real on-disk `ChunkStore` / `write_chunks_db_discriminator` fixtures -- no
mocking of the layout resolver or chunk store.
"""

from pathlib import Path

from code_indexer.server.services.repository_health_aggregator import (
    collection_has_vector_shards,
    discover_incomplete_collections,
)
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_TEST_VECTOR = [0.1, 0.2, 0.3, 0.4]
_NONZERO_ROW_COUNT = 3
_ZERO_ROW_COUNT = 0


def _write_base_collection_meta(coll_dir: Path) -> None:
    coll_dir.mkdir(parents=True, exist_ok=True)
    (coll_dir / "collection_meta.json").write_text(
        '{"name": "voyage-code-3", "vector_size": 4}'
    )


def _make_chunks_db_collection(coll_dir: Path, *, row_count: int) -> None:
    _write_base_collection_meta(coll_dir)
    store = ChunkStore(coll_dir / "chunks.db")
    try:
        if row_count:
            store.write_batch(
                [{"id": f"point-{i}", "vector": _TEST_VECTOR} for i in range(row_count)]
            )
    finally:
        store.close()
    write_chunks_db_discriminator(coll_dir)


def _flag_chunks_db_without_real_file(coll_dir: Path) -> None:
    """Write the CHUNKS_DB discriminator into collection_meta.json WITHOUT
    creating a real chunks.db -- the exact "flagged but missing" crash-
    window state Issue #1459 remediation Finding 2 guards against."""
    _write_base_collection_meta(coll_dir)
    write_chunks_db_discriminator(coll_dir)


class TestCollectionHasVectorShardsChunksDbRemediation:
    """Issue #1459 remediation Findings 2/3: a read-only status probe must
    never CREATE a missing chunks.db as a side effect, and must never
    crash on a corrupt chunks.db."""

    def test_missing_chunks_db_does_not_create_file(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        _flag_chunks_db_without_real_file(collection)
        db_path = collection / "chunks.db"
        assert not db_path.exists()

        result = collection_has_vector_shards(collection)

        assert result is False
        assert not db_path.exists(), (
            "collection_has_vector_shards must not create chunks.db as a "
            "side effect of a read-only status check"
        )

    def test_corrupt_chunks_db_does_not_crash(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        _flag_chunks_db_without_real_file(collection)
        (collection / "chunks.db").write_bytes(b"not a sqlite file at all")

        result = collection_has_vector_shards(collection)

        assert result is False


class TestCollectionHasVectorShardsChunksDb:
    def test_chunks_db_with_real_rows_returns_true(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        _make_chunks_db_collection(collection, row_count=_NONZERO_ROW_COUNT)

        assert collection_has_vector_shards(collection) is True

    def test_chunks_db_empty_returns_false(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        _make_chunks_db_collection(collection, row_count=_ZERO_ROW_COUNT)

        assert collection_has_vector_shards(collection) is False


class TestDiscoverIncompleteCollectionsChunksDb:
    def test_chunks_db_shards_without_graph_are_incomplete(self, tmp_path: Path):
        index_base = tmp_path / "index"
        collection = index_base / "voyage-code-3"
        _make_chunks_db_collection(collection, row_count=_NONZERO_ROW_COUNT)

        assert discover_incomplete_collections(index_base) == [collection]

    def test_chunks_db_empty_collection_is_not_incomplete(self, tmp_path: Path):
        index_base = tmp_path / "index"
        collection = index_base / "voyage-code-3"
        _make_chunks_db_collection(collection, row_count=_ZERO_ROW_COUNT)

        assert discover_incomplete_collections(index_base) == []
