"""Unit tests for ContextualTemporalEmbedder (Story #1290, voyage-context-4 adapter).

Mocks only the external-service HTTP boundary
(VoyageAIClient._make_sync_contextualized_request), matching the pattern used
for VoyageAIClient's own tests. Exercises the real adapter contract (0%
overlap, 1024 dims, embed_commit_chunks/embed_query wiring to the
contextualized endpoint with the correct input_type).
"""

import os
from unittest.mock import patch

import pytest

from src.code_indexer.config import Config
from src.code_indexer.services.temporal.embedders.contextual import (
    ContextualTemporalEmbedder,
)
from src.code_indexer.services.temporal.embedders.registry import create_embedder


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


class TestContextualTemporalEmbedderContract:
    def test_adapter_attributes(self, mock_api_key):
        embedder = ContextualTemporalEmbedder(Config())
        assert embedder.name == "voyage-context-4"
        assert embedder.model_slug == "voyage_context_4"
        assert embedder.dimensions == 1024
        assert embedder.overlap_percentage == 0.0

    def test_created_via_registry(self, mock_api_key):
        embedder = create_embedder("voyage-context-4", Config())
        assert isinstance(embedder, ContextualTemporalEmbedder)

    def test_embed_commit_chunks_empty_returns_empty(self, mock_api_key):
        embedder = ContextualTemporalEmbedder(Config())
        assert embedder.embed_commit_chunks([]) == []

    def test_embed_commit_chunks_calls_contextualized_endpoint_as_document(
        self, mock_api_key
    ):
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
            result = embedder.embed_commit_chunks(["chunk a", "chunk b"])

        assert result == [[0.1] * 1024, [0.2] * 1024]
        _, kwargs = mocked.call_args
        assert kwargs["input_type"] == "document"
        assert kwargs["output_dimension"] == 1024

    def test_embed_query_calls_contextualized_endpoint_as_query(self, mock_api_key):
        embedder = ContextualTemporalEmbedder(Config())
        mock_response = {
            "data": [{"index": 0, "data": [{"index": 0, "embedding": [0.5] * 1024}]}],
            "model": "voyage-context-4",
        }
        with patch.object(
            embedder._client,
            "_make_sync_contextualized_request",
            return_value=mock_response,
        ) as mocked:
            result = embedder.embed_query("a query string")

        assert result == [0.5] * 1024
        _, kwargs = mocked.call_args
        assert kwargs["input_type"] == "query"
