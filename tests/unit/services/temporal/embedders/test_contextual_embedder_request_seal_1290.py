"""Unit tests: ContextualTemporalEmbedder wires the AC23 REQUEST-level seal.

Code review finding (Story #1290 BLOCKING): `token_preflight.enforce_request_seal`
was defined + unit-tested in isolation but never wired into the production
contextual-embed path -- the adapter always sent ALL of a commit's chunks in
ONE HTTP request with no enforcement of the per-request caps (max documents /
max total chunks / max total tokens). These tests exercise the WIRED
production path (`embed_commit_chunks` -> `_make_sync_contextualized_request`)
directly, proving a large commit is split into MULTIPLE HTTP requests, each
respecting the configured caps, with the 1:1 chunk<->vector contract preserved
in original order across the split requests.
"""

import os
from unittest.mock import patch

import pytest

from src.code_indexer.config import Config
from src.code_indexer.services.temporal.embedders.contextual import (
    ContextualTemporalEmbedder,
)


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


def _response_for(documents):
    """Deterministic fake response: embedding = [float(len(chunk))] * 1024,
    echoing the request's own document/chunk shape back with the API's
    authoritative `index` fields (mirrors the real endpoint contract)."""
    return {
        "data": [
            {
                "index": doc_idx,
                "data": [
                    {"index": chunk_idx, "embedding": [float(len(chunk))] * 1024}
                    for chunk_idx, chunk in enumerate(doc)
                ],
            }
            for doc_idx, doc in enumerate(documents)
        ],
        "model": "voyage-context-4",
    }


class TestRequestSealSplitsOnChunkCount:
    def test_large_commit_splits_into_multiple_requests_by_chunk_count(
        self, mock_api_key
    ):
        embedder = ContextualTemporalEmbedder(Config())
        # Force splitting via the CHUNK-COUNT cap; token cap kept generous so
        # it never binds in this test (isolates the constraint under test).
        embedder._max_chunks_per_request = 2
        embedder._max_documents_per_request = 1000
        embedder._max_tokens_per_request = 1_000_000
        embedder._max_tokens_per_chunk = 1_000_000

        chunks = [f"chunk-{i}" for i in range(5)]  # ceil(5 / 2) == 3 requests

        def _side_effect(documents, **kwargs):
            return _response_for(documents)

        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            side_effect=_side_effect,
        ) as mocked:
            result = embedder.embed_commit_chunks(chunks)

        assert mocked.call_count == 3, (
            "5 chunks capped at 2/request must split into 3 requests"
        )
        for call in mocked.call_args_list:
            documents = call.args[0]
            total_chunks = sum(len(d) for d in documents)
            assert total_chunks <= 2

        # 1:1 chunk<->vector contract, ORIGINAL order preserved across the
        # split requests.
        assert len(result) == 5
        for i, chunk in enumerate(chunks):
            assert result[i] == pytest.approx([float(len(chunk))] * 1024)

    def test_small_commit_still_issues_exactly_one_request(self, mock_api_key):
        """Regression guard: the common case (under every cap) is UNCHANGED --
        one document, one HTTP request, exactly as before this fix."""
        embedder = ContextualTemporalEmbedder(Config())
        chunks = ["short a", "short b", "short c"]

        def _side_effect(documents, **kwargs):
            return _response_for(documents)

        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            side_effect=_side_effect,
        ) as mocked:
            result = embedder.embed_commit_chunks(chunks)

        assert mocked.call_count == 1
        sent_documents = mocked.call_args.args[0]
        assert sent_documents == [chunks]
        assert len(result) == 3


class TestRequestSealSplitsOnTokenCount:
    def test_large_commit_splits_into_multiple_requests_by_token_budget(
        self, mock_api_key
    ):
        embedder = ContextualTemporalEmbedder(Config())
        # Deterministic 1-token-per-char counter (same technique used by the
        # existing per-chunk preflight tests) so the token split is exact.
        embedder._count_tokens = lambda text: len(text)  # type: ignore[assignment]
        # Story #1292 bug fix: document packing is now bounded by
        # _max_tokens_per_chunk (the per-document context-window cap), not
        # _max_tokens_per_request. Set it to 15 so each 10-token chunk packs
        # into its OWN document (2 chunks would total 20 > 15) -- isolating
        # this test's intent (REQUEST-level splitting across documents) from
        # document-packing splitting.
        embedder._max_tokens_per_chunk = 15
        embedder._max_chunks_per_request = 1000  # never binds here
        embedder._max_documents_per_request = 1000
        embedder._max_tokens_per_request = 25  # pairs of 10-token docs -> 2/request

        chunks = ["x" * 10, "y" * 10, "z" * 10, "w" * 10]  # 10 tokens each

        def _side_effect(documents, **kwargs):
            return _response_for(documents)

        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            side_effect=_side_effect,
        ) as mocked:
            result = embedder.embed_commit_chunks(chunks)

        assert mocked.call_count == 2, (
            "25-token cap with 10-token chunks must split 2+2"
        )
        for call in mocked.call_args_list:
            documents = call.args[0]
            total_tokens = sum(len(c) for d in documents for c in d)
            assert total_tokens <= 25

        assert len(result) == 4
        for i, chunk in enumerate(chunks):
            assert result[i] == pytest.approx([float(len(chunk))] * 1024)


class TestDocumentPackingRespectsPerDocumentContextWindow:
    """Bug (found running real indexing, Story #1292): pack_chunks_into_documents
    was called with max_tokens_per_document=self._max_tokens_per_request (the
    120000-token REQUEST-level cap), not self._max_tokens_per_chunk (the
    ~32000-token model CONTEXT-WINDOW cap). Voyage's contextualized-embeddings
    API rejects a single "example" (one packed document) whose combined chunk
    tokens exceed the model's context window, even when comfortably under the
    request-level cap -- confirmed via a real HTTP 400: "The example at index 0
    in your batch has too many tokens and does not fit into the model's
    context window of 32000 tokens." A commit with many small chunks (each
    individually well under the per-chunk preflight cap) previously got
    packed into ONE oversized document and crashed indexing.
    """

    def test_document_never_exceeds_the_per_chunk_context_window_cap(
        self, mock_api_key
    ):
        embedder = ContextualTemporalEmbedder(Config())
        embedder._count_tokens = lambda text: len(text)  # type: ignore[assignment]
        # Per-document/context-window cap is SMALL; request-level cap is HUGE
        # -- proves packing is bounded by the SMALLER (context-window) cap,
        # not the request-level cap.
        embedder._max_tokens_per_chunk = 25
        embedder._max_chunks_per_request = 1000
        embedder._max_documents_per_request = 1000
        embedder._max_tokens_per_request = 1_000_000

        # 8 chunks of 10 tokens each == 80 tokens total: comfortably under the
        # request cap (1_000_000) but 4x over the per-document cap (25) if
        # packed into a single document.
        chunks = [f"chunk{i:02d}xx" for i in range(8)]  # 10 chars == 10 tokens each

        def _side_effect(documents, **kwargs):
            return _response_for(documents)

        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            side_effect=_side_effect,
        ) as mocked:
            result = embedder.embed_commit_chunks(chunks)

        for call in mocked.call_args_list:
            for document in call.args[0]:
                doc_tokens = sum(len(chunk) for chunk in document)
                assert doc_tokens <= 25, (
                    f"document {document!r} totals {doc_tokens} tokens, "
                    f"exceeding the per-document context-window cap of 25"
                )

        assert len(result) == 8
