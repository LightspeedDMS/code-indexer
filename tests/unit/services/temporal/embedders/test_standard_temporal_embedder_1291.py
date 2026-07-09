"""Unit tests for StandardTemporalEmbedder (Story #1291, Cohere embed-v4.0 adapter).

Mocks only the external-service HTTP boundary
(CohereEmbeddingProvider._make_sync_request), matching the pattern used for
ContextualTemporalEmbedder's own tests. Exercises the real adapter contract:
15% overlap, 1536 dims, embed_commit_chunks/embed_query wiring to
get_embeddings_batch with the correct embedding_purpose, per-chunk token
preflight-split-and-mean-pool, and is_available() key-presence gating.
"""

import os
from unittest.mock import patch

import pytest

from src.code_indexer.config import Config
from src.code_indexer.services.temporal.embedders.standard import (
    StandardTemporalEmbedder,
)
from src.code_indexer.services.temporal.embedders.registry import create_embedder


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"CO_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


@pytest.fixture
def no_api_key():
    env = dict(os.environ)
    env.pop("CO_API_KEY", None)
    with patch.dict(os.environ, env, clear=True):
        yield


class TestStandardTemporalEmbedderContract:
    def test_adapter_attributes(self, mock_api_key):
        embedder = StandardTemporalEmbedder(Config())
        assert embedder.name == "embed-v4.0"
        assert embedder.model_slug == "embed_v4_0"
        assert embedder.dimensions == 1536
        assert embedder.overlap_percentage == 0.15

    def test_created_via_registry(self, mock_api_key):
        embedder = create_embedder("embed-v4.0", Config())
        assert isinstance(embedder, StandardTemporalEmbedder)

    def test_embed_commit_chunks_empty_returns_empty(self, mock_api_key):
        embedder = StandardTemporalEmbedder(Config())
        assert embedder.embed_commit_chunks([]) == []

    def test_embed_commit_chunks_calls_batch_endpoint_as_document(self, mock_api_key):
        embedder = StandardTemporalEmbedder(Config())
        mock_response = {"embeddings": {"float": [[0.1] * 1536, [0.2] * 1536]}}
        with patch.object(
            embedder._client, "_make_sync_request", return_value=mock_response
        ) as mocked:
            result = embedder.embed_commit_chunks(["chunk a", "chunk b"])

        assert result == [[0.1] * 1536, [0.2] * 1536]
        args, kwargs = mocked.call_args
        # input_type is positional-or-keyword on _make_sync_request
        input_type = kwargs.get("input_type") if "input_type" in kwargs else args[1]
        assert input_type == "search_document"

    def test_embed_query_calls_batch_endpoint_as_query(self, mock_api_key):
        embedder = StandardTemporalEmbedder(Config())
        mock_response = {"embeddings": {"float": [[0.5] * 1536]}}
        with patch.object(
            embedder._client, "_make_sync_request", return_value=mock_response
        ) as mocked:
            result = embedder.embed_query("a query string")

        assert result == [0.5] * 1536
        args, kwargs = mocked.call_args
        input_type = kwargs.get("input_type") if "input_type" in kwargs else args[1]
        assert input_type == "search_query"

    def test_oversized_chunk_is_split_and_mean_pooled_back_to_one_vector(
        self, mock_api_key
    ):
        """AC3: a chunk whose estimated tokens exceed the per-chunk cap is split
        deterministically; the caller still gets exactly one vector per input
        chunk (mean-pooled), preserving the 1:1 chunk<->embedding contract."""
        embedder = StandardTemporalEmbedder(Config())
        # Force a tiny per-chunk cap so a short string is "oversized".
        embedder._max_tokens_per_chunk = 1

        call_texts = []

        def _fake_make_sync_request(texts, input_type="search_document", **kwargs):
            call_texts.append(list(texts))
            return {"embeddings": {"float": [[float(len(t))] * 1536 for t in texts]}}

        with (
            patch.object(embedder, "_count_tokens", side_effect=len),
            patch.object(
                embedder._client,
                "_make_sync_request",
                side_effect=_fake_make_sync_request,
            ),
        ):
            result = embedder.embed_commit_chunks(["ab", "c"])

        # Exactly one pooled vector per INPUT chunk, regardless of how many
        # sub-pieces the oversized chunk was split into.
        assert len(result) == 2
        assert all(len(v) == 1536 for v in result)
        # The oversized "ab" chunk must have been split into >1 piece across
        # the batch call(s).
        total_pieces_sent = sum(len(t) for t in call_texts)
        assert total_pieces_sent > 2  # more pieces sent than the 2 original chunks

    def test_is_available_true_when_key_present(self, mock_api_key):
        embedder = StandardTemporalEmbedder(Config())
        assert embedder.is_available() is True

    def test_is_available_false_when_key_absent_does_not_raise(self, no_api_key):
        config = Config()
        config.cohere.api_key = ""
        embedder = StandardTemporalEmbedder(config)
        assert embedder.is_available() is False

    def test_embed_commit_chunks_raises_when_unavailable(self, no_api_key):
        config = Config()
        config.cohere.api_key = ""
        embedder = StandardTemporalEmbedder(config)
        with pytest.raises(RuntimeError):
            embedder.embed_commit_chunks(["chunk a"])
