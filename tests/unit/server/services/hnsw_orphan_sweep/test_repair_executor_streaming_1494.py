"""Story #1494 AC3: orphan-sweep repair streams instead of materializing.

Finding C7 (GIL-blocking analysis report): `repair_executor.py`'s
`_read_sister_chunk_records` called `list(store.stream_all())`, fully
materializing (and zstd/JSON-decoding) an ENTIRE collection into RAM in the
server process before handing it off to `build_fresh_consolidated_temporal_
version`. Combined with Finding A1, a single repaired collection was
simultaneously the biggest GIL scan and the biggest decode loop in the
process. Fixed by making `_read_sister_chunk_records` a generator that
streams rows lazily (mirroring `ChunkStore.stream_all()`'s own contract)
instead of eagerly materializing them into a list.

Real infrastructure throughout -- an actual on-disk ChunkStore/SQLite file,
zero mocking or monkeypatching of any kind. Laziness is proven via CPython's
own generator-function guarantee (no body code executes until the first
`next()` call): pointing the function at a NON-EXISTENT chunks.db path must
NOT raise at call time (a strict/eager implementation opening the store
immediately would raise there) -- only the first `next()` may raise.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Chunk records are heterogeneous dicts (str id, List[float] vector, Dict
# payload) with no dedicated dataclass/TypedDict in the production code --
# ChunkStore.write_batch/stream_all() themselves accept/yield untyped
# dict/list values, so `Any` here mirrors that existing contract rather
# than inventing a stricter type the production API does not have.
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.sqlite_chunk_store import ChunkStore
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    _read_sister_chunk_records,
)

CORPUS_DIM = 8
RECORD_COUNT = 5


def _make_records(n: int, dim: int) -> List[Dict[str, Any]]:
    rng = np.random.RandomState(7)
    records: List[Dict[str, Any]] = []
    for i in range(n):
        vector = rng.randn(dim).astype(np.float32).tolist()
        records.append(
            {
                "id": f"proj:commit:{'a' * 40}{i}:0",
                "vector": vector,
                "payload": {"path": f"file_{i}.py", "chunk_text": f"chunk {i}"},
            }
        )
    return records


def _build_chunks_db(tmp_path: Path, records: List[Dict[str, Any]]) -> Path:
    version_path = tmp_path / "v_1"
    version_path.mkdir()
    store = ChunkStore(version_path / "chunks.db", expected_dim=CORPUS_DIM)
    try:
        store.write_batch(records)
    finally:
        store.close()
    return version_path


def _records_by_id(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r["id"]: r for r in records}


class TestReadSisterChunkRecordsStreams:
    def test_returns_a_lazy_iterator_not_a_list(self, tmp_path: Path) -> None:
        """The function must hand back a lazy iterator, never a fully
        materialized list -- the whole point of the streaming fix."""
        records = _make_records(RECORD_COUNT, CORPUS_DIM)
        version_path = _build_chunks_db(tmp_path, records)

        result = _read_sister_chunk_records(version_path, CORPUS_DIM)

        assert not isinstance(result, list)
        assert hasattr(result, "__next__"), "result must be a lazy iterator"

    def test_full_consumption_round_trips_exact_records(self, tmp_path: Path) -> None:
        """Streaming must not lose, duplicate, or corrupt any record --
        every field (id, vector values, payload) must match exactly. Vector
        round-trip is BIT-EXACT: ChunkStore stores vectors as raw float32
        bytes (tobytes()/frombuffer()), and the source vectors here are
        themselves already float32-derived (.tolist() on a float32 array
        promotes to Python float losslessly), so casting back to float32
        reproduces the identical bit pattern -- no tolerance needed."""
        original = _make_records(RECORD_COUNT, CORPUS_DIM)
        version_path = _build_chunks_db(tmp_path, original)

        streamed = list(_read_sister_chunk_records(version_path, CORPUS_DIM))

        assert len(streamed) == RECORD_COUNT
        original_by_id = _records_by_id(original)
        streamed_by_id = _records_by_id(streamed)
        assert set(streamed_by_id) == set(original_by_id)
        for record_id, orig_record in original_by_id.items():
            got_record = streamed_by_id[record_id]
            assert np.array_equal(
                np.asarray(got_record["vector"], dtype=np.float32),
                np.asarray(orig_record["vector"], dtype=np.float32),
            )
            assert got_record["payload"] == orig_record["payload"]

    def test_construction_is_lazy_no_store_access_until_first_next(
        self, tmp_path: Path
    ) -> None:
        """CPython guarantees a generator function's body does not execute
        until the first next() call. Proven without any mocking: pointing
        the function at a NON-EXISTENT chunks.db path must NOT raise when
        called -- an eager (list(store.stream_all())-style) implementation
        would open the ChunkStore immediately and raise right there. Only
        the first next() may surface the real sqlite3.OperationalError from
        trying to open a missing immutable database file."""
        nonexistent_version_path = tmp_path / "does_not_exist"

        gen = _read_sister_chunk_records(nonexistent_version_path, CORPUS_DIM)
        assert hasattr(gen, "__next__")

        with pytest.raises(sqlite3.OperationalError):
            next(gen)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
