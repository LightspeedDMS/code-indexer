"""Unit tests for ImageValidationResult dataclass - Story #64 AC1-AC4."""

from src.code_indexer.indexing.image_extractor import ImageValidationResult


class TestImageValidationResult:
    """Test ImageValidationResult dataclass."""

    def test_valid_image_result(self):
        """Test creation of valid image result."""
        result = ImageValidationResult(
            path="images/valid.png",
            is_valid=True,
            skip_reason=None
        )

        assert result.path == "images/valid.png"
        assert result.is_valid is True
        assert result.skip_reason is None

    def test_missing_image_result(self):
        """Test creation of result for missing image."""
        result = ImageValidationResult(
            path="images/missing.png",
            is_valid=False,
            skip_reason="missing"
        )

        assert result.path == "images/missing.png"
        assert result.is_valid is False
        assert result.skip_reason == "missing"

    def test_remote_url_result(self):
        """Test creation of result for remote URL."""
        result = ImageValidationResult(
            path="https://example.com/image.png",
            is_valid=False,
            skip_reason="remote_url"
        )

        assert result.path == "https://example.com/image.png"
        assert result.is_valid is False
        assert result.skip_reason == "remote_url"

    def test_oversized_result(self):
        """Test creation of result for oversized image."""
        result = ImageValidationResult(
            path="images/huge.png",
            is_valid=False,
            skip_reason="oversized"
        )

        assert result.path == "images/huge.png"
        assert result.is_valid is False
        assert result.skip_reason == "oversized"

    def test_unsupported_format_result(self):
        """Test creation of result for unsupported format."""
        result = ImageValidationResult(
            path="images/file.bmp",
            is_valid=False,
            skip_reason="unsupported_format"
        )

        assert result.path == "images/file.bmp"
        assert result.is_valid is False
        assert result.skip_reason == "unsupported_format"

    def test_data_uri_result(self):
        """Test creation of result for data URI."""
        result = ImageValidationResult(
            path="data:image/png;base64,iVBORw0KGgo...",
            is_valid=False,
            skip_reason="data_uri"
        )

        assert result.path == "data:image/png;base64,iVBORw0KGgo..."
        assert result.is_valid is False
        assert result.skip_reason == "data_uri"

    def test_default_skip_reason_is_none(self):
        """Test that skip_reason defaults to None."""
        result = ImageValidationResult(
            path="images/test.png",
            is_valid=True
        )

        assert result.skip_reason is None

    def test_all_skip_reasons_are_valid_strings(self):
        """Test all expected skip_reason values."""
        valid_reasons = [
            "missing",
            "remote_url",
            "oversized",
            "unsupported_format",
            "data_uri"
        ]

        for reason in valid_reasons:
            result = ImageValidationResult(
                path=f"test_{reason}.png",
                is_valid=False,
                skip_reason=reason
            )
            assert result.skip_reason == reason
