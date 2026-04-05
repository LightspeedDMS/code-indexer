"""Tests for Cohere Multimodal embedding client."""

import base64
import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.code_indexer.config import CohereConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SMALL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

EMBED_DIM = 1024


def _make_config(
    api_key: str = "test-co-key", dimension: int = EMBED_DIM
) -> CohereConfig:
    return CohereConfig(
        model="embed-v4.0-multimodal",
        api_key=api_key,
        default_dimension=dimension,
    )


def _fake_response(embeddings: List[List[float]]) -> Dict[str, Any]:
    """Build a fake Cohere embed-v4.0 API response (embeddings.float format)."""
    return {"embeddings": {"float": embeddings}}


@pytest.fixture
def png_image(tmp_path: Path) -> Path:
    image_path = tmp_path / "test.png"
    image_path.write_bytes(SMALL_PNG)
    return image_path


@pytest.fixture
def cohere_config() -> CohereConfig:
    return _make_config()


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestCohereMultimodalClientInit:
    """Test CohereMultimodalClient initialization."""

    def test_init_with_api_key_from_config(self, cohere_config: CohereConfig) -> None:
        """Test client initializes when API key is in config."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client.api_key == "test-co-key"
        assert client.config.model == "embed-v4.0-multimodal"

    def test_init_with_api_key_from_env(self, tmp_path: Path) -> None:
        """Test client falls back to CO_API_KEY env var when config key is empty."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        config = CohereConfig(model="embed-v4.0-multimodal", api_key="")
        with patch.dict(os.environ, {"CO_API_KEY": "env-key-123"}):
            client = CohereMultimodalClient(config)
        assert client.api_key == "env-key-123"

    def test_init_missing_api_key_raises_value_error(self) -> None:
        """Test initialization raises ValueError when no API key is available."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        config = CohereConfig(model="embed-v4.0-multimodal", api_key="")
        env_without_key = {k: v for k, v in os.environ.items() if k != "CO_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            with pytest.raises(ValueError, match="Cohere API key required"):
                CohereMultimodalClient(config)

    def test_init_stores_config(self, cohere_config: CohereConfig) -> None:
        """Test that config is stored on the client instance."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client.config is cohere_config


# ---------------------------------------------------------------------------
# Payload format
# ---------------------------------------------------------------------------


class TestCohereMultimodalPayload:
    """Test that API requests use the correct Cohere content-block payload format."""

    @patch("httpx.Client")
    def test_get_multimodal_embedding_builds_correct_payload(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig, png_image: Path
    ) -> None:
        """Test payload uses 'inputs' with typed content blocks and embedding_types."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.1] * EMBED_DIM])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        client.get_multimodal_embedding(text="hello", image_paths=[png_image])

        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]

        # Must use 'inputs' (not 'texts')
        assert "inputs" in payload
        # Must include embedding_types
        assert payload.get("embedding_types") == ["float"]
        # Must include output_dimension
        assert payload.get("output_dimension") == EMBED_DIM
        # inputs contains one item with content blocks
        assert len(payload["inputs"]) == 1
        content = payload["inputs"][0]["content"]
        # First block is text
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "hello"
        # Second block is image_url
        assert content[1]["type"] == "image_url"
        assert "image_url" in content[1]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    @patch("httpx.Client")
    def test_get_multimodal_embedding_text_only_no_image_block(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig
    ) -> None:
        """Test text-only request does not include image content block."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.2] * EMBED_DIM])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        client.get_multimodal_embedding(text="text only", image_paths=[])

        payload = mock_http.post.call_args[1]["json"]
        content = payload["inputs"][0]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestCohereMultimodalResponseParsing:
    """Test parsing of Cohere API response format."""

    @patch("httpx.Client")
    def test_parses_embeddings_float_format(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig
    ) -> None:
        """Test response parsing extracts embedding from embeddings.float path."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        expected = [0.5] * EMBED_DIM
        mock_response = Mock()
        mock_response.json.return_value = _fake_response([expected])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        result = client.get_multimodal_embedding(text="test", image_paths=[])

        assert result == expected

    @patch("httpx.Client")
    def test_raises_value_error_on_unexpected_response_format(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig
    ) -> None:
        """Test ValueError raised when response lacks embeddings key."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        mock_response = Mock()
        mock_response.json.return_value = {"unexpected": "format"}
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        with pytest.raises((ValueError, KeyError, RuntimeError)):
            client.get_multimodal_embedding(text="test", image_paths=[])


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


