"""Tests for Issue #1459 AC1 item 3: `_is_shared_bookkeeping_directory` must
recognize a CHUNKS_DB-layout collection's real data.

`_is_shared_bookkeeping_directory` (temporal_blank_out.py, Bug #1405)
discriminates the shared bare-named `code-indexer-temporal` bookkeeping
directory from a genuine legacy monolith by DATA PRESENCE: `hnsw_index.bin`
OR nested `vector_*.json` files means "has real data -> not the bookkeeping
dir". Neither check can see a CHUNKS_DB-layout collection's real rows (they
live in `chunks.db`, invisible to both predicates) -- so a real CHUNKS_DB
temporal collection with committed rows but no HNSW graph yet gets
misclassified as the bookkeeping directory.
"""

from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.services.temporal.temporal_blank_out import (
    _is_shared_bookkeeping_directory,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)

_TEST_VECTOR = [0.1, 0.2, 0.3, 0.4]
_NONZERO_ROW_COUNT = 2
_ZERO_ROW_COUNT = 0


def _write_base_collection_meta(coll_dir: Path) -> None:
    coll_dir.mkdir(parents=True, exist_ok=True)
    (coll_dir / "collection_meta.json").write_text(
        '{"name": "code-indexer-temporal", "vector_size": 4}'
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


def test_corrupt_chunks_db_raises_instead_of_proceeding_to_delete(tmp_path):
    """Issue #1459 remediation Finding 3, fail-loud contract: a corrupt
    chunks.db inside a bare-named directory blank_out is about to inspect
    must propagate loudly (raise), NEVER be silently treated as "no data,
    proceed" toward a hard-delete decision (Messi Rule #13)."""
    import sqlite3

    from code_indexer.services.temporal.temporal_blank_out import (
        blank_out_legacy_temporal_collections,
    )

    coll_dir = tmp_path / LEGACY_TEMPORAL_COLLECTION
    _write_base_collection_meta(coll_dir)
    write_chunks_db_discriminator(coll_dir)
    (coll_dir / "chunks.db").write_bytes(b"not a sqlite file at all")

    with pytest.raises(sqlite3.DatabaseError):
        blank_out_legacy_temporal_collections(tmp_path)

    # Must never have proceeded to delete the directory it could not
    # safely inspect.
    assert coll_dir.exists()


def test_chunks_db_with_real_rows_is_not_bookkeeping_directory(tmp_path):
    coll_dir = tmp_path / LEGACY_TEMPORAL_COLLECTION
    _make_chunks_db_collection(coll_dir, row_count=_NONZERO_ROW_COUNT)
    # Deliberately absent, matching the bookkeeping-dir shape the two
    # existing predicates check: no hnsw_index.bin, no vector_*.json.
    assert not (coll_dir / "hnsw_index.bin").exists()
    assert next(coll_dir.rglob("vector_*.json"), None) is None

    result = _is_shared_bookkeeping_directory(LEGACY_TEMPORAL_COLLECTION, coll_dir)

    assert result is False


def test_chunks_db_empty_bare_name_still_treated_as_bookkeeping_directory(tmp_path):
    """Regression: a genuinely empty (zero-row) CHUNKS_DB-layout bare-named
    directory must still resolve as the bookkeeping directory (no real data
    to protect it from being skipped/preserved)."""
    coll_dir = tmp_path / LEGACY_TEMPORAL_COLLECTION
    _make_chunks_db_collection(coll_dir, row_count=_ZERO_ROW_COUNT)

    result = _is_shared_bookkeeping_directory(LEGACY_TEMPORAL_COLLECTION, coll_dir)

    assert result is True
