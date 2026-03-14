"""Tests for Story #439: Remove rglob count-mismatch fallback from HNSW is_stale().

These tests verify that is_stale() does NOT perform filesystem scans (rglob) and
relies solely on the explicit is_stale flag in metadata.

Acceptance criteria:
1. is_stale returns False when flag is not set, regardless of count mismatch on disk
2. is_stale returns True when explicit flag is set
3. is_stale returns True when no metadata exists
4. is_stale returns True when no hnsw_index key in metadata
5. Filtered rebuild staleness still works (count != visible_count)
6. Filtered rebuild fresh detection still works (count == visible_count)
7. No rglob or filesystem scan is performed during is_stale()
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


@pytest.fixture
def collection_path(tmp_path):
    """Create a temporary collection directory."""
    coll = tmp_path / "test_collection"
    coll.mkdir()
    return coll


def write_metadata(collection_path: Path, hnsw_info: dict) -> None:
    """Helper: write collection_meta.json with the given hnsw_index block."""
    meta = {"hnsw_index": hnsw_info}
    with open(collection_path / "collection_meta.json", "w") as f:
        json.dump(meta, f)


def create_vector_files(collection_path: Path, count: int) -> None:
    """Helper: create dummy vector_*.json files to simulate on-disk vectors."""
    for i in range(count):
        (collection_path / f"vector_{i:06d}.json").write_text("{}")


class TestIsStaleNoRglob:
    """Verify is_stale() no longer performs rglob filesystem scans."""

    def test_is_stale_false_despite_count_mismatch(self, collection_path):
        """AC #1: is_stale returns False when is_stale flag is False,
        even when actual file count differs from stored count."""
        # Metadata says 100 vectors, is_stale=False
        write_metadata(
            collection_path,
            {"vector_count": 100, "is_stale": False},
        )
        # Put 105 actual vector files on disk (count mismatch)
        create_vector_files(collection_path, 105)

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is False, (
            "is_stale() should return False when is_stale flag is False, "
            "even if disk vector count (105) differs from stored count (100). "
            "The rglob fallback must be removed."
        )

    def test_is_stale_no_filesystem_scan(self, collection_path):
        """AC #7: is_stale() must NOT call Path.rglob during execution."""
        write_metadata(
            collection_path,
            {"vector_count": 100, "is_stale": False},
        )
        create_vector_files(collection_path, 200)  # big mismatch

        manager = HNSWIndexManager()

        # Patch rglob to raise AssertionError if called
        with patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("rglob must not be called in is_stale()"),
        ):
            result = manager.is_stale(collection_path)

        assert result is False

    def test_is_stale_true_when_flag_set(self, collection_path):
        """AC #2: is_stale returns True when is_stale flag is explicitly True."""
        write_metadata(
            collection_path,
            {"vector_count": 100, "is_stale": True},
        )

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True

    def test_is_stale_true_when_no_metadata(self, collection_path):
        """AC #3: is_stale returns True when no collection_meta.json exists."""
        # Do NOT create metadata file
        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True

    def test_is_stale_true_when_no_hnsw_key(self, collection_path):
        """AC #4: is_stale returns True when hnsw_index key is absent from metadata."""
        # Write metadata without hnsw_index key
        with open(collection_path / "collection_meta.json", "w") as f:
            json.dump({"some_other_key": "value"}, f)

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True

    def test_is_stale_filtered_staleness_still_works(self, collection_path):
        """AC #5: Filtered rebuild staleness: count != visible_count returns True."""
        write_metadata(
            collection_path,
            {
                "vector_count": 80,
                "visible_count": 100,  # mismatch -> stale
                "is_stale": False,
                "filtered": True,
            },
        )

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True

    def test_is_stale_filtered_fresh_detection_still_works(self, collection_path):
        """AC #6: Filtered rebuild fresh: count == visible_count returns False."""
        write_metadata(
            collection_path,
            {
                "vector_count": 80,
                "visible_count": 80,  # match -> fresh
                "is_stale": False,
                "filtered": True,
            },
        )

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is False

    def test_is_stale_flag_missing_defaults_true(self, collection_path):
        """Backward compat: is_stale flag missing -> defaults to True (no regression)."""
        write_metadata(
            collection_path,
            {"vector_count": 50},  # no is_stale key
        )

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True

    def test_is_stale_corrupted_metadata_returns_true(self, collection_path):
        """Corrupted JSON metadata -> returns True (no regression)."""
        (collection_path / "collection_meta.json").write_text("{ not valid json }")

        manager = HNSWIndexManager()
        result = manager.is_stale(collection_path)

        assert result is True
