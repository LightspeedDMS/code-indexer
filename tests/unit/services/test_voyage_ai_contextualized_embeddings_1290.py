"""Unit tests for VoyageAIClient.get_contextualized_embeddings (Story #1290).

Covers the client-side wrapper around POST /v1/contextualizedembeddings used by
the per-commit contextual temporal embedder (voyage-context-4). The low-level
HTTP call (_make_sync_contextualized_request) is the external-service boundary
and is mocked here, matching the existing pattern used for
_make_sync_request in test_voyage_ai_partial_response.py. get_contextualized_embeddings
itself (response parsing, ordering, count-mismatch fail-loud) is the code under
test and is exercised for real.
"""

import os
import pytest
from unittest.mock import patch

from src.code_indexer.services.voyage_ai import VoyageAIClient
from src.code_indexer.config import VoyageAIConfig


@pytest.fixture
def voyage_config():
    return VoyageAIConfig(model="voyage-context-4", parallel_requests=4)


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


class TestGetContextualizedEmbeddings:
    def test_empty_documents_returns_empty_list(self, voyage_config, mock_api_key):
        client = VoyageAIClient(voyage_config)
        assert client.get_contextualized_embeddings([], input_type="document") == []

    def test_single_document_multi_chunk_returns_ordered_embeddings(
        self, voyage_config, mock_api_key
    ):
        client = VoyageAIClient(voyage_config)
        mock_response = {
            "object": "list",
            "data": [
                {
                    "object": "list",
                    "index": 0,
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [0.1] * 1024},
                        {"object": "embedding", "index": 1, "embedding": [0.2] * 1024},
                    ],
                }
            ],
            "model": "voyage-context-4",
            "usage": {"total_tokens": 10},
        }
        with patch.object(
            client, "_make_sync_contextualized_request", return_value=mock_response
        ):
            result = client.get_contextualized_embeddings(
                [["chunk one", "chunk two"]],
                input_type="document",
                output_dimension=1024,
            )

        assert len(result) == 1
        assert len(result[0]) == 2
        assert result[0][0] == [0.1] * 1024
        assert result[0][1] == [0.2] * 1024

    def test_multi_document_results_ordered_by_document_index(
        self, voyage_config, mock_api_key
    ):
        client = VoyageAIClient(voyage_config)
        # API response groups arrive out of order; index field is authoritative.
        mock_response = {
            "data": [
                {
                    "index": 1,
                    "data": [{"index": 0, "embedding": [0.9] * 1024}],
                },
                {
                    "index": 0,
                    "data": [{"index": 0, "embedding": [0.1] * 1024}],
                },
            ],
            "model": "voyage-context-4",
        }
        with patch.object(
            client, "_make_sync_contextualized_request", return_value=mock_response
        ):
            result = client.get_contextualized_embeddings(
                [["doc0 chunk"], ["doc1 chunk"]],
                input_type="document",
                output_dimension=1024,
            )

        assert result[0] == [[0.1] * 1024]
        assert result[1] == [[0.9] * 1024]

    def test_chunk_count_mismatch_raises_runtime_error(
        self, voyage_config, mock_api_key
    ):
        """AC21: a contextualized-response chunk-count mismatch RAISES (fail-loud)."""
        client = VoyageAIClient(voyage_config)
        mock_response = {
            "data": [
                {
                    "index": 0,
                    "data": [
                        {"index": 0, "embedding": [0.1] * 1024}
                    ],  # only 1, expected 2
                }
            ],
            "model": "voyage-context-4",
        }
        with patch.object(
            client, "_make_sync_contextualized_request", return_value=mock_response
        ):
            with pytest.raises(RuntimeError, match="chunk count mismatch"):
                client.get_contextualized_embeddings(
                    [["chunk one", "chunk two"]],
                    input_type="document",
                    output_dimension=1024,
                )

    def test_query_input_type_passed_through_to_request(
        self, voyage_config, mock_api_key
    ):
        client = VoyageAIClient(voyage_config)
        mock_response = {
            "data": [{"index": 0, "data": [{"index": 0, "embedding": [0.3] * 1024}]}],
            "model": "voyage-context-4",
        }
        with patch.object(
            client,
            "_make_sync_contextualized_request",
            return_value=mock_response,
        ) as mocked:
            client.get_contextualized_embeddings(
                [["a query string"]],
                input_type="query",
                output_dimension=1024,
            )
            _, kwargs = mocked.call_args
            assert kwargs["input_type"] == "query"
            assert kwargs["output_dimension"] == 1024
