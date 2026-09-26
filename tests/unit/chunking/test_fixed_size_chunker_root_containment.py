"""The point where a file's content is actually
opened for chunking must re-check containment against the codebase root,
independently of whatever discovery-time filtering already ran.

This closes the gap between file discovery and the file actually being
read (TOCTOU): even if a path passed discovery-time filtering, the file on
disk could have been replaced with an out-of-root symlink by the time
chunking opens it. ``FixedSizeChunker.chunk_file()`` is the single choke
point every discovery path (full walk, incremental, watch, reconcile,
resume) funnels through before content ever reaches chunking/embedding,
so the check belongs here as the last line of defense.

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- no
mocks.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.config import IndexingConfig


@pytest.fixture
def chunker() -> FixedSizeChunker:
    return FixedSizeChunker(IndexingConfig())


class TestChunkFileRootContainment:
    def test_in_root_file_is_read_normally(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "a.py"
        target.write_text("print('hello')\n")

        chunks = chunker.chunk_file(target, repo_root=root)

        assert chunks
        assert chunks[0]["text"] == "print('hello')\n"

    def test_symlink_to_inside_file_is_read_normally(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        real_target = root / "real.py"
        real_target.write_text("print('inside')\n")
        link = root / "link.py"
        link.symlink_to(real_target)

        chunks = chunker.chunk_file(link, repo_root=root)

        assert chunks
        assert chunks[0]["text"] == "print('inside')\n"

    def test_symlink_to_outside_file_is_refused_content_never_returned(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        outside_target = tmp_path / "outside.py"
        outside_content = "MARKER_OUTSIDE_CONTENT_SHOULD_NEVER_BE_CHUNKED\n"
        outside_target.write_text(outside_content)
        link = root / "escape.py"
        link.symlink_to(outside_target)

        with pytest.raises(ValueError) as exc_info:
            chunker.chunk_file(link, repo_root=root)

        assert "MARKER_OUTSIDE_CONTENT" not in str(exc_info.value), (
            "The outside file's content must never appear in the raised "
            f"error. Got: {exc_info.value}"
        )

    def test_toctou_swap_between_discovery_and_read_is_refused(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        """Simulates the race: a path that was a legitimate in-root file
        at discovery time is replaced with an out-of-root symlink before
        chunking actually opens it."""
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "swapped.py"
        target.write_text("print('originally legitimate')\n")

        # Discovery time: this path is a real, in-root file.
        assert target.is_symlink() is False

        # Between discovery and read: swap it for an out-of-root symlink.
        outside_target = tmp_path / "outside_swapped.py"
        outside_content = "MARKER_TOCTOU_SWAP_CONTENT\n"
        outside_target.write_text(outside_content)
        target.unlink()
        target.symlink_to(outside_target)

        with pytest.raises(ValueError) as exc_info:
            chunker.chunk_file(target, repo_root=root)

        assert "MARKER_TOCTOU_SWAP_CONTENT" not in str(exc_info.value), (
            "The swapped-in outside file's content must never appear in "
            f"the raised error. Got: {exc_info.value}"
        )

    def test_concurrent_chunk_file_calls_share_chunker_without_races(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        """A single FixedSizeChunker instance is shared across the
        high-throughput processor's worker thread pool -- the resolved-
        root cache backing the containment check must be safe under
        concurrent chunk_file() calls, never raising and never rejecting
        a legitimate in-root file."""
        root = tmp_path / "repo"
        root.mkdir()
        targets = []
        for i in range(20):
            target = root / f"concurrent_{i}.py"
            target.write_text(f"print({i})\n")
            targets.append(target)

        def _chunk(target: Path):
            return chunker.chunk_file(target, repo_root=root)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(_chunk, targets))

        for i, chunks in enumerate(results):
            assert chunks, f"concurrent_{i}.py produced no chunks"
            assert chunks[0]["text"] == f"print({i})\n"

    def test_no_repo_root_argument_skips_containment_check(
        self, chunker: FixedSizeChunker, tmp_path: Path
    ) -> None:
        """Backward-compatible: callers that never pass repo_root (e.g.
        isolated unit tests of chunk_text-adjacent behaviour) get no
        containment check -- every production caller always passes
        repo_root, so this only affects test-only direct usage."""
        target = tmp_path / "no_root_arg.py"
        target.write_text("print('no root arg')\n")

        chunks = chunker.chunk_file(target)

        assert chunks
        assert chunks[0]["text"] == "print('no root arg')\n"
