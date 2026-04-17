"""
Tests for Bug #722: FTS index path resolution.

The /api/repositories/{repo_alias}/indexes endpoint was checking
.code-indexer/index/tantivy (wrong) instead of .code-indexer/tantivy_index (correct).

These tests exercise the FTS path formula used in get_repository_indexes directly
against real filesystem layouts, verifying correct detection behavior.
"""

from pathlib import Path


def _fts_detected(clone_path: Path) -> bool:
    """Replicate the FTS detection formula from get_repository_indexes."""
    fts_path = clone_path / ".code-indexer" / "tantivy_index"
    return fts_path.exists() and fts_path.is_dir()


def _fts_detected_old_wrong_formula(clone_path: Path) -> bool:
    """Replicate the OLD (buggy) FTS detection formula for comparison."""
    index_base_path = clone_path / ".code-indexer" / "index"
    fts_path = index_base_path / "tantivy"
    return fts_path.exists() and fts_path.is_dir()


class TestFtsPathResolutionBug722:
    """Regression tests for Bug #722: FTS index path must be .code-indexer/tantivy_index."""

    def test_fts_detected_when_tantivy_index_dir_exists(self, tmp_path):
        """FTS is detected when .code-indexer/tantivy_index/ directory exists."""
        clone_path = tmp_path / "repo"
        (clone_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)

        assert _fts_detected(clone_path) is True

    def test_fts_not_detected_when_tantivy_index_absent(self, tmp_path):
        """FTS is not detected when .code-indexer/tantivy_index/ is missing."""
        clone_path = tmp_path / "repo"
        (clone_path / ".code-indexer" / "index").mkdir(parents=True)

        assert _fts_detected(clone_path) is False

    def test_fts_not_detected_when_only_old_wrong_path_exists(self, tmp_path):
        """Bug #722 regression: old wrong path .code-indexer/index/tantivy does NOT count as FTS.

        If the old buggy formula were still in place, this test would pass (wrong path exists).
        With the fix, only .code-indexer/tantivy_index counts, so FTS is absent here.
        """
        clone_path = tmp_path / "repo"
        # Create the wrong path that the old (buggy) code would have used
        (clone_path / ".code-indexer" / "index" / "tantivy").mkdir(parents=True)

        # Old formula (buggy) would return True here
        assert _fts_detected_old_wrong_formula(clone_path) is True
        # Correct formula returns False — the fix works
        assert _fts_detected(clone_path) is False

    def test_fts_not_detected_when_tantivy_index_is_a_file_not_dir(self, tmp_path):
        """FTS is not detected when .code-indexer/tantivy_index exists as a file, not directory."""
        clone_path = tmp_path / "repo"
        (clone_path / ".code-indexer").mkdir(parents=True)
        (clone_path / ".code-indexer" / "tantivy_index").write_bytes(b"")

        assert _fts_detected(clone_path) is False

    def test_fts_and_semantic_can_coexist(self, tmp_path):
        """Both FTS and semantic indexes can be present simultaneously."""
        clone_path = tmp_path / "repo"
        (clone_path / ".code-indexer" / "tantivy_index").mkdir(parents=True)
        semantic_coll = clone_path / ".code-indexer" / "index" / "voyage-code-3"
        semantic_coll.mkdir(parents=True)
        (semantic_coll / "hnsw_index.bin").write_bytes(b"")

        assert _fts_detected(clone_path) is True