class TestCohereMultimodalBatch:
    """Test batch embedding operations."""

    def test_empty_batch_returns_empty_list(self, cohere_config: CohereConfig) -> None:
        """Test get_multimodal_embeddings_batch returns [] for empty input."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        result = client.get_multimodal_embeddings_batch(items=[])
        assert result == []

    @patch("httpx.Client")
    def test_batch_splits_on_token_limit(
        self,
        mock_client_cls: MagicMock,
        cohere_config: CohereConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test batch is split when token limit (90% of 128000) would be exceeded."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        # 3 items at 50000 tokens each: first two fit (100000 < 115200), third triggers split
        call_count = [0]
        responses = [
            _fake_response([[0.1] * EMBED_DIM, [0.2] * EMBED_DIM]),
            _fake_response([[0.3] * EMBED_DIM]),
        ]

        def mock_post(*args: Any, **kwargs: Any) -> Mock:
            resp = Mock()
            resp.json.return_value = responses[call_count[0]]
            resp.raise_for_status = Mock()
            resp.status_code = 200
            call_count[0] += 1
            return resp

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.side_effect = mock_post
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        monkeypatch.setattr(client, "_count_tokens", lambda text: 50000)

        embeddings = client.get_multimodal_embeddings_batch(
            items=[
                {"text": "item1", "image_paths": []},
                {"text": "item2", "image_paths": []},
                {"text": "item3", "image_paths": []},
            ]
        )

        assert len(embeddings) == 3
        assert embeddings[0][0] == 0.1
        assert embeddings[1][0] == 0.2
        assert embeddings[2][0] == 0.3
        assert call_count[0] == 2  # Two API calls due to split

    @patch("httpx.Client")
    def test_batch_splits_on_96_image_cap(
        self,
        mock_client_cls: MagicMock,
        cohere_config: CohereConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Test batch splits when 96-image cap (MAX_IMAGES_PER_REQUEST) is reached.

        Uses 2 items with 50 images each (100 total > 96 cap) to trigger batch splitting.
        Batch splitting is item-level, so a single item cannot be split across batches.
        With 2 items: item1 (50 images) fills batch1 partially, but adding item2 would
        bring total to 100 > 96, so item2 goes into batch2.
        """
        from src.code_indexer.services.cohere_multimodal import (
            CohereMultimodalClient,
            MAX_IMAGES_PER_REQUEST,
        )

        # Create 50 images for each of the 2 items (100 total > 96 cap)
        images_per_item = MAX_IMAGES_PER_REQUEST // 2 + 2  # 50 images each
        images1 = []
        images2 = []
        for i in range(images_per_item):
            img1 = tmp_path / f"img1_{i}.png"
            img1.write_bytes(SMALL_PNG)
            images1.append(img1)
            img2 = tmp_path / f"img2_{i}.png"
            img2.write_bytes(SMALL_PNG)
            images2.append(img2)

        call_count = [0]

        def mock_post(*args: Any, **kwargs: Any) -> Mock:
            resp = Mock()
            # Each call returns 1 embedding (one item per batch)
            resp.json.return_value = _fake_response([[0.1] * EMBED_DIM])
            resp.raise_for_status = Mock()
            resp.status_code = 200
            call_count[0] += 1
            return resp

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.side_effect = mock_post
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        monkeypatch.setattr(client, "_count_tokens", lambda text: 1)

        # Two items with 50 images each = 100 total > 96 cap -> splits into 2 batches
        items = [
            {"text": "text1", "image_paths": images1},
            {"text": "text2", "image_paths": images2},
        ]
        embeddings = client.get_multimodal_embeddings_batch(items=items)

        assert len(embeddings) == 2
        # Should have made 2 calls due to image cap
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# 5MB image size enforcement
# ---------------------------------------------------------------------------


