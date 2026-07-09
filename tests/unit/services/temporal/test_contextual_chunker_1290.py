"""Unit tests for the zero-overlap contextual chunker (Story #1290 AC1/AC2/AC6/AC26).

Chunks an AggregatedCommitDocument (from commit_aggregator.py) into fixed-size,
0%-overlap pieces, attaching per-chunk paths[]/primary_path (from the
section-range provenance map) and is_head (chunk_index == 0).
"""

from src.code_indexer.services.temporal.commit_aggregator import (
    AggregatedCommitDocument,
    ProvenanceSection,
)
from src.code_indexer.services.temporal.contextual_chunker import (
    chunk_aggregated_document,
)


class TestZeroOverlapChunking:
    def test_deterministic_exact_chunk_count_matches_ceiling_formula(self):
        """AC1/AC2: exact vector count == ceil(total_chars / chunk_chars), zero overlap."""
        total_len = 8300  # ceil(8300 / 4096) == 3
        text = "x" * total_len
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=total_len, path="only_file.py")],
            file_paths=["only_file.py"],
        )

        chunks = chunk_aggregated_document(doc, chunk_chars=4096)

        assert len(chunks) == 3

    def test_zero_overlap_chunks_tile_text_exactly(self):
        """0% overlap: concatenating chunk texts reconstructs the original exactly."""
        text = "".join(f"line{i}\n" for i in range(2000))
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=len(text), path="a.py")],
            file_paths=["a.py"],
        )

        chunks = chunk_aggregated_document(doc, chunk_chars=1000)

        assert "".join(c.text for c in chunks) == text
        # zero overlap: char ranges are contiguous and non-overlapping
        for i in range(len(chunks) - 1):
            assert chunks[i].char_end == chunks[i + 1].char_start

    def test_only_chunk_index_zero_is_head(self):
        text = "x" * 9000
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=len(text), path="a.py")],
            file_paths=["a.py"],
        )

        chunks = chunk_aggregated_document(doc, chunk_chars=4096)

        assert chunks[0].is_head is True
        assert all(c.is_head is False for c in chunks[1:])

    def test_empty_document_yields_no_chunks(self):
        doc = AggregatedCommitDocument(text="", provenance=[], file_paths=[])
        assert chunk_aggregated_document(doc, chunk_chars=4096) == []


class TestProvenancePathsAndPrimaryPath:
    def test_chunk_spanning_two_files_has_both_paths(self):
        """AC6: a chunk whose span overlaps files A and B has paths == [A, B]."""
        message_section = "commit message\n"
        file_a_header = "--- a.py ---\n"
        file_a_body = "a" * 30
        file_b_header = "--- b.py ---\n"
        file_b_body = "b" * 30

        text = (
            message_section + file_a_header + file_a_body + file_b_header + file_b_body
        )
        msg_end = len(message_section)
        a_start = msg_end
        a_end = a_start + len(file_a_header) + len(file_a_body)
        b_start = a_end
        b_end = b_start + len(file_b_header) + len(file_b_body)

        doc = AggregatedCommitDocument(
            text=text,
            provenance=[
                ProvenanceSection(start=0, end=msg_end, path=None),
                ProvenanceSection(start=a_start, end=a_end, path="a.py"),
                ProvenanceSection(start=b_start, end=b_end, path="b.py"),
            ],
            file_paths=["a.py", "b.py"],
        )

        # chunk_chars large enough to span the whole text in ONE chunk
        chunks = chunk_aggregated_document(doc, chunk_chars=len(text))

        assert len(chunks) == 1
        assert chunks[0].paths == ["a.py", "b.py"]
        assert chunks[0].primary_path in ("a.py", "b.py")

    def test_chunk_overlapping_only_message_has_no_paths(self):
        """AC26-adjacent: a chunk covering only the message section has paths == []."""
        text = "just a message, no file sections\n"
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=len(text), path=None)],
            file_paths=[],
        )

        chunks = chunk_aggregated_document(doc, chunk_chars=4096)

        assert len(chunks) == 1
        assert chunks[0].is_head is True
        assert chunks[0].paths == []
        assert chunks[0].primary_path is None
