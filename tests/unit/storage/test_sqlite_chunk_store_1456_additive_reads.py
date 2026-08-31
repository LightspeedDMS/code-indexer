"""Story #1456 additive ChunkStore read methods.

AC7's technical requirements literally prescribe ``SELECT point_id FROM
chunks`` and ``SELECT DISTINCT path FROM chunks`` as the mechanism several
consumers (``get_all_indexed_files``, public ``load_id_index``,
``_calculate_and_save_unique_file_count``, count fallbacks) must use instead
of the retired ``id_index.bin`` / rglob scans. Story #1455's ``ChunkStore``
did not anticipate these lightweight queries (only ``count()`` and the
full-decode ``stream_all()`` existed). These two methods are PURELY
ADDITIVE -- they do not change any existing ChunkStore behavior or
signature.
"""

from pathlib import Path

from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _record(point_id: str, path: str) -> dict:
    return {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3],
        "payload": {"path": path},
        "chunk_text": "x",
    }


class TestAllPointIds:
    def test_returns_every_stored_point_id(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch(
            [_record("v0", "a.py"), _record("v1", "b.py"), _record("v2", "c.py")]
        )

        result = store.all_point_ids()

        assert result == {"v0", "v1", "v2"}

    def test_empty_store_returns_empty_set(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")

        assert store.all_point_ids() == set()

    def test_reflects_deletions(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record("v0", "a.py"), _record("v1", "b.py")])
        store.delete(["v0"])

        assert store.all_point_ids() == {"v1"}


class TestDistinctPaths:
    def test_returns_unique_paths_across_multiple_chunks_per_file(
        self, tmp_path: Path
    ) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch(
            [
                _record("v0", "a.py"),
                _record("v1", "a.py"),  # same file, second chunk
                _record("v2", "b.py"),
            ]
        )

        result = store.distinct_paths()

        assert result == {"a.py", "b.py"}

    def test_empty_store_returns_empty_set(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")

        assert store.distinct_paths() == set()

    def test_ignores_records_with_no_path(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        no_path_record = {
            "id": "v0",
            "vector": [0.1, 0.2, 0.3],
            "payload": {},
            "chunk_text": "x",
        }
        store.write_batch([no_path_record, _record("v1", "b.py")])

        result = store.distinct_paths()

        assert result == {"b.py"}
