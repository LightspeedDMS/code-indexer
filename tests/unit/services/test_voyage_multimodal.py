"""Tests for VoyageAI multimodal embeddings client."""

import base64
import os
from unittest.mock import Mock, patch, MagicMock
import pytest

from src.code_indexer.services.voyage_multimodal import VoyageMultimodalClient
from src.code_indexer.config import VoyageAIConfig


@pytest.fixture
def mock_api_key():
    """Mock VOYAGE_API_KEY environment variable."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


@pytest.fixture
def voyage_config():
    """Create test VoyageAI configuration."""
    return VoyageAIConfig(
        model="voyage-multimodal-3.5",
        api_endpoint="https://api.voyageai.com/v1/multimodalembeddings",
        timeout=30.0,
        max_retries=3,
        retry_delay=1.0,
        exponential_backoff=True,
    )


@pytest.fixture
def client(mock_api_key, voyage_config):
    """Create VoyageMultimodalClient instance."""
    return VoyageMultimodalClient(voyage_config)


@pytest.fixture
def sample_image_path(tmp_path):
    """Create a sample PNG image for testing."""
    # Create a minimal 1x1 PNG image (base64-encoded)
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    image_path = tmp_path / "test.png"
    image_path.write_bytes(png_data)
    return image_path


class TestVoyageMultimodalClientInitialization:
    """Test client initialization and configuration."""

    def test_init_success(self, mock_api_key, voyage_config):
        """Test successful client initialization."""
        client = VoyageMultimodalClient(voyage_config)

        assert client.config == voyage_config
        assert client.api_key == "PLACEHOLDER"
        assert client.config.model == "voyage-multimodal-3.5"

    def test_init_missing_api_key(self, voyage_config):
        """Test initialization fails without API key."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(
                ValueError, match="VOYAGE_API_KEY environment variable is required"
            ):
                VoyageMultimodalClient(voyage_config)

    def test_default_endpoint(self, mock_api_key, voyage_config):
        """Test default multimodal endpoint is set correctly."""
        client = VoyageMultimodalClient(voyage_config)
        assert "multimodalembeddings" in client.config.api_endpoint

    def test_init_overrides_default_endpoint_to_multimodal(self, mock_api_key):
        """Test that client overrides default embeddings endpoint to multimodal endpoint.

        CRITICAL-3: VoyageAIConfig defaults to /v1/embeddings, but multimodal client
        needs /v1/multimodalembeddings. This test verifies the override works.
        """
        # Create config with DEFAULT endpoint (wrong for multimodal)
        config_with_default = VoyageAIConfig(
            model="voyage-multimodal-3.5",
            # api_endpoint NOT specified - uses default /v1/embeddings
        )

        # Verify config starts with wrong endpoint
        assert (
            config_with_default.api_endpoint == "https://api.voyageai.com/v1/embeddings"
        )

        # Initialize multimodal client
        client = VoyageMultimodalClient(config_with_default)

        # Verify client OVERRODE the endpoint to multimodal
        assert (
            client.config.api_endpoint
            == "https://api.voyageai.com/v1/multimodalembeddings"
        )
        assert "multimodalembeddings" in client.config.api_endpoint
        assert "/v1/embeddings" not in client.config.api_endpoint


class TestImageEncoding:
    """Test image encoding to base64 data URLs."""

    def test_encode_image_to_base64_png(self, client, sample_image_path):
        """Test encoding PNG image to base64 data URL."""
        data_url = client._encode_image_to_base64(sample_image_path)

        assert data_url.startswith("data:image/png;base64,")
        # Verify it's valid base64
        encoded_data = data_url.split(",", 1)[1]
        decoded = base64.b64decode(encoded_data)
        assert len(decoded) > 0

    def test_encode_image_to_base64_jpeg(self, client, tmp_path):
        """Test encoding JPEG image with correct media type."""
        jpeg_path = tmp_path / "test.jpg"
        # Minimal JPEG header
        jpeg_data = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        jpeg_path.write_bytes(jpeg_data)

        data_url = client._encode_image_to_base64(jpeg_path)
        assert data_url.startswith("data:image/jpeg;base64,")

    def test_encode_image_to_base64_webp(self, client, tmp_path):
        """Test encoding WebP image with correct media type."""
        webp_path = tmp_path / "test.webp"
        # Minimal WebP header
        webp_data = b"RIFF\x00\x00\x00\x00WEBP"
        webp_path.write_bytes(webp_data)

        data_url = client._encode_image_to_base64(webp_path)
        assert data_url.startswith("data:image/webp;base64,")

    def test_encode_image_nonexistent_file(self, client, tmp_path):
        """Test encoding raises error for non-existent file."""
        fake_path = tmp_path / "nonexistent.png"

        with pytest.raises(FileNotFoundError):
            client._encode_image_to_base64(fake_path)

    def test_encode_image_unsupported_format(self, client, tmp_path):
        """Test encoding raises error for unsupported image format."""
        unsupported_path = tmp_path / "test.bmp"
        unsupported_path.write_bytes(b"BM")  # BMP header

        with pytest.raises(ValueError, match="Unsupported image format"):
            client._encode_image_to_base64(unsupported_path)


