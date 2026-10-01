"""Story #1492 AC1: resolve_chunk_layout() accepts pre-parsed cached_meta.

FilesystemVectorStore.search() fetches collection_meta.json ONCE (via
CollectionMetaCache) and must be able to pass the ALREADY-PARSED dict into
resolve_chunk_layout() instead of it re-reading+re-parsing the file a
second time. The bare-call (no cached_meta) behavior must remain 100%
byte-identical for every pre-existing caller (backward compatibility);
the new cached_meta parameter is purely additive/opt-in.
"""

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)


class TestResolveChunkLayoutCachedMetaProvided:
    def test_cached_meta_chunks_db_skips_file_read(self, tmp_path):
        # Deliberately do NOT write collection_meta.json to disk -- if the
        # function tried to read the file, this would fail closed to
        # SHARDED_JSON, proving the cached_meta path is actually taken.
        cached_meta = {"chunks_db": {"version": 1}}
        assert (
            resolve_chunk_layout(tmp_path, cached_meta=cached_meta)
            == ChunkLayout.CHUNKS_DB
        )

    def test_cached_meta_sharded_json_when_no_discriminator(self, tmp_path):
        cached_meta = {"vector_size": 1024}
        assert (
            resolve_chunk_layout(tmp_path, cached_meta=cached_meta)
            == ChunkLayout.SHARDED_JSON
        )

    def test_cached_meta_none_bypasses_disk_even_when_disk_says_chunks_db(
        self, tmp_path
    ):
        # The ON-DISK file genuinely resolves to CHUNKS_DB (proven by the
        # bare-call assertion below). Passing cached_meta=None explicitly
        # must still resolve SHARDED_JSON (the fail-closed contract for
        # "cache reported no valid metadata") -- proving the disk is never
        # consulted when cached_meta is explicitly supplied, even as None.
        (tmp_path / "collection_meta.json").write_text("{}")
        write_chunks_db_discriminator(tmp_path)
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

        assert (
            resolve_chunk_layout(tmp_path, cached_meta=None) == ChunkLayout.SHARDED_JSON
        )


class TestResolveChunkLayoutBackwardCompatibility:
    def test_no_cached_meta_arg_reads_real_file_unchanged(self, tmp_path):
        """Backward compatibility: omitting cached_meta reads the real
        on-disk file exactly as before this story."""
        (tmp_path / "collection_meta.json").write_text("{}")
        write_chunks_db_discriminator(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
