"""Unit tests for overlap-parameterized chunking (Story #1291 AC2).

The shared chunker (contextual_chunker.py) must support a per-adapter
overlap_percentage so the Cohere embed-v4.0 adapter can chunk the IDENTICAL
aggregated document with 15% overlap while voyage-context-4 keeps 0% overlap
-- producing DIFFERENT chunk boundaries for the same input.
"""

from src.code_indexer.services.temporal.commit_aggregator import (
    AggregatedCommitDocument,
    ProvenanceSection,
)
from src.code_indexer.services.temporal.contextual_chunker import (
    chunk_aggregated_document,
)


def _doc(total_len: int) -> AggregatedCommitDocument:
    text = "x" * total_len
    return AggregatedCommitDocument(
        text=text,
        provenance=[ProvenanceSection(start=0, end=total_len, path="only_file.py")],
        file_paths=["only_file.py"],
    )


class TestOverlapParameterizedChunking:
    def test_default_overlap_is_zero_byte_identical_to_legacy_call(self):
        """Omitting overlap_percentage must be byte-identical to the old 0% call."""
        doc = _doc(9000)
        default_chunks = chunk_aggregated_document(doc, chunk_chars=4096)
        explicit_zero_chunks = chunk_aggregated_document(
            doc, chunk_chars=4096, overlap_percentage=0.0
        )
        assert [c.text for c in default_chunks] == [
            c.text for c in explicit_zero_chunks
        ]
        assert [c.char_start for c in default_chunks] == [
            c.char_start for c in explicit_zero_chunks
        ]

    def test_15_percent_overlap_produces_different_boundaries_than_zero_overlap(self):
        """AC2: cohere's 15% overlap must yield different chunk boundaries than
        voyage's 0% overlap for the IDENTICAL document."""
        doc = _doc(9000)
        zero_overlap_chunks = chunk_aggregated_document(
            doc, chunk_chars=4096, overlap_percentage=0.0
        )
        fifteen_pct_chunks = chunk_aggregated_document(
            doc, chunk_chars=4096, overlap_percentage=0.15
        )

        zero_starts = [c.char_start for c in zero_overlap_chunks]
        overlap_starts = [c.char_start for c in fifteen_pct_chunks]
        assert zero_starts != overlap_starts

    def test_15_percent_overlap_chunks_actually_overlap(self):
        """Each chunk (after the first) must overlap the previous chunk by
        exactly int(chunk_chars * overlap_percentage) characters."""
        doc = _doc(9000)
        chunk_chars = 4096
        overlap_pct = 0.15
        expected_overlap = int(chunk_chars * overlap_pct)

        chunks = chunk_aggregated_document(
            doc, chunk_chars=chunk_chars, overlap_percentage=overlap_pct
        )

        assert len(chunks) >= 2
        for i in range(len(chunks) - 1):
            actual_overlap = chunks[i].char_end - chunks[i + 1].char_start
            assert actual_overlap == expected_overlap

    def test_overlap_chunks_cover_full_text_with_no_gaps(self):
        """Overlapping chunks must still fully cover doc.text (no gaps), even
        though chunks now share characters at the boundaries."""
        text = "".join(f"line{i}\n" for i in range(2000))
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=len(text), path="a.py")],
            file_paths=["a.py"],
        )

        chunks = chunk_aggregated_document(
            doc, chunk_chars=1000, overlap_percentage=0.15
        )

        # Reconstruct coverage: every character index in [0, len(text)) must be
        # covered by at least one chunk's [char_start, char_end) range.
        covered = [False] * len(text)
        for c in chunks:
            for idx in range(c.char_start, c.char_end):
                covered[idx] = True
        assert all(covered)

    def test_only_first_chunk_is_head_under_overlap(self):
        doc = _doc(9000)
        chunks = chunk_aggregated_document(
            doc, chunk_chars=4096, overlap_percentage=0.15
        )
        assert chunks[0].is_head is True
        assert all(c.is_head is False for c in chunks[1:])

    def test_empty_document_yields_no_chunks_under_overlap(self):
        doc = AggregatedCommitDocument(text="", provenance=[], file_paths=[])
        assert (
            chunk_aggregated_document(doc, chunk_chars=4096, overlap_percentage=0.15)
            == []
        )