class TestMultimodalEmbeddingGeneration:
    """Test multimodal embedding generation (text + images)."""

    @patch("httpx.Client")
    def test_get_multimodal_embedding_text_only(self, mock_client_cls, client):
        """Test generating embedding for text-only content."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "object": "list",
            "data": [{"embedding": [0.1] * 1024, "index": 0}],
            "model": "voyage-multimodal-3.5",
            "usage": {"text_tokens": 10, "total_tokens": 10},
        }
        mock_response.raise_for_status = Mock()

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        embedding = client.get_multimodal_embedding(
            text="Sample code snippet", image_paths=[]
        )

        assert len(embedding) == 1024
        assert all(isinstance(x, float) for x in embedding)

    @patch("httpx.Client")
    def test_get_multimodal_embedding_with_images(
        self, mock_client_cls, client, sample_image_path
    ):
        """Test generating embedding for text + images."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "object": "list",
            "data": [{"embedding": [0.2] * 1024, "index": 0}],
            "model": "voyage-multimodal-3.5",
            "usage": {"text_tokens": 10, "image_pixels": 1, "total_tokens": 11},
        }
        mock_response.raise_for_status = Mock()

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        embedding = client.get_multimodal_embedding(
            text="Database schema diagram", image_paths=[sample_image_path]
        )

        assert len(embedding) == 1024
        # Verify API was called with correct structure
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]

        assert payload["model"] == "voyage-multimodal-3.5"
        assert len(payload["inputs"]) == 1
        assert len(payload["inputs"][0]["content"]) == 2  # text + 1 image
        assert payload["inputs"][0]["content"][0]["type"] == "text"
        assert payload["inputs"][0]["content"][1]["type"] == "image_base64"


class TestVoyageMultimodalBatchProcessing:
    """Test batch processing with token limit handling."""

    EMBEDDING_DIM = 1024

    def test_batch_embeddings_single_item(
        self, client, httpx_mock, sample_image_path, monkeypatch
    ):
        """Test batch processing with single item."""
        # Mock token counting (voyage-multimodal-3.5 doesn't have a tokenizer)
        monkeypatch.setattr(
            client, "_count_tokens_accurately", lambda text: len(text.split())
        )

        # Mock API response
        httpx_mock.add_response(
            method="POST",
            url="https://api.voyageai.com/v1/multimodalembeddings",
            json={"data": [{"embedding": [0.1] * self.EMBEDDING_DIM}]},
        )

        # Call batch method
        embeddings = client.get_multimodal_embeddings_batch(
            items=[{"text": "test query", "image_paths": [sample_image_path]}]
        )

        # Verify results
        assert len(embeddings) == 1
        assert len(embeddings[0]) == self.EMBEDDING_DIM
        assert embeddings[0][0] == 0.1

    def test_batch_embeddings_empty_list(self, client):
        """Test batch processing with empty list returns empty list."""
        embeddings = client.get_multimodal_embeddings_batch(items=[])
        assert embeddings == []

    def test_batch_splits_when_exceeding_token_limit(
        self, client, httpx_mock, sample_image_path, monkeypatch
    ):
        """Test batch automatically splits when token limit would be exceeded."""
        # Mock token counting to simulate large token counts that trigger splitting
        # Each item will report 50000 tokens, safety limit is 108000 (90% of 120000)
        # First two items = 100000 tokens (OK), third item would be 150000 (exceeds limit) - triggers split
        # Result: batch1=[item1, item2], batch2=[item3]
        monkeypatch.setattr(client, "_count_tokens_accurately", lambda text: 50000)

        # Mock responses for two separate batch calls
        httpx_mock.add_response(
            method="POST",
            url="https://api.voyageai.com/v1/multimodalembeddings",
            json={
                "data": [
                    {"embedding": [0.1] * self.EMBEDDING_DIM},
                    {"embedding": [0.2] * self.EMBEDDING_DIM},
                ]
            },
        )
        httpx_mock.add_response(
            method="POST",
            url="https://api.voyageai.com/v1/multimodalembeddings",
            json={
                "data": [
                    {"embedding": [0.3] * self.EMBEDDING_DIM},
                ]
            },
        )

        # Create items with text
        embeddings = client.get_multimodal_embeddings_batch(
            items=[
                {"text": "item1", "image_paths": [sample_image_path]},
                {"text": "item2", "image_paths": [sample_image_path]},
                {"text": "item3", "image_paths": [sample_image_path]},
            ]
        )

        # Verify results from both batches merged correctly
        assert len(embeddings) == 3
        assert embeddings[0][0] == 0.1
        assert embeddings[1][0] == 0.2
        assert embeddings[2][0] == 0.3

        # Verify API was called twice (batch split occurred)
        requests = httpx_mock.get_requests()
        assert len(requests) == 2
