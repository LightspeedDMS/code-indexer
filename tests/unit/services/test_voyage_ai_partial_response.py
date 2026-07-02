"""
Unit tests for VoyageAI partial response validation.

Tests the critical bug where VoyageAI returns fewer embeddings than requested,
leading to zip() length mismatches and IndexError in temporal_indexer.py.
"""

import os
import pytest
from unittest.mock import patch
from src.code_indexer.services.voyage_ai import VoyageAIClient
from src.code_indexer.config import VoyageAIConfig


class TestVoyageAIPartialResponse:
    """Test VoyageAI API partial response handling."""

    @pytest.fixture
    def voyage_config(self):
        """Create VoyageAI configuration."""
        return VoyageAIConfig(
            model="voyage-code-3",
            parallel_requests=4,
            batch_size=64,
        )

    @pytest.fixture
    def mock_api_key(self):
        """Mock API key environment variable."""
        with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
            yield "PLACEHOLDER"

    def test_partial_response_single_batch_detected(self, voyage_config, mock_api_key):
        """
        Test that partial response in single batch is detected and raises error.

        Bug scenario: VoyageAI returns 7 embeddings when 10 were requested.
        Expected: RuntimeError with clear message about partial response.
        """
        # Setup
        service = VoyageAIClient(voyage_config)
        texts = [f"text_{i}" for i in range(10)]

        # Mock API to return only 7 embeddings instead of 10
        mock_response = {
            "data": [{"embedding": [0.1] * 1536} for _ in range(7)]  # Only 7 embeddings
        }

        with patch.object(service, "_make_sync_request", return_value=mock_response):
            # Execute & Verify
            with pytest.raises(RuntimeError) as exc_info:
                service.get_embeddings_batch(texts)

            # Verify error message describes the problem
            error_msg = str(exc_info.value)
            assert "returned 7 embeddings" in error_msg.lower()
            assert "expected 10" in error_msg.lower()
            assert "partial response" in error_msg.lower()

    def test_correct_response_length_passes(self, voyage_config, mock_api_key):
        """
        Test that correct response length passes validation.

        Scenario: VoyageAI returns exactly the number of embeddings requested.
        Expected: No error, all embeddings returned.
        """
        # Setup
        service = VoyageAIClient(voyage_config)
        texts = [f"text_{i}" for i in range(10)]

        # Mock API to return correct number of embeddings with correct dimensions
        # voyage-code-3 expects 1024 dims
        mock_response = {
            "data": [
                {"embedding": [0.1] * 1024}
                for _ in range(10)  # Exactly 10 embeddings
            ]
        }

        with patch.object(service, "_make_sync_request", return_value=mock_response):
            # Execute
            embeddings = service.get_embeddings_batch(texts)

            # Verify
            assert len(embeddings) == 10
            assert all(len(emb) == 1024 for emb in embeddings)


# ---------------------------------------------------------------------------
# Story #619 Gap 6: Voyage embedding dimension validation tests
# ---------------------------------------------------------------------------


class TestVoyageEmbeddingDimensionValidation:
    """Tests for VoyageAIClient._validate_embeddings (Story #619 Gap 6)."""

    @pytest.fixture
    def voyage_client(self):
        """Create VoyageAIClient with mocked API key."""
        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            config = VoyageAIConfig(model="voyage-code-3")
            yield VoyageAIClient(config)

    def test_voyage_validate_embeddings_wrong_dimensions(self, voyage_client):
        """_validate_embeddings must raise RuntimeError on dimension mismatch."""
        expected_dims = voyage_client.get_model_info()["dimensions"]
        wrong_dim = expected_dims + 1  # force mismatch
        with pytest.raises(RuntimeError, match="dims"):
            voyage_client._validate_embeddings(
                [[0.1] * wrong_dim], voyage_client.config.model
            )

    def test_voyage_validate_embeddings_nan_values(self, voyage_client):
        """_validate_embeddings must raise RuntimeError when embedding contains NaN."""
        expected_dims = voyage_client.get_model_info()["dimensions"]
        nan_embedding = [float("nan")] + [0.1] * (expected_dims - 1)
        with pytest.raises(RuntimeError, match="NaN or Inf"):
            voyage_client._validate_embeddings(
                [nan_embedding], voyage_client.config.model
            )

    def test_voyage_validate_embeddings_inf_values(self, voyage_client):
        """_validate_embeddings must raise RuntimeError when embedding contains Inf."""
        expected_dims = voyage_client.get_model_info()["dimensions"]
        inf_embedding = [float("inf")] + [0.1] * (expected_dims - 1)
        with pytest.raises(RuntimeError, match="NaN or Inf"):
            voyage_client._validate_embeddings(
                [inf_embedding], voyage_client.config.model
            )


# ---------------------------------------------------------------------------
# Story #619 Gap 2: Connect vs Read timeout split tests (Voyage)
# ---------------------------------------------------------------------------


class TestVoyageConnectReadTimeoutSplit:
    """Tests for connect vs read timeout split in VoyageAI provider (Story #619 Gap 2)."""

    def test_voyage_uses_split_timeout(self):
        """_make_sync_request must pass httpx.Timeout with distinct connect vs read values."""
        import httpx

        captured_timeouts = []

        class CapturingClient:
            def __init__(self, *args, **kwargs):
                captured_timeouts.append(kwargs.get("timeout"))
                raise ConnectionError("test-abort")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            from code_indexer.services.voyage_ai import VoyageAIClient
            from code_indexer.config import VoyageAIConfig

            config = VoyageAIConfig(max_retries=0)
            client = VoyageAIClient(config)

        with patch("httpx.Client", CapturingClient):
            with pytest.raises((RuntimeError, ConnectionError)):
                client._make_sync_request(["test"])

        assert len(captured_timeouts) >= 1, "httpx.Client must be called at least once"
        timeout_arg = captured_timeouts[0]
        assert isinstance(timeout_arg, httpx.Timeout), (
            f"Expected httpx.Timeout instance, got {type(timeout_arg)}"
        )
        assert timeout_arg.connect != timeout_arg.read, (
            f"connect={timeout_arg.connect} must differ from read={timeout_arg.read}"
        )
