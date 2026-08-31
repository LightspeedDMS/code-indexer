"""Unit test for Issue #1601 Priority 9 -- ``_BoundedLineReader`` must not
exhibit quadratic behavior for a single pathologically long line spanning
many chunks.

Before this fix, ``buffer += decoder.decode(raw)`` repeatedly copied the
entire accumulated string on every chunk read whenever no newline had
appeared yet -- O(n^2) for a single long line split across many small
chunks. Empirically measured: a 4 MiB single-line file read in 4 KiB
chunks (1000 chunks, no newline until EOF) took ~1.3s under the
quadratic implementation. A linear (list-accumulate-then-join-once)
implementation completes this in well under 0.5s.
"""

from __future__ import annotations

import time

from code_indexer.global_repos.regex_search import _BoundedLineReader

_SINGLE_LINE_SIZE_BYTES = 4 * 1024 * 1024  # 4 MiB
_CHUNK_BYTES = 4096  # forces ~1000 chunks for the single line above
_MAX_ELAPSED_SECONDS = 0.5


class TestBoundedLineReaderQuadraticFix:
    """Priority 9: a single long line spanning many chunks must be read
    in roughly linear time, not quadratic."""

    def test_single_long_line_across_many_chunks_stays_fast(self, tmp_path):
        path = tmp_path / "single_long_line.txt"
        path.write_bytes(b"x" * _SINGLE_LINE_SIZE_BYTES)

        reader = _BoundedLineReader(
            str(path), max_bytes=64 * 1024 * 1024, chunk_bytes=_CHUNK_BYTES
        )

        start = time.monotonic()
        lines = list(reader)
        elapsed = time.monotonic() - start

        # Correctness: the whole file is one line, decoded intact.
        assert len(lines) == 1
        assert len(lines[0]) == _SINGLE_LINE_SIZE_BYTES

        assert elapsed < _MAX_ELAPSED_SECONDS, (
            f"reading a single long line across many chunks took "
            f"{elapsed:.2f}s (expected < {_MAX_ELAPSED_SECONDS}s) -- looks "
            f"quadratic (repeated full-string concatenation per chunk) "
            f"rather than linear (list-accumulate-then-join-once)"
        )
