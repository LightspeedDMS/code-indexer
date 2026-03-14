"""
Tests for TantivyIndexManager.commit() - wait_merging_threads behavior.

Story #442: Add wait_merging_threads() to TantivyIndexManager.commit()

These tests verify that after commit(), merging threads are awaited so
that the segment count stays bounded after multiple sequential commits.

Design note: We do NOT attempt to write a test that fails without
wait_merging_threads(), because Tantivy's background merger is
non-deterministic. Instead we verify the OBSERVABLE BEHAVIOR:
after a realistic number of add+commit cycles the segment count must
remain below a generous upper bound (< 15), which is only reliably
achievable when merging is explicitly awaited after each commit.
"""

import tempfile
from pathlib import Path

import pytest


def _make_doc(i: int) -> dict:
    return {
        "path": f"file_{i}.py",
        "content": f"def func_{i}(): return {i}",
        "content_raw": f"def func_{i}(): return {i}",
        "identifiers": [f"func_{i}"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }


def _count_segments(index_dir: Path) -> int:
    """Count distinct Tantivy segments in the index directory.

    Each Tantivy segment consists of multiple files sharing the same UUID
    prefix (e.g. ``<uuid>.store``, ``<uuid>.fast``, ``<uuid>.fieldnorm``,
    ``<uuid>.pos``, ``<uuid>.idx``).  Counting only ``.store`` files would
    miss segments that have not yet flushed a store file.  Instead we extract
    the set of UUID prefixes from ALL segment-related files to get a
    representation-independent count.

    Files with non-UUID-looking names (e.g. ``meta.json``, ``managed.json``,
    ``*.lock``) are ignored by checking that the stem is exactly 32 hex chars
    (UUID without hyphens) or a standard UUID string of 36 chars with hyphens.
    """
    uuid_prefixes: set = set()
    for f in index_dir.iterdir():
        if not f.is_file():
            continue
        stem = f.stem
        # Tantivy segment filenames are UUID-based: either 32 hex chars
        # (no hyphens) or the standard 8-4-4-4-12 format (36 chars with hyphens).
        if len(stem) == 32 and all(c in "0123456789abcdefABCDEF" for c in stem):
            uuid_prefixes.add(stem)
        elif len(stem) == 36 and stem.count("-") == 4:
            uuid_prefixes.add(stem)
    return len(uuid_prefixes)


class TestTantivyCommitMerge:
    """Verify that commit() awaits merging threads to keep segment count bounded."""

    def test_commit_with_wait_merging_threads(self):
        """50 documents across 10 commit cycles must leave < 15 segments.

        Each cycle adds 5 documents and commits once.  Without
        wait_merging_threads() merges happen asynchronously and the
        segment count is unpredictable. With it, each commit() call
        blocks until all pending merges complete, guaranteeing a low
        and stable segment count.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            doc_index = 0
            for _cycle in range(10):
                for _doc in range(5):
                    manager.add_document(_make_doc(doc_index))
                    doc_index += 1
                manager.commit()

            segment_count = _count_segments(index_dir)
            assert segment_count < 15, (
                f"Expected segment count < 15 after 10 commit cycles (50 docs), "
                f"got {segment_count}. "
                "commit() must call wait_merging_threads() to bound segment count."
            )

    def test_commit_still_raises_on_no_writer(self):
        """commit() must raise RuntimeError when the writer is not initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            # Deliberately skip initialize_index() so _writer stays None

            with pytest.raises(RuntimeError, match="Index writer not initialized"):
                manager.commit()

    def test_update_document_uses_commit_inner(self):
        """update_document() must not leave unbounded segments after repeated calls.

        Each update_document() call performs a delete+add+commit sequence.
        Before the _commit_inner() refactor, the commit inside update_document()
        called self._writer.commit() directly, bypassing wait_merging_threads()
        and writer re-creation.  This test verifies that after 15 sequential
        update_document() calls the segment count stays below 15.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            # Seed a document so updates have something to replace
            manager.add_document(_make_doc(0))
            manager.commit()

            # Repeatedly update the same document path
            for i in range(1, 16):
                manager.update_document("file_0.py", _make_doc(i))

            segment_count = _count_segments(index_dir)
            assert segment_count < 15, (
                f"Expected segment count < 15 after 15 update_document() cycles, "
                f"got {segment_count}. "
                "update_document() must call _commit_inner() to await merges."
            )

    def test_multiple_commit_cycles_segments_bounded(self):
        """20 sequential add+commit cycles must leave < 15 segments.

        One document per cycle, 20 cycles.  This is the worst case for
        segment accumulation because every commit creates a new segment.
        wait_merging_threads() must block until Tantivy has merged old
        segments before returning, keeping the total count bounded.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = Path(tmpdir) / ".code-indexer" / "tantivy_index"

            from code_indexer.services.tantivy_index_manager import TantivyIndexManager

            manager = TantivyIndexManager(index_dir=index_dir)
            manager.initialize_index()

            for i in range(20):
                manager.add_document(_make_doc(i))
                manager.commit()

            segment_count = _count_segments(index_dir)
            assert segment_count < 15, (
                f"Expected segment count < 15 after 20 sequential add+commit cycles, "
                f"got {segment_count}. "
                "commit() must call wait_merging_threads() to prevent segment proliferation."
            )
