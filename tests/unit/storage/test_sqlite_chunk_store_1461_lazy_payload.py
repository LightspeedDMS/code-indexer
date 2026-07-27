"""Story #1461 salvage item #9 [LOW, perf] (Epic #1454).

``ChunkStore.stream_all()`` unconditionally zstd-decompresses + json.loads
the full opaque ``data`` blob (payload + chunk_text/git_blob_hash + diff) for
every row. HNSW rebuild (``HNSWIndexManager._load_vectors_from_chunks_db``)
only needs the vector, the point_id, and the top-level indexed ``path``
column in the common case -- it decodes the payload ONLY to read
``hidden_branches`` for the Bug #306 branch-visibility filter.

``stream_for_index_rebuild(need_payload)`` is a new, purely additive method
(``stream_all`` is untouched) that lets the caller skip the decompress+parse
work entirely when the payload isn't needed: with ``need_payload=False`` it
selects only ``point_id, vector, path`` and never touches the ``data``
column; with ``need_payload=True`` it decodes ``data`` exactly like
``stream_all`` and additionally yields the decoded ``payload`` dict.
"""

from pathlib import Path

import zstandard

from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _record(point_id: str, path: str, **payload_extra) -> dict:
    payload = {"path": path}
    payload.update(payload_extra)
    return {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3],
        "payload": payload,
        "chunk_text": "large text blob " * 50,
    }


def _install_decompress_counter(monkeypatch) -> dict:
    """Wrap the REAL zstandard.ZstdDecompressor.decompress with a counting
    spy that still performs the real decompression -- proves both the call
    count AND that any calls that do happen still produce correct data."""
    call_count = {"n": 0}
    original = zstandard.ZstdDecompressor.decompress

    def counting_decompress(self, *args, **kwargs):
        call_count["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(zstandard.ZstdDecompressor, "decompress", counting_decompress)
    return call_count


class TestStreamForIndexRebuildNoPayload:
    def test_yields_id_vector_path_with_payload_none(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record("v0", "a.py"), _record("v1", "b.py")])

        results = list(store.stream_for_index_rebuild(need_payload=False))
        by_id = {r[0]: r for r in results}

        assert set(by_id) == {"v0", "v1"}
        assert by_id["v0"][2] == "a.py"
        assert by_id["v0"][3] is None
        assert by_id["v1"][2] == "b.py"
        assert by_id["v1"][3] is None

    def test_vectors_identical_to_stream_all(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        records = [_record(f"v{i}", f"f{i}.py") for i in range(4)]
        store.write_batch(records)

        baseline = {r["id"]: list(r["vector"]) for r in store.stream_all()}
        fast = {
            r[0]: list(r[1]) for r in store.stream_for_index_rebuild(need_payload=False)
        }

        assert set(baseline) == set(fast)
        for point_id in baseline:
            assert baseline[point_id] == fast[point_id]

    def test_never_decompresses_data_blob(self, tmp_path: Path, monkeypatch) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"v{i}", f"f{i}.py") for i in range(5)])

        call_count = _install_decompress_counter(monkeypatch)

        list(store.stream_for_index_rebuild(need_payload=False))

        assert call_count["n"] == 0

    def test_empty_store_yields_nothing(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")

        assert list(store.stream_for_index_rebuild(need_payload=False)) == []


class TestStreamForIndexRebuildWithPayload:
    def test_yields_full_decoded_payload(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record("v0", "a.py", hidden_branches=["feature-x"])])

        results = list(store.stream_for_index_rebuild(need_payload=True))

        assert len(results) == 1
        point_id, vector, path, payload = results[0]
        assert point_id == "v0"
        assert path == "a.py"
        assert payload is not None
        assert payload["hidden_branches"] == ["feature-x"]

    def test_decompresses_exactly_once_per_row(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"v{i}", f"f{i}.py") for i in range(5)])

        call_count = _install_decompress_counter(monkeypatch)

        list(store.stream_for_index_rebuild(need_payload=True))

        assert call_count["n"] == 5

    def test_matches_stream_all_payload_and_vector(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        records = [
            _record("v0", "a.py", hidden_branches=["x"]),
            _record("v1", "b.py"),
        ]
        store.write_batch(records)

        baseline = {r["id"]: r for r in store.stream_all()}
        fast_results = list(store.stream_for_index_rebuild(need_payload=True))

        assert len(fast_results) == 2
        for point_id, vector, path, payload in fast_results:
            baseline_record = baseline[point_id]
            assert list(vector) == list(baseline_record["vector"])
            assert path == baseline_record["payload"]["path"]
            assert payload == baseline_record["payload"]

    def test_empty_store_yields_nothing(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")

        assert list(store.stream_for_index_rebuild(need_payload=True)) == []


class TestStreamForIndexRebuildCursorCleanup:
    def test_cursor_closed_on_early_break(self, tmp_path: Path) -> None:
        """Mirrors stream_all's guaranteed-close-on-early-exit contract."""
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"v{i}", f"f{i}.py") for i in range(3)])

        gen = store.stream_for_index_rebuild(need_payload=False)
        first = next(gen)
        assert first is not None
        gen.close()

        # Store remains usable afterward -- no leaked/locked cursor.
        assert len(list(store.stream_for_index_rebuild(need_payload=False))) == 3
