"""Unit tests for the v2 temporal_structure.json marker (Story #1290 AC8, AC27).

The marker discriminates a per-commit v2 shard from a legacy per-file-diff
shard sharing the same model slug (blank-out reads this marker, not the
collection name -- AC19). It MUST be written at collection CREATE time,
before the first embed/flush, so a crash mid-index cannot leave a new
collection looking legacy (AC27).
"""

import json

from src.code_indexer.services.temporal.temporal_structure_marker import (
    STRUCTURE_MARKER_FILENAME,
    is_v2_structure,
    read_structure_marker,
    write_structure_marker,
)


class TestWriteAndReadStructureMarker:
    def test_write_then_read_round_trips_exact_content(self, tmp_path):
        write_structure_marker(tmp_path, model_slug="voyage_context_4")

        marker = read_structure_marker(tmp_path)

        assert marker == {
            "version": 2,
            "layout": "per_commit",
            "model": "voyage_context_4",
        }

    def test_write_creates_marker_file_on_disk(self, tmp_path):
        write_structure_marker(tmp_path, model_slug="voyage_context_4")

        marker_path = tmp_path / STRUCTURE_MARKER_FILENAME
        assert marker_path.exists()
        on_disk = json.loads(marker_path.read_text())
        assert on_disk["version"] == 2

    def test_read_missing_marker_returns_none(self, tmp_path):
        assert read_structure_marker(tmp_path) is None

    def test_read_corrupt_marker_returns_none(self, tmp_path):
        (tmp_path / STRUCTURE_MARKER_FILENAME).write_text("{not valid json")
        assert read_structure_marker(tmp_path) is None


class TestIsV2Structure:
    def test_v2_marker_is_v2(self, tmp_path):
        write_structure_marker(tmp_path, model_slug="voyage_context_4")
        assert is_v2_structure(tmp_path) is True

    def test_missing_marker_is_not_v2(self, tmp_path):
        assert is_v2_structure(tmp_path) is False

    def test_legacy_version_1_marker_is_not_v2(self, tmp_path):
        (tmp_path / STRUCTURE_MARKER_FILENAME).write_text(
            json.dumps({"version": 1, "layout": "per_file_diff"})
        )
        assert is_v2_structure(tmp_path) is False

    def test_corrupt_marker_is_not_v2(self, tmp_path):
        (tmp_path / STRUCTURE_MARKER_FILENAME).write_text("garbage")
        assert is_v2_structure(tmp_path) is False
