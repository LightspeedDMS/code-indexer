"""Unit tests for temporal token-preflight + request-seal utilities (Story #1290 AC23).

Covers:
- preflight_split_chunk: deterministic char-based splitting of an oversized
  chunk (a long no-whitespace diff, a huge commit message) so every piece fits
  under the provider per-chunk token cap, or fails loud with commit hash + path
  when it genuinely cannot converge.
- enforce_request_seal: groups per-commit document chunk-lists into
  sub-batches respecting the contextualized-endpoint request-level seal
  (max documents / max total chunks / max total tokens per request).
"""

import pytest

from src.code_indexer.services.temporal.token_preflight import (
    enforce_request_seal,
    pack_chunks_into_documents,
    preflight_split_chunk,
)


def _char_token_counter(text: str) -> int:
    """Deterministic token estimate: 1 token per 4 chars (like the codebase's fallback)."""
    return max(1, len(text) // 4)


def _constant_huge_token_counter(text: str) -> int:
    """Pathological counter: every non-empty string reports as huge (never converges)."""
    return 999_999_999 if text else 0


class TestPreflightSplitChunk:
    def test_chunk_under_cap_returned_unchanged(self):
        result = preflight_split_chunk(
            "short text", _char_token_counter, max_tokens_per_chunk=1000
        )
        assert result == ["short text"]

    def test_long_no_whitespace_diff_is_split_under_cap(self):
        """AC23: a long no-whitespace diff is split deterministically, not failed."""
        long_text = "a" * 4000  # no whitespace anywhere to split on
        pieces = preflight_split_chunk(
            long_text, _char_token_counter, max_tokens_per_chunk=200
        )
        assert len(pieces) > 1
        assert "".join(pieces) == long_text
        for piece in pieces:
            assert _char_token_counter(piece) <= 200

    def test_huge_commit_message_is_split_under_cap(self):
        """AC23: a huge commit message is split deterministically."""
        huge_message = "Fix bug. " * 2000
        pieces = preflight_split_chunk(
            huge_message, _char_token_counter, max_tokens_per_chunk=500
        )
        assert len(pieces) > 1
        assert "".join(pieces) == huge_message
        for piece in pieces:
            assert _char_token_counter(piece) <= 500

    def test_empty_text_returns_empty_list(self):
        assert preflight_split_chunk("", _char_token_counter, 100) == []

    def test_unsplittable_chunk_fails_loud_with_commit_hash_and_path(self):
        """AC23: when splitting cannot converge, fails loud with commit hash + path."""
        with pytest.raises(RuntimeError, match="abc1234.*src/foo.py"):
            preflight_split_chunk(
                "x" * 100,
                _constant_huge_token_counter,
                max_tokens_per_chunk=10,
                context_label="commit abc1234 path src/foo.py",
            )


class TestEnforceRequestSeal:
    def test_small_batch_returns_single_group_preserving_order(self):
        documents = [["chunk a"], ["chunk b"], ["chunk c"]]
        groups = enforce_request_seal(documents, _char_token_counter)
        assert groups == [documents]

    def test_splits_when_max_documents_exceeded(self):
        documents = [[f"doc{i} chunk"] for i in range(5)]
        groups = enforce_request_seal(documents, _char_token_counter, max_documents=2)
        assert [d for g in groups for d in g] == documents
        assert all(len(g) <= 2 for g in groups)

    def test_splits_when_max_chunks_exceeded(self):
        documents = [["c1", "c2", "c3"] for _ in range(4)]  # 3 chunks each, 12 total
        groups = enforce_request_seal(documents, _char_token_counter, max_chunks=5)
        assert [d for g in groups for d in g] == documents
        for g in groups:
            total_chunks = sum(len(d) for d in g)
            assert (
                total_chunks <= 5 or len(g) == 1
            )  # single oversized doc allowed alone

    def test_splits_when_max_tokens_exceeded(self):
        # Each document ~250 tokens (1000 chars / 4); cap of 400 tokens per request
        # forces a new group roughly every document.
        documents = [["x" * 1000] for _ in range(4)]
        groups = enforce_request_seal(documents, _char_token_counter, max_tokens=400)
        assert [d for g in groups for d in g] == documents
        assert len(groups) > 1

    def test_empty_documents_returns_empty_list(self):
        assert enforce_request_seal([], _char_token_counter) == []


class TestPackChunksIntoDocuments:
    """AC23: packs a FLAT ordered chunk list (one commit's already-preflighted
    pieces) into minimal sequential document groups, sealing BEFORE a document
    would exceed either the per-document chunk-count or token-sum cap --
    mirrors the coalescer's dual-constraint sealing, at chunk granularity
    rather than document granularity (enforce_request_seal's granularity)."""

    def test_small_chunk_list_returns_single_document_preserving_order(self):
        chunks = ["a", "b", "c"]
        documents = pack_chunks_into_documents(
            chunks,
            _char_token_counter,
            max_chunks_per_document=100,
            max_tokens_per_document=1000,
        )
        assert documents == [["a", "b", "c"]]

    def test_splits_when_max_chunks_per_document_exceeded(self):
        chunks = [f"chunk{i}" for i in range(5)]
        documents = pack_chunks_into_documents(
            chunks,
            _char_token_counter,
            max_chunks_per_document=2,
            max_tokens_per_document=1000,
        )
        # Order preserved, flattened output equals the original chunk list.
        assert [c for doc in documents for c in doc] == chunks
        assert all(len(doc) <= 2 for doc in documents)
        assert len(documents) == 3  # 2 + 2 + 1

    def test_splits_when_max_tokens_per_document_exceeded(self):
        # Each chunk ~250 tokens (1000 chars / 4); cap of 400 tokens per
        # document forces a new document roughly every chunk.
        chunks = ["x" * 1000 for _ in range(4)]
        documents = pack_chunks_into_documents(
            chunks,
            _char_token_counter,
            max_chunks_per_document=100,
            max_tokens_per_document=400,
        )
        assert [c for doc in documents for c in doc] == chunks
        assert len(documents) > 1
        for doc in documents:
            assert sum(_char_token_counter(c) for c in doc) <= 400 or len(doc) == 1

    def test_empty_chunks_returns_empty_list(self):
        assert (
            pack_chunks_into_documents(
                [],
                _char_token_counter,
                max_chunks_per_document=10,
                max_tokens_per_document=100,
            )
            == []
        )

    def test_single_oversized_chunk_still_emitted_alone(self):
        """Best-effort: a single chunk whose own token count already exceeds
        the cap is still emitted alone in its own document (mirrors
        enforce_request_seal's documented lone-oversized-unit behavior)."""
        chunks = ["a", "x" * 10_000, "b"]
        documents = pack_chunks_into_documents(
            chunks,
            _char_token_counter,
            max_chunks_per_document=100,
            max_tokens_per_document=50,
        )
        assert [c for doc in documents for c in doc] == chunks
        assert ["x" * 10_000] in documents
