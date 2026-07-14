"""Unit tests for Bug #1405: blank-out must never delete the shared temporal
bookkeeping directory (`code-indexer-temporal/`).

`blank_out_legacy_temporal_collections()` (Story #1290 AC19/AC20) enumerates
every temporal collection under the index path and hard-deletes any without
a v2 `temporal_structure.json` marker. The bare-named bookkeeping directory
that anchors the single shared `TemporalMetadataStore` (holding
`temporal_metadata.db`, used by ALL quarterly shards) shares that exact bare
name (`code-indexer-temporal`, `LEGACY_TEMPORAL_COLLECTION`) with the
genuinely obsolete pre-#1290 monolithic legacy collection -- and never
receives a v2 marker either. Blank-out was hard-deleting it on every single
run, amputating the shared metadata store.

The fix discriminates by DATA PRESENCE: the bookkeeping directory never
holds `hnsw_index.bin` or any `vector_*.json` file (only
`temporal_metadata.db` / `temporal_progress.json` / lock/tmp files), while a
genuine legacy monolith always holds real vector data. These tests prove:
the bookkeeping dir survives (repeatedly), while genuine legacy monoliths
(bare-named with data, provider-suffixed, v1-marker) are still deleted, and
v2-marked collections are still preserved.
"""

from pathlib import Path

from src.code_indexer.services.temporal.temporal_blank_out import (
    blank_out_legacy_temporal_collections,
)
from src.code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from src.code_indexer.services.temporal.temporal_structure_marker import (
    write_structure_marker,
)


def _make_bookkeeping_dir(index_path: Path) -> Path:
    """The shared bookkeeping directory: bare name, metadata only, no vector data."""
    coll_dir = index_path / LEGACY_TEMPORAL_COLLECTION
    coll_dir.mkdir(parents=True)
    (coll_dir / "temporal_metadata.db").write_bytes(b"sqlite-bytes-here")
    (coll_dir / "temporal_progress.json").write_text('{"done": true}')
    return coll_dir


def _make_legacy_collection_with_hnsw(index_path: Path, name: str) -> Path:
    """A genuine pre-#1290 legacy monolith: real HNSW data, no v2 marker."""
    coll_dir = index_path / name
    coll_dir.mkdir(parents=True)
    (coll_dir / "temporal_progress.json").write_text("{}")
    (coll_dir / "meta.json").write_text("{}")
    (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
    return coll_dir


def _make_legacy_collection_with_nested_vector(index_path: Path, name: str) -> Path:
    """A genuine legacy monolith whose vector data lives in nested vector_*.json
    (quantization-style shard layout), with no hnsw_index.bin and no v2 marker.
    """
    coll_dir = index_path / name
    coll_dir.mkdir(parents=True)
    nested = coll_dir / "ab" / "cd"
    nested.mkdir(parents=True)
    (nested / "vector_deadbeef.json").write_text("{}")
    return coll_dir


def _make_v2_collection(index_path: Path, name: str, model_slug: str) -> Path:
    coll_dir = index_path / name
    coll_dir.mkdir(parents=True)
    write_structure_marker(coll_dir, model_slug=model_slug)
    (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
    return coll_dir


class TestBookkeepingDirectorySurvives:
    def test_bookkeeping_dir_survives_blank_out(self, tmp_path):
        bookkeeping_dir = _make_bookkeeping_dir(tmp_path)
        metadata_bytes_before = (bookkeeping_dir / "temporal_metadata.db").read_bytes()
        progress_before = (bookkeeping_dir / "temporal_progress.json").read_text()

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert LEGACY_TEMPORAL_COLLECTION not in deleted
        assert bookkeeping_dir.exists()
        assert (
            bookkeeping_dir / "temporal_metadata.db"
        ).read_bytes() == metadata_bytes_before
        assert (
            bookkeeping_dir / "temporal_progress.json"
        ).read_text() == progress_before

    def test_bookkeeping_dir_survives_repeated_runs(self, tmp_path):
        bookkeeping_dir = _make_bookkeeping_dir(tmp_path)

        first_run = blank_out_legacy_temporal_collections(tmp_path)
        second_run = blank_out_legacy_temporal_collections(tmp_path)

        assert LEGACY_TEMPORAL_COLLECTION not in first_run
        assert LEGACY_TEMPORAL_COLLECTION not in second_run
        assert bookkeeping_dir.exists()


class TestGenuineLegacyMonolithStillDeleted:
    def test_bare_named_legacy_with_hnsw_still_deleted(self, tmp_path):
        legacy_dir = _make_legacy_collection_with_hnsw(
            tmp_path, LEGACY_TEMPORAL_COLLECTION
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert LEGACY_TEMPORAL_COLLECTION in deleted
        assert not legacy_dir.exists()

    def test_bare_named_legacy_with_nested_vector_json_still_deleted(self, tmp_path):
        legacy_dir = _make_legacy_collection_with_nested_vector(
            tmp_path, LEGACY_TEMPORAL_COLLECTION
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert LEGACY_TEMPORAL_COLLECTION in deleted
        assert not legacy_dir.exists()

    def test_provider_suffixed_legacy_dir_still_deleted(self, tmp_path):
        name = "code-indexer-temporal-embed_v4_0-2023Q1"
        legacy_dir = _make_legacy_collection_with_hnsw(tmp_path, name)

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert name in deleted
        assert not legacy_dir.exists()


class TestV2CollectionStillPreserved:
    def test_v2_marked_shard_still_preserved(self, tmp_path):
        v2_dir = _make_v2_collection(
            tmp_path,
            "code-indexer-temporal-voyage_context_4-2024Q1",
            "voyage_context_4",
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert deleted == []
        assert v2_dir.exists()


class TestMixedTree:
    def test_mixed_tree_only_legacy_shard_deleted(self, tmp_path):
        bookkeeping_dir = _make_bookkeeping_dir(tmp_path)
        v2_dir = _make_v2_collection(
            tmp_path,
            "code-indexer-temporal-voyage_context_4-2024Q1",
            "voyage_context_4",
        )
        legacy_name = "code-indexer-temporal-embed_v4_0-2023Q1"
        legacy_dir = _make_legacy_collection_with_hnsw(tmp_path, legacy_name)

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert deleted == [legacy_name]
        assert bookkeeping_dir.exists()
        assert v2_dir.exists()
        assert not legacy_dir.exists()
