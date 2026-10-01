"""TDD tests for Bug #1575 Part A -- FilesystemVectorStore-level
`distinct_content_paths()` / `fetch_points_for_paths()`, dispatching
correctly across BOTH storage layouts (SHARDED_JSON and CHUNKS_DB).

RED phase: every test in this file must FAIL against pre-Part-A
FilesystemVectorStore (no `distinct_content_paths`/`fetch_points_for_paths`
methods).
"""

from typing import Any, Dict

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
)

VECTOR_DIM = 16


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _points(paths, prefix="p"):
    return [
        {
            "id": f"{prefix}_{i}",
            "vector": _vector(i),
            "payload": {"path": path, "type": "content"},
        }
        for i, path in enumerate(paths)
    ]


class TestDistinctContentPathsShardedJsonLiveSession:
    """SHARDED_JSON: mid-session (before end_indexing), the LIVE in-memory
    PathIndex maintained by begin_indexing()/upsert_points() is
    authoritative and must be used directly -- no disk I/O needed.
    """

    def test_returns_paths_upserted_this_session_before_end_indexing(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)

        store.begin_indexing("coll")
        store.upsert_points("coll", _points(["src/a.py", "src/b.py"]))

        result = store.distinct_content_paths("coll")

        assert result == {"src/a.py", "src/b.py"}


class TestDistinctContentPathsShardedJsonFallbackRebuild:
    """SHARDED_JSON: a FRESH store instance (no in-memory session state for
    this collection) must fall back to an authoritative streaming rebuild
    from disk -- never trust an unverified persisted path_index.bin.
    """

    def test_returns_paths_from_a_prior_sessions_finalized_collection(self, tmp_path):
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points("coll", _points(["src/a.py", "src/b.py"]))
        writer.end_indexing("coll")

        fresh_reader = FilesystemVectorStore(base_path=tmp_path)
        result = fresh_reader.distinct_content_paths("coll")

        assert result == {"src/a.py", "src/b.py"}

    def test_excludes_non_content_path_bearing_record_from_mixed_corpus(self, tmp_path):
        """Codex review follow-up (Bug #1575 Part A, finding 3): the
        SHARDED_JSON disk-fallback scan must filter on
        ``payload.type == "content"`` exactly like the CHUNKS_DB path
        does -- the discriminating case is a MIX of a real content record
        alongside a synthetic non-content path-bearing record (a
        content-only corpus would pass on both a correct and a broken
        implementation).
        """
        import json as _json

        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points("coll", _points(["src/a.py"]))
        writer.end_indexing("coll")

        # Inject a raw, non-content path-bearing record directly onto disk
        # -- simulating a hypothetical writer shape, mirroring
        # test_chunk_storage_1575_part_a_ac5.py's identical CHUNKS_DB-level
        # discriminating test.
        collection_path = tmp_path / "coll"
        extra_file = collection_path / "vector_extra_noncontent.json"
        extra_file.write_text(
            _json.dumps(
                {
                    "id": "extra-noncontent",
                    "payload": {"path": "src/diff_only.py", "type": "diff"},
                }
            )
        )

        fresh_reader = FilesystemVectorStore(base_path=tmp_path)
        result = fresh_reader.distinct_content_paths("coll")

        assert result == {"src/a.py"}
        assert "src/diff_only.py" not in result

    def test_fallback_scan_does_not_persist_path_index_bin(self, tmp_path):
        """Codex review follow-up (Bug #1575 Part A, finding 4): the
        disk-fallback for a pure "what content paths exist" question must
        be a genuinely separate, lightweight streaming scan -- never the
        full ``_rebuild_path_index_from_disk`` (which persists
        ``path_index.bin`` as a side effect on every call). Discriminating:
        the OLD implementation (reusing ``_resolve_authoritative_path_index``
        -> ``_rebuild_path_index_from_disk``) would create this file; the
        fix must not.
        """
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points("coll", _points(["src/a.py", "src/b.py"]))
        writer.end_indexing("coll")

        collection_path = tmp_path / "coll"
        path_index_file = collection_path / "path_index.bin"
        if path_index_file.exists():
            path_index_file.unlink()

        fresh_reader = FilesystemVectorStore(base_path=tmp_path)
        result = fresh_reader.distinct_content_paths("coll")

        assert result == {"src/a.py", "src/b.py"}
        assert not path_index_file.exists(), (
            "distinct_content_paths()'s disk fallback must be a pure "
            "path-only scan -- it must never persist path_index.bin as a "
            "side effect (that full point-id reverse index belongs only "
            "to _rebuild_path_index_from_disk's own targeted-lookup job)"
        )


