"""Issue #1601 remediation round 4, Priority 4 (Codex Medium, real
reproducible edge case).

``_BoundedLineReader.__iter__`` inferred "the byte ceiling stopped the
scan" purely from ``bytes_read >= max_bytes`` once its internal ``while``
loop exited without an explicit ``break``. When a file's real size is
EXACTLY equal to ``max_bytes``, the loop clamps each read to at most
``remaining`` bytes, consumes the whole file, and lands on
``bytes_read == max_bytes`` -- indistinguishable, under the old logic,
from a file that had MORE data beyond the ceiling. ``read_capped`` was
therefore incorrectly ``True`` even though the file was read to genuine
EOF with nothing left, which could also cause a final partial (no
trailing newline) line to be wrongly discarded as "possibly corrupted by
the cap" when it was in fact complete.

The fix must disambiguate the two cases (e.g. a one-byte probe read at
the exact boundary) without violating the documented "bytes_read never
exceeds max_bytes" exact byte-count guarantee.
"""

from __future__ import annotations

from code_indexer.global_repos.regex_search import _BoundedLineReader

_CEILING_BYTES = 64


class TestBoundedLineReaderExactBoundary:
    """Priority 4: a file whose real size exactly equals max_bytes must
    NOT be reported as read_capped."""

    def test_file_exactly_at_ceiling_is_not_read_capped(self, tmp_path):
        content = ("x" * (_CEILING_BYTES - 1)) + "\n"  # exactly _CEILING_BYTES bytes
        assert len(content.encode("utf-8")) == _CEILING_BYTES

        path = tmp_path / "exact_boundary.txt"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(str(path), _CEILING_BYTES)
        lines = list(reader)

        assert reader.bytes_read == _CEILING_BYTES
        assert reader.read_capped is False, (
            "a file whose real size exactly equals max_bytes was wrongly "
            "reported as read_capped -- genuine EOF at the boundary must "
            "not be mistaken for a truncated read"
        )
        # The final line (terminated by \n exactly at the ceiling) must
        # come back intact, not discarded as a "possibly corrupted"
        # fragment of an incomplete read.
        assert lines == ["x" * (_CEILING_BYTES - 1)]

    def test_file_exactly_at_ceiling_with_no_trailing_newline_is_not_capped(
        self, tmp_path
    ):
        """Same boundary, but the final line has no trailing newline at
        all -- the trailing-partial-line flush path must still fire."""
        content = "x" * _CEILING_BYTES  # exactly _CEILING_BYTES bytes, no \n
        assert len(content.encode("utf-8")) == _CEILING_BYTES

        path = tmp_path / "exact_boundary_no_newline.txt"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(str(path), _CEILING_BYTES)
        lines = list(reader)

        assert reader.bytes_read == _CEILING_BYTES
        assert reader.read_capped is False
        assert lines == [content]

    def test_file_one_byte_beyond_ceiling_is_genuinely_capped(self, tmp_path):
        """Control case: real data beyond the ceiling must still be
        reported as capped -- the fix must not blindly flip the flag to
        False at every exact-bytes_read==max_bytes boundary."""
        content = ("x" * _CEILING_BYTES) + "MORE-DATA-BEYOND-CEILING\n"
        path = tmp_path / "one_byte_beyond.txt"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(str(path), _CEILING_BYTES)
        list(reader)

        assert reader.bytes_read == _CEILING_BYTES
        assert reader.read_capped is True, (
            "a file with genuine data beyond the ceiling must still be "
            "reported as read_capped"
        )