class TestCohereMultimodalImageSizeEnforcement:
    """Test 5MB image size limit enforcement."""

    @patch("httpx.Client")
    def test_oversized_image_is_skipped_with_warning(
        self,
        mock_client_cls: MagicMock,
        cohere_config: CohereConfig,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test images over 5MB are skipped with a warning, text still embedded."""
        import logging
        from src.code_indexer.services.cohere_multimodal import (
            CohereMultimodalClient,
            COHERE_MAX_IMAGE_SIZE,
        )

        # Create a 'large' image by mocking file size check
        large_image = tmp_path / "large.png"
        large_image.write_bytes(SMALL_PNG)

        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.4] * EMBED_DIM])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)

        # Patch os.path.getsize to simulate an oversized image
        with patch("os.path.getsize", return_value=COHERE_MAX_IMAGE_SIZE + 1):
            with caplog.at_level(logging.WARNING):
                result = client.get_multimodal_embedding(
                    text="text with large image", image_paths=[large_image]
                )

        # Text embedding should still be generated
        assert len(result) == EMBED_DIM
        # Should have logged a warning about the oversized image
        assert any(
            "5MB" in record.message
            or "large" in record.message.lower()
            or "size" in record.message.lower()
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# get_embedding for queries
# ---------------------------------------------------------------------------


class TestCohereMultimodalGetEmbedding:
    """Test get_embedding method for query vectorization."""

    @patch("httpx.Client")
    def test_get_embedding_sends_search_query_input_type(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig
    ) -> None:
        """Test get_embedding uses input_type=search_query for query vectorization."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.7] * EMBED_DIM])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        result = client.get_embedding("my query")

        assert len(result) == EMBED_DIM
        payload = mock_http.post.call_args[1]["json"]
        assert payload.get("input_type") == "search_query"

    @patch("httpx.Client")
    def test_get_embedding_returns_correct_dimension(
        self, mock_client_cls: MagicMock
    ) -> None:
        """Test get_embedding returns vector with configured dimension."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        config = _make_config(dimension=512)
        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.1] * 512])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(config)
        result = client.get_embedding("query text")

        assert len(result) == 512


# ---------------------------------------------------------------------------
# _map_input_type
# ---------------------------------------------------------------------------


class TestCohereMultimodalCollectionName:
    """Test collection_name property for multimodal collection directory naming."""

    def test_collection_name_property_returns_cohere_multimodal_model(
        self, cohere_config: CohereConfig
    ) -> None:
        """Test collection_name returns COHERE_MULTIMODAL_MODEL constant.

        The collection name (directory under .code-indexer/index/) must be
        'embed-v4.0-multimodal' even though the Cohere API model is 'embed-v4.0'.
        This decouples the collection directory name from the API model name.
        """
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient
        from src.code_indexer.config import COHERE_MULTIMODAL_MODEL

        client = CohereMultimodalClient(cohere_config)
        assert client.collection_name == COHERE_MULTIMODAL_MODEL
        assert client.collection_name == "embed-v4.0-multimodal"


class TestCohereMultimodalGetEmbeddingKwargs:
    """Test get_embedding method accepts extra keyword arguments."""

    @patch("httpx.Client")
    def test_get_embedding_accepts_embedding_purpose_kwarg(
        self, mock_client_cls: MagicMock, cohere_config: CohereConfig
    ) -> None:
        """Test get_embedding does not raise TypeError for embedding_purpose kwarg.

        filesystem_vector_store.py calls
        embedding_provider.get_embedding(query, embedding_purpose='query').
        CohereMultimodalClient.get_embedding() must accept **kwargs for interface
        compatibility without crashing.
        """
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        mock_response = Mock()
        mock_response.json.return_value = _fake_response([[0.7] * EMBED_DIM])
        mock_response.raise_for_status = Mock()
        mock_response.status_code = 200

        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = mock_response
        mock_client_cls.return_value = mock_http

        client = CohereMultimodalClient(cohere_config)
        # Must not raise TypeError
        result = client.get_embedding("test query", embedding_purpose="query")
        assert len(result) == EMBED_DIM


class TestCohereMultimodalMapInputType:
    """Test _map_input_type mapping logic."""

    def test_query_maps_to_search_query(self, cohere_config: CohereConfig) -> None:
        """Test 'query' maps to 'search_query'."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client._map_input_type("query") == "search_query"

    def test_document_maps_to_search_document(
        self, cohere_config: CohereConfig
    ) -> None:
        """Test 'document' maps to 'search_document'."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client._map_input_type("document") == "search_document"

    def test_none_maps_to_search_document(self, cohere_config: CohereConfig) -> None:
        """Test None maps to 'search_document' (indexing default)."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client._map_input_type(None) == "search_document"

    def test_arbitrary_string_maps_to_search_document(
        self, cohere_config: CohereConfig
    ) -> None:
        """Test any non-query value maps to 'search_document'."""
        from src.code_indexer.services.cohere_multimodal import CohereMultimodalClient

        client = CohereMultimodalClient(cohere_config)
        assert client._map_input_type("other") == "search_document"