@pytest.fixture
def _indexed_collection_fresh_reader(tmp_path):
    """Shared setup for the Gap-2 malformed-file tests below: a finalized
    single-content-point collection plus a FRESH reader instance (no
    active in-memory session -- forces the disk-fallback scan path) and
    the collection's on-disk directory, so each test only needs to write
    its own malformed file before calling ``distinct_content_paths``.
    """
    writer = FilesystemVectorStore(base_path=tmp_path)
    writer.create_collection("coll", vector_size=VECTOR_DIM)
    writer.begin_indexing("coll")
    writer.upsert_points("coll", _points(["src/a.py"]))
    writer.end_indexing("coll")

    collection_path = tmp_path / "coll"
    fresh_reader = FilesystemVectorStore(base_path=tmp_path)
    return fresh_reader, collection_path


class TestDistinctContentPathsShardedJsonFallbackMalformedFilesFailLoud:
    """2nd Codex review follow-up (Bug #1575 Part A, Gap 2): the disk
    fallback (``_stream_authoritative_content_paths_from_disk``) used to
    log-and-skip a malformed record and never handled
    ``UnicodeDecodeError`` at all. This module's own established
    convention for exactly this class of problem (see
    ``ScrollDataIntegrityError``, used by the legacy SHARDED_JSON scroll
    path elsewhere in ``filesystem_vector_store.py``) is: a genuinely
    malformed/corrupt record that is PRESENT must FAIL LOUD naming the
    file, never be silently dropped -- silent skipping in a
    path-enumeration context can under-report which files exist, which is
    a real data-integrity risk for a fallback whose whole job is being the
    "authoritative" answer.
    """

    def test_invalid_json_file_fails_loud_naming_the_file(
        self, _indexed_collection_fresh_reader
    ):
        fresh_reader, collection_path = _indexed_collection_fresh_reader
        bad_file = collection_path / "vector_corrupt.json"
        bad_file.write_text("{not valid json")

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            fresh_reader.distinct_content_paths("coll")

        assert str(bad_file) in str(exc_info.value)

    def test_non_dict_json_root_fails_loud_naming_the_file(
        self, _indexed_collection_fresh_reader
    ):
        fresh_reader, collection_path = _indexed_collection_fresh_reader
        bad_file = collection_path / "vector_listroot.json"
        bad_file.write_text("[1, 2, 3]")

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            fresh_reader.distinct_content_paths("coll")

        assert str(bad_file) in str(exc_info.value)

    def test_undecodable_bytes_file_fails_loud_naming_the_file(
        self, _indexed_collection_fresh_reader
    ):
        fresh_reader, collection_path = _indexed_collection_fresh_reader
        bad_file = collection_path / "vector_badbytes.json"
        # 0xff is not a valid UTF-8 lead byte -- guarantees a
        # UnicodeDecodeError on read/decode, regardless of platform locale.
        bad_file.write_bytes(b"\xff\xfe\x00\x01")

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            fresh_reader.distinct_content_paths("coll")

        assert str(bad_file) in str(exc_info.value)


