"""Unit tests for the shared chunk-layout discriminator + resolver (Story #1456
prerequisite for Epic #1454's chunk-storage consolidation).

This module is the SOLE authority both Story #1456 (semantic index
consolidation) and the future Story #1458 (fleet migration) share for
deciding whether a collection directory uses the legacy sharded
``vector_*.json`` layout or the new consolidated ``chunks.db`` layout.

Fail-closed contract under test: any absent/malformed/invalid discriminator
must resolve to ``ChunkLayout.SHARDED_JSON`` -- NEVER raise, NEVER guess.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.storage.shared.chunk_layout import (
    CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)


class TestResolveChunkLayoutFailClosed:
    """Every absent/malformed input resolves to SHARDED_JSON, never raises."""

    def test_missing_collection_meta_json_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        # No collection_meta.json at all in the directory.
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_missing_collection_dir_resolves_sharded_json(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist"
        result = resolve_chunk_layout(nonexistent)
        assert result == ChunkLayout.SHARDED_JSON

    def test_collection_meta_json_without_chunks_db_key_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"name": "coll", "vector_size": 1024}))
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_chunks_db_key_present_but_enabled_false_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(
            json.dumps({"chunks_db": {"enabled": False, "version": 1}})
        )
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_chunks_db_key_wrong_type_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"chunks_db": "not-a-dict"}))
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_chunks_db_enabled_missing_type_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"chunks_db": {"enabled": "yes"}}))
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_malformed_json_resolves_sharded_json_never_raises(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text("{not valid json::")
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_empty_file_resolves_sharded_json_never_raises(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text("")
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_top_level_not_a_dict_resolves_sharded_json(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps([1, 2, 3]))
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.SHARDED_JSON

    def test_unreadable_directory_path_given_as_file_resolves_sharded_json(
        self, tmp_path: Path
    ) -> None:
        # Pass a path to a FILE (not a directory) as collection_dir -- the
        # resolver must not raise NotADirectoryError, it must fail closed.
        not_a_dir = tmp_path / "im_a_file"
        not_a_dir.write_text("hello")
        result = resolve_chunk_layout(not_a_dir)
        assert result == ChunkLayout.SHARDED_JSON


class TestResolveChunkLayoutValidDiscriminator:
    def test_valid_enabled_discriminator_resolves_chunks_db(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "chunks_db": {
                        "enabled": True,
                        "version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
                    }
                }
            )
        )
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.CHUNKS_DB

    def test_discriminator_coexists_with_untouched_metadata_keys(
        self, tmp_path: Path
    ) -> None:
        # AC1: chunks_db must NOT be nested under metadata/hnsw_index -- it
        # must be a top-level sibling key, and existing keys are untouched.
        meta_path = tmp_path / "collection_meta.json"
        original = {
            "name": "coll",
            "vector_size": 1024,
            "metadata": {"hnsw_index": {"id_mapping": {"0": "point-a"}}},
            "chunks_db": {"enabled": True, "version": 1},
        }
        meta_path.write_text(json.dumps(original))
        result = resolve_chunk_layout(tmp_path)
        assert result == ChunkLayout.CHUNKS_DB

        # untouched sibling keys still readable directly
        reloaded = json.loads(meta_path.read_text())
        assert reloaded["metadata"]["hnsw_index"]["id_mapping"] == {"0": "point-a"}
        assert reloaded["vector_size"] == 1024


class TestWriteChunksDbDiscriminator:
    """write_chunks_db_discriminator is the sole writer of the flag -- it must
    merge into an existing collection_meta.json without disturbing other
    top-level keys, and must never nest under metadata/hnsw_index."""

    def test_writes_discriminator_into_fresh_file(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"name": "coll", "vector_size": 1024}))

        write_chunks_db_discriminator(tmp_path)

        reloaded = json.loads(meta_path.read_text())
        assert reloaded["chunks_db"] == {
            "enabled": True,
            "version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
        }
        assert reloaded["name"] == "coll"
        assert reloaded["vector_size"] == 1024

    def test_write_then_resolve_round_trips_to_chunks_db(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"name": "coll"}))

        write_chunks_db_discriminator(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

    def test_write_raises_when_collection_meta_json_missing(
        self, tmp_path: Path
    ) -> None:
        # AC1: the discriminator write is a mandatory FINAL step layered onto
        # an already-existing collection_meta.json (written earlier in the
        # fresh-collection path) -- it must never silently create one from
        # scratch, which would mask a caller ordering bug.
        with pytest.raises(FileNotFoundError):
            write_chunks_db_discriminator(tmp_path)


class TestWriteChunksDbDiscriminatorAtomicity:
    """Code review finding #2 (HIGH): collection_meta.json holds the
    load-bearing hnsw_index.id_mapping. A mid-write crash on a bare
    write_text() could DESTROY that file -- strictly worse than AC1's
    fail-closed guarantee. write_chunks_db_discriminator must use the same
    atomic+fsync+os.replace pattern this repo already has for this exact
    file (hnsw_index_manager.py's _atomic_write_metadata_durable, Bug #1407)."""

    def test_commit_goes_through_os_replace(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"name": "coll"}))

        with patch("os.replace", wraps=os.replace) as spy:
            write_chunks_db_discriminator(tmp_path)

        assert spy.call_count >= 1
        # The final replace target must be collection_meta.json itself.
        final_call = spy.call_args_list[-1]
        assert str(final_call.args[1]) == str(meta_path)

    def test_no_leftover_tmp_file_after_successful_write(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(json.dumps({"name": "coll"}))

        write_chunks_db_discriminator(tmp_path)

        leftover_tmp_files = [
            f for f in tmp_path.iterdir() if f.name != "collection_meta.json"
        ]
        assert leftover_tmp_files == []

    def test_preserves_existing_keys_through_atomic_rewrite(
        self, tmp_path: Path
    ) -> None:
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "name": "coll",
                    "vector_size": 1024,
                    "metadata": {"hnsw_index": {"id_mapping": {"0": "point-a"}}},
                }
            )
        )

        write_chunks_db_discriminator(tmp_path)

        reloaded = json.loads(meta_path.read_text())
        assert reloaded["metadata"]["hnsw_index"]["id_mapping"] == {"0": "point-a"}
        assert reloaded["chunks_db"] == {
            "enabled": True,
            "version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
        }


class TestChunkLayoutEnum:
    def test_has_sharded_json_and_chunks_db_members(self) -> None:
        assert ChunkLayout.SHARDED_JSON is not None
        assert ChunkLayout.CHUNKS_DB is not None
        assert ChunkLayout.SHARDED_JSON != ChunkLayout.CHUNKS_DB
