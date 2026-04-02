"""Tests for Bug #600 and Bug #601.

Bug #600: ProviderHealthMonitor.record_call() must be instrumented in
          VoyageAIClient._make_sync_request() and
          CohereEmbeddingProvider._make_sync_request().

Bug #601 Issue 1: CohereEmbeddingProvider.get_embeddings_batch() must validate
                  per-element None values inside embedding vectors.
Bug #601 Issue 2: DEFAULT_COLLECTION_NAME in engine.py must not be hardcoded
                  to "voyage-3" when Cohere is the active provider.
"""

import os
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_health_monitor():
    """Reset ProviderHealthMonitor singleton before and after each test."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def voyage_client():
    """VoyageAIClient with mocked API key."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import VoyageAIConfig

        config = VoyageAIConfig()
        yield VoyageAIClient(config, None)


@pytest.fixture
def cohere_provider():
    """CohereEmbeddingProvider with mocked API key."""
    with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        yield CohereEmbeddingProvider(config, None)


# ---------------------------------------------------------------------------
# Bug #600: VoyageAIClient health monitor instrumentation
# ---------------------------------------------------------------------------


class TestBug600VoyageHealthMonitor:
    """VoyageAIClient._make_sync_request must record calls to ProviderHealthMonitor."""

    def test_voyage_make_sync_request_records_success(self, voyage_client):
        """On successful response, record_call is called with success=True."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        fake_response = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

        with patch("httpx.Client") as mock_client_cls:
            mock_http = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_http
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = fake_response
            mock_http.post.return_value = mock_response

            voyage_client._make_sync_request(["hello world"])

        monitor = ProviderHealthMonitor.get_instance()
        health = monitor.get_health("voyage-ai")
        status = health["voyage-ai"]
        assert status.total_requests == 1
        assert status.successful_requests == 1
        assert status.failed_requests == 0

    def test_voyage_make_sync_request_records_failure(self, voyage_client):
        """On exception, record_call is called with success=False."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
        import httpx

        with patch("httpx.Client") as mock_client_cls:
            mock_http = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_http
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.headers = {}
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=mock_response,
            )
            mock_http.post.return_value = mock_response

            # Disable retries for speed
            voyage_client.config.max_retries = 0

            with pytest.raises(Exception):
                voyage_client._make_sync_request(["hello"])

        monitor = ProviderHealthMonitor.get_instance()
        health = monitor.get_health("voyage-ai")
        status = health["voyage-ai"]
        assert status.total_requests >= 1
        assert status.failed_requests >= 1


# ---------------------------------------------------------------------------
# Bug #600: CohereEmbeddingProvider health monitor instrumentation
# ---------------------------------------------------------------------------


class TestBug600CohereHealthMonitor:
    """CohereEmbeddingProvider._make_sync_request must record calls."""

    def test_cohere_make_sync_request_records_success(self, cohere_provider):
        """On successful response, record_call is called with success=True."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        fake_response = {
            "embeddings": {"float": [[0.1, 0.2, 0.3]]},
            "id": "test",
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = fake_response
            mock_client.post.return_value = mock_response

            cohere_provider._make_sync_request(["hello"], "search_document")

        monitor = ProviderHealthMonitor.get_instance()
        health = monitor.get_health("cohere")
        status = health["cohere"]
        assert status.total_requests == 1
        assert status.successful_requests == 1
        assert status.failed_requests == 0

    def test_cohere_make_sync_request_records_failure(self, cohere_provider):
        """On exception/error, record_call is called with success=False."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        # Disable retries for speed
        cohere_provider.config.max_retries = 0

        with patch("httpx.post") as mock_post:
            mock_post.side_effect = ConnectionError("network failure")

            with pytest.raises(Exception):
                cohere_provider._make_sync_request(["hello"], "search_document")

        monitor = ProviderHealthMonitor.get_instance()
        health = monitor.get_health("cohere")
        status = health["cohere"]
        assert status.total_requests >= 1
        assert status.failed_requests >= 1


# ---------------------------------------------------------------------------
# Bug #601 Issue 1: Cohere per-element None validation
# ---------------------------------------------------------------------------


class TestBug601CohereNoneValidation:
    """get_embeddings_batch must raise RuntimeError for None values inside vectors."""

    def test_cohere_get_embeddings_batch_raises_on_none_values_in_embedding(
        self, cohere_provider
    ):
        """RuntimeError raised when embedding vector contains None values."""
        bad_response = {
            "embeddings": {"float": [[0.1, None, 0.3]]},
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = bad_response
            mock_client.post.return_value = mock_response

            with pytest.raises(RuntimeError, match="None values"):
                cohere_provider.get_embeddings_batch(["hello world"])

    def test_cohere_get_embeddings_batch_accepts_valid_embeddings(
        self, cohere_provider
    ):
        """No error raised for well-formed embeddings (all values are floats)."""
        good_response = {
            "embeddings": {"float": [[0.1, 0.2, 0.3]]},
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {}
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = good_response
            mock_client.post.return_value = mock_response

            result = cohere_provider.get_embeddings_batch(["hello world"])
        assert result == [[0.1, 0.2, 0.3]]


# ---------------------------------------------------------------------------
# Bug #601 Issue 2: DEFAULT_COLLECTION_NAME resolution
# ---------------------------------------------------------------------------


class TestBug601DefaultCollectionName:
    """DEFAULT_COLLECTION_NAME must not be hardcoded to 'voyage-3' for Cohere."""

    def test_default_collection_name_is_not_hardcoded_voyage3(self):
        """DEFAULT_COLLECTION_NAME must not be the string literal 'voyage-3'."""
        from code_indexer.server.validation.engine import DEFAULT_COLLECTION_NAME

        assert DEFAULT_COLLECTION_NAME != "voyage-3", (
            "DEFAULT_COLLECTION_NAME is still hardcoded to 'voyage-3'. "
            "It should be a provider-neutral sentinel or empty string."
        )

    def test_validation_engine_uses_config_provider_for_collection_fallback(self):
        """IndexValidationEngine collection fallback is provider-aware for Cohere."""
        from code_indexer.server.validation.engine import IndexValidationEngine
        from code_indexer.config import Config, CohereConfig

        config = Config(
            embedding_provider="cohere",
            cohere=CohereConfig(api_key="test-key"),
        )

        mock_vector_store = MagicMock()
        # No existing collections — exercises the DEFAULT_COLLECTION_NAME fallback
        mock_vector_store.list_collections.return_value = []

        engine = IndexValidationEngine(
            config=config, vector_store_client=mock_vector_store
        )

        # When Cohere is active and no collections exist, the fallback collection
        # name must not reference a VoyageAI model name
        assert "voyage" not in engine.collection_name.lower(), (
            f"collection_name '{engine.collection_name}' contains 'voyage' "
            "but Cohere is the active provider."
        )
