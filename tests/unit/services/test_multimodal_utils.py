"""Tests for shared multimodal utility functions."""

import base64
import pytest
from pathlib import Path

from src.code_indexer.services.multimodal_utils import (
    encode_image_to_base64,
    SUPPORTED_MEDIA_TYPES,
)


@pytest.fixture
def png_image(tmp_path: Path) -> Path:
    """Create a minimal valid PNG image file."""
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    image_path = tmp_path / "test.png"
    image_path.write_bytes(png_data)
    return image_path


@pytest.fixture
def jpeg_image(tmp_path: Path) -> Path:
    """Create a minimal JPEG image file."""
    jpeg_path = tmp_path / "test.jpg"
    jpeg_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    return jpeg_path


@pytest.fixture
def jpeg_extension_image(tmp_path: Path) -> Path:
    """Create a minimal JPEG image file with .jpeg extension."""
    jpeg_path = tmp_path / "test.jpeg"
    jpeg_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    return jpeg_path


@pytest.fixture
def webp_image(tmp_path: Path) -> Path:
    """Create a minimal WebP image file."""
    webp_path = tmp_path / "test.webp"
    webp_path.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
    return webp_path


@pytest.fixture
def gif_image(tmp_path: Path) -> Path:
    """Create a minimal GIF image file."""
    gif_path = tmp_path / "test.gif"
    gif_path.write_bytes(
        b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"
    )
    return gif_path


class TestEncodeImageToBase64:
    """Test encode_image_to_base64 shared utility function."""

    def test_encode_png_image(self, png_image: Path) -> None:
        """Test encoding PNG image produces correct data URL format."""
        data_url = encode_image_to_base64(png_image)

        assert data_url.startswith("data:image/png;base64,")
        # Verify base64 portion is valid
        encoded_data = data_url.split(",", 1)[1]
        decoded = base64.b64decode(encoded_data)
        assert len(decoded) > 0

    def test_encode_jpeg_image_jpg_extension(self, jpeg_image: Path) -> None:
        """Test encoding JPEG image with .jpg extension produces image/jpeg media type."""
        data_url = encode_image_to_base64(jpeg_image)

        assert data_url.startswith("data:image/jpeg;base64,")

    def test_encode_jpeg_image_jpeg_extension(self, jpeg_extension_image: Path) -> None:
        """Test encoding JPEG image with .jpeg extension produces image/jpeg media type."""
        data_url = encode_image_to_base64(jpeg_extension_image)

        assert data_url.startswith("data:image/jpeg;base64,")

    def test_encode_webp_image(self, webp_image: Path) -> None:
        """Test encoding WebP image produces correct data URL format."""
        data_url = encode_image_to_base64(webp_image)

        assert data_url.startswith("data:image/webp;base64,")

    def test_encode_gif_image(self, gif_image: Path) -> None:
        """Test encoding GIF image produces correct data URL format."""
        data_url = encode_image_to_base64(gif_image)

        assert data_url.startswith("data:image/gif;base64,")

    def test_encode_accepts_string_path(self, png_image: Path) -> None:
        """Test that encode_image_to_base64 accepts string paths as well as Path objects."""
        data_url = encode_image_to_base64(str(png_image))

        assert data_url.startswith("data:image/png;base64,")

    def test_encode_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Test that a missing image file raises FileNotFoundError."""
        missing_path = tmp_path / "nonexistent.png"

        with pytest.raises(FileNotFoundError, match="Image file not found"):
            encode_image_to_base64(missing_path)

    def test_encode_unsupported_format_raises_value_error(self, tmp_path: Path) -> None:
        """Test that unsupported image format raises ValueError."""
        bmp_path = tmp_path / "test.bmp"
        bmp_path.write_bytes(b"BM\x00\x00")

        with pytest.raises(ValueError, match="Unsupported image format"):
            encode_image_to_base64(bmp_path)

    def test_data_url_content_matches_file_content(self, png_image: Path) -> None:
        """Test that base64-decoded data URL content matches the original file bytes."""
        original_content = png_image.read_bytes()
        data_url = encode_image_to_base64(png_image)

        encoded_data = data_url.split(",", 1)[1]
        decoded_content = base64.b64decode(encoded_data)

        assert decoded_content == original_content

    def test_data_url_format_has_three_parts(self, png_image: Path) -> None:
        """Test data URL has format: data:[mediatype];base64,[data]."""
        data_url = encode_image_to_base64(png_image)

        # Must start with 'data:'
        assert data_url.startswith("data:")
        # Must contain ';base64,'
        assert ";base64," in data_url


class TestSupportedMediaTypes:
    """Test SUPPORTED_MEDIA_TYPES constant."""

    def test_supported_media_types_contains_png(self) -> None:
        """Test that PNG is in supported media types."""
        assert ".png" in SUPPORTED_MEDIA_TYPES
        assert SUPPORTED_MEDIA_TYPES[".png"] == "image/png"

    def test_supported_media_types_contains_jpg(self) -> None:
        """Test that JPG is in supported media types."""
        assert ".jpg" in SUPPORTED_MEDIA_TYPES
        assert SUPPORTED_MEDIA_TYPES[".jpg"] == "image/jpeg"

    def test_supported_media_types_contains_jpeg(self) -> None:
        """Test that JPEG is in supported media types."""
        assert ".jpeg" in SUPPORTED_MEDIA_TYPES
        assert SUPPORTED_MEDIA_TYPES[".jpeg"] == "image/jpeg"

    def test_supported_media_types_contains_webp(self) -> None:
        """Test that WebP is in supported media types."""
        assert ".webp" in SUPPORTED_MEDIA_TYPES
        assert SUPPORTED_MEDIA_TYPES[".webp"] == "image/webp"

    def test_supported_media_types_contains_gif(self) -> None:
        """Test that GIF is in supported media types."""
        assert ".gif" in SUPPORTED_MEDIA_TYPES
        assert SUPPORTED_MEDIA_TYPES[".gif"] == "image/gif"
