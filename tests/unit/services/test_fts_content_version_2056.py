"""Bug #2056: FTS content-version marker. An index whose marker is missing,
garbled or older than FTS_CONTENT_VERSION is rebuilt once from disk; a
current or newer marker never triggers a rebuild."""

from pathlib import Path

from code_indexer.services.fts_file_documents import (
    FTS_CONTENT_VERSION,
    FTS_CONTENT_VERSION_FILE,
    fts_content_version_is_current,
    mark_fts_content_current,
)


class TestFtsContentVersionMarker2056:
    def test_missing_marker_is_not_current(self, tmp_path: Path) -> None:
        assert not fts_content_version_is_current(tmp_path)

    def test_marked_index_is_current_and_leaves_no_temp_file(
        self, tmp_path: Path
    ) -> None:
        mark_fts_content_current(tmp_path)

        assert fts_content_version_is_current(tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == [FTS_CONTENT_VERSION_FILE]

    def test_garbled_marker_is_not_current(self, tmp_path: Path) -> None:
        (tmp_path / FTS_CONTENT_VERSION_FILE).write_text("not-a-version")

        assert not fts_content_version_is_current(tmp_path)

    def test_older_marker_is_not_current(self, tmp_path: Path) -> None:
        (tmp_path / FTS_CONTENT_VERSION_FILE).write_text(f"{FTS_CONTENT_VERSION - 1}")

        assert not fts_content_version_is_current(tmp_path)

    def test_newer_marker_is_current(self, tmp_path: Path) -> None:
        """A marker written by a later release never makes this one rebuild."""
        (tmp_path / FTS_CONTENT_VERSION_FILE).write_text(f"{FTS_CONTENT_VERSION + 1}\n")

        assert fts_content_version_is_current(tmp_path)
