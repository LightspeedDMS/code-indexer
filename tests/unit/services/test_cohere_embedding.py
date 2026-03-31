"""Tests for Cohere embedding provider and multi-provider infrastructure.

Increment 1: ABC embedding_purpose kwarg and VoyageAI acceptance.
"""

import os
import pytest
from unittest.mock import patch


@pytest.fixture
def voyage_client():
    """Create a VoyageAIClient with mocked API key for testing."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import VoyageAIConfig

        config = VoyageAIConfig()
        yield VoyageAIClient(config, None)


class TestABCEmbeddingPurposeParam:
    """The ABC and VoyageAI must accept embedding_purpose as keyword-only arg."""

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_document(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='document' kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="document")
        assert isinstance(result, list)

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_query(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='query' kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="query")
        assert isinstance(result, list)

    def test_voyage_ai_get_embeddings_batch_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch must accept embedding_purpose kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {
                "data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]
            }
            result = voyage_client.get_embeddings_batch(
                ["hello", "world"], embedding_purpose="document"
            )
        assert len(result) == 2

    def test_voyage_ai_get_embedding_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embedding_with_metadata must accept embedding_purpose kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding_with_metadata(
                "hello", embedding_purpose="document"
            )
        assert result.embedding == [0.1, 0.2, 0.3]

    def test_voyage_ai_get_embeddings_batch_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch_with_metadata must accept embedding_purpose."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
            result = voyage_client.get_embeddings_batch_with_metadata(
                ["hello"], embedding_purpose="document"
            )
        assert len(result.embeddings) == 1

    def test_voyage_ai_health_check_accepts_test_api_as_keyword_arg(
        self, voyage_client
    ):
        """VoyageAI.health_check must accept test_api as keyword arg."""
        result = voyage_client.health_check(test_api=False)
        assert result is True

    def test_voyage_ai_embedding_purpose_does_not_affect_request_payload(
        self, voyage_client
    ):
        """VoyageAI ignores embedding_purpose; payload sent to API must be unchanged."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1]}]}
            voyage_client.get_embedding("test", embedding_purpose="query")
            call_args = mock_req.call_args
            positional_args = call_args[0]
            # First positional arg is texts list
            assert positional_args[0] == ["test"]
