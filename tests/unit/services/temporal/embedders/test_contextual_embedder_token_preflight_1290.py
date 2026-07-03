"""Unit tests: ContextualTemporalEmbedder wires token preflight (Story #1290 AC23).

embed_commit_chunks() must preflight each chunk's estimated token count
against the provider per-chunk cap before calling the contextualized
endpoint. An oversized chunk is split DETERMINISTICALLY (contextual_chunker
already produced fixed CHARACTER chunks; a pathological chunk can still
exceed the token cap due to token-density content). Splitting must preserve
the 1:1 contract between requested chunks and returned embeddings (each
AggregatedChunk needs exactly one embedding for its point) -- sub-piece
embeddings for one oversized original chunk are mean-pooled back into a
single vector.
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


class TestContextualEmbedderTokenPreflight:
    def test_normal_sized_chunks_are_not_split(self, mock_api_key):
        """No preflight-splitting when chunks are comfortably under the cap."""
        embedder = ContextualTemporalEmbedder(Config())
        mock_response = {
            "data": [
                {
                    "index": 0,
                    "data": [
                        {"index": 0, "embedding": [0.1] * 1024},
                        {"index": 1, "embedding": [0.2] * 1024},
                    ],
                }
            ],
            "model": "voyage-context-4",
        }
        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            return_value=mock_response,
        ) as mocked:
            result = embedder.embed_commit_chunks(["short chunk a", "short chunk b"])

        assert result == [[0.1] * 1024, [0.2] * 1024]
        sent_documents = mocked.call_args[0][0]
        assert sent_documents == [["short chunk a", "short chunk b"]]

    def test_oversized_chunk_is_split_and_mean_pooled_back(self, mock_api_key):
        """AC23: an oversized chunk is split before the API call; the returned
        embedding list still has exactly one vector per REQUESTED chunk
        (sub-piece embeddings for the oversized chunk are mean-pooled)."""
        embedder = ContextualTemporalEmbedder(Config())
        # Stub the token counter deterministically (1 token per character) so
        # the split count is fully predictable regardless of the real
        # tokenizer's exact behavior -- "ok" (2 chars) stays under the cap,
        # "this is over 12 chars" (22 chars) is forced to split into 2 pieces.
        embedder._count_tokens = lambda text: len(text)  # type: ignore[assignment]
        embedder._max_tokens_per_chunk = 12

        mock_response = {
            "data": [
                {
                    "index": 0,
                    "data": [
                        {"index": 0, "embedding": [1.0] * 1024},  # "ok"
                        {"index": 1, "embedding": [2.0] * 1024},  # split piece 1
                        {"index": 2, "embedding": [4.0] * 1024},  # split piece 2
                    ],
                }
            ],
            "model": "voyage-context-4",
        }
        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            return_value=mock_response,
        ) as mocked:
            result = embedder.embed_commit_chunks(["ok", "this is over 12 chars"])

        # Exactly one embedding per REQUESTED chunk (2 in, 2 out).
        assert len(result) == 2
        assert result[0] == [1.0] * 1024
        # Mean of [2.0]*1024 and [4.0]*1024 == [3.0]*1024
        assert result[1] == pytest.approx([3.0] * 1024)

        sent_documents = mocked.call_args[0][0]
        assert len(sent_documents[0]) == 3  # 1 unsplit + 2 split pieces
