"""Unit tests for point_id + payload building (Story #1290 AC3, AC5, AC12).

Covers the unified point_id scheme ("{project_id}:commit:{hash}:{j}") and the
per-chunk payload contract: canonical type=="commit_chunk" + is_head fields,
and commit_message populated ONLY on the head chunk (short-capped), empty on
all others.
"""

from src.code_indexer.services.temporal.commit_aggregator import (
    AggregatedCommitDocument,
    ProvenanceSection,
)
from src.code_indexer.services.temporal.contextual_chunker import (
    chunk_aggregated_document,
)
from src.code_indexer.services.temporal.models import CommitInfo
from src.code_indexer.services.temporal.temporal_point_builder import (
    build_chunk_payload,
    build_point_id,
    short_cap_commit_message,
)


class TestBuildPointId:
    def test_point_id_format(self):
        assert build_point_id("proj123", "abc456", 2) == "proj123:commit:abc456:2"

    def test_point_id_never_contains_diff_marker(self):
        point_id = build_point_id("proj123", "abc456", 0)
        assert ":diff:" not in point_id


class TestShortCapCommitMessage:
    def test_short_message_returned_unchanged(self):
        assert short_cap_commit_message("short message") == "short message"

    def test_long_message_capped(self):
        long_message = "x" * 500
        capped = short_cap_commit_message(long_message, cap=200)
        assert len(capped) == 200

    def test_empty_message_returns_empty_string(self):
        assert short_cap_commit_message("") == ""
        assert short_cap_commit_message(None) == ""  # type: ignore[arg-type]


class TestBuildChunkPayload:
    def _commit(self):
        return CommitInfo(
            hash="abc123def456",
            timestamp=1700000000,
            author_name="Jane Dev",
            author_email="jane@example.com",
            message="Fix the thing that was broken",
            parent_hashes="parent1hash",
        )

    def test_head_chunk_has_populated_commit_message(self):
        text = "Fix the thing that was broken\n--- a.py ---\nsome diff\n"
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[
                ProvenanceSection(start=0, end=31, path=None),
                ProvenanceSection(start=31, end=len(text), path="a.py"),
            ],
            file_paths=["a.py"],
        )
        chunks = chunk_aggregated_document(doc, chunk_chars=len(text))
        payload = build_chunk_payload(self._commit(), chunks[0], project_id="proj1")

        assert payload["type"] == "commit_chunk"
        assert payload["is_head"] is True
        assert payload["commit_message"] == "Fix the thing that was broken"

    def test_non_head_chunk_has_empty_commit_message(self):
        text = "head section\n" + ("z" * 100)
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[ProvenanceSection(start=0, end=len(text), path="a.py")],
            file_paths=["a.py"],
        )
        chunks = chunk_aggregated_document(doc, chunk_chars=20)
        assert len(chunks) > 1

        non_head_payload = build_chunk_payload(
            self._commit(), chunks[1], project_id="proj1"
        )

        assert non_head_payload["is_head"] is False
        assert non_head_payload["commit_message"] == ""

    def test_payload_includes_paths_and_primary_path_and_project_id(self):
        text = "msg\n--- a.py ---\nbody"
        doc = AggregatedCommitDocument(
            text=text,
            provenance=[
                ProvenanceSection(start=0, end=4, path=None),
                ProvenanceSection(start=4, end=len(text), path="a.py"),
            ],
            file_paths=["a.py"],
        )
        chunks = chunk_aggregated_document(doc, chunk_chars=len(text))
        payload = build_chunk_payload(self._commit(), chunks[0], project_id="proj1")

        assert payload["paths"] == ["a.py"]
        assert payload["primary_path"] == "a.py"
        assert payload["project_id"] == "proj1"
        assert payload["commit_hash"] == "abc123def456"
