"""Unit tests for temporal blank-out (Story #1290 AC19, AC20).

Blank-out hard-deletes any temporal collection whose temporal_structure.json
is MISSING or whose version < 2 (including a legacy embed_v4_0 slug that
would otherwise collide with a new Cohere v2 collection), BEFORE any read,
reconcile, or write. It must be idempotent (a second run is a no-op) and
must never touch a v2 collection or a non-temporal collection.
"""

from pathlib import Path

from src.code_indexer.services.temporal.temporal_blank_out import (
    blank_out_legacy_temporal_collections,
)
from src.code_indexer.services.temporal.temporal_structure_marker import (
    write_structure_marker,
)


def _make_legacy_collection(index_path: Path, name: str) -> Path:
    """A legacy collection: directory exists, NO temporal_structure.json."""
    coll_dir = index_path / name
    coll_dir.mkdir(parents=True)
    (coll_dir / "temporal_progress.json").write_text("{}")
    (coll_dir / "meta.json").write_text("{}")
    (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
    return coll_dir


def _make_v2_collection(index_path: Path, name: str, model_slug: str) -> Path:
    coll_dir = index_path / name
    coll_dir.mkdir(parents=True)
    write_structure_marker(coll_dir, model_slug=model_slug)
    (coll_dir / "hnsw_index.bin").write_bytes(b"fake")
    return coll_dir


class TestBlankOutDeletesLegacyCollections:
    def test_legacy_collection_missing_marker_is_deleted(self, tmp_path):
        legacy_dir = _make_legacy_collection(
            tmp_path, "code-indexer-temporal-voyage_code_3-2024Q1"
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert "code-indexer-temporal-voyage_code_3-2024Q1" in deleted
        assert not legacy_dir.exists()

    def test_version_1_marker_collection_is_deleted(self, tmp_path):
        coll_dir = tmp_path / "code-indexer-temporal-embed_v4_0-2024Q1"
        coll_dir.mkdir(parents=True)
        (coll_dir / "temporal_structure.json").write_text(
            '{"version": 1, "layout": "monolith"}'
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert "code-indexer-temporal-embed_v4_0-2024Q1" in deleted
        assert not coll_dir.exists()

    def test_legacy_and_new_share_slug_only_legacy_deleted(self, tmp_path):
        """AC19: OLD Cohere embed_v4_0 index deleted BEFORE the new one is touched."""
        old_legacy = _make_legacy_collection(
            tmp_path, "code-indexer-temporal-embed_v4_0-2023Q1"
        )
        new_v2 = _make_v2_collection(
            tmp_path, "code-indexer-temporal-embed_v4_0-2024Q2", "embed_v4_0"
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert "code-indexer-temporal-embed_v4_0-2023Q1" in deleted
        assert not old_legacy.exists()
        assert "code-indexer-temporal-embed_v4_0-2024Q2" not in deleted
        assert new_v2.exists()


class TestBlankOutPreservesV2AndNonTemporal:
    def test_v2_collection_is_not_deleted(self, tmp_path):
        v2_dir = _make_v2_collection(
            tmp_path,
            "code-indexer-temporal-voyage_context_4-2024Q1",
            "voyage_context_4",
        )

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert deleted == []
        assert v2_dir.exists()

    def test_non_temporal_collection_untouched(self, tmp_path):
        regular_dir = tmp_path / "voyage-code-3"
        regular_dir.mkdir(parents=True)
        (regular_dir / "hnsw_index.bin").write_bytes(b"fake")

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert deleted == []
        assert regular_dir.exists()


class TestBlankOutIdempotent:
    def test_second_run_is_a_no_op(self, tmp_path):
        _make_legacy_collection(tmp_path, "code-indexer-temporal-voyage_code_3-2024Q1")

        first_run = blank_out_legacy_temporal_collections(tmp_path)
        second_run = blank_out_legacy_temporal_collections(tmp_path)

        assert first_run == ["code-indexer-temporal-voyage_code_3-2024Q1"]
        assert second_run == []

    def test_missing_index_path_returns_empty_list(self, tmp_path):
        missing_path = tmp_path / "does_not_exist"
        assert blank_out_legacy_temporal_collections(missing_path) == []