class TestDistinctContentPathsShardedJsonFallbackVanishedFileGraceful:
    """Deliberately the OPPOSITE of the class above: a file that is
    genuinely MISSING (deleted between rglob()'s listing and the open()
    call -- the Bug #1486 Finding-5 race) must NOT raise -- only a
    PRESENT-but-malformed file is an integrity fault.
    """

    def test_vanished_file_mid_scan_is_still_skipped_gracefully(
        self, tmp_path, monkeypatch
    ):
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        writer.begin_indexing("coll")
        writer.upsert_points("coll", _points(["src/a.py", "src/b.py"]))
        writer.end_indexing("coll")

        fresh_reader = FilesystemVectorStore(base_path=tmp_path)

        import builtins

        real_open = builtins.open
        state = {"raised": False}

        def flaky_open(file, *args, **kwargs):
            path_str = str(file)
            if (
                not state["raised"]
                and "vector_" in path_str
                and path_str.endswith(".json")
            ):
                state["raised"] = True
                raise FileNotFoundError(f"simulated concurrent deletion: {file}")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky_open)

        result = fresh_reader.distinct_content_paths("coll")

        assert state["raised"] is True, (
            "the simulated concurrent-deletion race must have actually "
            "fired for this test to be meaningful"
        )
        assert result.issubset({"src/a.py", "src/b.py"})


class TestDistinctContentPathsChunksDb:
    """CHUNKS_DB: dispatches to ChunkStore.distinct_content_paths()."""

    def test_returns_content_paths_for_chunks_db_collection(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(["src/a.py", "src/c.py"]))
        store.end_indexing("coll")

        result = store.distinct_content_paths("coll")

        assert result == {"src/a.py", "src/c.py"}


class TestFetchPointsForPathsShardedJson:
    """SHARDED_JSON: path-index lookup to point ids, then targeted record
    reads -- never a full collection scan. Points for paths NOT requested
    must be excluded.
    """

    def test_returns_only_points_for_requested_paths(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(["src/a.py", "src/b.py", "src/c.py"]))
        store.end_indexing("coll")

        result = store.fetch_points_for_paths("coll", {"src/a.py", "src/c.py"})

        ids = {p["id"] for p in result}
        assert ids == {"p_0", "p_2"}
        for point in result:
            assert point["payload"]["path"] in {"src/a.py", "src/c.py"}


class TestFetchPointsForPathsChunksDb:
    """CHUNKS_DB: dispatches to ChunkStore.fetch_points_for_paths()."""

    def test_returns_only_points_for_requested_paths(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(["src/a.py", "src/b.py", "src/c.py"]))
        store.end_indexing("coll")

        result = store.fetch_points_for_paths("coll", {"src/b.py"})

        ids = {p["id"] for p in result}
        assert ids == {"p_1"}
        assert result[0]["payload"]["path"] == "src/b.py"

    def test_requests_payload_only_from_chunk_store(self, tmp_path, monkeypatch):
        """Codex review follow-up (Bug #1575 Part A, finding 6): the FSV
        caller only ever needs id/payload -- it must ask ChunkStore for a
        payload_only fetch rather than decoding (and discarding) the
        vector for every matched row.
        """
        from code_indexer.storage.sqlite_chunk_store import ChunkStore

        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(["src/a.py", "src/b.py"]))
        store.end_indexing("coll")

        original = ChunkStore.fetch_points_for_paths
        captured: Dict[str, Any] = {}

        def spy(self, paths, **kwargs):
            captured["payload_only"] = kwargs.get("payload_only")
            return original(self, paths, **kwargs)

        monkeypatch.setattr(ChunkStore, "fetch_points_for_paths", spy)

        result = store.fetch_points_for_paths("coll", {"src/a.py"})

        assert captured.get("payload_only") is True, (
            "FilesystemVectorStore.fetch_points_for_paths() must request "
            "payload_only=True from ChunkStore -- it never needs the vector"
        )
        assert result[0]["payload"]["path"] == "src/a.py"
